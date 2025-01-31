import argparse
import agent
import environment
import runner
import graph
import logging
import numpy as np
import sys
import pickle
import os
import time
from utils.vis import str2bool

# Set up logger
logging.basicConfig(
    format='%(asctime)s:%(levelname)s:%(message)s',
    level=logging.INFO
)

parser = argparse.ArgumentParser(description='RL running machine')
parser.add_argument('--environment_name', metavar='ENV_CLASS', type=str, default='bss', help='Class to use for the environment. Must be in the \'environment\' module')
parser.add_argument('--agent', metavar='AGENT_CLASS', default='Agent', type=str, help='Class to use for the agent. Must be in the \'agent\' module.')
parser.add_argument('--graph_nbr', type=int, default='5000', help='number of differente graph to generate for the training sample')
parser.add_argument('--model', type=str, default='GATv2', help='model name')
parser.add_argument('--ngames', type=int, metavar='n', default='4000', help='number of games to simulate') #1250
parser.add_argument('--nepisode', type=int, metavar='n', default=5, help='max number of episodes per game')
parser.add_argument('--niter', type=int, metavar='n', default='100', help='max number of iterations per episode')
parser.add_argument('--epoch', type=int, metavar='nepoch',default=1, help="number of epochs")
parser.add_argument('--lr',type=float, default=0.0000625,help="learning rate")
parser.add_argument('--bs',type=int,default=32,help="minibatch size for training")
parser.add_argument('--n_nodes', type=int, metavar='node_numbers',default=10, help="number of node in generated graphs")
parser.add_argument('--knn', type=int, metavar='k_neighbor_node',default=5, help="number of node's KNN in generated graphs")
parser.add_argument('--coeff_demand',type=float, default=5.,help="obj coeff, penalty_cost_demand")
parser.add_argument('--coeff_time',type=float, default=5.,help="obj coeff, penalty_cost_time")
parser.add_argument('--car_speed',type=float, default=30.)
parser.add_argument('--time_limit',type=float, default=35.)
parser.add_argument('--n_car', type=int, metavar='car_nums', default=3, help='number of vehicles used in game')
parser.add_argument('--verbose', action='store_true', default=True, help='Display cumulative results at each step')
parser.add_argument('--val', metavar='validation_mode', default=False)
parser.add_argument('--replace_freq', type=int,default=300, help='How frequently target netowrk updates')
parser.add_argument('--penalty_unvisited', type=int, default=2, help='obj coeff, penalty_unvisited')
parser.add_argument('--starting_fraction', type=float, default=0.5, help='starting amount of bikes to load, as a fraction of max_load')
parser.add_argument('--reward_scale', type=float, default=500, help='scales the reward')
parser.add_argument('--max_load', type=int, default=20, help='maximum vehicle capacity' )
parser.add_argument('--max_demand', type=int, default=9, help='maximum demand at each station' )
parser.add_argument('--force_n_vehicles', type=bool, default=True, help='force agent to respect vehicle limit by masking.')
parser.add_argument('--n_features',type=int, default=7, help="number of features in GNN")


def main():
    args = parser.parse_args()
    logging.info('Loading graph: nodes{}, ngames {}, graph_nbr {}, knn {} '.format(args.n_nodes, args.ngames, args.graph_nbr, args.knn))
    val_mode = str2bool(args.val)

    if not val_mode:

        start_time = time.time()
        graph_dic_train = {}
        for graph_ in range(args.graph_nbr):
            seed = np.random.seed(120 + graph_)

            graph_dic_train[graph_] = graph.Graph(n_nodes=args.n_nodes,
                                            k_nn=args.knn,
                                            n_vehicles=args.n_car,
                                            penalty_cost_demand=args.coeff_demand,
                                            penalty_cost_time=args.coeff_time,
                                            speed=args.car_speed,
                                            time_limit=args.time_limit,
                                            starting_fraction=args.starting_fraction,
                                            max_demand=args.max_demand,
                                            max_load=args.max_load)

        logging.info('Loading agent...')
        agent_class = agent.Agent(args.model, args.lr, args.bs, args.replace_freq, args.n_nodes, args.n_features)

        logging.info('Loading environment %s' % args.environment_name)
        env_train = environment.Environment(graph_dic_train,
            args.environment_name, 
            penalty_unvisited=args.penalty_unvisited, 
            reward_scale=args.reward_scale,
            force_n_vehicles=str2bool(args.force_n_vehicles))

        print("Training...")
        runner_train = runner.Runner(env_train, agent_class, args.verbose, render = False)
        cumul_reward_list, cumul_loss_list, cumul_epsilon_list = runner_train.train_loop(args.ngames, args.epoch, args.nepisode, args.niter)
        print("Training finished after {} episodes".format(len(cumul_reward_list)))
        agent_class.save_model()

        print("Time to train:", time.time() - start_time)


        

    if val_mode:

        start_time = time.time()

        dataset = 'graph_dic_val.pickle'
        load_data = os.path.isfile(dataset)

        if load_data:
            # Load Validation Dataset
            with open('graph_dic_val.pickle', 'rb') as handle:
                graph_dic_val = pickle.load(handle)
            ngames = len(graph_dic_val)
        else:
            # Create New Validation Dataset
            graph_dic_val = {}
            ngames = 1000

            for graph_ in range(ngames):
                graph_dic_val[graph_] = graph.Graph(n_nodes=args.n_nodes,
                                                k_nn=args.knn,
                                                n_vehicles=args.n_car,
                                                penalty_cost_demand=args.coeff_demand,
                                                penalty_cost_time=args.coeff_time,
                                                speed=args.car_speed,
                                                time_limit=args.time_limit,
                                                starting_fraction=args.starting_fraction,
                                                max_demand=args.max_demand,
                                                max_load=args.max_load)

            # Save Validation Dataset
            with open('graph_dic_val.pickle', 'wb') as handle:
                pickle.dump(graph_dic_val, handle, protocol=pickle.HIGHEST_PROTOCOL)

        logging.info('Loading agent...')
        agent_class = agent.Agent(args.model, args.lr, args.bs, args.replace_freq, args.n_nodes, args.n_features)
        agent_class.load_model("model.pt")

        logging.info('Loading environment %s' % args.environment_name)
        env_val = environment.Environment(graph_dic_val, 
            args.environment_name, 
            penalty_unvisited=args.penalty_unvisited, 
            reward_scale=args.reward_scale,
            force_n_vehicles=str2bool(args.force_n_vehicles))

        print("Validating...")
        runner_val = runner.Runner(env_val, agent_class, args.verbose, render=False)
        reward_list  = runner_val.validate_loop(ngames, args.niter)
        print("Validation finished")
        print("RL mean reward:", np.mean(reward_list))

        print("Time to validate:", time.time() - start_time)    

if __name__ == "__main__":
    main()
