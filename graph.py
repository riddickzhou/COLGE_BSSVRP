import numpy as np
import networkx as nx
import collections
from sklearn.decomposition import PCA
from scipy.spatial.distance import pdist, squareform
import matplotlib.pyplot as plt
import os
import torch
from scipy import sparse
import scipy.stats as stats
from mat_fact import  compute_pmi_inf, compute_log_ramp, compute_mat_embed

os.environ['KMP_DUPLICATE_LIB_OK'] = 'True'


# seed = np.random.seed(120)

class Graph:
    def __init__(self,
                 n_nodes,
                 k_nn,
                 n_vehicles,
                 penalty_cost_demand,
                 penalty_cost_time,
                 speed,
                 time_limit,
                 starting_fraction=0.5,
                 bike_load_time=0.0,
                 max_load=20,
                 max_demand=9,
                 area=10):

        if max_load < max_demand:
            raise ValueError(':param max_load: must be > max_demand')

        self.n_nodes = n_nodes
        self.num_neighbors = k_nn
        self.max_load = max_load
        self.max_demand = max_demand
        self.bike_load_time = bike_load_time
        self.area = area  # km
        self.n_vehicles = n_vehicles
        self.penalty_cost_demand = penalty_cost_demand
        self.penalty_cost_time = penalty_cost_time
        self.speed = speed
        self.time_limit = time_limit
        self.starting_fraction = starting_fraction
        self.num_start = int(self.max_load * self.starting_fraction)

        self.rng = np.random.RandomState()
        self.seed_used = None
        self.bss_graph_gen()

    def seed(self, _seed):
        self.seed_used = _seed
        self.rng.seed(_seed)

    def gen_instance(self):  # Generate random instance
        # self.rng.seed(0)
        self.locations = self.rng.rand(self.n_nodes, 2) * self.area  # node num with (dimension) coordinates in [0,1]
        pca = PCA(n_components=2)  # center & rotate coordinates
        self.locations[0] = [0.5 * self.area , 0.5 * self.area]  # force depot to be at center
        self.locations = pca.fit_transform(self.locations)
        self.refresh_demand()

    def get_norm_demand(self):
        x = np.arange(-self.max_demand, self.max_demand+1)
        xU, xL = x + 0.5, x - 0.5
        prob = stats.norm.cdf(xU, scale=3) - stats.norm.cdf(xL, scale=3)
        prob = prob / prob.sum()
        demand = np.random.choice(x, size=self.n_nodes, p=prob)
        return  demand

    def refresh_demand(self):
        self.demands = self.get_demands()
        # self.demands = self.get_norm_demand()
        demands_tensor = torch.tensor(self.demands)
        cur_node = torch.zeros(self.n_nodes)
        prev_node = torch.zeros(self.n_nodes)
        cur_node[0] = 1
        prev_node[0] = 1
        trip_time = torch.zeros(self.n_nodes)
        trip_overage = torch.zeros(self.n_nodes)

        loads = torch.zeros(self.n_nodes)

        self.static = torch.tensor(self.locations)
        self.observation = torch.zeros(self.n_nodes)
        car_count = torch.zeros(self.n_nodes)
        self.dynamic = torch.stack((self.observation, loads, demands_tensor, cur_node, prev_node,trip_time,trip_overage,car_count),
                                   dim=0)


    def adjacenct_gen(self, n_nodes, num_neighbors, coords):
        assert num_neighbors < n_nodes

        # add KNN edges with random K
        W_val = squareform(pdist(coords, metric='euclidean'))
        W_val = self.get_time_based_distance_matrix(W_val)
        self.W_full = W_val.copy()

        W = np.zeros((n_nodes, n_nodes))
        knns = np.argpartition(W_val, kth=num_neighbors, axis=-1)[:, num_neighbors::-1]

        # depot is fully connected to all the other nodes
        W[0, :] = 1
        W[:, 0] = 1

        for idx in range(n_nodes):
            W[idx][knns[idx]] = 1
            W = W.T
            W[idx][knns[idx]] = 1

        # np.fill_diagonal(W, 0)

        W_val *= W
        return W.astype(int), W_val

    def node_emb(self,adj):
        pmi_inf = compute_pmi_inf(adj)
        pmi_inf_trans = compute_log_ramp(pmi_inf, T = 3)
        adj = compute_mat_embed(pmi_inf_trans, dims = 4)
        return adj

    def get_time_based_distance_matrix(self, W):
        return (W / self.speed) * 60

    def bss_graph_gen(self):
        self.gen_instance()
        self.W, self.W_val = self.adjacenct_gen(self.n_nodes, self.num_neighbors, self.static)
        while np.any(self.W_val[0]>=30):
            self.W, self.W_val = self.adjacenct_gen(self.n_nodes, self.num_neighbors, self.static)
        self.W_weighted = np.multiply(self.W_val, self.W)
        self.emb = torch.tensor(self.node_emb(self.W_weighted))
        self.W_weighted = torch.tensor(self.W_weighted)
        self.W = torch.tensor(self.W)
        self.A = sparse.csr_matrix(self.W_weighted)
        self.g = nx.from_numpy_matrix(np.matrix(self.W), create_using=nx.Graph)
        self.g_weighted = nx.from_numpy_matrix(np.matrix(self.W_weighted), create_using=nx.Graph)
        self.g.edges(data=True)

    def nodes(self):

        return nx.number_of_nodes(self.g)

    def edges(self):

        return self.g.edges()

    def neighbors(self, node):

        return nx.all_neighbors(self.g, node)

    def average_neighbor_degree(self, node):

        return nx.average_neighbor_degree(self.g, nodes=node)

    def adj(self):

        return nx.adjacency_matrix(self.g)

    def get_demands(self):
        """ Gets random demand vector that has zero sum. """

        # randomly sample demands
        demands = self.rng.randint(1, self.max_demand, self.n_nodes)
        demands *= self.rng.choice([-1, 1], self.n_nodes)  # exclude 0

        # zero demand at depot
        demands[0] = 0

        # adjust demands until they sum to zero.
        while True:

            if demands.sum() == 0:
                return demands

            demand_sum_pos = demands.sum() > 0

            idx = self.rng.randint(1, demands.shape[0])

            if demands[idx] < 0:
                if demand_sum_pos:
                    if demands[idx] == - self.max_demand:
                        continue
                    demands[idx] -= 1
                else:
                    if demands[idx] == -1:  # case for over -1 to 1
                        demands[idx] += 1
                    demands[idx] += 1

            elif demands[idx] > 0:
                if demand_sum_pos:
                    if demands[idx] == 1:  # case for over 1 to -1
                        demands[idx] -= 1
                    demands[idx] -= 1
                else:
                    if demands[idx] == self.max_demand:
                        continue
                    demands[idx] += 1

        return demands


# Toy Case Test
def test():
    g = Graph(
        n_nodes=10,
        k_nn=4,
        num_vehicles=3,
        penalty_cost_demand=1,
        penalty_cost_time=1,
        speed=30,
        time_limit=120)
    nx.draw(g.g, with_labels=True)
    plt.show()



if __name__ == "__main__":
    test()
