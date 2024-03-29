import gpytorch
import numpy as np
import torch
from gp_utils import MultiTaskBoTorchGP, BoTorchGP
import sobol_seq
from gpytorch.kernels import MaternKernel, ScaleKernel
import time
from dataclasses import dataclass
import math

"""
Script where all the Bayesian Optimization benchmarks are stored.

mfLiveBatch (original name of the paper) refers to the MF-GP-UCB with Local Penalization [Kandasamy et al. 2016, Gonzales et al. 2016
Alvi et al. 2019]

UCBwILP refers to PLAyBOOK [Alvi et al. 2019]

mfUCB refers to MF-GP-UCB [Kandasamy et al. 2016]

simpleUCB refers to the classical Upper Confidence Bound benchmark [Svrinivas et al. 2010]

MultiTaskUCBwILP refers to our method, using Local Penalization for batching

MF_MES refers to Multi-fidelity Max-Entropy Search [Takeno et al. 2020]

MF_TuRBO refers to our method, when used in conjunction with [Eriksson et al. 2019]

TuRBO refers to the classical method of [Eriksson et al. 2019]
#
"""


class mfLiveBatch():
    def __init__(self, env, beta = None, fidelity_thresholds = None, lipschitz_constant = 1, num_of_starts = 75, num_of_optim_epochs = 25, \
        hp_update_frequency = None, budget = 10, cost_budget = 4, initial_bias = 0.1, local_lipschitz = True, increasing_thresholds = False):
        '''
        Takes as inputs:
        env - optimization environment
        beta - parameter of UCB bayesian optimization, default uses 0.2 * self.dim * np.log(2 * (self.env.t + 1))
        lipschitz_consant - initial lipschitz_consant, will be re-estimated at every step
        num_of_starts - number of multi-starts for optimizing the acquisition function, default is 75
        num_of_optim_epochs - number of epochs for optimizing the acquisition function, default is 150
        hp_update_frequency - how ofter should GP hyper-parameters be re-evaluated, default is None
        '''
        # initialise the environment
        self.env = env
        self.dim = env.dim

        # initialize the maximum fidelity cost
        self.cost_budget = cost_budget
        # initialize budgets
        self.batch_costs = 0
        self.budget = budget

        # multifidelity parameters
        self.num_of_fidelities = self.env.num_of_fidelities
        if fidelity_thresholds is None:
            self.fidelity_thresholds = [0.1 for _ in range(self.num_of_fidelities)]
            self.fidelity_thresholds_init = [0.1 for _ in range(self.num_of_fidelities)]
        else:
            self.fidelity_thresholds = fidelity_thresholds
            self.fidelity_thresholds_init = fidelity_thresholds.copy()
        self.increasing_thresholds = increasing_thresholds
        # check if we need to update the maximum bias or it is set by the user
        self.update_max_bias = True
        # costs thresholds to double the fidelity thresholds
        self.cost_thresholds = [0] + [self.env.func.expected_costs[i] / self.env.func.expected_costs[i + 1] * self.cost_budget \
             for i in range(self.num_of_fidelities - 1)]
        
        # initialize count list
        self.fidelity_count_list = [0 for _ in range(self.num_of_fidelities)]
        # initialize bias
        if type(initial_bias) in [float, int]:
            self.bias_list = [i for i in range(self.num_of_fidelities)]
            self.bias_constant = initial_bias
        else:
            self.bias_list = initial_bias

        # gp hyperparams
        self.set_hyperparams()

        # values of LP
        if beta == None:
            self.fixed_beta = False
            self.beta = [float(0.2 * self.dim * np.log(2 * (self.env.current_time + 1))) for _ in range(self.num_of_fidelities)]
        else:
            self.fixed_beta = True
            self.beta = [beta for _ in range(self.num_of_fidelities)]
        
        self.penalization_gamma = 1

        # parameters of local penalization method
        self.lipschitz_constant = [lipschitz_constant for _ in range(self.num_of_fidelities)]
        self.max_value = [0 for _ in range(self.num_of_fidelities)]
        # initialize grid to select lipschitz constant
        self.estimate_lipschitz = True
        self.local_lipschitz = local_lipschitz
        self.num_of_grad_points = 50 * self.dim
        self.lipschitz_grid = sobol_seq.i4_sobol_generate(self.dim, self.num_of_grad_points)
        # do we require transform?
        if (self.env.func.require_transform == True):
            self.soft_plus_transform = True
        else:
            self.soft_plus_transform = False

        # optimisation parameters
        self.num_of_starts = num_of_starts
        self.num_of_optim_epochs = num_of_optim_epochs
        self.grid_search = self.env.func.grid_search
        self.grid_to_search = None
        # hp hyperparameters update frequency
        self.hp_update_frequency = hp_update_frequency
        self.last_n_obs = [0 for _ in range(self.num_of_fidelities)]

        # define domain
        self.domain = np.zeros((self.dim,))
        self.domain = np.stack([self.domain, np.ones(self.dim, )], axis=1)
        
        self.initialise_stuff()

    def set_hyperparams(self, constant = None, lengthscale = None, noise = None, mean_constant = None, constraints = False):
        '''
        This function is used to set the hyper-parameters of the GP.
        INPUTS:
        constant: positive float, multiplies the RBF kernel and defines the initital variance
        lengthscale: tensor of positive floats of length (dim), defines the kernel of the rbf kernel
        noise: positive float, noise assumption
        mean_constant: float, value of prior mean
        constraints: boolean, if True, we will apply constraints from paper based on the given hyperparameters
        '''
        if constant == None:
            self.constant = 0.6
            self.length_scale = torch.tensor([0.15 for _ in range(self.dim)])
            self.noise = 1e-4
            self.mean_constant = 0
        
        else:
            self.constant = constant
            self.length_scale = lengthscale
            self.noise = noise
            self.mean_constant = mean_constant
        
        self.gp_hyperparams = [(self.constant, self.length_scale, self.noise, self.mean_constant) for _ in range(self.num_of_fidelities)]
        # check if we want our constraints based on these hyperparams
        if constraints is True:
            for i in range(self.num_of_fidelities):
                self.model[i].define_constraints(self.length_scale, self.mean_constant, self.constant)
    
    def initialise_stuff(self):
        # list of queries
        self.queried_batch = [[]] * self.num_of_fidelities
        # list of queries and observations
        self.X = [[] for _ in range(self.num_of_fidelities)]
        self.Y = [[] for _ in range(self.num_of_fidelities)]
        # list of times at which we obtained the observations
        self.T = [[] for _ in range(self.num_of_fidelities)]
        # initialize bias observations, bias_X[i] will contain bias observations of f_{i}(x) = f_{i-1}(x) + bias_{i-1}(x)
        self.bias_X = [[] for _ in range(self.num_of_fidelities)]
        self.bias_Y = [[] for _ in range(self.num_of_fidelities)]
        # define model
        self.model = [BoTorchGP(lengthscale_dim = self.dim) for _ in range(self.num_of_fidelities)]
        # define bias model
        bias_kernel = ScaleKernel(MaternKernel(nu = 1.5, ard_num_dims = self.dim))
        self.bias_model = [BoTorchGP(lengthscale_dim = self.dim, kernel = bias_kernel) for _ in range(self.num_of_fidelities)]
        # time
        self.current_time = 0
        # initialise new_obs
        self.new_obs = None
    
    def run_optim(self, verbose = False):
        '''
        Runs the whole optimisation procedure, returns all queries and evaluations
        '''
        # self.env.initialise_optim()
        while self.current_time <= self.budget - 1:
            self.optim_loop()
            if verbose:
                print(f'Current time-step: {self.current_time}')
        # obtain all queries
        X, M, Y = self.env.finished_with_optim()
        # reformat all queries before returning
        num_of_remaining_queries = X.shape[0]
        for i in range(num_of_remaining_queries):
            fid = int(M[i, :])
            self.X[fid].append(list(X[i, :].reshape(1, -1)))
            self.Y[fid].append(Y[i])
            self.T[fid].append(self.current_time + 1)
        return self.X, self.Y, self.T
    
    def optim_loop(self):
        '''
        Performs a single loop of the optimisation
        '''
        # check if we need to update beta
        # check if we need to update beta
        if self.fixed_beta == False:
            beta = float(0.2 * self.dim * np.log(2 * (self.current_time / self.env.func.expected_costs[0]) + 1))
            self.beta = [beta for _ in range(self.num_of_fidelities)]

        # optimise acquisition function to obtain new queries until batch is full

        new_Xs = np.empty((0, self.dim))
        new_Ms = np.empty((0, 1))
        
        self.lipschitz_batch_list = []
        # obtain current batch
        self.current_batch = self.env.query_list.copy()
        self.current_batch_fids = self.env.fidelities_list.copy()
        if self.local_lipschitz:
            for i, penalty_point_fidelity in enumerate(zip(self.current_batch, self.current_batch_fids)):
                penalty_point = penalty_point_fidelity[0].reshape(1, -1)
                fidelity = int(penalty_point_fidelity[1])
                # re-define penalty point as tensor
                penalty_point = torch.tensor(penalty_point)
                # calculate local lipschitz constant
                local_lip_constant = self.calculate_local_lipschitz(penalty_point, fidelity)
                self.lipschitz_batch_list.append(local_lip_constant)
        else:
            # otherwise simply append the global lipschitz constant for each fidelity
            batch = self.env.query_list
            batch_fids = self.env.fidelities_list
            for i, penalty_point_fidelity in enumerate(zip(batch, batch_fids)):
                fidelity = int(penalty_point_fidelity[1])
                self.lipschitz_batch_list.append(self.lipschitz_constant[fidelity])

        # fill batch
        while self.batch_costs < self.cost_budget:
