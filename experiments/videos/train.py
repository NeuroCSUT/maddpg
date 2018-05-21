import argparse
import numpy as np
import os
import tensorflow as tf
import time
import pickle
import random
import copy
import maddpg.common.tf_util as U
from maddpg.trainer.maddpg import MADDPGAgentTrainer
import tensorflow.contrib.layers as layers
import skvideo.io
from Video import AddTextToImage,FixPosition
def parse_args(args=None):
    parser = argparse.ArgumentParser("Reinforcement Learning experiments for multiagent environments")
    # Environment
    parser.add_argument("--scenario", type=str, default="simple_spread", help="name of the scenario script")
    parser.add_argument("--max-episode-len", type=int, default=25, help="maximum episode length")
    parser.add_argument("--num-episodes", type=int, default=60000, help="number of episodes")
    parser.add_argument("--num-adversaries", type=int, default=0, help="number of adversaries")
    parser.add_argument("--good-policy", type=str, default="maddpg", help="policy for good agents")
    parser.add_argument("--adv-policy", type=str, default="maddpg", help="policy of adversaries")
    # Core training parameters
    parser.add_argument("--lr", type=float, default=1e-2, help="learning rate for Adam optimizer")
    parser.add_argument("--gamma", type=float, default=0.95, help="discount factor")
    parser.add_argument("--batch-size", type=int, default=1024, help="number of episodes to optimize at the same time")
    parser.add_argument("--num-units", type=int, default=128, help="number of units in the mlp")
    parser.add_argument("--shuffle", choices=['episode', 'timestep'], default=None, help="shuffle agents at each step")
    parser.add_argument("--shared", action="store_true", default=False, help="use shared model for all agents")
    # Checkpointing
    parser.add_argument("--exp-name", type=str, default=None, help="name of the experiment")
    parser.add_argument("--save-dir", type=str, default="/tmp/policy/", help="directory in which training state and model should be saved")
    parser.add_argument("--save-rate", type=int, default=1000, help="save model once every time this many episodes are completed")
    parser.add_argument("--load-dir", type=str, default="", help="directory in which training state and model are loaded")
    parser.add_argument("--restore", action="store_true", default=False, help="restore model from checkpoint")
    # Evaluation
    parser.add_argument("--display", action="store_true", default=False, help="render environment")
    parser.add_argument("--benchmark", action="store_true", default=False, help="run evaluation")
    parser.add_argument("--benchmark-iters", type=int, default=100000, help="number of iterations run for benchmarking")
    parser.add_argument("--benchmark-dir", type=str, default="./benchmark_files/", help="directory where benchmark data is saved")
    parser.add_argument("--plots-dir", type=str, default="./learning_curves/", help="directory where plot data is saved")
    parser.add_argument("--save-replay", action="store_true", default=False, help="save replay memory contents along with benchmark data")
    parser.add_argument("--deterministic", action="store_true", default=False, help="use deterministic policy during benchmarking")
    parser.add_argument("--record",default=False,help="record video file, Add video path + name to activate")
    return parser.parse_args(args)

def mlp_model(input, num_outputs, scope, reuse=False, num_units=64, rnn_cell=None):
    # This model takes as input an observation and returns values of all actions
    with tf.variable_scope(scope, reuse=reuse):
        hidden1 = layers.fully_connected(input, num_outputs=num_units, activation_fn=tf.nn.relu)
        hidden2 = layers.fully_connected(hidden1, num_outputs=num_units, activation_fn=tf.nn.relu)
        out = layers.fully_connected(hidden2, num_outputs=num_outputs, activation_fn=None)
        return out, hidden1, hidden2

def make_env(scenario_name, arglist, benchmark=False):
    from multiagent.environment import MultiAgentEnv
    import multiagent.scenarios as scenarios

    # load scenario from script
    scenario = scenarios.load(scenario_name + ".py").Scenario()
    # create world
    world = scenario.make_world()
    # create multiagent environment
    if benchmark:
        env = MultiAgentEnv(world, scenario.reset_world, scenario.reward, scenario.observation, scenario.benchmark_data)
    else:
        env = MultiAgentEnv(world, scenario.reset_world, scenario.reward, scenario.observation)
    return env

def get_trainers(env, num_adversaries, obs_shape_n, arglist):
    trainers = []
    model = mlp_model
    trainer = MADDPGAgentTrainer
    for i in range(num_adversaries):
        trainers.append(trainer(
            "bad" if arglist.shared else "agent_%d" % i,
            model, obs_shape_n, env.action_space, i, arglist,
            local_q_func=(arglist.adv_policy=='ddpg'),
            reuse=tf.AUTO_REUSE if arglist.shared else False))
    for i in range(num_adversaries, env.n):
        trainers.append(trainer(
            "good" if arglist.shared else "agent_%d" % i,
            model, obs_shape_n, env.action_space, i, arglist,
            local_q_func=(arglist.good_policy=='ddpg'),
            reuse=tf.AUTO_REUSE if arglist.shared else False))
    return trainers


