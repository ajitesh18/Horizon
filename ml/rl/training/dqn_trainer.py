#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.

import logging
from copy import deepcopy
from typing import Dict, Optional

import torch
import torch.nn.functional as F
from ml.rl.models.convolutional_network import ConvolutionalNetwork
from ml.rl.models.dueling_q_network import DuelingQNetwork
from ml.rl.models.fully_connected_network import FullyConnectedNetwork
from ml.rl.preprocessing.normalization import (
    NormalizationParameters,
    get_num_output_features,
)
from ml.rl.thrift.core.ttypes import (
    AdditionalFeatureTypes,
    DiscreteActionModelParameters,
)
from ml.rl.training.dqn_predictor import DQNPredictor
from ml.rl.training.evaluator import BatchStatsForCPE, Evaluator
from ml.rl.training.rl_trainer_pytorch import (
    DEFAULT_ADDITIONAL_FEATURE_TYPES,
    RLTrainer,
)
from ml.rl.training.training_data_page import TrainingDataPage


logger = logging.getLogger(__name__)


class DQNTrainer(RLTrainer):
    def __init__(
        self,
        parameters: DiscreteActionModelParameters,
        state_normalization_parameters: Dict[int, NormalizationParameters],
        use_gpu: bool = False,
        additional_feature_types: AdditionalFeatureTypes = DEFAULT_ADDITIONAL_FEATURE_TYPES,
        metrics_to_score=None,
        gradient_handler=None,
        use_all_avail_gpus: bool = False,
    ) -> None:

        self.double_q_learning = parameters.rainbow.double_q_learning
        self.warm_start_model_path = parameters.training.warm_start_model_path
        self.minibatch_size = parameters.training.minibatch_size
        self._actions = parameters.actions if parameters.actions is not None else []

        if parameters.training.cnn_parameters is None:
            self.state_normalization_parameters: Optional[
                Dict[int, NormalizationParameters]
            ] = state_normalization_parameters
            self.num_features = get_num_output_features(state_normalization_parameters)
            logger.info("Number of state features: " + str(self.num_features))
            parameters.training.layers[0] = self.num_features
        else:
            self.state_normalization_parameters = None
        parameters.training.layers[-1] = self.num_actions

        RLTrainer.__init__(
            self,
            parameters,
            use_gpu,
            additional_feature_types,
            metrics_to_score,
            gradient_handler,
        )

        self.reward_boosts = torch.zeros([1, len(self._actions)]).type(self.dtype)
        if parameters.rl.reward_boost is not None:
            for k in parameters.rl.reward_boost.keys():
                i = self._actions.index(k)
                self.reward_boosts[0, i] = parameters.rl.reward_boost[k]

        if parameters.rainbow.dueling_architecture:
            self.q_network = DuelingQNetwork(
                parameters.training.layers,
                parameters.training.activations,
                use_batch_norm=parameters.training.use_batch_norm,
            )
        else:
            if parameters.training.cnn_parameters is None:
                self.q_network = FullyConnectedNetwork(
                    parameters.training.layers,
                    parameters.training.activations,
                    use_noisy_linear_layers=parameters.training.use_noisy_linear_layers,
                    min_std=parameters.training.weight_init_min_std,
                    use_batch_norm=parameters.training.use_batch_norm,
                )
            else:
                self.q_network = ConvolutionalNetwork(
                    parameters.training.cnn_parameters,
                    parameters.training.layers,
                    parameters.training.activations,
                    use_noisy_linear_layers=parameters.training.use_noisy_linear_layers,
                    min_std=parameters.training.weight_init_min_std,
                    use_batch_norm=parameters.training.use_batch_norm,
                )

        self.q_network_target = deepcopy(self.q_network)
        self._set_optimizer(parameters.training.optimizer)
        self.q_network_optimizer = self.optimizer_func(
            self.q_network.parameters(),
            lr=parameters.training.learning_rate,
            weight_decay=parameters.training.l2_decay,
        )

        reward_network_layers = deepcopy(parameters.training.layers)
        if self.metrics_to_score:
            num_output_nodes = len(self.metrics_to_score) * self.num_actions
        else:
            num_output_nodes = self.num_actions

        reward_network_layers[-1] = num_output_nodes
        self.reward_idx_offsets = torch.range(
            0, num_output_nodes - self.num_actions, self.num_actions
        ).type(self.dtype)
        logger.info(
            "Reward network for CPE will have {} output nodes.".format(num_output_nodes)
        )

        if parameters.training.cnn_parameters is None:
            self.reward_network = FullyConnectedNetwork(
                reward_network_layers, parameters.training.activations
            )
            self.q_network_cpe = FullyConnectedNetwork(
                reward_network_layers, parameters.training.activations
            )
        else:
            self.reward_network = ConvolutionalNetwork(
                parameters.training.cnn_parameters,
                reward_network_layers,
                parameters.training.activations,
            )
            self.q_network_cpe = ConvolutionalNetwork(
                parameters.training.cnn_parameters,
                reward_network_layers,
                parameters.training.activations,
            )
        self.q_network_cpe_target = deepcopy(self.q_network_cpe)
        self.q_network_cpe_optimizer = self.optimizer_func(
            self.q_network_cpe.parameters(), lr=parameters.training.learning_rate
        )
        self.reward_network_optimizer = self.optimizer_func(
            self.reward_network.parameters(), lr=parameters.training.learning_rate
        )

        if self.use_gpu:
            self.q_network.cuda()
            self.q_network_target.cuda()
            self.reward_network.cuda()
            self.q_network_cpe.cuda()
            self.q_network_cpe_target.cuda()

            if use_all_avail_gpus:
                self.q_network = torch.nn.DataParallel(self.q_network)
                self.q_network_target = torch.nn.DataParallel(self.q_network_target)
                self.reward_network = torch.nn.DataParallel(self.reward_network)
                self.q_network_cpe = torch.nn.DataParallel(self.q_network_cpe)
                self.q_network_cpe_target = torch.nn.DataParallel(
                    self.q_network_cpe_target
                )

    @property
    def num_actions(self) -> int:
        return len(self._actions)

    def calculate_q_values(self, states):
        return self.q_network(states).detach()

    def calculate_metric_q_values(self, states):
        return self.q_network_cpe(states).detach()

    def get_max_q_values(self, states, possible_actions, double_q_learning):
        """
        Used in Q-learning update.
        :param states: Numpy array with shape (batch_size, state_dim). Each row
            contains a representation of a state.
        :param possible_actions: Numpy array with shape (batch_size, action_dim).
            possible_next_actions[i][j] = 1 iff the agent can take action j from
            state i.
        :param double_q_learning: bool to use double q-learning
        """
        if double_q_learning:
            q_values = self.q_network(states).detach()
            q_values_target = self.q_network_target(states).detach()
            # Set q-values of impossible actions to a very large negative number.
            inverse_pna = 1 - possible_actions
            impossible_action_penalty = self.ACTION_NOT_POSSIBLE_VAL * inverse_pna
            q_values += impossible_action_penalty
            # Select max_q action after scoring with online network
            max_q_values, max_indicies = torch.max(q_values, dim=1, keepdim=True)
            # Use q_values from target network for max_q action from online q_network
            # to decouble selection & scoring, preventing overestimation of q-values
            q_values = torch.gather(q_values_target, 1, max_indicies)
            return q_values, max_indicies.type(self.dtype)
        else:
            q_values = self.q_network_target(states).detach()
            # Set q-values of impossible actions to a very large negative number.
            inverse_pna = 1 - possible_actions
            impossible_action_penalty = self.ACTION_NOT_POSSIBLE_VAL * inverse_pna
            q_values += impossible_action_penalty
            max_q_values, max_indicies = torch.max(q_values, dim=1, keepdim=True)
            return max_q_values, max_indicies.type(self.dtype)

    def get_next_action_q_values(self, states, next_actions):
        """
        Used in SARSA update.
        :param states: Numpy array with shape (batch_size, state_dim). Each row
            contains a representation of a state.
        :param next_actions: Numpy array with shape (batch_size, action_dim).
        """
        q_values = self.q_network_target(states).detach()
        # Max-q action indexes used in CPE
        max_q_values, max_indicies = torch.max(q_values, dim=1, keepdim=True)
        return (
            torch.sum(q_values * next_actions, dim=1, keepdim=True),
            max_indicies.type(self.dtype),
        )

    def train(
        self, training_samples: TrainingDataPage, evaluator: Optional[Evaluator] = None
    ):

        if self.minibatch == 0:
            # Assume that the tensors are the right shape after the first minibatch
            assert (
                training_samples.states.shape[0] == self.minibatch_size
            ), "Invalid shape: " + str(training_samples.states.shape)
            assert training_samples.actions.shape == torch.Size(
                [self.minibatch_size, len(self._actions)]
            ), "Invalid shape: " + str(training_samples.actions.shape)
            assert training_samples.rewards.shape == torch.Size(
                [self.minibatch_size, 1]
            ), "Invalid shape: " + str(training_samples.rewards.shape)
            assert (
                training_samples.next_states.shape == training_samples.states.shape
            ), "Invalid shape: " + str(training_samples.next_states.shape)
            assert (
                training_samples.not_terminals.shape == training_samples.rewards.shape
            ), "Invalid shape: " + str(training_samples.not_terminals.shape)
            if training_samples.possible_next_actions is not None:
                assert (
                    training_samples.possible_next_actions.shape
                    == training_samples.actions.shape
                ), "Invalid shape: " + str(training_samples.possible_next_actions.shape)
            if training_samples.propensities is not None:
                assert (
                    training_samples.propensities.shape
                    == training_samples.rewards.shape
                ), "Invalid shape: " + str(training_samples.propensities.shape)
            if training_samples.metrics is not None:
                assert (
                    training_samples.metrics.shape[0] == self.minibatch_size
                ), "Invalid shape: " + str(training_samples.metrics.shape)

        # Apply reward boost if specified
        reward_boosts = torch.sum(
            training_samples.actions.float() * self.reward_boosts, dim=1, keepdim=True
        )
        boosted_rewards = training_samples.rewards + reward_boosts

        self.minibatch += 1
        states = training_samples.states.detach().requires_grad_(True)
        actions = training_samples.actions
        rewards = boosted_rewards
        next_states = training_samples.next_states
        discount_tensor = torch.full(
            training_samples.time_diffs.shape, self.gamma
        ).type(self.dtype)
        not_done_mask = training_samples.not_terminals

        if self.use_seq_num_diff_as_time_diff:
            discount_tensor = discount_tensor.pow(training_samples.time_diffs)

        if self.maxq_learning:
            # Compute max a' Q(s', a') over all possible actions using target network
            possible_next_actions = training_samples.possible_next_actions
            next_q_values, max_q_action_idxs = self.get_max_q_values(
                next_states, possible_next_actions, self.double_q_learning
            )
        else:
            # SARSA
            next_actions = training_samples.next_actions
            next_q_values, max_q_action_idxs = self.get_next_action_q_values(
                next_states, next_actions
            )

        filtered_next_q_vals = next_q_values * not_done_mask

        if self.minibatch < self.reward_burnin:
            target_q_values = rewards
        else:
            target_q_values = rewards + (discount_tensor * filtered_next_q_vals)

        # Get Q-value of action taken
        all_q_values = self.q_network(states)
        self.all_action_scores = all_q_values.detach()
        q_values = torch.sum(all_q_values * actions, 1, keepdim=True)

        loss = self.q_network_loss(q_values, target_q_values)
        self.loss = loss.detach()

        self.q_network_optimizer.zero_grad()
        loss.backward()
        if self.gradient_handler:
            self.gradient_handler(self.q_network.parameters())
        self.q_network_optimizer.step()

        if self.minibatch < self.reward_burnin:
            # Reward burnin: force target network
            self._soft_update(self.q_network, self.q_network_target, 1.0)
        else:
            # Use the soft update rule to update target network
            self._soft_update(self.q_network, self.q_network_target, self.tau)

        if training_samples.metrics is None:
            metrics_reward_concat_real_vals = training_samples.rewards
        else:
            metrics_reward_concat_real_vals = torch.cat(
                (training_samples.metrics, training_samples.rewards), dim=1
            )

        ######### Train separate reward network for CPE evaluation #############
        reward_estimates = self.reward_network(states)
        logged_action_idxs = actions.argmax(dim=1, keepdim=True).type(self.dtype)
        self.reward_estimates = reward_estimates.detach()
        reward_estimates_for_logged_actions = reward_estimates.gather(
            1, (self.reward_idx_offsets + logged_action_idxs).type(self.dtypelong)
        )
        reward_loss = F.mse_loss(
            reward_estimates_for_logged_actions, metrics_reward_concat_real_vals
        )
        self.reward_network_optimizer.zero_grad()
        reward_loss.backward()
        self.reward_network_optimizer.step()
        self.loss_reporter.report(
            td_loss=float(self.loss.cpu()), reward_loss=float(reward_loss.cpu())
        )

        ######### Train separate q-network for CPE evaluation #############
        metric_q_values = self.q_network_cpe(states).gather(
            1, (self.reward_idx_offsets + logged_action_idxs).type(self.dtypelong)
        )
        metric_target_q_values = self.q_network_cpe_target(states).detach()
        max_q_values_metrics = metric_target_q_values.gather(
            1, (self.reward_idx_offsets + max_q_action_idxs).type(self.dtypelong)
        )
        filtered_max_q_values_metrics = max_q_values_metrics * not_done_mask
        if self.minibatch < self.reward_burnin:
            target_metric_q_values = metrics_reward_concat_real_vals
        else:
            target_metric_q_values = metrics_reward_concat_real_vals + (
                discount_tensor * filtered_max_q_values_metrics
            )
        metric_q_value_loss = self.q_network_loss(
            metric_q_values, target_metric_q_values
        )
        self.q_network_cpe.zero_grad()
        metric_q_value_loss.backward()
        self.q_network_cpe_optimizer.step()

        if self.minibatch < self.reward_burnin:
            # Reward burnin: force target network
            self._soft_update(self.q_network_cpe, self.q_network_cpe_target, 1.0)
        else:
            # Use the soft update rule to update target network
            self._soft_update(self.q_network_cpe, self.q_network_cpe_target, self.tau)

        training_metadata = {}
        if evaluator is not None:

            model_propensities = torch.from_numpy(
                Evaluator.softmax(
                    self.all_action_scores.cpu().numpy(), self.rl_temperature
                )
            )

            if training_samples.metrics is None:
                # If no metrics, use rewards tensor
                metrics = rewards.cpu().numpy()
            else:
                metrics = training_samples.metrics.cpu().numpy()

            cpe_stats = BatchStatsForCPE(
                logged_actions=training_samples.actions.cpu().numpy(),
                logged_propensities=training_samples.propensities.cpu().numpy(),
                logged_rewards=rewards.cpu().numpy(),
                logged_values=None,  # Compute at end of each epoch for CPE
                model_propensities=model_propensities.cpu().numpy(),
                model_rewards=self.reward_estimates.cpu().numpy(),
                model_values=self.all_action_scores.cpu().numpy(),
                model_values_on_logged_actions=None,  # Compute at end of each epoch for CPE
                model_action_idxs=self.all_action_scores.argmax(dim=1, keepdim=True)
                .cpu()
                .numpy(),
                metrics=metrics,
            )
            evaluator.report(cpe_stats)
            training_metadata["model_rewards"] = self.reward_estimates.cpu().numpy()

        return training_metadata

    def predictor(self) -> DQNPredictor:
        """Builds a DQNPredictor."""
        return DQNPredictor.export(
            self,
            self._actions,
            self.state_normalization_parameters,
            self._additional_feature_types.int_features,
            self.use_gpu,
        )

    def export(self) -> DQNPredictor:
        return self.predictor()