#            self.lipshitz_batch_list = []
            new_X, new_M = self.optimise_af()
            new_Xs = np.concatenate((new_Xs, new_X))
            new_Ms = np.concatenate((new_Ms, new_M))
            # add new_X and new_M to current batch
            self.current_batch = np.concatenate((self.current_batch, new_X))
            self.current_batch_fids = np.concatenate((self.current_batch_fids, new_M))
            # update batch costs
            self.batch_costs = self.batch_costs + self.env.func.fidelity_costs[int(new_M)]
            # if loop is not going to break, add new lipschitz constant
            if self.batch_costs < self.cost_budget:
                # new_M and new_X define the penalty point
                fidelity = int(new_M)
                penalty_point = torch.tensor(new_X)
                # calculate local lipschitz constant
                local_lip_constant = self.calculate_local_lipschitz(penalty_point, fidelity)
                self.lipschitz_batch_list.append(local_lip_constant)

        obtain_query, obtain_fidelities, self.new_obs = self.env.step(new_Xs, new_Ms)

        # update model if there are new observations
        if self.new_obs is not None:

            num_of_obs = obtain_query.shape[0]
            bias_updates = []

            for i in range(num_of_obs):
                # append new observations and the time at which they were observed
                fid = int(obtain_fidelities[i])
                self.X[fid].append(list(obtain_query[i, :].reshape(-1)))
                self.Y[fid].append(self.new_obs[i])
                self.T[fid].append(self.current_time + 1)
                # if fidelity is not the lowest, check bias assumption. If broken, double the penalty.
                if fid != self.num_of_fidelities - 1:
                    # calculate posterior prediction
                    f_low_fid_pred, _ = self.model[fid + 1].posterior(obtain_query[i, :].reshape(1, -1))
                    f_low_fid_pred = f_low_fid_pred.detach().numpy()
                    f_high_fid_obs = self.new_obs[i]
                    # check if difference is higher than expected
                    diff = float(f_high_fid_obs - f_low_fid_pred)
                    if (diff > self.bias_constant) & (self.update_max_bias):
                        self.bias_constant = 1.2 * diff
                    # if we observe highest fidelity, obtain bias observations
                    if fid == 0:
                        for lower_fid in range(1, self.num_of_fidelities):
                            # check the mean of each prediction
                            f_low_fid_pred, _ = self.model[lower_fid].posterior(obtain_query[i, :].reshape(1, -1))
                            f_low_fid_pred = f_low_fid_pred.detach().numpy()
                            # calculate difference
                            diff = f_high_fid_obs - f_low_fid_pred
                            # append corresponding bias observations
                            self.bias_X[lower_fid].append(list(obtain_query[i, :].reshape(-1)))
                            self.bias_Y[lower_fid].append(diff)
                            bias_updates.append(lower_fid)
                # redefine new maximum value
                self.max_value[fid] = float(max(self.max_value[fid], float(self.new_obs[i])))
                # take away batch cost
                self.batch_costs = self.batch_costs - self.env.func.fidelity_costs[fid]

            # check which model need to be updated according to the fidelities
            update_set = set(obtain_fidelities.reshape(-1))
            bias_update_set = set(bias_updates)
            self.update_model(update_set)
            self.update_model_bias(bias_update_set)

        # update hyperparams if needed
        # if there is only one fidelity, simple case
        if self.num_of_fidelities == 1:
            n_obs = 0
            for fid in range(self.num_of_fidelities):
                n_obs = n_obs + len(self.X[fid])

            if (self.hp_update_frequency is not None) & (len(self.X[0]) > 0):
                if (n_obs >= (self.hp_update_frequency + self.last_n_obs[0])):
                    self.last_n_obs[fid] = n_obs
                    self.model[0].optim_hyperparams()
                    self.gp_hyperparams[0] = self.model[0].current_hyperparams()
        # multi-fidelity case is more complicated. We use the lowest fidelity as the base for our initializations
        else:
            # first fidelity is special, as we might retrain all other models as well
            if (self.hp_update_frequency is not None) & (len(self.X[self.num_of_fidelities - 1]) > 0):
                n_obs = len(self.X[self.num_of_fidelities - 1])
                # check if enough observations have passed since last time
                if (n_obs >= (self.hp_update_frequency + self.last_n_obs[self.num_of_fidelities - 1])):
                    # update n on last update
                    self.last_n_obs[self.num_of_fidelities - 1] = n_obs
                    # optimize hyperparameters
                    self.model[self.num_of_fidelities - 1].optim_hyperparams()
                    self.gp_hyperparams[self.num_of_fidelities - 1] = self.model[self.num_of_fidelities - 1].current_hyperparams()
                    # for fid2 in range(self.num_of_fidelities - 1):
                        # obtain hyperparameters of lower model
                        # self.gp_hyperparams[fid2] = self.model[self.num_of_fidelities - 1].current_hyperparams()
                        # if we have enough observations for the other fidelities, retrain those as well but not the prior mean constant
                        # if len(self.X[fid2]) > 10:
                        #     self.model[fid2].set_hyperparams(self.model[self.num_of_fidelities - 1].current_hyperparams())
                        #    self.model[fid2].optim_hyperparams(train_only_outputscale_and_noise = True)
                        #    self.gp_hyperparams[fid2] = self.model[fid2].current_hyperparams()
            
            # we further train the hyper-parameters whenever we reach the correct number of new observations for each fidelity
            for fid in range(self.num_of_fidelities - 1):
                # obtain number of observations
                n_obs = len(self.X[fid])
                # update the hyper-parameters if there are enough new observations
                if (self.hp_update_frequency is not None) & (len(self.X[fid]) > 0):
                    if (n_obs >= (self.hp_update_frequency + self.last_n_obs[fid])):
                        # update last n when it was updated
                        self.last_n_obs[fid] = n_obs
                        # initialize hyper-parameters using lower fidelity
                        self.model[fid].set_hyperparams(self.model[self.num_of_fidelities - 1].current_hyperparams())
                        # train hyper-parameters
                        self.model[fid].optim_hyperparams(train_only_outputscale_and_noise = True)
                        # set new hyper-parameters
                        self.gp_hyperparams[fid] = self.model[fid].current_hyperparams()
        # update current temperature and time
        self.current_time = self.current_time + 1
    
    def update_model(self, update_set):
        '''
        This function updates the GP model
        '''
        if self.new_obs is not None:
            for i in update_set:
                i = int(i)
                # fit new model
                self.model[i].fit_model(self.X[i], self.Y[i], previous_hyperparams=self.gp_hyperparams[i])
                # we also update our estimate of the lipschitz constant, since we have a new model
                # define the grid over which we will calculate gradients
                grid = torch.tensor(self.lipschitz_grid, requires_grad = True).double()
                # we only do this if we are in asynchronous setting, otherwise this should behave as normal UCB algorithm
                if self.estimate_lipschitz == True:
                    # calculate mean of the GP
                    mean, _ = self.model[i].posterior(grid)
                    # calculate the gradient of the mean
                    external_grad = torch.ones(self.num_of_grad_points)
                    mean.backward(gradient = external_grad)
                    mu_grads = grid.grad
                    # find the norm of all the mean gradients
                    mu_norm = torch.norm(mu_grads, dim = 1)
                    # choose the largest one as our estimate
                    self.lipschitz_constant[i] = max(mu_norm).item()
    
    def calculate_local_lipschitz(self, pen_point, fid):
        # if there is no model yet, use the prior
        if self.X[fid] != []:
            pass
        else:
            return self.lipschitz_constant[fid]

        with torch.no_grad():
            # first center grid around pen_point
            grid = torch.tensor(self.lipschitz_grid)
            # now scale the grid by the lengthscales
            if self.X[fid] != []:
                hypers = self.model[fid].current_hyperparams()
            else:
                hypers = self.gp_hyperparams[fid]
            
            lengthscale = hypers[1]
            # multiply grid by lengthscales and center
            grid = (grid - grid[0]) * lengthscale + pen_point
            # clamp grid in the correct bounds
            bounds = torch.stack([torch.zeros(self.dim), torch.ones(self.dim)])
            for j, (lb, ub) in enumerate(zip(*bounds)):
                grid.data[..., j].clamp_(lb, ub)

        # finally estimate lipschitz constant
        grid = grid.clone().detach().double().requires_grad_(True)
        # calculate mean of the GP
        mean, _ = self.model[fid].posterior(grid)
        # calculate the gradient of the mean
        external_grad = torch.ones(self.num_of_grad_points)
        mean.backward(gradient = external_grad)
        mu_grads = grid.grad
        # find the norm of all the mean gradients
        mu_norm = torch.norm(mu_grads, dim = 1)
        # choose the largest one as our estimate
        lipschitz_constant = max(mu_norm).item()
        return lipschitz_constant

    def update_model_bias(self, update_set):
        '''
        This function updates the GP model for the biases
        '''
        if self.new_obs is not None:
            for i in update_set:
                i = int(i)
                # fit new bias models
                hypers_function = list(self.model[i].current_hyperparams())
                hypers = ((self.bias_list[i] * self.bias_constant / self.beta[i])**2, hypers_function[1], 1e-3, 0)
                self.bias_model[i].fit_model(self.bias_X[i], self.bias_Y[i], previous_hyperparams=hypers)

    def build_af(self, X):
        '''
        This takes input locations, X, and returns the value of the acquisition function
        '''
        # check the batch of points being evaluated
        batch = self.current_batch.copy()
        batch_fids = self.current_batch_fids.copy()
        # initialize ucb
        ucb_shape = (self.num_of_fidelities, X.shape[0])
        ucb = torch.zeros(size = ucb_shape)
        # for every fidelity
        for i in range(self.num_of_fidelities):
            # check if we should use trained model or simply the prior
            if self.X[i] != []:
                mean, std = self.model[i].posterior(X)
            else:
                hypers = self.gp_hyperparams[i]
                mean_constant = hypers[3]
                constant = hypers[0]
                mean, std = torch.tensor(mean_constant), torch.tensor(constant)
            # calculate bias upper confidence bound
            if self.bias_X[i] != []:
                mean_bias, std_bias = self.bias_model[i].posterior(X)
            else:
                mean_bias, std_bias = torch.tensor(0), torch.tensor(self.bias_list[i] * self.bias_constant) / self.beta[i]
            ucb_bias = mean_bias + self.beta[i] * std_bias
            # calculate total upper confidence bound
            ucb[i, :] = mean + self.beta[i] * std + ucb_bias
        # apply softmax transform if necessary
        if self.soft_plus_transform:
            ucb = torch.log(1 + torch.exp(ucb))
        # penalize acquisition function, loop through batch of evaluations
        for i, penalty_point_fidelity in enumerate(zip(batch, batch_fids)):
            penalty_point = penalty_point_fidelity[0].reshape(1, -1)
            fidelity = int(penalty_point_fidelity[1])
            # re-define penalty point as tensor
            penalty_point = torch.tensor(penalty_point)
            # calculate mean and variance of model at penalty point
            if self.X[fidelity] != []:
                mean_pp, std_pp = self.model[fidelity].posterior(penalty_point)
            else:
                hypers = self.gp_hyperparams[fidelity]
                mean_constant = hypers[3]
                constant = hypers[0]
                mean_pp, std_pp = torch.tensor(mean_constant), torch.tensor(constant)
            # calculate values of r_j
            r_j = (self.max_value[fidelity] - mean_pp) / self.lipschitz_batch_list[i]
            denominator = r_j + self.penalization_gamma * std_pp / self.lipschitz_batch_list[i]
            # calculate norm between x and penalty point
            norm = torch.norm(penalty_point - X, dim = 1)
            # define penaliser
            penaliser = torch.min(norm / denominator, torch.tensor(1))
            # penalise ucb
            ucb[fidelity, :] = ucb[fidelity, :].clone() * penaliser
        # return acquisition function
        min_ucb, _ = torch.min(ucb, dim = 0)
        return min_ucb
    
    def optimise_af(self):
        '''
        This function optimizes the acquisition function, and returns the next query point
        '''
        # if time is zero, pick point at random, lowest fidelity
        if self.current_time == 0:
            new_X = np.random.uniform(size = self.dim).reshape(1, -1)
            new_M = np.array(self.num_of_fidelities - 1).reshape(1, 1)
            return new_X, new_M
        
        # if we are simply optimizing with grid search, to be used when there are constraints
        if self.grid_search is True:
            with torch.no_grad():
                # regenerate search grid every time
                self.grid_to_search = self.env.func.gen_search_grid(int(100 * self.num_of_starts))
                X = self.grid_to_search.clone()
                # check acquisition function in grid
                af = self.build_af(X)
                # choose the best point
                best_idx = torch.argmax(af)
            # return the best value in the grid
            best_input = X[best_idx, :].detach()
            best = best_input.detach().numpy().reshape(1, -1)
            new_X = best.reshape(1, -1)
            # choose fidelity level for this point
            for i in reversed(range(self.num_of_fidelities)):
                # set fidelity
                new_M = np.array(i).reshape(1, 1)
                # if we reach target fidelity, break
                if i == 0:
                    break
                # if there is data use posterior, else use prior
                if self.X[i] != []:
                    _, std = self.model[i].posterior(new_X)
                else:
                    hypers = self.gp_hyperparams[i]
                    mean_constant = hypers[3]
                    constant = hypers[0]
                    _, std = torch.tensor(mean_constant), torch.tensor(constant)
                
                # check fidelity thresholds
                threshold = self.beta[i] * std
                if threshold > self.fidelity_thresholds[i]:
                    break

            new_M_int = int(new_M)
            self.fidelity_count_list[new_M_int] = self.fidelity_count_list[new_M_int] + 1
            if new_M_int != self.num_of_fidelities - 1:
                self.fidelity_count_list[new_M_int + 1] = 0
            
            if (self.fidelity_count_list[new_M_int] > self.cost_thresholds[new_M_int]) & (new_M_int != 0):
                # self.fidelity_thresholds[new_M_int] = self.fidelity_thresholds[new_M_int] + self.fidelity_thresholds_init[new_M_int] * self.dim
                self.fidelity_thresholds[new_M_int] = self.fidelity_thresholds[new_M_int] * 2
                self.fidelity_count_list[new_M_int] = 0

            return new_X, new_M

        # optimisation bounds
        bounds = torch.stack([torch.zeros(self.dim), torch.ones(self.dim)])
        # sobol initialization initialization, on 100 * num_of_starts, check for best 10 and optimize from there
        sobol_gen = torch.quasirandom.SobolEngine(self.dim, scramble = True)
        if self.dim > 5:
            dim_multiplier = 5
        else:
            dim_multiplier = self.dim
        X = sobol_gen.draw(100 * self.num_of_starts * dim_multiplier).double()

        with torch.no_grad():
            af = self.build_af(X)
            idx_list = list(range(0, self.num_of_starts * 100 * dim_multiplier))
            sorted_af_idx = [idx for _, idx in sorted(zip(af, idx_list))]
            best_idx = sorted_af_idx[-10:]

        # choose best starts for X
        X = X[best_idx, :]
        X.requires_grad = True
        # define optimiser
        optimiser = torch.optim.Adam([X], lr = 0.01)
        af = self.build_af(X)
        
        # do the optimisation
        for _ in range(self.num_of_optim_epochs):
            # set zero grad
            optimiser.zero_grad()
            # losses for optimiser
            losses = -self.build_af(X)
            loss = losses.sum()
            loss.backward()
            # optim step
            optimiser.step()

            # make sure we are still within the bounds
            for j, (lb, ub) in enumerate(zip(*bounds)):
                X.data[..., j].clamp_(lb, ub)
        
        # find the best start
        best_start = torch.argmax(-losses)

        # corresponding best input
        best_input = X[best_start, :].detach()
        best = best_input.detach().numpy().reshape(1, -1)
        new_X = best.reshape(1, -1)

        # now choose the corresponding fidelity
        for i in reversed(range(self.num_of_fidelities)):
            # set fidelity
            new_M = np.array(i).reshape(1, 1)
            # if we reach target fidelity, break
            if i == 0:
                break
            # if there is data use posterior, else use prior
            if self.X[i] != []:
                _, std = self.model[i].posterior(new_X)
            else:
                hypers = self.gp_hyperparams[i]
                mean_constant = hypers[3]
                constant = hypers[0]
                _, std = torch.tensor(mean_constant), torch.tensor(constant)
            
            # check fidelity thresholds
            if self.increasing_thresholds:
                threshold = self.beta[i] * std
            else:
                threshold = std

            if threshold > self.fidelity_thresholds[i]:
                break
        
        new_M_int = int(new_M)
        self.fidelity_count_list[new_M_int] = self.fidelity_count_list[new_M_int] + 1
        if new_M_int != self.num_of_fidelities - 1:
            self.fidelity_count_list[new_M_int + 1] = 0
        
        if (self.fidelity_count_list[new_M_int] > self.cost_thresholds[new_M_int]) & (new_M_int != 0):
            # self.fidelity_thresholds[new_M_int] = self.fidelity_thresholds[new_M_int] + self.fidelity_thresholds_init[new_M_int] * self.dim
            self.fidelity_thresholds[new_M_int] = self.fidelity_thresholds[new_M_int] * 2
            self.fidelity_count_list[new_M_int] = 0
        
        return new_X, new_M

class UCBwILP(mfLiveBatch):
    def __init__(self, env, beta=None, fidelity_thresholds=None, lipschitz_constant=1, num_of_starts=75, num_of_optim_epochs=25, hp_update_frequency=None, budget=10, cost_budget=4, initial_bias=0, local_lipschitz = True):
        super().__init__(env, beta, fidelity_thresholds, lipschitz_constant, num_of_starts, num_of_optim_epochs, hp_update_frequency, budget, cost_budget, initial_bias)
        self.num_of_fidelities = 1
        self.local_lipschitz = local_lipschitz