def train(arglist):
    with U.single_threaded_session():
        # Create environment
        env = make_env(arglist.scenario, arglist, arglist.benchmark)
        # Create agent trainers
        obs_shape_n = [env.observation_space[i].shape for i in range(env.n)]
        num_adversaries = min(env.n, arglist.num_adversaries)
        trainers = get_trainers(env, num_adversaries, obs_shape_n, arglist)
        print('Using good policy {} and adv policy {}'.format(arglist.good_policy, arglist.adv_policy))
        agents = copy.copy(trainers)

        # Initialize
        U.initialize()

        # Load previous results, if necessary
        if arglist.load_dir == "":
            arglist.load_dir = arglist.save_dir
        if arglist.display or arglist.restore or arglist.benchmark:
            print('Loading previous state...')
            U.load_state(arglist.load_dir)

        episode_rewards = [0.0]  # sum of rewards for all agents
        agent_rewards = [[0.0] for _ in range(env.n)]  # individual agent reward
        final_ep_rewards = []  # sum of rewards for training curve
        final_ep_ag_rewards = []  # agent rewards for training curve
        agent_info = [[[]]]  # placeholder for benchmarking info
        saver = tf.train.Saver()
        obs_n = env.reset()
        episode_step = 0
        train_step = 0
        t_start = time.time()

        print('Starting iterations...')
        if (arglist.record!=False):
            writer = skvideo.io.FFmpegWriter("{}.avi".format(arglist.record))
        while True:
            # shuffle agents to prevent them from learning fixed strategy
            if not arglist.benchmark and arglist.shuffle == 'timestep':
                random.shuffle(agents)
            # get action
            action_n = [agent.action(obs) for agent, obs in zip(agents,obs_n)]
            # environment step
            new_obs_n, rew_n, done_n, info_n = env.step(action_n)
            episode_step += 1
            done = all(done_n)
            terminal = (episode_step >= arglist.max_episode_len)
            # collect experience
            #print([ag.state.p_pos for ag in env.agents])
            for i, agent in enumerate(agents):
                agent.experience(obs_n[i], action_n[i], rew_n[i], new_obs_n[i], done_n[i], terminal)
            obs_n = new_obs_n

            for i, rew in enumerate(rew_n):
                episode_rewards[-1] += rew
                agent_rewards[i][-1] += rew

            for i, info in enumerate(info_n):
                agent_info[-1][i].append(info_n['n'])

            # for displaying learned policies
            if arglist.display:
                time.sleep(0.01)
                x = env.render(mode='rgb_array')
                if (arglist.record!=False):
                    LM = [ag.state.p_pos for ag in env.world.landmarks]
                    LM = [FixPosition(j,10,10) for j in LM]
                    AP = [ag.state.p_pos for ag in env.agents]
                    AP = [FixPosition(j) for j in AP]
                    img = np.copy(x[0])
                    img = AddTextToImage(img,text=['Agent {}','Agent {}','Agent {}'],color=(0,0,255),pos=AP)
                    img = AddTextToImage(img,text=['LM{}','LM{}','LM{}'],pos=LM,color=(255,0,0))
                    writer.writeFrame(img)

            if done or terminal:
                if (arglist.record!=False):
                    writer.close()
                    exit()
                obs_n = env.reset()
                episode_step = 0
                episode_rewards.append(0)
                for a in agent_rewards:
                    a.append(0)
                agent_info.append([[]])
                # shuffle agents to prevent them from learning fixed strategy
                if not arglist.benchmark and arglist.shuffle == 'episode':
                    random.shuffle(agents)

            # increment global step counter
            train_step += 1

            # for benchmarking learned policies
            if arglist.benchmark:
                if train_step >= arglist.benchmark_iters and (done or terminal):
                    file_name = arglist.benchmark_dir + arglist.exp_name + '.pkl'
                    print('Finished benchmarking, now saving...')
                    with open(file_name, 'wb') as fp:
                        pickle.dump(agent_info[:-1], fp)
                        if arglist.save_replay:
                            # save in original order
                            for i, agent in enumerate(trainers):
                                pickle.dump(agent.replay_buffer._storage, fp)
                    break
                continue

            # update all agents, if not in display or benchmark mode
            loss = None
            for agent in agents:
                agent.preupdate()
            for agent in agents:
                loss = agent.update(agents, train_step)
                # if shared model, train only once
                if arglist.shared:
                    break

            # save model, display training output
            if terminal and (len(episode_rewards) % arglist.save_rate == 0):
                U.save_state(arglist.save_dir, saver=saver)
                # print statement depends on whether or not there are adversaries
                if num_adversaries == 0:
                    print("steps: {}, episodes: {}, mean episode reward: {}, time: {}".format(
                        train_step, len(episode_rewards), np.mean(episode_rewards[-arglist.save_rate:]), round(time.time()-t_start, 3)))
                else:
                    print("steps: {}, episodes: {}, mean episode reward: {}, agent episode reward: {}, time: {}".format(
                        train_step, len(episode_rewards), np.mean(episode_rewards[-arglist.save_rate:]),
                        [np.mean(rew[-arglist.save_rate:]) for rew in agent_rewards], round(time.time()-t_start, 3)))
                t_start = time.time()
                # Keep track of final episode reward
                final_ep_rewards.append(np.mean(episode_rewards[-arglist.save_rate:]))
                for rew in agent_rewards:
                    final_ep_ag_rewards.append(np.mean(rew[-arglist.save_rate:]))

            # saves final episode reward for plotting training curve later
            if len(episode_rewards) > arglist.num_episodes:
                rew_file_name = arglist.plots_dir + arglist.exp_name + '_rewards.pkl'
                with open(rew_file_name, 'wb') as fp:
                    pickle.dump(final_ep_rewards, fp)
                agrew_file_name = arglist.plots_dir + arglist.exp_name + '_agrewards.pkl'
                with open(agrew_file_name, 'wb') as fp:
                    pickle.dump(final_ep_ag_rewards, fp)
                print('...Finished total of {} episodes.'.format(len(episode_rewards)))
                break

if __name__ == '__main__':
    arglist = parse_args()
    train(arglist)