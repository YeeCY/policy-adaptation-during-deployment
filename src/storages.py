# Adapt from https://github.com/ikostrikov/pytorch-a2c-ppo-acktr-gail

import numpy as np
import torch
from torch.utils.data.sampler import BatchSampler, SubsetRandomSampler


def _flatten_helper(T, N, _tensor):
    return _tensor.view(T * N, *_tensor.size()[2:])


class RolloutStorage(object):
    def __init__(self, num_steps, num_processes, obs_shape, action_space, device):
        self.obs = torch.zeros(num_steps + 1, num_processes, *obs_shape).to(device)
        self.rewards = torch.zeros(num_steps, num_processes, 1).to(device)
        self.value_preds = torch.zeros(num_steps + 1, num_processes, 1).to(device)
        self.returns = torch.zeros(num_steps + 1, num_processes, 1).to(device)
        self.log_pis = torch.zeros(num_steps, num_processes, 1).to(device)
        if action_space.__class__.__name__ == 'Discrete':
            action_shape = 1
        else:
            action_shape = action_space.shape[0]
        self.actions = torch.zeros(num_steps, num_processes, action_shape).to(device)
        if action_space.__class__.__name__ == 'Discrete':
            self.actions = self.actions.long()
        self.masks = torch.ones(num_steps + 1, num_processes, 1).to(device)

        # Masks that indicate whether it's a true terminal state
        # or time limit end state
        self.bad_masks = torch.ones(num_steps + 1, num_processes, 1).to(device)

        self.num_steps = num_steps
        self.num_processes = num_processes
        self.device = device
        self.step = 0

    # def to(self, device):
    #     self.obs = self.obs.to(device)
    #     self.rewards = self.rewards.to(device)
    #     self.value_preds = self.value_preds.to(device)
    #     self.returns = self.returns.to(device)
    #     self.log_pis = self.log_pis.to(device)
    #     self.actions = self.actions.to(device)
    #     self.masks = self.masks.to(device)
    #     self.bad_masks = self.bad_masks.to(device)

    def update_num_steps(self, num_steps):
        # (chongyi zheng): used for memory resize
        self.num_steps = num_steps
        self.obs = self.obs[:num_steps + 1]
        self.rewards = self.rewards[:num_steps]
        self.value_preds = self.value_preds[:num_steps + 1]
        self.returns = self.returns[:num_steps + 1]
        self.log_pis = self.log_pis[:num_steps]
        self.actions = self.actions[:num_steps]
        self.masks = self.masks[:num_steps + 1]
        self.bad_masks = self.bad_masks[:num_steps + 1]

    def insert(self, obs, actions, log_pis,
               value_preds, rewards, masks, bad_masks):
        self.obs[self.step + 1].copy_(torch.Tensor(obs).to(self.device))
        self.actions[self.step].copy_(torch.Tensor(actions).to(self.device))
        self.log_pis[self.step].copy_(torch.Tensor(log_pis).to(self.device))
        self.value_preds[self.step].copy_(torch.Tensor(value_preds).to(self.device))
        self.rewards[self.step].copy_(torch.Tensor(
            np.expand_dims(rewards, axis=-1)
        ).to(self.device))
        self.masks[self.step + 1].copy_(torch.Tensor(masks).to(self.device))
        self.bad_masks[self.step + 1].copy_(torch.Tensor(bad_masks).to(self.device))

        self.step = (self.step + 1) % self.num_steps

    def after_update(self):
        self.obs[0].copy_(self.obs[-1])
        self.masks[0].copy_(self.masks[-1])
        self.bad_masks[0].copy_(self.bad_masks[-1])

    def compute_returns(self,
                        next_value,
                        gamma,
                        gae_lambda,
                        use_proper_time_limits=True):
        if not isinstance(next_value, torch.Tensor):
            next_value = torch.Tensor(next_value).to(self.device)

        # (chongyi zheng): force use GAE
        if use_proper_time_limits:
            # if use_gae:
            self.value_preds[-1] = next_value
            gae = 0
            for step in reversed(range(self.rewards.size(0))):
                delta = self.rewards[step] + gamma * self.value_preds[step + 1] \
                        * self.masks[step + 1] - self.value_preds[step]
                gae = delta + gamma * gae_lambda * self.masks[step + 1] * gae
                gae = gae * self.bad_masks[step + 1]
                self.returns[step] = gae + self.value_preds[step]
            # else:
            #     self.returns[-1] = next_value
            #     for step in reversed(range(self.rewards.size(0))):
            #         self.returns[step] = (self.returns[step + 1] * \
            #                               gamma * self.masks[step + 1] + \
            #                               self.rewards[step]) * self.bad_masks[step + 1] \
            #                              + (1 - self.bad_masks[step + 1]) * self.value_preds[step]
        else:
            # if use_gae:
            self.value_preds[-1] = next_value
            gae = 0
            for step in reversed(range(self.rewards.size(0))):
                delta = self.rewards[step] + gamma * self.value_preds[step + 1] \
                        * self.masks[step + 1] - self.value_preds[step]
                gae = delta + gamma * gae_lambda * self.masks[step + 1] * gae
                self.returns[step] = gae + self.value_preds[step]
            # else:
            #     self.returns[-1] = next_value
            #     for step in reversed(range(self.rewards.size(0))):
            #         self.returns[step] = self.returns[step + 1] * gamma \
            #                              * self.masks[step + 1] + self.rewards[step]

    def feed_forward_generator(self,
                               advantages,
                               num_mini_batch=None,
                               mini_batch_size=None):
        num_steps, num_processes = self.rewards.size()[0:2]
        batch_size = num_processes * num_steps

        if mini_batch_size is None:
            assert batch_size >= num_mini_batch, (
                "PPO requires the number of processes ({}) "
                "* number of steps ({}) = {} "
                "to be greater than or equal to the number of PPO mini batches ({})."
                "".format(num_processes, num_steps, num_processes * num_steps,
                          num_mini_batch))
            mini_batch_size = batch_size // num_mini_batch
        sampler = BatchSampler(
            SubsetRandomSampler(range(batch_size)),
            mini_batch_size,
            drop_last=True)
        for indices in sampler:
            obs_batch = self.obs[:-1].view(-1, *self.obs.size()[2:])[indices]
            actions_batch = self.actions.view(-1,
                                              self.actions.size(-1))[indices]
            value_preds_batch = self.value_preds[:-1].view(-1, 1)[indices]
            return_batch = self.returns[:-1].view(-1, 1)[indices]
            old_log_pis = self.log_pis.view(-1, 1)[indices]
            if advantages is None:
                adv_targ = None
            else:
                adv_targ = advantages.view(-1, 1)[indices]

            yield obs_batch, actions_batch, value_preds_batch, return_batch, old_log_pis, adv_targ