class mfUCB(mfLiveBatch):
    '''
    Class for Multi-Fidelity Upper Confidence Bound Bayesian Optimization model. This is a sequential method that takes advantage of multi-fidelity measurements.
    '''
    def __init__(self, env, beta=None, fidelity_thresholds=None, lipschitz_constant=1, num_of_starts=75, num_of_optim_epochs=25, hp_update_frequency=None, budget=10, cost_budget=4, initial_bias=0, increasing_thesholds = False):
        super().__init__(env, beta, fidelity_thresholds, lipschitz_constant, num_of_starts, num_of_optim_epochs, hp_update_frequency, budget, cost_budget, initial_bias, increasing_thresholds=increasing_thesholds)
        self.query_being_evaluated = False
        # costs thresholds to double the fidelity thresholds
        self.cost_thresholds = [0] + [self.env.func.expected_costs[i] / self.env.func.expected_costs[i +1] \
             for i in range(self.num_of_fidelities - 1)]

    def optim_loop(self):
        '''
        Performs a single loop of the optimisation
        '''
        # check if we need to update beta
        if self.fixed_beta == False:
            beta = float(0.2 * self.dim * np.log(2 * (self.current_time + 1) / (self.env.func.expected_costs[0])))
            self.beta = [beta for _ in range(self.num_of_fidelities)]

        # optimise acquisition function if no function is being evaluated
        new_Xs = np.empty((0, self.dim))
        new_Ms = np.empty((0, 1))

        if self.query_being_evaluated is False:
            # obtain new query
            new_X, new_M = self.optimise_af()
            new_Xs = np.concatenate((new_Xs, new_X))
            new_Ms = np.concatenate((new_Ms, new_M))
            # add to batch costs
            self.batch_costs = self.batch_costs + self.env.func.fidelity_costs[int(new_M)]
            # check if our main query is finished evaluating
            self.query_being_evaluated = True

            # now fill the remaining batch space randomly
            while self.batch_costs < self.cost_budget:
                new_X = np.random.uniform(size = (1, self.dim))
                # add to new_Xs and new_Ms
                new_Xs = np.concatenate((new_Xs, new_X))
                new_Ms = np.concatenate((new_Ms, new_M))
                # add to batch costs
                self.batch_costs = self.batch_costs + self.env.func.fidelity_costs[int(new_M)]

        obtain_query, obtain_fidelities, self.new_obs = self.env.step(new_Xs, new_Ms)

        # update model if there are new observations
        if self.new_obs is not None:

            num_of_obs = obtain_query.shape[0]

            for i in range(num_of_obs):
                # append new observations and the time at which they were observed
                fid = int(obtain_fidelities[i])
                self.X[fid].append(list(obtain_query[i, :].reshape(-1)))
                self.Y[fid].append(self.new_obs[i])
                self.T[fid].append(self.current_time + 1)
                # if fidelity is not the lowest, check bias assumption
                if fid != self.num_of_fidelities - 1:
                    # calculate posterior prediction
                    f_low_fid_pred, _ = self.model[fid + 1].posterior(obtain_query[i, :].reshape(1, -1))
                    f_low_fid_pred = f_low_fid_pred.detach().numpy()
                    f_high_fid_obs = self.new_obs[i]
                    # check if difference is higher than expected
                    diff = float(f_high_fid_obs - f_low_fid_pred)
                    if (diff > self.bias_constant) & (self.update_max_bias):
                        self.bias_constant = 1.2 * diff
                # take away batch cost
                self.batch_costs = self.batch_costs - self.env.func.fidelity_costs[fid]

            # check which model need to be updated according to the fidelities
            update_set = set(obtain_fidelities.reshape(-1))
            self.update_model(update_set)
            # this means we are no longer evaluating a query
            self.query_being_evaluated = False
        
        # update hyperparams if needed
        # if there is only one fidelity, simple case
        if self.num_of_fidelities == 1:
            n_obs = 0
            for fid in range(self.num_of_fidelities):
                n_obs = n_obs + len(self.X[fid])

            if (self.hp_update_frequency is not None) & (len(self.X[0]) > 0):
                if (n_obs >= (self.hp_update_frequency + self.last_n_obs[0])):
                    self.last_n_obs[fid] = n_obs
                    self.model[0].optim_hyperparams()
                    self.gp_hyperparams[0] = self.model[0].current_hyperparams()
        # multi-fidelity case is more complicated. We use the lowest fidelity as the base for our initializations
        else:
            # first fidelity is special, as we might retrain all other models as well
            if (self.hp_update_frequency is not None) & (len(self.X[self.num_of_fidelities - 1]) > 0):
                n_obs = len(self.X[self.num_of_fidelities - 1])
                # check if enough observations have passed since last time
                if (n_obs >= (self.hp_update_frequency + self.last_n_obs[self.num_of_fidelities - 1])):
                    # update n on last update
                    self.last_n_obs[self.num_of_fidelities - 1] = n_obs
                    # optimize hyperparameters
                    self.model[self.num_of_fidelities - 1].optim_hyperparams()
                    self.gp_hyperparams[self.num_of_fidelities - 1] = self.model[self.num_of_fidelities - 1].current_hyperparams()
                    # for fid2 in range(self.num_of_fidelities - 1):
                        # obtain hyperparameters of lower model
                        # self.gp_hyperparams[fid2] = self.model[self.num_of_fidelities - 1].current_hyperparams()
                        # if we have enough observations for the other fidelities, retrain those as well but not the prior mean constant
                        # if len(self.X[fid2]) > 10:
                        #     self.model[fid2].set_hyperparams(self.model[self.num_of_fidelities - 1].current_hyperparams())
                        #    self.model[fid2].optim_hyperparams(train_only_outputscale_and_noise = True)
                        #    self.gp_hyperparams[fid2] = self.model[fid2].current_hyperparams()
            
            # we further train the hyper-parameters whenever we reach the correct number of new observations for each fidelity
            for fid in range(self.num_of_fidelities - 1):
                # obtain number of observations
                n_obs = len(self.X[fid])
                # update the hyper-parameters if there are enough new observations
                if (self.hp_update_frequency is not None) & (len(self.X[fid]) > 0):
                    if (n_obs >= (self.hp_update_frequency + self.last_n_obs[fid])):
                        # update last n when it was updated
                        self.last_n_obs[fid] = n_obs
                        # initialize hyper-parameters using lower fidelity
                        self.model[fid].set_hyperparams(self.model[self.num_of_fidelities - 1].current_hyperparams())
                        # train hyper-parameters
                        self.model[fid].optim_hyperparams(train_only_outputscale_and_noise = True)
                        # set new hyper-parameters
                        self.gp_hyperparams[fid] = self.model[fid].current_hyperparams()
        
        # update current temperature and time
        self.current_time = self.current_time + 1
    
    def build_af(self, X):
        '''
        This takes input locations, X, and returns the value of the acquisition function
        '''
        # initialize ucb
        ucb_shape = (self.num_of_fidelities, X.shape[0])
        ucb = torch.zeros(size = ucb_shape)
        # for every fidelity
        for i in range(self.num_of_fidelities):
            if self.X[i] != []:
                # if we have no data return prior
                if self.model[i].train_x == None:
                    print('x')
                mean, std = self.model[i].posterior(X)
            else:
                hypers = self.gp_hyperparams[i]
                mean_constant = hypers[3]
                constant = hypers[0]
                mean, std = torch.tensor(mean_constant), torch.tensor(constant)
            # calculate upper confidence bound
            ucb[i, :] = mean + self.beta[i] * std + self.bias_list[i] * self.bias_constant
        
        # return acquisition function
        min_ucb, _ = torch.min(ucb, dim = 0)
        return min_ucb

    def optimise_af(self):
        '''
        This function optimizes the acquisition function, and returns the next query point
        '''
        # if time is zero, pick point at random, lowest fidelity
        if self.current_time == 0:
            new_X = np.random.uniform(size = self.dim).reshape(1, -1)
            new_M = np.array(self.num_of_fidelities - 1).reshape(1, 1)
            return new_X, new_M
        
        # if we are simply optimizing with grid search, to be used when there are constraints
        if self.grid_search is True:
            with torch.no_grad():
                # regenerate search grid every time
                self.grid_to_search = self.env.func.gen_search_grid(int(100 * self.num_of_starts))
                X = self.grid_to_search.clone()
                # check acquisition function in grid
                af = self.build_af(X)
                # choose the best point
                best_idx = torch.argmax(af)
            # return the best value in the grid
            best_input = X[best_idx, :].detach()
            best = best_input.detach().numpy().reshape(1, -1)
            new_X = best.reshape(1, -1)
            # choose fidelity level for this point
            for i in reversed(range(self.num_of_fidelities)):
                # set fidelity
                new_M = np.array(i).reshape(1, 1)
                # if we reach target fidelity, break
                if i == 0:
                    break
                # if there is data use posterior, else use prior
                if self.X[i] != []:
                    _, std = self.model[i].posterior(new_X)
                else:
                    hypers = self.gp_hyperparams[i]
                    mean_constant = hypers[3]
                    constant = hypers[0]
                    _, std = torch.tensor(mean_constant), torch.tensor(constant)
                
                # check fidelity thresholds
                threshold = self.beta[i] * std
                if threshold > self.fidelity_thresholds[i]:
                    break

            new_M_int = int(new_M)
            self.fidelity_count_list[new_M_int] = self.fidelity_count_list[new_M_int] + 1
            if new_M_int != self.num_of_fidelities - 1:
                self.fidelity_count_list[new_M_int + 1] = 0
            
            if (self.fidelity_count_list[new_M_int] > self.cost_thresholds[new_M_int]) & (new_M_int != 0):
                # self.fidelity_thresholds[new_M_int] = self.fidelity_thresholds[new_M_int] + self.fidelity_thresholds_init[new_M_int] * self.dim
                self.fidelity_thresholds[new_M_int] = self.fidelity_thresholds[new_M_int] * 2
                self.fidelity_count_list[new_M_int] = 0

            return new_X, new_M

        # optimisation bounds
        bounds = torch.stack([torch.zeros(self.dim), torch.ones(self.dim)])
        
        # sobol initialization initialization, on 100 * num_of_starts, check for best 10 and optimize from there
        sobol_gen = torch.quasirandom.SobolEngine(self.dim, scramble = True)

        if self.dim > 5:
            dim_multiplier = 5
        else:
            dim_multiplier = self.dim

        X = sobol_gen.draw(100 * self.num_of_starts * dim_multiplier).double()
        with torch.no_grad():
            af = self.build_af(X)
            idx_list = list(range(0, self.num_of_starts * 100 * dim_multiplier))
            sorted_af_idx = [idx for _, idx in sorted(zip(af, idx_list))]
            best_idx = sorted_af_idx[-10:]

        # choose best starts for X
        X = X[best_idx, :]
        X.requires_grad = True
        # define optimiser
        optimiser = torch.optim.Adam([X], lr = 0.01)
        af = self.build_af(X)
        
        # do the optimisation
        for _ in range(self.num_of_optim_epochs):
            # set zero grad
            optimiser.zero_grad()
            # losses for optimiser
            losses = -self.build_af(X)
            loss = losses.sum()
            loss.backward()
            # optim step
            optimiser.step()

            # make sure we are still within the bounds
            for j, (lb, ub) in enumerate(zip(*bounds)):
                X.data[..., j].clamp_(lb, ub)
        
        # find the best start
        best_start = torch.argmax(-losses)

        # corresponding best input
        best_input = X[best_start, :].detach()
        best = best_input.detach().numpy().reshape(1, -1)
        new_X = best.reshape(1, -1)

        # now choose the corresponding fidelity
        for i in reversed(range(self.num_of_fidelities)):
            # set fidelity
            new_M = np.array(i).reshape(1, 1)
            # if we reach target fidelity, break
            if i == 0:
                break
            # if there is data use posterior, else use prior
            if self.X[i] != []:
                _, std = self.model[i].posterior(new_X)
            else:
                hypers = self.gp_hyperparams[i]
                mean_constant = hypers[3]
                constant = hypers[0]
                _, std = torch.tensor(mean_constant), torch.tensor(constant)
            # check fidelity thresholds
            if self.increasing_thresholds:
                threshold = self.beta[i] * std
            else:
                threshold = std
            
            if threshold > self.fidelity_thresholds[i]:
                break
        
        new_M_int = int(new_M)
        self.fidelity_count_list[new_M_int] = self.fidelity_count_list[new_M_int] + 1
        if new_M_int != self.num_of_fidelities - 1:
            self.fidelity_count_list[new_M_int + 1] = 0
        
        if (self.fidelity_count_list[new_M_int] > self.cost_thresholds[new_M_int]) & (new_M_int != 0):
            # self.fidelity_thresholds[new_M_int] = self.fidelity_thresholds[new_M_int] + self.fidelity_thresholds_init[new_M_int]
            self.fidelity_thresholds[new_M_int] = self.fidelity_thresholds[new_M_int] * 2
            self.fidelity_count_list[new_M_int] = 0
        
        return new_X, new_M

class mfUCBPlus(mfLiveBatch):
    '''
    Class for Multi-Fidelity Upper Confidence Bound Bayesian Optimization model. This is a sequential method that takes advantage of multi-fidelity measurements.
    '''
    def __init__(self, env, beta=None, fidelity_thresholds=None, lipschitz_constant=1, num_of_starts=75, num_of_optim_epochs=25, hp_update_frequency=None, budget=10, cost_budget=4, initial_bias=0):
        super().__init__(env, beta, fidelity_thresholds, lipschitz_constant, num_of_starts, num_of_optim_epochs, hp_update_frequency, budget, cost_budget, initial_bias)
        self.query_being_evaluated = False
        # costs thresholds to double the fidelity thresholds
        self.cost_thresholds = [0] + [self.env.func.expected_costs[i] / self.env.func.expected_costs[i + 1] \
             for i in range(self.num_of_fidelities - 1)]

    def optim_loop(self):
        '''
        Performs a single loop of the optimisation
        '''
        # check if we need to update beta
        if self.fixed_beta == False:
            for fid in self.num_of_fidelities:
                self.beta[fid] = float(0.2 * self.dim * np.log(2 * (len(self.X[fid]) + 1)))
        # optimise acquisition function if no function is being evaluated
        new_Xs = np.empty((0, self.dim))
        new_Ms = np.empty((0, 1))

        if self.query_being_evaluated is False:
            # obtain new query
            new_X, new_M = self.optimise_af()
            new_Xs = np.concatenate((new_Xs, new_X))
            new_Ms = np.concatenate((new_Ms, new_M))
            # add to batch costs
            self.batch_costs = self.batch_costs + self.env.func.fidelity_costs[int(new_M)]
            # check if our main query is finished evaluating
            self.query_being_evaluated = True

            # now fill the remaining batch space randomly
            while self.batch_costs < self.cost_budget:
                new_X = np.random.uniform(size = (1, self.dim))
                # add to new_Xs and new_Ms
                new_Xs = np.concatenate((new_Xs, new_X))
                new_Ms = np.concatenate((new_Ms, new_M))
                # add to batch costs
                self.batch_costs = self.batch_costs + self.env.func.fidelity_costs[int(new_M)]

        obtain_query, obtain_fidelities, self.new_obs = self.env.step(new_Xs, new_Ms)

        # update model if there are new observations
        if self.new_obs is not None:

            num_of_obs = obtain_query.shape[0]
            bias_updates = []

            for i in range(num_of_obs):
                # append new observations and the time at which they were observed
                fid = int(obtain_fidelities[i])
                self.X[fid].append(list(obtain_query[i, :].reshape(-1)))
                self.Y[fid].append(self.new_obs[i])
                self.T[fid].append(self.current_time + 1)
                if fid != self.num_of_fidelities - 1:
                    # calculate posterior prediction
                    f_low_fid_pred, _ = self.model[fid + 1].posterior(obtain_query[i, :].reshape(1, -1))
                    f_low_fid_pred = f_low_fid_pred.detach().numpy()
                    f_high_fid_obs = self.new_obs[i]
                    # check if difference is higher than expected
                    diff = float(f_high_fid_obs - f_low_fid_pred)
                    if diff > self.bias_constant:
                        self.bias_constant = 1.2 * diff
                    # if we observe highest fidelity, obtain bias observations
                    if fid == 0:
                        for lower_fid in range(1, self.num_of_fidelities):
                            # check the mean of each prediction
                            f_low_fid_pred, _ = self.model[lower_fid].posterior(obtain_query[i, :].reshape(1, -1))
                            f_low_fid_pred = f_low_fid_pred.detach().numpy()
                            # calculate difference
                            diff = f_high_fid_obs - f_low_fid_pred
                            # append corresponding bias observations
                            self.bias_X[lower_fid].append(list(obtain_query[i, :].reshape(-1)))
                            self.bias_Y[lower_fid].append(diff)
                            bias_updates.append(lower_fid)
                            if lower_fid == 2:
                                print('wut')
                # redefine new maximum value
                self.max_value[fid] = float(max(self.max_value[fid], float(self.new_obs[i])))
                # remove cost
                self.batch_costs = self.batch_costs - self.env.func.fidelity_costs[fid]
                # query no longer being evaluated
                self.query_being_evaluated = False

            # check which model need to be updated according to the fidelities
            update_set = set(obtain_fidelities.reshape(-1))
            bias_update_set = set(bias_updates)
            self.update_model(update_set)
            self.update_model_bias(bias_update_set)
        
        # update hyperparams if needed
        if (self.hp_update_frequency is not None) & (len(self.X[self.num_of_fidelities - 1]) > 0):
            if len(self.X[self.num_of_fidelities - 1]) % self.hp_update_frequency == 0:
                self.model[self.num_of_fidelities - 1].optim_hyperparams()
                for fid2 in range(self.num_of_fidelities):
                    self.gp_hyperparams[fid2] = self.model[self.num_of_fidelities - 1].current_hyperparams()
        # update current temperature and time
        self.current_time = self.current_time + 1
    
    def build_af(self, X):
        '''
        This takes input locations, X, and returns the value of the acquisition function
        '''
        # initialize ucb
        ucb_shape = (self.num_of_fidelities, X.shape[0])
        ucb = torch.zeros(size = ucb_shape)
        # for every fidelity
        for i in range(self.num_of_fidelities):
            if self.X[i] != []:
                # if we have no data return prior
                if self.model[i].train_x == None:
                    print('x')
                mean, std = self.model[i].posterior(X)
            else:
                hypers = self.gp_hyperparams[i]
                mean_constant = hypers[3]
                constant = hypers[0]
                mean, std = torch.tensor(mean_constant), torch.tensor(constant)
            # calculate bias upper confidence bound
            if self.bias_X[i] != []:
                mean_bias, std_bias = self.bias_model[i].posterior(X)
            else:
                mean_bias, std_bias = torch.tensor(0), torch.tensor(self.bias_list[i] * self.bias_constant) / self.beta[i]
            ucb_bias = mean_bias + self.beta[i] * std_bias
            # calculate upper confidence bound
            ucb[i, :] = mean + self.beta[i] * std + ucb_bias
        
        # return acquisition function
        min_ucb, _ = torch.min(ucb, dim = 0)
        return min_ucb

