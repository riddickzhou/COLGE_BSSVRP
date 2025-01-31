import numpy as np
import torch
import matplotlib.pyplot as plt
from utils.vis import timestamp
"""
This file contains the definition of the environment
in which the agents are run.
"""

VISITED = 0 
LOAD = 1
DEMAND = 2
CURR_NODE = 3
PREV_NODE = 4
TRIP_TIME = 5 
TRIP_OVER = 6


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
device = torch.device("cpu")

class Environment:
    def __init__(self, graph_dict, name, verbose=True, reward_scale=500, penalty_unvisited=None, force_n_vehicles=True, overage_percent=0.05):
        self.graph_dict = graph_dict
        self.name = name
        self.verbose = verbose
        self.reward_scale = reward_scale
        self.penalty_unvisited = penalty_unvisited
        self.force_n_vehicles = force_n_vehicles
        self.overage_percent = overage_percent

    def reset(self, g):
        """ Reset graph per game input. """
        self.games = g
        self.graph = self.graph_dict[self.games]
        self.dynamic = self.graph.dynamic.detach().clone()
        self.dynamic_init = self.dynamic.detach().clone()
        self.static = self.graph.static.detach()
        self.emb = self.graph.emb.detach()
        self.state = self.compute_state(0)
        self.prev_node = 0
        self.t_total = 0.
        self.tour_indices = [0]
        self.prev_demand = np.abs(self.dynamic[2, 1:]).sum()
        self.mask = self.mask_reset()

        # 
        self.dynamic[1][0] = self.graph.num_start
        self.dynamic[7, :] = 1 / self.graph.n_vehicles
        if self.penalty_unvisited is None:
            self.penalty_unvisited = self.graph.penalty_cost_demand

        self.trip_count = 0
        self.ep_reward_tour = 0
        self.ep_reward_demand = 0 
        self.ep_reward_overage = 0
        self.ep_reward_car = 0

        return self.state, self.graph.W_weighted, self.mask

    def mask_reset(self):
        """ Reset mask, exclude only depot node at the init. """
        mask = torch.ones_like(self.dynamic[0]).unsqueeze(0).int()
        mask[:,0] = 0
        return mask

    def compute_state(self, chosen_idx):
        """ Combine graph dynamic feature and static coordinate location. """
        #node_dist = torch.tensor(self.graph.W_full[chosen_idx]/self.graph.W_full[chosen_idx].max()).unsqueeze(0).float() # dist to neighbor, normalized
        #state = torch.cat((self.dynamic, self.static.T/self.graph.area, self.emb.T), dim=0)
        # state = torch.cat((self.dynamic, node_dist,self.emb.T), dim=0)

        t_shape = (1, self.graph.n_nodes)

        visited = self.dynamic[0].reshape(t_shape)
        demand = self.dynamic[2].reshape(t_shape)
        load = torch.ones(t_shape) * self.dynamic[1][chosen_idx]
        trip_time = torch.ones(t_shape) * self.dynamic[5][chosen_idx]
        trip_over = torch.ones(t_shape) * self.dynamic[6][chosen_idx]
        pos = self.static.T/self.graph.area

        state = torch.cat((visited, demand, load, trip_time, trip_over, pos))

        # state = torch.cat((self.dynamic,self.static.T),dim=0)
        return state.float()

    # def demand_reset(self):
    #     """ Reset demand per input graph. """
    #     self.graph.refresh_demand()
    #     self.dynamic = self.graph.dynamic.detach().clone()
    #     self.dynamic_init = self.dynamic.detach().clone()
    #     self.state = self.compute_state()
    #     return self.state

    def update_dynamic(self, chosen_idx, prev_idx, new_load, new_demand):
        # Updates the dynamic(observation, load, demand)
        if not chosen_idx == 0:
            self.dynamic[0, chosen_idx] = 1
        self.dynamic[1, chosen_idx] = new_load
        self.dynamic[2, chosen_idx] = new_demand
        self.dynamic[3, :] = 0  # current node
        self.dynamic[3, chosen_idx] = 1
        self.dynamic[4, :] = 0  # previous node
        self.dynamic[4, prev_idx] = 1

        if self.prev_node == 0:
            self.dynamic[6, :] = 0 # trip overage reset
        else:
            self.dynamic[6, chosen_idx] = self.get_overage_time(chosen_idx)/self.graph.time_limit # normalized trip overtime

        if chosen_idx == 0: # return to depot
            self.dynamic[5, :] = 0 # zero trip time
            self.dynamic[7, :] += 1/self.graph.n_vehicles # normalized car overage
        else:
            self.dynamic[5, chosen_idx] = self.get_travel_dist(prev_idx, chosen_idx)

    def step(self, action):
        done = False
        chosen_idx = action.item()

        if chosen_idx == 0: # reset load and demand to zero per formulation
            new_load = self.graph.num_start
            new_demand = 0
            self.trip_count += 1
        else:
            new_load, new_demand = self.get_updated_load_and_demand(chosen_idx)

        reward = self.get_reward(chosen_idx)
        self.update_dynamic(chosen_idx, self.prev_node, new_load, new_demand)
        self.compute_mask(chosen_idx, self.prev_node)

        self.t_total += self.get_travel_dist(self.prev_node, chosen_idx)
        self.tour_indices.append(chosen_idx)
        self.prev_node = chosen_idx


        # demand_met = bool(np.abs(self.dynamic[2]).sum() == 0 )
        all_node_visit = bool((self.dynamic[0][1:] == 1).all())
        # all_car_used =  bool(self.dynamic[7][0] == self.graph.n_vehicles)

        # terminal case
        # if all_node_visit or all_car_used:
        if all_node_visit:
            reward += self.get_terminal_reward(chosen_idx, new_load)

            self.t_total += self.get_travel_dist(chosen_idx, 0)
            self.tour_indices.append(0)
            done = True
            self.print_info()

        self.state = self.compute_state(chosen_idx)

        info = (self.prev_node, self.t_total, self.tour_indices, self.mask)

        return (self.state, reward, done, info)

    def compute_mask(self, chosen_idx, last_node):
        """ Compute mask for agent's action """
        with torch.no_grad():
            state = self.state

        visited_nodes = state[0].int()

        nbr_nodes = (self.graph.W[chosen_idx] > 0).int()
        uncovered_nodes = (visited_nodes[:] == 0).int()
        # uncovered_nodes = (state[2][:] != 0).int()

        cur_load = state[1][last_node]
        if cur_load ==0:
            underload = state[2].lt(0)
        else:
            underload =torch.zeros_like(state[2], dtype=torch.bool)

        if cur_load ==self.graph.max_load:
            overload = state[2].gt(0)
        else:
            overload =torch.zeros_like(state[2], dtype=torch.bool)
        #overload = state[2].gt(20 - cur_load)
        #underload = state[2].lt(0) * state[2].abs().gt(cur_load)
        #overtime = self.get_next_route_time(chosen_idx) > self.graph.time_limit # TODO need to count time to go back depot

        mask = uncovered_nodes *  ~underload * ~overload # * nbr_nodes * ~overtime
        mask[last_node] = 0
        mask[0] = 1  # depot is always available unless last visit
        mask[chosen_idx] = 0  # mask out visited node

        # mask2 = uncovered_nodes * ~underload *~overload # mask2 without neighbor node restriction
        # mask2[0] = 1  # depot is always available unless last visit
        # mask2[last_node] = 0
        # mask2[chosen_idx] = 0  # mask out visited node


        if (visited_nodes == 1).all() or (mask[:] == 0).all():
            # all nodes are visited or no node to go, then go back to depot
            mask[:] = 0
            mask[0] = 1

        #
        # if (visited_nodes == 1).all() or (mask2[:] == 0).all():
        #     # all nodes are visited or no node to go, then go back to depot
        #     mask[:] = 0
        #     mask[0] = 1

        # elif (mask[1:] == 0).all():
        #     # when no neighbor node in the graph to go
        #     mask = mask2
        #
        # else:
        #     pass

        # check if we should return to depot
        # this condition basically determines the threshold for when the vehicle should go back to the depot
        if chosen_idx != 0 and not (self.force_n_vehicles and self.trip_count == self.graph.n_vehicles-1):
            cur_trip_len = self.get_current_route_time()
            max_trip_len = self.graph.time_limit * (1 + self.overage_percent)
            visitable = []
            for i in range(1, self.graph.n_nodes):
                if mask[i] == 0 or i == chosen_idx:
                    continue
                dist_to_depot_i = self.get_travel_dist(chosen_idx, i) + self.get_travel_dist(i, 0)
                if cur_trip_len + dist_to_depot_i > max_trip_len:
                    mask[i] = 0
                else:
                    visitable.append(i)
                    
            if len(visitable) > 0: # mask depot if there are good nodes to visit
                mask[0] = 0

        # force to visit rest of nodes on last route
        if self.force_n_vehicles and self.trip_count == self.graph.n_vehicles-1:
            if mask[1:].sum() == 0: # if no options except depot
                mask = 1 - self.dynamic[0,:] # unmask all unvisited nodes
            mask[0] = 0 # mask depot to ensure visitable 

        self.mask = mask.unsqueeze(0)
        return self.mask


    def get_terminal_reward(self, chosen_idx, excess):
        """ Gets the reward when terminal state is reached. """
        reward = 0
        excess_load = np.abs(self.graph.num_start - excess)

        reward_tour = self.get_travel_dist(chosen_idx, 0) # time to go back to depot
        reward_demand = excess_load * self.graph.penalty_cost_demand # additional bikes on vehicle
        reward_overage = self.get_overage_last_step(chosen_idx)  * self.graph.penalty_cost_time # overtime

        # if self.dynamic[7][0] > 1:
        #     reward_car = (self.dynamic[7][0] - 1) * self.graph.n_vehicles * 10
        # else:
        #     reward_car = 0

        reward = reward_tour + reward_demand + reward_overage #+ reward_car
        self.ep_reward_tour -= reward_tour
        self.ep_reward_demand -= reward_demand
        self.ep_reward_overage -= reward_overage
        # self.ep_reward_car -= reward_car
        
        assert(self._get_demand_unvisited()  == 0) # done for now to ensure all nodes are visited at termination.

        return torch.tensor([-reward]) / self.reward_scale

    def get_reward(self, chosen_idx):
        """ Gets the reward action.  """
        reward_tour = self.get_travel_dist(self.prev_node, chosen_idx) # travel time from prev node to next node
        reward_demand = self.get_demand_reward(chosen_idx) * self.graph.penalty_cost_demand # difference in unmet demand
        reward_overage = self.get_overage_time(chosen_idx) * self.graph.penalty_cost_time # overtime
        # reward = reward_tour + reward_demand + reward_overage

        # if self.dynamic[7][0] > 1:
        #     reward_car = (self.dynamic[7][0] - 1) * self.graph.n_vehicles * 10
        # else:
        #     reward_car = 0

        reward = reward_tour + reward_demand + reward_overage #+ reward_car

        self.ep_reward_tour -= reward_tour
        self.ep_reward_demand -= reward_demand
        self.ep_reward_overage -= reward_overage
        # self.ep_reward_car -= reward_car

        return torch.tensor([-reward]) / self.reward_scale

    def get_overage_last_step(self, chosen_idx):
        """ Gets the overage time for moving to the depot in the last step.  """
        dist_to_idx = self.get_current_route_time() + self.get_travel_dist(self.prev_node, chosen_idx)
        dist_to_depot = self.get_current_route_time() + self.get_travel_dist(self.prev_node, chosen_idx)
        dist_to_depot += self.get_travel_dist(chosen_idx, 0)

        if dist_to_idx > self.graph.time_limit:
            return self.get_travel_dist(chosen_idx, 0)
        elif dist_to_depot > self.graph.time_limit:
            return dist_to_depot - self.graph.time_limit
        else:
            return 0

    def get_overage_time(self, chosen_idx):
        """ Gets the overage time for moving a node.  """
        if self.get_current_route_time() > self.graph.time_limit:
            return self.get_travel_dist(self.prev_node, chosen_idx)
        elif self.get_current_route_time() + self.get_travel_dist(self.prev_node, chosen_idx) > self.graph.time_limit:
            return self.get_current_route_time() + self.get_travel_dist(self.prev_node, chosen_idx) - self.graph.time_limit
        else:
            return 0

    def get_travel_dist(self, cur_node, next_node):
        """ Gets the travel distance between two nodes.  """
        return self.graph.W_full[cur_node, next_node]

    def get_current_route_time(self):
        """ Gets the current route time. """
        return self.dynamic[5].sum().item()

    def get_next_route_time(self, chosen_idx):
        """ Gets all the next routes time. """
        cur_time = self.dynamic[5].sum().item()
        # next_time = self.graph.W_weighted[chosen_idx]+ self.graph.W_weighted[chosen_idx][0]
        # next_time[0] = self.graph.W_weighted[chosen_idx][0] # avoid double count depot
        adj = torch.tensor(self.graph.W_weighted.clone().detach())
        # next_time = adj[chosen_idx]+ adj[:][0] # cur node to other nodes + other nodes to depot
        next_time = adj[chosen_idx]
        return cur_time + next_time

    def get_updated_load_and_demand(self, chosen_idx):
        """ Gets the updated load and demands. """
        return self._get_new_load_demand(chosen_idx)

    def get_demand_reward(self, chosen_idx):
        """ Gets the unmet demand at a current node or load if returning to depot. """
        load, demand = self._get_new_load_demand(chosen_idx)
        if chosen_idx == 0:
            return np.abs(self.graph.num_start - load)
        else:
            return np.abs(demand)

    def _get_new_load_demand(self, chosen_idx):
        """ Gets the new load and demand from visiting chosen_idx. """
        # difference in unmet demand
        load_idx = self.dynamic[1].clone()[self.prev_node]
        demand_idx = self.graph.demands[chosen_idx]

        new_load = torch.clamp(load_idx + demand_idx, max=self.graph.max_load, min=0)
        load_diff = new_load - load_idx
        new_demand = demand_idx - load_diff

        return new_load, new_demand

    def _get_demand_unvisited(self):
        return np.abs(self.dynamic[2]* (1- self.dynamic[0])).sum()

    def print_info(self):
        if self.verbose:
            print("#" * 100)
            print("Tour: ", self.tour_indices)
            print("Tour Reward:    ", self.ep_reward_tour)
            print("Demand Reward:  ", self.ep_reward_demand.item())
            print("Overage Reward: ", self.ep_reward_overage)
            print("Total Reward:   ", self.ep_reward_overage + self.ep_reward_demand.item() + self.ep_reward_tour)
            print("Left Demand: ", np.abs(self.dynamic[2]).sum().item())
            print("Node Visits: ", len(self.tour_indices))
            print("Games Finished: ", self.games)

    def render(self, save_path=None):
        """Plots the found solution."""
        plt.ion()
        plt.figure(0, figsize=(10, 10))

        nodes = self.graph.static.numpy()
        W = self.graph.W.detach().numpy()

        # Plot nodes
        colors = ['red']  # First node as depot
        for i in range(len(nodes) - 1):
            colors.append('blue')

        xs, ys = nodes[:, 0], nodes[:, 1]
        plt.scatter(xs, ys, color=colors)

        # Plot edges
        edgeSet = set()
        for row in range(W.shape[0]):
            for column in range(W.shape[1]):
                if W.item(row, column) == 1 and (column, row) not in edgeSet:  # get rid of repeat edge
                    edgeSet.add((row, column))

        for edge in edgeSet:
            X = nodes[edge, 0]
            Y = nodes[edge, 1]
            plt.plot(X, Y, "b-", lw=2, alpha=0.05)

        # Plot tours
        cars = self.tour_indices.count(0)
        cmap = plt.get_cmap('gist_ncar')
        colors_tour = [cmap(i) for i in np.linspace(0, 1, cars+1)]
        c = 0
        for i, idx in enumerate(self.tour_indices):
            if idx == 0:
                c+=1

            if i < len(self.tour_indices) - 1:
                next_node = self.tour_indices[i + 1]
                X = [nodes[idx][0], nodes[next_node][0]]
                Y = [nodes[idx][1], nodes[next_node][1]]
                dx = nodes[next_node][0] - nodes[idx][0]
                dy = nodes[next_node][1] - nodes[idx][1]

            else:
                X = [nodes[idx][0], nodes[0][0]]
                Y = [nodes[idx][1], nodes[0][1]]
                dx = nodes[0][0] - nodes[idx][0]
                dy = nodes[0][1] - nodes[idx][1]

            plt.arrow(x=nodes[idx][0], y=nodes[idx][1], dx=dx, dy=dy, width=0.0005, length_includes_head=True,
                      head_width=0.1, color=colors_tour[c])

            # plt.plot(X, Y, lw=1, color=colors_tour[c])

        # Show dynamic
        for i, (x, y) in enumerate(zip(xs, ys)):
            label = "N{}: {}/{}".format(i,self.dynamic_init[2][i].int(),self.dynamic[2][i].int())
            plt.annotate(label,  # this is the text
                         (x, y),  # these are the coordinates to position the label
                         textcoords="offset points",  # how to position the text
                         xytext=(0, 5),  # distance from text to points (x,y)
                         ha='center',
                         fontsize=10,
                         color="darkorange")

        plt.xlabel('X')
        plt.ylabel('Y')
        reward = (self.ep_reward_tour + self.ep_reward_demand + self.ep_reward_overage + self.ep_reward_car).item()
        plt_name = "Game {}, Total Reward: {:.1f} \n " \
                   "Tour: {:.1f}, Demand: {:.1f}, Overage: {:.1f}, Car: {:.1f}\n ".format(self.games, reward,
                                                                 self.ep_reward_tour,
                                                                 self.ep_reward_demand,
                                                                 self.ep_reward_overage,
                                                                self.ep_reward_car)
        plt.title(plt_name)
        # plt.axis('off')

        if save_path is None:
           save_path = 'rl_results/render_{}.pdf'.format(timestamp())

        plt.savefig(save_path, bbox_inches='tight', dpi=200)

        plt.pause(0.001)
        plt.close()