class simpleUCB(mfUCB):
    def __init__(self, env, beta=None, fidelity_thresholds=None, lipschitz_constant=1, num_of_starts=75, num_of_optim_epochs=25, hp_update_frequency=None, budget=10, cost_budget=4, initial_bias=0):
        super().__init__(env, beta, fidelity_thresholds, lipschitz_constant, num_of_starts, num_of_optim_epochs, hp_update_frequency, budget, cost_budget, initial_bias)
        self.num_of_fidelities = 1

class MultiTaskUCBwILP():
    def __init__(self, 
            env, 
            budget, 
            cost_budget, 
            num_of_latents = None, 
            ranks = None, 
            hp_update_frequency = None, 
            num_of_starts = 75, 
            num_of_optim_epochs = 25, 
            beta = None, 
            local_lipschitz = True, 
            fidelity_thresholds = None, 
            increasing_thresholds = False,
            fidelity_choice = 'variance_thresholds'):
        '''
        Takes as inputs:
        env - optimization environment
        num_of_starts - number of multi-starts for optimizing the acquisition function, default is 75
        num_of_optim_epochs - number of epochs for optimizing the acquisition function, default is 150
        hp_update_frequency - how ofter should GP hyper-parameters be re-evaluated, default is None
        '''
        # initialise the environment
        self.env = env
        self.dim = env.dim

        # initialize the maximum fidelity cost
        self.cost_budget = cost_budget
    
        # initialize budgets
        self.batch_costs = 0
        self.budget = budget

        # multifidelity parameters
        self.num_of_fidelities = self.env.num_of_fidelities
        self.increasing_thresholds = increasing_thresholds
        # way of choosing fidelity
        assert fidelity_choice in ['variance_thresholds', 'information_based']
        self.fidelity_choice = fidelity_choice
        # initialize information variables if needed
        if fidelity_choice == 'information_based':
            self.min_integration = -10
            self.max_integration = 10
            self.num_of_integration_steps = 250
            self.num_of_fantasies = 100
            self.num_stability = 1e-30

        # LCM parameters
        # number of latent functions
        if num_of_latents is None:
            self.num_of_latents = self.env.num_of_fidelities * 2
        else:
            self.num_of_latents = num_of_latents
        # rank of each latent function
        if ranks is None:
            self.ranks = [self.num_of_fidelities for _ in range(self.num_of_latents)]
        else:
            self.ranks = [ranks for _ in range(self.num_of_latents)]
        
        # gp initial hyperparams
        self.set_hyperparams()

        # af optimization parameters
        if beta is None:
            self.fixed_beta = False
            self.beta = float(0.2 * self.dim * np.log(2 * (1) / (self.env.func.expected_costs[0])))
        else:
            self.fixed_beta = True
            self.beta = beta
        # initialize local penalization parameters
        self.local_lipschitz = local_lipschitz
        self.max_value = -10
        self.lipschitz_constant = 1
        self.penalization_gamma = 1
        # initialize grid to select lipschitz constant
        self.estimate_lipschitz = True
        self.num_of_grad_points = 50 * self.dim
        self.lipschitz_grid = sobol_seq.i4_sobol_generate(self.dim, self.num_of_grad_points)
        # acquisition function optimization parameters
        self.num_of_starts = num_of_starts
        self.num_of_optim_epochs = num_of_optim_epochs
        self.grid_search = self.env.func.grid_search
        self.grid_to_search = None
        # hp hyperparameters update frequency
        self.hp_update_frequency = hp_update_frequency
        self.last_n_obs = 0
        # multifidelity parameters
        if fidelity_thresholds is None:
            self.fidelity_thresholds = [0.1 for _ in range(self.num_of_fidelities)]
        else:
            self.fidelity_thresholds = fidelity_thresholds

        # define domain
        self.domain = np.zeros((self.dim,))
        self.domain = np.stack([self.domain, np.ones(self.dim, )], axis=1)

        self.initialise_stuff()
    
    def initialise_stuff(self):
        # list of queries
        self.queried_batch = [[]] * self.num_of_fidelities
        # list of queries and observations
        self.X = [[] for _ in range(self.num_of_fidelities)]
        self.Y = [[] for _ in range(self.num_of_fidelities)]
        # list of times at which we obtained the observations
        self.T = [[] for _ in range(self.num_of_fidelities)]
        # define model
        self.model = MultiTaskBoTorchGP(num_of_tasks = self.num_of_fidelities, num_of_latents = self.num_of_latents, ranks = self.ranks, lengthscale_dim = self.dim)
        # time
        self.current_time = 0
        # initialise new_obs
        self.new_obs = None

    def set_hyperparams(self, constant = None, lengthscale = None, noise = None, mean_constant = None, constraints = False):
        '''
        This function is used to set the hyper-parameters of the GP.
        INPUTS:
        constant: positive float, multiplies the RBF kernel and defines the initital variance
        lengthscale: tensor of positive floats of length (dim), defines the kernel of the rbf kernel
        noise: positive float, noise assumption
        mean_constant: float, value of prior mean
        constraints: boolean, if True, we will apply constraints from paper based on the given hyperparameters
        '''
        if constant == None:
            self.constant = 0.6
            self.length_scale = torch.tensor([0.15 for _ in range(self.dim)])
            self.noise = 1e-4
            self.mean_constant = 0
        else:
            self.constant = constant
            self.length_scale = lengthscale
            self.noise = noise
            self.mean_constant = mean_constant
        
        hypers = {}
        for latent in range(self.num_of_latents):
            parameter_key_lengthscale = 'covar_module_' + str(latent) + '.lengthscale'
            parameter_key_variance = 'task_covar_module_' + str(latent) + '.var'
            hypers[parameter_key_lengthscale] = self.length_scale.clone()
            hypers[parameter_key_variance] = torch.tensor([self.constant for _ in range(self.num_of_fidelities)])
        hypers['likelihood.noise'] = torch.tensor([self.noise for _ in range(self.num_of_fidelities)])
        hypers['mean_module.constant'] = torch.tensor(self.mean_constant)

        self.gp_hyperparams = hypers
        # check if we want our constraints based on these hyperparams
        if constraints is True:
            self.model.define_constraints(self.length_scale, self.mean_constant, self.constant)

    def run_optim(self, verbose = False):
        '''
        Runs the whole optimisation procedure, returns all queries and evaluations
        '''
        # self.env.initialise_optim()
        while self.current_time <= self.budget - 1:
            self.optim_loop()
            if verbose:
                print(f'Current time-step: {self.current_time}')
        # obtain all queries
        X, M, Y = self.env.finished_with_optim()
        # reformat all queries before returning
        num_of_remaining_queries = X.shape[0]
        for i in range(num_of_remaining_queries):
            fid = int(M[i, :])
            self.X[fid].append(list(X[i, :].reshape(1, -1)))
            self.Y[fid].append(Y[i])
            self.T[fid].append(self.current_time + 1)
        return self.X, self.Y, self.T
    
    def optim_loop(self):
        '''
        Performs a single loop of the optimisation
        '''
        # check if we need to update beta
        if self.fixed_beta == False:
            self.beta = float(0.2 * self.dim * np.log(2 * (self.current_time / self.env.func.expected_costs[0]) + 1))
        # optimise acquisition function to obtain new queries until batch is full
        new_Xs = np.empty((0, self.dim))
        new_Ms = np.empty((0, 1))
        
        self.lipschitz_batch_list = []
        # obtain current batch
        self.current_batch = self.env.query_list.copy()
        self.current_batch_fids = self.env.fidelities_list.copy()
        if self.local_lipschitz:
            for i, penalty_point_fidelity in enumerate(zip(self.current_batch, self.current_batch_fids)):
                penalty_point = penalty_point_fidelity[0].reshape(1, -1)
                fidelity = int(penalty_point_fidelity[1]) # not needed
                # re-define penalty point as tensor
                penalty_point = torch.tensor(penalty_point)
                # calculate local lipschitz constant
                local_lip_constant = self.calculate_local_lipschitz(penalty_point)
                self.lipschitz_batch_list.append(local_lip_constant)
        else:
            # otherwise simply append the global lipschitz constant for each fidelity
            batch = self.env.query_list
            batch_fids = self.env.fidelities_list
            for i, penalty_point_fidelity in enumerate(zip(batch, batch_fids)):
                fidelity = int(penalty_point_fidelity[1]) # not really needed
                self.lipschitz_batch_list.append(self.lipschitz_constant)
        
        # generate maximum samples if we are going to use information based fidelity selection
        if (self.batch_costs < self.cost_budget) & (self.fidelity_choice == 'information_based') & (self.current_time > 0):
            self.generate_max_samples()

        # fill batch
        while self.batch_costs < self.cost_budget:
#            self.lipshitz_batch_list = []
            new_X, new_M = self.optimise_af()
            new_Xs = np.concatenate((new_Xs, new_X))
            new_Ms = np.concatenate((new_Ms, new_M))
            # add new_X and new_M to current batch
            self.current_batch = np.concatenate((self.current_batch, new_X))
            self.current_batch_fids = np.concatenate((self.current_batch_fids, new_M))
            # update batch costs
            self.batch_costs = self.batch_costs + self.env.func.fidelity_costs[int(new_M)]
            # if loop is not going to break, add new lipschitz constant
            if self.batch_costs < self.cost_budget:
                # new_M and new_X define the penalty point
                fidelity = int(new_M) # not needed
                penalty_point = torch.tensor(new_X)
                # calculate local lipschitz constant
                local_lip_constant = self.calculate_local_lipschitz(penalty_point)
                self.lipschitz_batch_list.append(local_lip_constant)

        obtain_query, obtain_fidelities, self.new_obs = self.env.step(new_Xs, new_Ms)

        # update model if there are new observations
        if self.new_obs is not None:
            num_of_obs = obtain_query.shape[0]

            for i in range(num_of_obs):
                # append new observations and the time at which they were observed
                fid = int(obtain_fidelities[i])
                self.X[fid].append(list(obtain_query[i, :].reshape(-1)))
                self.Y[fid].append(self.new_obs[i])
                self.T[fid].append(self.current_time + 1)
                # redefine new maximum value
                self.max_value = float(max(self.max_value, float(self.new_obs[i])))
                # take away batch cost
                self.batch_costs = self.batch_costs - self.env.func.fidelity_costs[fid]

            # check which model need to be updated according to the fidelities
            self.update_model()
        
        # calculate total number of observations
        n_obs = 0
        for fid in range(self.num_of_fidelities):
            n_obs = n_obs + len(self.X[fid])

        if (self.hp_update_frequency is not None) & (len(self.X[-1]) > 0):
            if (n_obs >= (self.hp_update_frequency + self.last_n_obs)):
                self.last_n_obs = n_obs
                self.model.optim_hyperparams()
                self.gp_hyperparams = self.model.current_hyperparams()
                print('Hyper-params Optimized')
        
        # update current temperature and time
        self.current_time = self.current_time + 1
    
    def update_model(self):
        '''
        This function updates the GP model
        '''
        if self.new_obs is not None:
            # fit new model
            self.model.fit_model(self.X, self.Y, previous_hyperparams=self.gp_hyperparams)

    def calculate_local_lipschitz(self, pen_point):
        # if there is no model yet, use the prior
        if self.X[-1] != []:
            pass
        else:
            return self.lipschitz_constant

        with torch.no_grad():
            # first center grid around pen_point
            grid = torch.tensor(self.lipschitz_grid)
            # now scale the grid by the lengthscales
            if self.X[-1] != []:
                lengthscale = self.model.model.covar_module_0.lengthscale.detach()
            else:
                hypers = self.gp_hyperparams
                lengthscale = hypers[1]
            # multiply grid by lengthscales and center
            grid = (grid - grid[0]) * lengthscale + pen_point
            # clamp grid in the correct bounds
            bounds = torch.stack([torch.zeros(self.dim), torch.ones(self.dim)])
            for j, (lb, ub) in enumerate(zip(*bounds)):
                grid.data[..., j].clamp_(lb, ub)

        # finally estimate lipschitz constant
        grid = grid.clone().detach().double().requires_grad_(True)
        target_task_i = torch.zeros(size = (grid.shape[0], 1))
        # calculate mean of the GP
        mean, _ = self.model.posterior(grid, target_task_i)
        # calculate the gradient of the mean
        external_grad = torch.ones(self.num_of_grad_points)
        mean.backward(gradient = external_grad)
        mu_grads = grid.grad
        # find the norm of all the mean gradients
        mu_norm = torch.norm(mu_grads, dim = 1)
        # choose the largest one as our estimate
        lipschitz_constant = max(mu_norm).item()
        return lipschitz_constant
    
    def build_af(self, X):
        '''
        This takes input locations, X, and returns the UCB at the target fidelity
        '''
        # we focus on the target task
        target_task_i = torch.zeros(size = (X.shape[0], 1))
        # calculate ucb
        mean, std = self.model.posterior(X, target_task_i)
        ucb = mean + self.beta * std
        # check the batch of points being evaluated
        batch = self.current_batch.copy()
        # penalize acquisition function, loop through batch of evaluations
        for i, penalty_point in enumerate(batch):
            penalty_point = penalty_point.reshape(1, -1)
            # re-define penalty point as tensor
            penalty_point = torch.tensor(penalty_point)
            # calculate mean and variance of model at penalty point
            if self.X[-1] != []:
                mean_pp, std_pp = self.model.posterior(penalty_point, torch.tensor(0).reshape(1, 1))
            else:
                hypers = self.gp_hyperparams
                mean_constant = hypers[3]
                constant = hypers[0]
                mean_pp, std_pp = torch.tensor(mean_constant), torch.tensor(constant)
            # calculate values of r_j
            r_j = (self.max_value - mean_pp) / self.lipschitz_batch_list[i]
            denominator = r_j + self.penalization_gamma * std_pp / self.lipschitz_batch_list[i]
            # calculate norm between x and penalty point
            norm = torch.norm(penalty_point - X, dim = 1)
            # define penaliser
            penaliser = torch.min(norm / denominator, torch.tensor(1))
            # penalise ucb
            ucb = ucb * penaliser
        return ucb

    def optimise_af(self):
        '''
        This function optimizes the acquisition function, and returns the next query point
        '''
        # if time is zero, pick point at random, lowest fidelity
        if self.current_time == 0:
            new_X = np.random.uniform(size = self.dim).reshape(1, -1)
            new_M = np.array(self.num_of_fidelities - 1).reshape(1, 1)
            return new_X, new_M
        
        # if we are simply optimizing with grid search, to be used when there are constraints
        if self.grid_search is True:
            with torch.no_grad():
                # regenerate search grid every time
                self.grid_to_search = self.env.func.gen_search_grid(int(100 * self.num_of_starts))
                X = self.grid_to_search.clone()
                # check acquisition function in grid
                af = self.build_af(X)
                # choose the best point
                best_idx = torch.argmax(af)
            # return the best value in the grid
            best_input = X[best_idx, :].detach()
            best = best_input.detach().numpy().reshape(1, -1)
            new_X = best.reshape(1, -1)
            # choose fidelity level for this point
            new_M = self.generate_fidelity(new_X)

            return new_X, new_M

        # optimisation bounds
        bounds = torch.stack([torch.zeros(self.dim), torch.ones(self.dim)])
        # sobol initialization initialization, on 100 * num_of_starts, check for best 10 and optimize from there
        sobol_gen = torch.quasirandom.SobolEngine(self.dim, scramble = True)
        X = sobol_gen.draw(100 * self.num_of_starts).double()

        with torch.no_grad():
            af = self.build_af(X)
            idx_list = list(range(0, self.num_of_starts * 100))
            sorted_af_idx = [idx for _, idx in sorted(zip(af, idx_list))]
            best_idx = sorted_af_idx[-10:]

        # choose best starts for X
        X = X[best_idx, :]
        X.requires_grad = True
        # define optimiser
        optimiser = torch.optim.Adam([X], lr = 0.01)
        af = self.build_af(X)
        
        # do the optimisation
        for _ in range(self.num_of_optim_epochs):
            # set zero grad
            optimiser.zero_grad()
            # losses for optimiser
            losses = -self.build_af(X)
            loss = losses.sum()
            loss.backward()
            # optim step
            optimiser.step()

            # make sure we are still within the bounds
            for j, (lb, ub) in enumerate(zip(*bounds)):
                X.data[..., j].clamp_(lb, ub)
        
        # find the best start
        best_start = torch.argmax(-losses)

        # corresponding best input
        best_input = X[best_start, :].detach()
        best = best_input.detach().numpy().reshape(1, -1)
        new_X = best.reshape(1, -1)

        new_M = self.generate_fidelity(new_X)
        
        return new_X, new_M

    def generate_fidelity(self, new_X):
        # now choose the corresponding fidelity
        if self.fidelity_choice == 'variance_thresholds':
            for fidelity in reversed(range(self.num_of_fidelities)):
                # set fidelity
                new_M = torch.tensor(fidelity).reshape(1, 1)
                # if we reach target fidelity, break
                if fidelity == 0:
                    break
                # if there is data use posterior, else use prior
                if self.X[-1] != []:
                    _, std = self.model.posterior(new_X, new_M)
                else:
                    hypers = self.gp_hyperparams
                    mean_constant = hypers[3]
                    constant = hypers[0]
                    _, std = torch.tensor(mean_constant), torch.tensor(constant)
                # check fidelity thresholds
                if self.increasing_thresholds:
                    threshold = self.beta * std
                else:
                    threshold = std
                if threshold > self.fidelity_thresholds[fidelity]:
                    break
            
            return new_M

        elif self.fidelity_choice == 'information_based':
            if type(new_X) is not torch.Tensor:
                new_X = torch.tensor(new_X).double()
            # self.generate_max_samples()
            IGs = torch.zeros(size = torch.Size([self.num_of_fidelities]))
            for fidelity in range(0, self.num_of_fidelities):
                IGs[fidelity] = self.information_gain(new_X, fidelity) / self.env.func.expected_costs[fidelity]
            new_M = torch.argmax(IGs).reshape(1, 1)
            return new_M

    def information_gain(self, X, fidelity):
        # X is batch size X dimension
        # fidelity is an integer, since we must optimize separately across all fidelities
        current_batch = torch.tensor(self.current_batch)
        current_batch_fids = torch.tensor(self.current_batch_fids)
        # batch size
        batch_size = X.shape[0]
        fidelity_vector = torch.full(size = (batch_size,), fill_value = fidelity)
        # reshape the max samples for calculations
        f_max_samples = self.f_max_samples.clone().reshape(1, -1).expand(batch_size, self.num_of_fantasies)
        num_max_samples = self.f_max_samples.shape[0]
        # initialize standard normal distribution
        standard_normal = torch.distributions.Normal(loc = 0, scale = 1)
        # if there is no batch being evaluated, do normal sequential MF-MSE
        if self.current_batch.shape[0] == 0:
            # calculate entropy of f(X, m) | D_t ; same procedure independently of fidelity
            mu_m, sigma_m = self.model.posterior(X, fidelity_vector)
            mu_m, sigma_m = mu_m.reshape(-1, 1), sigma_m.reshape(-1, 1)
            H_1 = torch.log(sigma_m * torch.sqrt(2 * torch.pi * torch.exp(torch.tensor(1))))
            # if we are querying target fidelity, use truncated normal approximation
            if fidelity == 0:
                # calculate expected entropy of f(X, m) | f_*, D_t
                # calculate gamma
                gamma = (f_max_samples - mu_m) / (sigma_m + 1e-7)
                # cdf and pdf terms
                cdf_term = standard_normal.cdf(gamma)
                pdf_term = torch.exp(standard_normal.log_prob(gamma))
                # finally calculate entropy
                # make sure value inside log is non-zero for numerical stability using masked_fill
                inner_log = torch.sqrt(2 * torch.pi * torch.exp(torch.tensor(1))) * sigma_m * cdf_term
                log_term = torch.log(inner_log.masked_fill(inner_log <= 0, 1e-10))
                # second term
                second_term = gamma * pdf_term / (2 * cdf_term + 1e-10)
                # finally take Monte Carlo Estimate
                H_2_samples = log_term - second_term
                H_2 = H_2_samples.mean(axis = 1).reshape(-1, 1)
            
            # if we are not querying target fidelity, we need integral approximation
            elif fidelity != 0:
                # define fidelity vectors
                target_fidelity_vector = torch.zeros(size = (batch_size,))
                joint_fidelity_vector = torch.concat((fidelity_vector, target_fidelity_vector))
                # obtain joint covariance matrix
                with gpytorch.settings.fast_pred_var():
                    out = self.model.model(X.repeat(2, 1), joint_fidelity_vector)
                    covar_matrix = out.lazy_covariance_matrix
                # obtain target fidelity mean vector
                mu_0, sigma_0 = self.model.posterior(X, target_fidelity_vector)
                mu_0, sigma_0 = mu_0.reshape(-1, 1), sigma_0.reshape(-1, 1)
                # obtain smaller covariance matrix
                covar_matrix_mM = covar_matrix[:batch_size, batch_size:]
                covar_matrix_MM = covar_matrix[batch_size:, batch_size:]
                # obtain variances
                sigma_mM_sqrd = covar_matrix_mM.diag().reshape(-1, 1)
                sigma_M_sqrd = covar_matrix_MM.diag().reshape(-1, 1)
                # define s^2
                s_sqrd = sigma_M_sqrd - (sigma_mM_sqrd)**2 / (sigma_m**2 + 1e-9)
                # now we can define Psi(x)
                def Psi(f):
                    u_x = mu_0 + sigma_mM_sqrd * (f - mu_m) / (sigma_m**2 + 1e-9) # should be size: batch size x 1
                    # cdf and pdf terms
                    cdf_term = standard_normal.cdf((f_max_samples - u_x) / (torch.sqrt(s_sqrd) + 1e-9)) # should be size: batch size x samples
                    pdf_term = torch.exp(standard_normal.log_prob((f - mu_m) / (sigma_m + 1e-9)))
                    return cdf_term * pdf_term
                # and define Z, add 1e-10 for numerical stability
                inv_Z = standard_normal.cdf((f_max_samples - mu_0) / (sigma_0 + 1e-9)) * sigma_m + 1e-10
                Z =  1 / inv_Z
                # we can now estimate the one dimensional integral
                # define integral range
                f_range = torch.linspace(self.min_integration, self.max_integration, steps = self.num_of_integration_steps)
                # preallocate the space 
                integral_grid = torch.zeros(size = (self.num_of_integration_steps, batch_size, num_max_samples))
                # calculate corresponding y values
                for idx, f in enumerate(f_range):
                    z_psi = Z * Psi(f)
                    # recall that limit of x * log(x) as x-> 0 is 0; but computationally we get nans, so set it to 1 to obtain correct values
                    z_psi = z_psi.masked_fill(z_psi <= 0, 1)
                    y_vals = z_psi * torch.log(z_psi)
                    if y_vals.isnan().any():
                        print('stap')
                    integral_grid[idx, :, :] = y_vals
                # estimate integral using trapezium rule
                integral_estimates = torch.trapezoid(integral_grid, f_range, dim = 0)
                # now estimate H2 using Monte Carlo
                H_2 = - integral_estimates.mean(axis = 1).reshape(-1, 1)
            
            # finally calculate information gain by summing entropies
            H = H_1 - H_2
            if H.isnan().any():
                print('stop')
            if (H == torch.inf).any():
                print('AAAAAAA')
            return H
        # otherwise do parallel optimization by considering queries being evaluated
        else:
            # define fidelity vectors
            target_fidelity_vector = torch.zeros(size = (batch_size,))
            joint_fidelity_vector = torch.concat((fidelity_vector, target_fidelity_vector))
            # now calculate matrices
            with gpytorch.settings.fast_pred_var():
                    # mean covariance matrix of vectors being evaluated
                    self.model.model.eval()
                    dist_f_q = self.model.model(current_batch, current_batch_fids)
                    # covar_matrix_q = dist_f_q.lazy_covariance_matrix
                    covar_matrix_q = dist_f_q.covariance_matrix
                    # if torch.linalg.det(covar_matrix_q.tensor) == 0:
                    if torch.linalg.det(covar_matrix_q) == 0:
                        # if matrix is singular, add jitter
                        covar_matrix_q = covar_matrix_q + torch.diag(standard_normal.sample(torch.Size([covar_matrix_q.shape[0]])) * 1e-8)
                    # covar_matrix_q_inv = gpytorch.lazify(torch.inverse(covar_matrix_q.tensor))
                    covar_matrix_q_inv = gpytorch.lazify(torch.inverse(covar_matrix_q))
                    mu_q = dist_f_q.mean.reshape(-1, 1) # q by 1
                    # further, create samples from f_q
                    f_q_samples = dist_f_q.sample(sample_shape=torch.Size([self.num_of_fantasies])) # size self.num_of_fantasies x q
                    # Sigma_M, covariance of current fidelity and target fidelity
                    out_Sigma_M = self.model.model(X.repeat(2, 1), joint_fidelity_vector)
                    covar_matrix_M = out_Sigma_M.lazy_covariance_matrix
                    mean_sigma_M = out_Sigma_M.mean
                    mu_m = mean_sigma_M[:batch_size]
                    mu_0 = mean_sigma_M[batch_size:]
                    # Sigma_MQ, covariance of current fidelity, target fidelity and points being evaluated
                    out_Sigma_MQ = self.model.model(torch.cat((X.repeat(2, 1), current_batch)), torch.cat((joint_fidelity_vector, current_batch_fids.reshape(-1))))
                    covar_matrix_MQ = out_Sigma_MQ.lazy_covariance_matrix[:2*batch_size, 2*batch_size:]
            # calculate matrix product
            covariance_matrix_given_q = covar_matrix_M - covar_matrix_MQ.matmul(covar_matrix_q_inv).matmul(covar_matrix_MQ.t())
            # now obtain variances we need for calculations, avoid negative variances fue to numerical issues by taking maximum for small numbers
            sigma_mM_sqrd_given_q = torch.maximum(covariance_matrix_given_q[:batch_size, batch_size:].diag().reshape(-1, 1), torch.tensor(self.num_stability))
            sigma_m_sqrd_given_q = torch.maximum(covariance_matrix_given_q[:batch_size, :batch_size].diag().reshape(-1, 1), torch.tensor(self.num_stability))
            sigma_M_sqrd_given_q = torch.maximum(covariance_matrix_given_q[batch_size:, batch_size:].diag().reshape(-1, 1), torch.tensor(self.num_stability))
            if (sigma_M_sqrd_given_q < 0).any():
                print('sad')
            # calculate f_star samples 
            # first calculate mean: q x self.num_of_fantasies
            fq_minus_mu_q = f_q_samples.T - mu_q.repeat(1, self.num_of_fantasies)
            # multiple by matrices to obtain final matrix of shape 2B x self.num_of_fantasies
            mu_update = covar_matrix_MQ.matmul(covar_matrix_q_inv).matmul(fq_minus_mu_q)
            mu_matrix = mean_sigma_M.reshape(-1, 1).repeat(1, self.num_of_fantasies) # now has shape 2B x self.num_of_fantasies
            # finally obtain samples of mean of f given q
            f_given_q_samples = mu_matrix + mu_update
            # and with this, obtain f_star_samples
            f_star_samples = f_max_samples - f_given_q_samples[batch_size:, :]
            # define nu function
            def nu(f):
                # first the cdf in the numerator
                numerator_cdf_inner_numerator = f_star_samples - sigma_mM_sqrd_given_q / (sigma_m_sqrd_given_q) * f
                numerator_cdf_inner_denominator = sigma_M_sqrd_given_q - sigma_mM_sqrd_given_q**2 / (sigma_m_sqrd_given_q) + self.num_stability
                numerator_cdf_inner = numerator_cdf_inner_numerator / numerator_cdf_inner_denominator
                numerator_cdf = standard_normal.cdf(numerator_cdf_inner)

                # now the pdf in the numerator
                numerator_pdf_inner = f / (sigma_m_sqrd_given_q.sqrt())
                numerator_pdf = torch.exp(standard_normal.log_prob(numerator_pdf_inner))

                # define full numerator now
                numerator = numerator_cdf * numerator_pdf

                # denominator cdf
                denominator_cdf_inner = f_star_samples / (sigma_M_sqrd_given_q.sqrt())
                denominator_cdf = standard_normal.cdf(denominator_cdf_inner)

                # define full denominator now
                denominator = denominator_cdf * sigma_m_sqrd_given_q.sqrt() + self.num_stability

                if (denominator == 0).any() or denominator.isnan().any() or denominator.isinf().any():
                    print('yaaaa')
                
                if numerator.isnan().any() or numerator.isinf().any():
                    print('yaaaa')

                if (numerator / denominator > 1e10).any():
                    a = 0

                return numerator / (denominator)
            
            # define integration range
            f_limit_range = [10**int(i) for i in range(-6, 2)]
            with torch.no_grad():
                for f in f_limit_range:
                    latest_f = f
                    max_nu_val = torch.max(nu(f))
                    if max_nu_val < 1e-30:
                        break
            # define integral range
            left_linspace = torch.linspace(-latest_f, 0, steps = self.num_of_integration_steps)
            right_linspace = torch.linspace(0, latest_f, steps = self.num_of_integration_steps)
            f_range = torch.concat((left_linspace, right_linspace[1:]))
            # preallocate the space 
            integral_grid = torch.zeros(size = (self.num_of_integration_steps * 2 - 1, batch_size, self.num_of_fantasies))
            # calculate corresponding y values
            for idx, f in enumerate(f_range):
                nu_preprocessed = nu(f)
                # recall that limit of x * log(x) as x-> 0 is 0; but computationally we get nans, so set it to 1 to obtain correct values
                nu_processed = nu_preprocessed.masked_fill(nu_preprocessed <= 0, self.num_stability)
                y_vals = nu_processed * torch.log(nu_processed)
                if y_vals.isnan().any():
                    print('stap')
                integral_grid[idx, :, :] = y_vals
            # estimate integral using trapezium rule
            integral_estimates = torch.trapezoid(integral_grid, f_range, dim = 0)
            # now estimate H2 using Monte Carlo
            H_2 = - integral_estimates.mean(axis = 1).reshape(-1, 1)

            # calculate H_1 which is analytical
            H_1_inner = sigma_m_sqrd_given_q.sqrt() * torch.sqrt(2 * torch.pi * torch.exp(torch.tensor(1)))
            H_1_inner = H_1_inner.masked_fill(H_1_inner <= 0, self.num_stability)
            H_1 = torch.log(H_1_inner)

            # finally calculate information gain by summing entropies
            H = H_1 - H_2
            if H.isnan().any():
                print('stop')
            if (H == torch.inf).any():
                print('AAAAAAA')
            return H

    def generate_max_samples(self, fidelity = 0):
        sobol_gen = torch.quasirandom.SobolEngine(self.dim, scramble = True)
        X_test_samples = sobol_gen.draw(100 * self.num_of_starts).double()
        samples = self.model.generate_samples(X_test_samples, fidelity = fidelity, num_of_samples = self.num_of_fantasies)
        self.f_max_samples, _ = torch.max(samples, dim = 1)

class MF_MES(MultiTaskUCBwILP):
    def __init__(self, env, budget, cost_budget, num_of_latents=None, ranks=None, hp_update_frequency=None, num_of_starts=75, num_of_optim_epochs=25, beta=None, local_lipschitz=True, fidelity_thresholds=None, increasing_thresholds=False):
        super().__init__(env, budget, cost_budget, num_of_latents, ranks, hp_update_frequency, num_of_starts, num_of_optim_epochs, beta, local_lipschitz, fidelity_thresholds, increasing_thresholds)
        self.min_integration = -10
        self.max_integration = 10
        self.num_of_integration_steps = 250
        self.num_of_fantasies = 100
        self.num_stability = 1e-30
    
    def optim_loop(self):
        '''
        Performs a single loop of the optimisation
        '''
        # check if we need to update beta
        if self.fixed_beta == False:
            self.beta = float(0.2 * self.dim * np.log(2 * (self.current_time + 1) / (self.env.func.expected_costs[0])))
        # optimise acquisition function to obtain new queries until batch is full
        new_Xs = np.empty((0, self.dim))
        new_Ms = np.empty((0, 1))
        
        # obtain current batch
        self.current_batch = self.env.query_list.copy()
        self.current_batch_fids = self.env.fidelities_list.copy()

        # fill batch
        while self.batch_costs < self.cost_budget:
            new_X, new_M = self.optimise_af()
            new_Xs = np.concatenate((new_Xs, new_X))
            new_Ms = np.concatenate((new_Ms, new_M))
            # add new_X and new_M to current batch
            self.current_batch = np.concatenate((self.current_batch, new_X))
            self.current_batch_fids = np.concatenate((self.current_batch_fids, new_M))
            # update batch costs
            self.batch_costs = self.batch_costs + self.env.func.fidelity_costs[int(new_M)]

        obtain_query, obtain_fidelities, self.new_obs = self.env.step(new_Xs, new_Ms)

        # update model if there are new observations
        if self.new_obs is not None:
            num_of_obs = obtain_query.shape[0]

            for i in range(num_of_obs):
                # append new observations and the time at which they were observed
                fid = int(obtain_fidelities[i])
                self.X[fid].append(list(obtain_query[i, :].reshape(-1)))
                self.Y[fid].append(self.new_obs[i])
                self.T[fid].append(self.current_time + 1)
                # redefine new maximum value
                self.max_value = float(max(self.max_value, float(self.new_obs[i])))
                # take away batch cost
                self.batch_costs = self.batch_costs - self.env.func.fidelity_costs[fid]

            # check which model need to be updated according to the fidelities
            self.update_model()
        
        # calculate total number of observations
        n_obs = 0
        for fid in range(self.num_of_fidelities):
            n_obs = n_obs + len(self.X[fid])

        if (self.hp_update_frequency is not None) & (len(self.X[-1]) > 0):
            if (n_obs >= (self.hp_update_frequency + self.last_n_obs)):
                self.last_n_obs = n_obs
                self.model.optim_hyperparams()
                self.gp_hyperparams = self.model.current_hyperparams()
                print('Hyper-params Optimized')
        
        # update current temperature and time
        self.current_time = self.current_time + 1

    def build_af(self, X, fidelity):
        # X is batch size X dimension
        # fidelity is an integer, since we must optimize separately across all fidelities
        current_batch = torch.tensor(self.current_batch)
        current_batch_fids = torch.tensor(self.current_batch_fids)
        # batch size
        batch_size = X.shape[0]
        fidelity_vector = torch.full(size = (batch_size,), fill_value = fidelity)
        # reshape the max samples for calculations
        f_max_samples = self.f_max_samples.clone().reshape(1, -1).expand(batch_size, self.num_of_fantasies)
        num_max_samples = self.f_max_samples.shape[0]
        # initialize standard normal distribution
        standard_normal = torch.distributions.Normal(loc = 0, scale = 1)
        # if there is no batch being evaluated, do normal sequential MF-MSE
        if self.current_batch.shape[0] == 0:
            # calculate entropy of f(X, m) | D_t ; same procedure independently of fidelity
            mu_m, sigma_m = self.model.posterior(X, fidelity_vector)
            mu_m, sigma_m = mu_m.reshape(-1, 1), sigma_m.reshape(-1, 1)
            H_1 = torch.log(sigma_m * torch.sqrt(2 * torch.pi * torch.exp(torch.tensor(1))))
            # if we are querying target fidelity, use truncated normal approximation
            if fidelity == 0:
                # calculate expected entropy of f(X, m) | f_*, D_t
                # calculate gamma
                gamma = (f_max_samples - mu_m) / (sigma_m + 1e-7)
                # cdf and pdf terms
                cdf_term = standard_normal.cdf(gamma)
                pdf_term = torch.exp(standard_normal.log_prob(gamma))
                # finally calculate entropy
                # make sure value inside log is non-zero for numerical stability using masked_fill
                inner_log = torch.sqrt(2 * torch.pi * torch.exp(torch.tensor(1))) * sigma_m * cdf_term
                log_term = torch.log(inner_log.masked_fill(inner_log <= 0, 1e-10))
                # second term
                second_term = gamma * pdf_term / (2 * cdf_term + 1e-10)
                # finally take Monte Carlo Estimate
                H_2_samples = log_term - second_term
                H_2 = H_2_samples.mean(axis = 1).reshape(-1, 1)
            
            # if we are not querying target fidelity, we need integral approximation
            elif fidelity != 0:
                # define fidelity vectors
                target_fidelity_vector = torch.zeros(size = (batch_size,))
                joint_fidelity_vector = torch.concat((fidelity_vector, target_fidelity_vector))
                # obtain joint covariance matrix
                with gpytorch.settings.fast_pred_var():
                    out = self.model.model(X.repeat(2, 1), joint_fidelity_vector)
                    covar_matrix = out.lazy_covariance_matrix
                # obtain target fidelity mean vector
                mu_0, sigma_0 = self.model.posterior(X, target_fidelity_vector)
                mu_0, sigma_0 = mu_0.reshape(-1, 1), sigma_0.reshape(-1, 1)
                # obtain smaller covariance matrix
                covar_matrix_mM = covar_matrix[:batch_size, batch_size:]
                covar_matrix_MM = covar_matrix[batch_size:, batch_size:]
                # obtain variances
                sigma_mM_sqrd = covar_matrix_mM.diag().reshape(-1, 1)
                sigma_M_sqrd = covar_matrix_MM.diag().reshape(-1, 1)
                # define s^2
                s_sqrd = sigma_M_sqrd - (sigma_mM_sqrd)**2 / (sigma_m**2 + 1e-9)
                # now we can define Psi(x)
                def Psi(f):
                    u_x = mu_0 + sigma_mM_sqrd * (f - mu_m) / (sigma_m**2 + 1e-9) # should be size: batch size x 1
                    # cdf and pdf terms
                    cdf_term = standard_normal.cdf((f_max_samples - u_x) / (torch.sqrt(s_sqrd) + 1e-9)) # should be size: batch size x samples
                    pdf_term = torch.exp(standard_normal.log_prob((f - mu_m) / (sigma_m + 1e-9)))
                    return cdf_term * pdf_term
                # and define Z, add 1e-10 for numerical stability
                inv_Z = standard_normal.cdf((f_max_samples - mu_0) / (sigma_0 + 1e-9)) * sigma_m + 1e-10
                Z =  1 / inv_Z
                # we can now estimate the one dimensional integral
                # define integral range
                f_range = torch.linspace(self.min_integration, self.max_integration, steps = self.num_of_integration_steps)
                # preallocate the space 
                integral_grid = torch.zeros(size = (self.num_of_integration_steps, batch_size, num_max_samples))
                # calculate corresponding y values
                for idx, f in enumerate(f_range):
                    z_psi = Z * Psi(f)
                    # recall that limit of x * log(x) as x-> 0 is 0; but computationally we get nans, so set it to 1 to obtain correct values
                    z_psi = z_psi.masked_fill(z_psi <= 0, 1)
                    y_vals = z_psi * torch.log(z_psi)
                    if y_vals.isnan().any():
                        print('stap')
                    integral_grid[idx, :, :] = y_vals
                # estimate integral using trapezium rule
                integral_estimates = torch.trapezoid(integral_grid, f_range, dim = 0)
                # now estimate H2 using Monte Carlo
                H_2 = - integral_estimates.mean(axis = 1).reshape(-1, 1)
            
            # finally calculate information gain by summing entropies
            H = H_1 - H_2
            if H.isnan().any():
                print('stop')
            if (H == torch.inf).any():
                print('AAAAAAA')
            return H
        # otherwise do parallel optimization by considering queries being evaluated
        else:
            # define fidelity vectors
            target_fidelity_vector = torch.zeros(size = (batch_size,))
            joint_fidelity_vector = torch.concat((fidelity_vector, target_fidelity_vector))
            # now calculate matrices
            with gpytorch.settings.fast_pred_var():
                    # mean covariance matrix of vectors being evaluated
                    self.model.model.eval()
                    dist_f_q = self.model.model(current_batch, current_batch_fids)
                    # covar_matrix_q = dist_f_q.lazy_covariance_matrix
                    covar_matrix_q = dist_f_q.covariance_matrix
                    # if torch.linalg.det(covar_matrix_q.tensor) == 0:
                    if torch.linalg.det(covar_matrix_q) == 0:
                        # if matrix is singular, add jitter
                        covar_matrix_q = covar_matrix_q + torch.diag(standard_normal.sample(torch.Size([covar_matrix_q.shape[0]])) * 1e-8)
                    # covar_matrix_q_inv = gpytorch.lazify(torch.inverse(covar_matrix_q.tensor))
                    covar_matrix_q_inv = gpytorch.lazify(torch.inverse(covar_matrix_q))
                    mu_q = dist_f_q.mean.reshape(-1, 1) # q by 1
                    # further, create samples from f_q
                    f_q_samples = dist_f_q.sample(sample_shape=torch.Size([self.num_of_fantasies])) # size self.num_of_fantasies x q
                    # Sigma_M, covariance of current fidelity and target fidelity
                    out_Sigma_M = self.model.model(X.repeat(2, 1), joint_fidelity_vector)
                    covar_matrix_M = out_Sigma_M.lazy_covariance_matrix
                    mean_sigma_M = out_Sigma_M.mean
                    mu_m = mean_sigma_M[:batch_size]
                    mu_0 = mean_sigma_M[batch_size:]
                    # Sigma_MQ, covariance of current fidelity, target fidelity and points being evaluated
                    out_Sigma_MQ = self.model.model(torch.cat((X.repeat(2, 1), current_batch)), torch.cat((joint_fidelity_vector, current_batch_fids.reshape(-1))))
                    covar_matrix_MQ = out_Sigma_MQ.lazy_covariance_matrix[:2*batch_size, 2*batch_size:]
            # calculate matrix product
            covariance_matrix_given_q = covar_matrix_M - covar_matrix_MQ.matmul(covar_matrix_q_inv).matmul(covar_matrix_MQ.t())
            # now obtain variances we need for calculations, avoid negative variances fue to numerical issues by taking maximum for small numbers
            sigma_mM_sqrd_given_q = torch.maximum(covariance_matrix_given_q[:batch_size, batch_size:].diag().reshape(-1, 1), torch.tensor(self.num_stability))
            sigma_m_sqrd_given_q = torch.maximum(covariance_matrix_given_q[:batch_size, :batch_size].diag().reshape(-1, 1), torch.tensor(self.num_stability))
            sigma_M_sqrd_given_q = torch.maximum(covariance_matrix_given_q[batch_size:, batch_size:].diag().reshape(-1, 1), torch.tensor(self.num_stability))
            if (sigma_M_sqrd_given_q < 0).any():
                print('sad')
            # calculate f_star samples 
            # first calculate mean: q x self.num_of_fantasies
            fq_minus_mu_q = f_q_samples.T - mu_q.repeat(1, self.num_of_fantasies)
            # multiple by matrices to obtain final matrix of shape 2B x self.num_of_fantasies
            mu_update = covar_matrix_MQ.matmul(covar_matrix_q_inv).matmul(fq_minus_mu_q)
            mu_matrix = mean_sigma_M.reshape(-1, 1).repeat(1, self.num_of_fantasies) # now has shape 2B x self.num_of_fantasies
            # finally obtain samples of mean of f given q
            f_given_q_samples = mu_matrix + mu_update
            # and with this, obtain f_star_samples
            f_star_samples = f_max_samples - f_given_q_samples[batch_size:, :]
            # define nu function
            def nu(f):
                # first the cdf in the numerator
                numerator_cdf_inner_numerator = f_star_samples - sigma_mM_sqrd_given_q / (sigma_m_sqrd_given_q) * f
                numerator_cdf_inner_denominator = sigma_M_sqrd_given_q - sigma_mM_sqrd_given_q**2 / (sigma_m_sqrd_given_q) + self.num_stability
                numerator_cdf_inner = numerator_cdf_inner_numerator / numerator_cdf_inner_denominator
                numerator_cdf = standard_normal.cdf(numerator_cdf_inner)

                # now the pdf in the numerator
                numerator_pdf_inner = f / (sigma_m_sqrd_given_q.sqrt())
                numerator_pdf = torch.exp(standard_normal.log_prob(numerator_pdf_inner))

                # define full numerator now
                numerator = numerator_cdf * numerator_pdf

                # denominator cdf
                denominator_cdf_inner = f_star_samples / (sigma_M_sqrd_given_q.sqrt())
                denominator_cdf = standard_normal.cdf(denominator_cdf_inner)

                # define full denominator now
                denominator = denominator_cdf * sigma_m_sqrd_given_q.sqrt() + self.num_stability

                if (denominator == 0).any() or denominator.isnan().any() or denominator.isinf().any():
                    print('yaaaa')
                
                if numerator.isnan().any() or numerator.isinf().any():
                    print('yaaaa')

                if (numerator / denominator > 1e10).any():
                    a = 0

                return numerator / (denominator)
            
            # define integration range
            f_limit_range = [10**int(i) for i in range(-6, 2)]
            with torch.no_grad():
                for f in f_limit_range:
                    latest_f = f
                    max_nu_val = torch.max(nu(f))
                    if max_nu_val < 1e-30:
                        break
            # define integral range
            left_linspace = torch.linspace(-latest_f, 0, steps = self.num_of_integration_steps)
            right_linspace = torch.linspace(0, latest_f, steps = self.num_of_integration_steps)
            f_range = torch.concat((left_linspace, right_linspace[1:]))
            # preallocate the space 
            integral_grid = torch.zeros(size = (self.num_of_integration_steps * 2 - 1, batch_size, self.num_of_fantasies))
            # calculate corresponding y values
            for idx, f in enumerate(f_range):
                nu_preprocessed = nu(f)
                # recall that limit of x * log(x) as x-> 0 is 0; but computationally we get nans, so set it to 1 to obtain correct values
                nu_processed = nu_preprocessed.masked_fill(nu_preprocessed <= 0, self.num_stability)
                y_vals = nu_processed * torch.log(nu_processed)
                if y_vals.isnan().any():
                    print('stap')
                integral_grid[idx, :, :] = y_vals
            # estimate integral using trapezium rule
            integral_estimates = torch.trapezoid(integral_grid, f_range, dim = 0)
            # now estimate H2 using Monte Carlo
            H_2 = - integral_estimates.mean(axis = 1).reshape(-1, 1)

            # calculate H_1 which is analytical
            H_1_inner = sigma_m_sqrd_given_q.sqrt() * torch.sqrt(2 * torch.pi * torch.exp(torch.tensor(1)))
            H_1_inner = H_1_inner.masked_fill(H_1_inner <= 0, self.num_stability)
            H_1 = torch.log(H_1_inner)

            # finally calculate information gain by summing entropies
            H = H_1 - H_2
            if H.isnan().any():
                print('stop')
            if (H == torch.inf).any():
                print('AAAAAAA')
            return H
            
    def optimise_af(self):
        '''
        This function optimizes the acquisition function, and returns the next query point
        '''
        # if time is zero, pick point at random, lowest fidelity
        if self.current_time == 0:
            new_X = np.random.uniform(size = self.dim).reshape(1, -1)
            new_M = np.array(self.num_of_fidelities - 1).reshape(1, 1)
            return new_X, new_M
        
        # if we are going to optimize, generate samples
        self.generate_max_samples()
        
        # if we are simply optimizing with grid search, to be used when there are constraints
        if self.grid_search is True:
            best_outputs = []
            best_inputs = []
            for fidelity in range(self.num_of_fidelities):
                with torch.no_grad():
                    # regenerate search grid every time
                    self.grid_to_search = self.env.func.gen_search_grid(int(100 * self.num_of_starts))
                    X = self.grid_to_search.clone()
                    # check acquisition function in grid
                    af = self.build_af(X, int(fidelity))
                    # choose the best point
                    best_idx = torch.argmax(af)
                    # choose best outputs as well
                    best_output = torch.max(af)
                # return the best value in the grid
                best_input = X[best_idx, :].detach()
                best = best_input.detach().numpy().reshape(1, -1)
                new_X = best.reshape(1, -1)
                # best output divided by cost
                best_outputs.append(best_output / self.env.func.expected_costs[fidelity])
                best_inputs.append(new_X)
            
            new_M = np.argmax(best_output)
            new_X = best_inputs[new_M]
            new_M = np.array(new_M).reshape(-1, 1)

            return new_X, new_M

        best_outputs = []
        best_inputs = []
        for fidelity in range(self.num_of_fidelities):
            # optimisation bounds
            bounds = torch.stack([torch.zeros(self.dim), torch.ones(self.dim)])
            # sobol initialization initialization, on 100 * num_of_starts, check for best 10 and optimize from there
            sobol_gen = torch.quasirandom.SobolEngine(self.dim, scramble = True)
            X = sobol_gen.draw(50 * self.num_of_starts).double()

            with torch.no_grad():
                af = self.build_af(X, int(fidelity))
                idx_list = list(range(0, self.num_of_starts * 50))
                sorted_af_idx = [idx for _, idx in sorted(zip(af, idx_list))]
                best_idx = sorted_af_idx[-10:]

            # choose best starts for X
            X = X[best_idx, :]
            X.requires_grad = True
            # define optimiser
            optimiser = torch.optim.Adam([X], lr = 0.01)
            af = self.build_af(X, fidelity = fidelity)
            
            # do the optimisation
            for _ in range(self.num_of_optim_epochs):
                # set zero grad
                optimiser.zero_grad()
                # losses for optimiser
                losses = -self.build_af(X, fidelity = fidelity)
                loss = losses.sum()
                loss.backward()
                # optim step
                optimiser.step()

                # make sure we are still within the bounds
                for j, (lb, ub) in enumerate(zip(*bounds)):
                    X.data[..., j].clamp_(lb, ub)
                
            
            # find the best start
            best_start = torch.argmax(-losses)

            # corresponding best input
            best_input = X[best_start, :].detach()
            best = best_input.detach().numpy().reshape(1, -1)
            new_X = best.reshape(1, -1)
            best_inputs.append(new_X)
            best_outputs.append(torch.max(-losses).detach().numpy() / self.env.func.expected_costs[fidelity])

        new_M = np.argmax(best_outputs)
        new_X = best_inputs[new_M]
        new_M = np.array(new_M).reshape(-1, 1)

        return new_X, new_M
    
    def generate_max_samples(self, fidelity = 0):
        sobol_gen = torch.quasirandom.SobolEngine(self.dim, scramble = True)
        X_test_samples = sobol_gen.draw(100 * self.num_of_starts).double()
        samples = self.model.generate_samples(X_test_samples, fidelity = fidelity, num_of_samples = self.num_of_fantasies)
        self.f_max_samples, _ = torch.max(samples, dim = 1)

class MF_TuRBO(MultiTaskUCBwILP):
    def __init__(self, 
            env, 
            budget, 
            cost_budget, 
            num_of_latents=None, 
            ranks=None, 
            hp_update_frequency=None, 
            num_of_starts=75, 
            num_of_optim_epochs=25,
            beta=None, 
            local_lipschitz=True, 
            fidelity_thresholds=None, 
            increasing_thresholds=False,
            fidelity_choice = 'variance_thresholds'):
        
        super().__init__(env, budget, cost_budget, num_of_latents, ranks, hp_update_frequency, num_of_starts, num_of_optim_epochs, beta, local_lipschitz, fidelity_thresholds, increasing_thresholds)
        # way of choosing fidelity
        assert fidelity_choice in ['variance_thresholds', 'information_based']
        self.fidelity_choice = fidelity_choice
        # initialize state
        self.state = TurboState(dim = self.dim)

        # initialize information variables
        self.min_integration = -10
        self.max_integration = 10
        self.num_of_integration_steps = 250
        self.num_of_fantasies = 100
        self.num_stability = 1e-30
    
    def optimise_af(self):
        '''
        This function optimizes the acquisition function, and returns the next query point
        '''
        # if time is zero, pick point at random, lowest fidelity
        if self.current_time == 0:
            new_X = np.random.uniform(size = self.dim).reshape(1, -1)
            new_M = np.array(self.num_of_fidelities - 1).reshape(1, 1)
            return new_X, new_M
        
        else:
            new_X = self.generate_batch(state = self.state)
            new_M = self.generate_fidelity(new_X)
            return new_X, new_M
    
    def update_state(self, state, Y_next):
        # count if a success, otherwise a failure
        if max(Y_next) > state.best_value + 1e-3 * math.fabs(state.best_value):
            state.success_counter += 1
            state.failure_counter = 0
        else:
            state.success_counter = 0
            state.failure_counter += 1
        # check if we need to expand or shrink the trust region
        if state.success_counter == state.success_tolerance:  # Expand trust region
            state.length = min(2.0 * state.length, state.length_max)
            state.success_counter = 0
        elif state.failure_counter == state.failure_tolerance:  # Shrink trust region
            state.length /= 2.0
            state.failure_counter = 0
        # set the best value if we got a new observation
        state.best_value = max(state.best_value, max(Y_next).item())
        if state.length < state.length_min:
            state.restart_triggered = True
        return state
    
    def generate_batch(
        self,
        state = None,
        model = None,  # GP model
        X = None,  # Evaluated points on the domain [0, 1]^d
        Y = None,  # Function values
        batch_size = 1, # fix batch size to 1
        n_candidates=None,  # Number of candidates for Thompson sampling
        num_restarts=10,
        raw_samples=512,
        acqf="ts",  # "ei" or "ts"
    ):
        # define variables
        model = self.model.model
        X = torch.tensor(self.X[0]).double()
        Y = torch.tensor(np.array(self.Y[0])).double()

        assert acqf in ("ts")
        if len(X) != 0:
            assert X.min() >= 0.0 and X.max() <= 1.0 and torch.all(torch.isfinite(Y))
        if n_candidates is None:
            n_candidates = min(5000, max(2000, 200 * X.shape[-1]))

        # Scale the trust region to be proportional to the length-scales
        if len(X) == 0:
            x_center = torch.full(size = (1, self.dim), fill_value = 0.5)
        else:
            x_center = X[Y.argmax(), :].clone()
        weights = self.model.model.covar_module_0.lengthscale.squeeze().detach()
        weights = weights / weights.mean()
        weights = weights / torch.prod(weights.pow(1.0 / len(weights)))
        tr_lb = torch.clamp(x_center - weights * state.length / 2.0, 0.0, 1.0)
        tr_ub = torch.clamp(x_center + weights * state.length / 2.0, 0.0, 1.0)
        # we focus only on thompson sampling as an acquisition function
        if acqf == "ts":
            dim = self.dim
            sobol = torch.quasirandom.SobolEngine(dim, scramble=True)
            pert = sobol.draw(n_candidates)
            pert = tr_lb + (tr_ub - tr_lb) * pert

            # Create a perturbation mask
            prob_perturb = min(20.0 / dim, 1.0)
            mask = (
                torch.rand(n_candidates, dim)
                <= prob_perturb
            )
            ind = torch.where(mask.sum(dim=1) == 0)[0]
            mask[ind, torch.randint(0, dim - 1, size=(len(ind),))] = 1

            # Create candidate points from the perturbations and the mask        
            X_cand = x_center.expand(n_candidates, dim).clone()
            # X_cand[mask]= pert[mask].double() 
            X_cand = X_cand.double()
            X_cand[mask] = pert[mask]

            # Sample on the candidate points
            # set model to evaluation mode
            model.eval()
            # target fidelity vector
            target_fidelity_vec = torch.zeros(size = torch.Size([n_candidates, 1])).double()
            posterior_distribution = model(X_cand.double(), target_fidelity_vec)
            with torch.no_grad():  # We don't need gradients when using TS
                posterior_sample = posterior_distribution.sample()
                X_next_idx = torch.argmax(posterior_sample)
                X_next = X_cand[X_next_idx]

        return X_next.detach().numpy().reshape(1, -1)
    
    def optim_loop(self):
        '''
        Performs a single loop of the optimisation
        '''
        # optimise acquisition function to obtain new queries until batch is full
        new_Xs = np.empty((0, self.dim))
        new_Ms = np.empty((0, 1))
        
        # obtain current batch
        self.current_batch = self.env.query_list.copy()
        self.current_batch_fids = self.env.fidelities_list.copy()
        # generate maximum samples if we are going to use information based fidelity selection
        if (self.batch_costs < self.cost_budget) & (self.fidelity_choice == 'information_based') & (self.current_time > 0):
            self.generate_max_samples()
        # fill batch
        while self.batch_costs < self.cost_budget:
#            self.lipshitz_batch_list = []
            new_X, new_M = self.optimise_af()
            new_Xs = np.concatenate((new_Xs, new_X))
            new_Ms = np.concatenate((new_Ms, new_M))
            # add new_X and new_M to current batch
            self.current_batch = np.concatenate((self.current_batch, new_X))
            self.current_batch_fids = np.concatenate((self.current_batch_fids, new_M))
            # update batch costs
            self.batch_costs = self.batch_costs + self.env.func.fidelity_costs[int(new_M)]

        obtain_query, obtain_fidelities, self.new_obs = self.env.step(new_Xs, new_Ms)

        # update model if there are new observations
        if self.new_obs is not None:
            num_of_obs = obtain_query.shape[0]

            for i in range(num_of_obs):
                # append new observations and the time at which they were observed
                fid = int(obtain_fidelities[i])
                self.X[fid].append(list(obtain_query[i, :].reshape(-1)))
                self.Y[fid].append(self.new_obs[i])
                self.T[fid].append(self.current_time + 1)
                # take away batch cost
                self.batch_costs = self.batch_costs - self.env.func.fidelity_costs[fid]
                # update TuRBO state
                if fid == 0:
                    self.state = self.update_state(self.state, self.new_obs[i])

            # check which model need to be updated according to the fidelities
            self.update_model()
        
        # calculate total number of observations
        n_obs = 0
        for fid in range(self.num_of_fidelities):
            n_obs = n_obs + len(self.X[fid])

        if (self.hp_update_frequency is not None) & (len(self.X[-1]) > 0):
            if (n_obs >= (self.hp_update_frequency + self.last_n_obs)):
                self.last_n_obs = n_obs
                self.model.optim_hyperparams()
                self.gp_hyperparams = self.model.current_hyperparams()
                print('Hyper-params Optimized')
        
        # update current temperature and time
        self.current_time = self.current_time + 1

    def generate_fidelity(self, new_X):
        # now choose the corresponding fidelity
        if self.fidelity_choice == 'variance_thresholds':
            for fidelity in reversed(range(self.num_of_fidelities)):
                # set fidelity
                new_M = torch.tensor(fidelity).reshape(1, 1)
                # if we reach target fidelity, break
                if fidelity == 0:
                    break
                # if there is data use posterior, else use prior
                if self.X[-1] != []:
                    _, std = self.model.posterior(new_X, new_M)
                else:
                    hypers = self.gp_hyperparams
                    mean_constant = hypers[3]
                    constant = hypers[0]
                    _, std = torch.tensor(mean_constant), torch.tensor(constant)
                # check fidelity thresholds
                if self.increasing_thresholds:
                    threshold = self.beta * std
                else:
                    threshold = std
                if threshold > self.fidelity_thresholds[fidelity]:
                    break
            
            return new_M

        elif self.fidelity_choice == 'information_based':
            if type(new_X) is not torch.Tensor:
                new_X = torch.tensor(new_X).double()
            # self.generate_max_samples()
            IGs = torch.zeros(size = torch.Size([self.num_of_fidelities]))
            for fidelity in range(0, self.num_of_fidelities):
                IGs[fidelity] = self.information_gain(new_X, fidelity) / self.env.func.expected_costs[fidelity]
            new_M = torch.argmax(IGs).reshape(1, 1)
            return new_M
    
    def information_gain(self, X, fidelity):
        # X is batch size X dimension
        # fidelity is an integer, since we must optimize separately across all fidelities
        current_batch = torch.tensor(self.current_batch)
        current_batch_fids = torch.tensor(self.current_batch_fids)
        # batch size
        batch_size = X.shape[0]
        fidelity_vector = torch.full(size = (batch_size,), fill_value = fidelity)
        # reshape the max samples for calculations
        f_max_samples = self.f_max_samples.clone().reshape(1, -1).expand(batch_size, self.num_of_fantasies)
        num_max_samples = self.f_max_samples.shape[0]
        # initialize standard normal distribution
        standard_normal = torch.distributions.Normal(loc = 0, scale = 1)
        # if there is no batch being evaluated, do normal sequential MF-MSE
        if self.current_batch.shape[0] == 0:
            # calculate entropy of f(X, m) | D_t ; same procedure independently of fidelity
            mu_m, sigma_m = self.model.posterior(X, fidelity_vector)
            mu_m, sigma_m = mu_m.reshape(-1, 1), sigma_m.reshape(-1, 1)
            H_1 = torch.log(sigma_m * torch.sqrt(2 * torch.pi * torch.exp(torch.tensor(1))))
            # if we are querying target fidelity, use truncated normal approximation
            if fidelity == 0:
                # calculate expected entropy of f(X, m) | f_*, D_t
                # calculate gamma
                gamma = (f_max_samples - mu_m) / (sigma_m + 1e-7)
                # cdf and pdf terms
                cdf_term = standard_normal.cdf(gamma)
                pdf_term = torch.exp(standard_normal.log_prob(gamma))
                # finally calculate entropy
                # make sure value inside log is non-zero for numerical stability using masked_fill
                inner_log = torch.sqrt(2 * torch.pi * torch.exp(torch.tensor(1))) * sigma_m * cdf_term
                log_term = torch.log(inner_log.masked_fill(inner_log <= 0, 1e-10))
                # second term
                second_term = gamma * pdf_term / (2 * cdf_term + 1e-10)
                # finally take Monte Carlo Estimate
                H_2_samples = log_term - second_term
                H_2 = H_2_samples.mean(axis = 1).reshape(-1, 1)
            
            # if we are not querying target fidelity, we need integral approximation
            elif fidelity != 0:
                # define fidelity vectors
                target_fidelity_vector = torch.zeros(size = (batch_size,))
                joint_fidelity_vector = torch.concat((fidelity_vector, target_fidelity_vector))
                # obtain joint covariance matrix
                with gpytorch.settings.fast_pred_var():
                    out = self.model.model(X.repeat(2, 1), joint_fidelity_vector)
                    covar_matrix = out.lazy_covariance_matrix
                # obtain target fidelity mean vector
                mu_0, sigma_0 = self.model.posterior(X, target_fidelity_vector)
                mu_0, sigma_0 = mu_0.reshape(-1, 1), sigma_0.reshape(-1, 1)
                # obtain smaller covariance matrix
                covar_matrix_mM = covar_matrix[:batch_size, batch_size:]
                covar_matrix_MM = covar_matrix[batch_size:, batch_size:]
                # obtain variances
                sigma_mM_sqrd = covar_matrix_mM.diag().reshape(-1, 1)
                sigma_M_sqrd = covar_matrix_MM.diag().reshape(-1, 1)
                # define s^2
                s_sqrd = sigma_M_sqrd - (sigma_mM_sqrd)**2 / (sigma_m**2 + 1e-9)
                # now we can define Psi(x)
                def Psi(f):
                    u_x = mu_0 + sigma_mM_sqrd * (f - mu_m) / (sigma_m**2 + 1e-9) # should be size: batch size x 1
                    # cdf and pdf terms
                    cdf_term = standard_normal.cdf((f_max_samples - u_x) / (torch.sqrt(s_sqrd) + 1e-9)) # should be size: batch size x samples
                    pdf_term = torch.exp(standard_normal.log_prob((f - mu_m) / (sigma_m + 1e-9)))
                    return cdf_term * pdf_term
                # and define Z, add 1e-10 for numerical stability
                inv_Z = standard_normal.cdf((f_max_samples - mu_0) / (sigma_0 + 1e-9)) * sigma_m + 1e-10
                Z =  1 / inv_Z
                # we can now estimate the one dimensional integral
                # define integral range
                f_range = torch.linspace(self.min_integration, self.max_integration, steps = self.num_of_integration_steps)
                # preallocate the space 
                integral_grid = torch.zeros(size = (self.num_of_integration_steps, batch_size, num_max_samples))
                # calculate corresponding y values
                for idx, f in enumerate(f_range):
                    z_psi = Z * Psi(f)
                    # recall that limit of x * log(x) as x-> 0 is 0; but computationally we get nans, so set it to 1 to obtain correct values
                    z_psi = z_psi.masked_fill(z_psi <= 0, 1)
                    y_vals = z_psi * torch.log(z_psi)
                    if y_vals.isnan().any():
                        print('stap')
                    integral_grid[idx, :, :] = y_vals
                # estimate integral using trapezium rule
                integral_estimates = torch.trapezoid(integral_grid, f_range, dim = 0)
                # now estimate H2 using Monte Carlo
                H_2 = - integral_estimates.mean(axis = 1).reshape(-1, 1)
            
            # finally calculate information gain by summing entropies
            H = H_1 - H_2
            if H.isnan().any():
                print('stop')
            if (H == torch.inf).any():
                print('AAAAAAA')
            return H
        # otherwise do parallel optimization by considering queries being evaluated
        else:
            # define fidelity vectors
            target_fidelity_vector = torch.zeros(size = (batch_size,))
            joint_fidelity_vector = torch.concat((fidelity_vector, target_fidelity_vector))
            # now calculate matrices
            with gpytorch.settings.fast_pred_var():
                    # mean covariance matrix of vectors being evaluated
                    self.model.model.eval()
                    dist_f_q = self.model.model(current_batch, current_batch_fids)
                    # covar_matrix_q = dist_f_q.lazy_covariance_matrix
                    covar_matrix_q = dist_f_q.covariance_matrix
                    # if torch.linalg.det(covar_matrix_q.tensor) == 0:
                    if torch.linalg.det(covar_matrix_q) == 0:
                        # if matrix is singular, add jitter
                        covar_matrix_q = covar_matrix_q + torch.diag(standard_normal.sample(torch.Size([covar_matrix_q.shape[0]])) * 1e-8)
                    # covar_matrix_q_inv = gpytorch.lazify(torch.inverse(covar_matrix_q.tensor))
                    covar_matrix_q_inv = gpytorch.lazify(torch.inverse(covar_matrix_q))
                    mu_q = dist_f_q.mean.reshape(-1, 1) # q by 1
                    # further, create samples from f_q
                    f_q_samples = dist_f_q.sample(sample_shape=torch.Size([self.num_of_fantasies])) # size self.num_of_fantasies x q
                    # Sigma_M, covariance of current fidelity and target fidelity
                    out_Sigma_M = self.model.model(X.repeat(2, 1), joint_fidelity_vector)
                    covar_matrix_M = out_Sigma_M.lazy_covariance_matrix
                    mean_sigma_M = out_Sigma_M.mean
                    mu_m = mean_sigma_M[:batch_size]
                    mu_0 = mean_sigma_M[batch_size:]
                    # Sigma_MQ, covariance of current fidelity, target fidelity and points being evaluated
                    out_Sigma_MQ = self.model.model(torch.cat((X.repeat(2, 1), current_batch)), torch.cat((joint_fidelity_vector, current_batch_fids.reshape(-1))))
                    covar_matrix_MQ = out_Sigma_MQ.lazy_covariance_matrix[:2*batch_size, 2*batch_size:]
            # calculate matrix product
            covariance_matrix_given_q = covar_matrix_M - covar_matrix_MQ.matmul(covar_matrix_q_inv).matmul(covar_matrix_MQ.t())
            # now obtain variances we need for calculations, avoid negative variances fue to numerical issues by taking maximum for small numbers
            sigma_mM_sqrd_given_q = torch.maximum(covariance_matrix_given_q[:batch_size, batch_size:].diag().reshape(-1, 1), torch.tensor(self.num_stability))
            sigma_m_sqrd_given_q = torch.maximum(covariance_matrix_given_q[:batch_size, :batch_size].diag().reshape(-1, 1), torch.tensor(self.num_stability))
            sigma_M_sqrd_given_q = torch.maximum(covariance_matrix_given_q[batch_size:, batch_size:].diag().reshape(-1, 1), torch.tensor(self.num_stability))
            if (sigma_M_sqrd_given_q < 0).any():
                print('sad')
            # calculate f_star samples 
            # first calculate mean: q x self.num_of_fantasies
            fq_minus_mu_q = f_q_samples.T - mu_q.repeat(1, self.num_of_fantasies)
            # multiple by matrices to obtain final matrix of shape 2B x self.num_of_fantasies
            mu_update = covar_matrix_MQ.matmul(covar_matrix_q_inv).matmul(fq_minus_mu_q)
            mu_matrix = mean_sigma_M.reshape(-1, 1).repeat(1, self.num_of_fantasies) # now has shape 2B x self.num_of_fantasies
            # finally obtain samples of mean of f given q
            f_given_q_samples = mu_matrix + mu_update
            # and with this, obtain f_star_samples
            f_star_samples = f_max_samples - f_given_q_samples[batch_size:, :]
            # define nu function
            def nu(f):
                # first the cdf in the numerator
                numerator_cdf_inner_numerator = f_star_samples - sigma_mM_sqrd_given_q / (sigma_m_sqrd_given_q) * f
                numerator_cdf_inner_denominator = sigma_M_sqrd_given_q - sigma_mM_sqrd_given_q**2 / (sigma_m_sqrd_given_q) + self.num_stability
                numerator_cdf_inner = numerator_cdf_inner_numerator / numerator_cdf_inner_denominator
                numerator_cdf = standard_normal.cdf(numerator_cdf_inner)

                # now the pdf in the numerator
                numerator_pdf_inner = f / (sigma_m_sqrd_given_q.sqrt())
                numerator_pdf = torch.exp(standard_normal.log_prob(numerator_pdf_inner))

                # define full numerator now
                numerator = numerator_cdf * numerator_pdf

                # denominator cdf
                denominator_cdf_inner = f_star_samples / (sigma_M_sqrd_given_q.sqrt())
                denominator_cdf = standard_normal.cdf(denominator_cdf_inner)

                # define full denominator now
                denominator = denominator_cdf * sigma_m_sqrd_given_q.sqrt() + self.num_stability

                if (denominator == 0).any() or denominator.isnan().any() or denominator.isinf().any():
                    print('yaaaa')
                
                if numerator.isnan().any() or numerator.isinf().any():
                    print('yaaaa')

                if (numerator / denominator > 1e10).any():
                    a = 0

                return numerator / (denominator)
            
            # define integration range
            f_limit_range = [10**int(i) for i in range(-6, 2)]
            with torch.no_grad():
                for f in f_limit_range:
                    latest_f = f
                    max_nu_val = torch.max(nu(f))
                    if max_nu_val < 1e-30:
                        break
            # define integral range
            left_linspace = torch.linspace(-latest_f, 0, steps = self.num_of_integration_steps)
            right_linspace = torch.linspace(0, latest_f, steps = self.num_of_integration_steps)
            f_range = torch.concat((left_linspace, right_linspace[1:]))
            # preallocate the space 
            integral_grid = torch.zeros(size = (self.num_of_integration_steps * 2 - 1, batch_size, self.num_of_fantasies))
            # calculate corresponding y values
            for idx, f in enumerate(f_range):
                nu_preprocessed = nu(f)
                # recall that limit of x * log(x) as x-> 0 is 0; but computationally we get nans, so set it to 1 to obtain correct values
                nu_processed = nu_preprocessed.masked_fill(nu_preprocessed <= 0, self.num_stability)
                y_vals = nu_processed * torch.log(nu_processed)
                if y_vals.isnan().any():
                    print('stap')
                integral_grid[idx, :, :] = y_vals
            # estimate integral using trapezium rule
            integral_estimates = torch.trapezoid(integral_grid, f_range, dim = 0)
            # now estimate H2 using Monte Carlo
            H_2 = - integral_estimates.mean(axis = 1).reshape(-1, 1)

            # calculate H_1 which is analytical
            H_1_inner = sigma_m_sqrd_given_q.sqrt() * torch.sqrt(2 * torch.pi * torch.exp(torch.tensor(1)))
            H_1_inner = H_1_inner.masked_fill(H_1_inner <= 0, self.num_stability)
            H_1 = torch.log(H_1_inner)

            # finally calculate information gain by summing entropies
            H = H_1 - H_2
            if H.isnan().any():
                print('stop')
            if (H == torch.inf).any():
                print('AAAAAAA')
            return H
    
    def generate_max_samples(self, fidelity = 0):
        sobol_gen = torch.quasirandom.SobolEngine(self.dim, scramble = True)
        X_test_samples = sobol_gen.draw(100 * self.num_of_starts).double()
        samples = self.model.generate_samples(X_test_samples, fidelity = fidelity, num_of_samples = self.num_of_fantasies)
        self.f_max_samples, _ = torch.max(samples, dim = 1)

class TuRBO(MF_TuRBO):
    def __init__(self, env, budget, cost_budget, num_of_latents=None, ranks=None, hp_update_frequency=None, num_of_starts=75, num_of_optim_epochs=25, beta=None, local_lipschitz=True, fidelity_thresholds=None, increasing_thresholds=False, fidelity_choice='variance_thresholds'):
        super().__init__(env, budget, cost_budget, num_of_latents, ranks, hp_update_frequency, num_of_starts, num_of_optim_epochs, beta, local_lipschitz, fidelity_thresholds, increasing_thresholds, fidelity_choice)
        self.num_of_latents = 1
        self.ranks = [1]
        # define model
        self.model = MultiTaskBoTorchGP(num_of_tasks = self.num_of_fidelities, num_of_latents = self.num_of_latents, ranks = self.ranks, lengthscale_dim = self.dim)
        self.num_of_fidelities = 1

    def generate_fidelity(self, new_X):
        # independently of the input, we return fidelity 0
        new_M = torch.tensor(0).reshape(1, 1)
        return new_M

# we define a dataclass for our state
@dataclass
class TurboState:
    dim: int # dimension of the problem, aka input dimension
    batch_size: int = 1 # we could do batch optimization, but the capstone only does one query at a time
    length: float = 0.8 # the length of the current trust region
    length_min: float = 0.5 ** 7 # minimum length for the trust region
    length_max: float = 1.6 # maximum length for the trust region
    failure_counter: int = 0 # initialize counter of the number of failures to improve on the best observation
    failure_tolerance: int = float("nan")  # Note: Post-initialized
    success_counter: int = 0 # initialize counter of the number of success to improve on the best observation
    success_tolerance: int = 10  # Note: The original paper uses 3, this is the number of successes in a row needed to expand the region
    best_value: float = -float("inf") # best value so far, initialized to be the infimum
    restart_triggered: bool = False 

    def __post_init__(self):
        self.failure_tolerance = math.ceil(
            max([4.0 / self.batch_size, float(self.dim) / self.batch_size]) # number of failures needed in a row to shrink the trust region
        )