import torch
from torch.distributions import Normal, Independent
from collections.abc import Iterable

import utils
from agent.sac.base_sac_agent import SacMlpAgent


class EwcV2SacMlpAgentV2(SacMlpAgent):
    """Adapt https://github.com/GMvandeVen/continual-learning"""
    def __init__(self,
                 obs_shape,
                 action_shape,
                 action_range,
                 device,
                 actor_hidden_dim=400,
                 critic_hidden_dim=256,
                 discount=0.99,
                 init_temperature=0.01,
                 alpha_lr=1e-3,
                 actor_lr=1e-3,
                 actor_log_std_min=-10,
                 actor_log_std_max=2,
                 actor_update_freq=2,
                 critic_lr=1e-3,
                 critic_tau=0.005,
                 critic_target_update_freq=2,
                 batch_size=128,
                 ewc_lambda=5000,
                 ewc_estimate_fisher_iters=100,
                 ewc_estimate_fisher_rollout_steps=1024,
                 online_ewc=False,
                 online_ewc_gamma=1.0,
                 ):
        super().__init__(obs_shape, action_shape, action_range, device, actor_hidden_dim, critic_hidden_dim,
                         discount, init_temperature, alpha_lr, actor_lr, actor_log_std_min, actor_log_std_max,
                         actor_update_freq, critic_lr, critic_tau, critic_target_update_freq, batch_size)

        self.ewc_lambda = ewc_lambda
        self.ewc_estimate_fisher_iters = ewc_estimate_fisher_iters
        self.ewc_estimate_fisher_rollout_steps = ewc_estimate_fisher_rollout_steps
        self.online_ewc = online_ewc
        self.online_ewc_gamma = online_ewc_gamma

        self.ewc_task_count = 0
        self.prev_task_params = {}
        self.prev_task_fishers = {}
        self.task_rollouts = {}

    def _compute_ewc_loss(self, named_parameters):
        assert isinstance(named_parameters, Iterable), "'named_parameters' must be a iterator"

        ewc_losses = []
        if self.ewc_task_count >= 1:
            if self.online_ewc:
                for name, param in named_parameters:
                    if param.grad is not None:
                        name = name + '_prev_task'
                        mean = self.prev_task_params[name]
                        # apply decay-term to the running sum of the Fisher Information matrices
                        fisher = self.online_ewc_gamma * self.prev_task_fishers[name]
                        ewc_loss = torch.sum(fisher * (param - mean) ** 2)
                        ewc_losses.append(ewc_loss)
            else:
                for task in range(self.ewc_task_count):
                    # compute ewc loss for each parameter
                    for name, param in named_parameters:
                        if param.grad is not None:
                            name = name + f'_prev_task{task}'
                            mean = self.prev_task_params[name]
                            fisher = self.prev_task_fishers[name]
                            ewc_loss = torch.sum(fisher * (param - mean) ** 2)
                            ewc_losses.append(ewc_loss)
            return torch.sum(torch.stack(ewc_losses)) / 2.0
        else:
            return torch.tensor(0.0, device=self.device)

    def estimate_fisher(self, env, **kwargs):
        # TODO (chongyi zheng): save trajectory for KL divergence
        self.task_rollouts[self.ewc_task_count] = []
        fishers = {}
        obs = env.reset()
        for _ in range(self.ewc_estimate_fisher_iters):
            rollout = {
                'obs': [],
                'action': [],
                'next_obs': [],
                'done': [],
                'mu': [],
                'log_std': [],
            }
            for _ in range(self.ewc_estimate_fisher_rollout_steps):
                with utils.eval_mode(self):
                    # action = self.act(obs, sample=True, **kwargs)
                    # compute log_pi and Q for later gradient projection
                    mu, action, log_pi, log_std = self.actor(
                        torch.Tensor(obs).to(device=self.device),
                        compute_pi=True, compute_log_pi=True, **kwargs)

                    action = utils.to_np(action.clamp(*self.action_range))

                next_obs, reward, done, _ = env.step(action)

                rollout['obs'].append(obs)
                rollout['action'].append(action)
                rollout['next_obs'].append(next_obs)
                rollout['done'].append(done)
                rollout['mu'].append(mu)
                rollout['log_std'].append(log_std)

                obs = next_obs

            self.task_rollouts[self.ewc_task_count].append(rollout)

            _, actor_loss, _ = self.compute_actor_and_alpha_loss(
                torch.Tensor(rollout['obs']).to(device=self.device),
                compute_alpha_loss=False, **kwargs
            )
            self.actor_optimizer.zero_grad()
            actor_loss.backward()

            for name, param in self.actor.named_parameters():
                if param.requires_grad:
                    if param.grad is not None:
                        fishers[name] = param.grad.detach().clone() ** 2 + \
                                        fishers.get(name, torch.zeros_like(param.grad))
                    else:
                        fishers[name] = torch.zeros_like(param)

        for name, param in self.actor.named_parameters():
            if param.requires_grad:
                fisher = fishers[name]

                if self.online_ewc:
                    name = name + '_prev_task'
                    self.prev_task_params[name] = param.detach().clone()
                    self.prev_task_fishers[name] = \
                        fisher / self.ewc_estimate_fisher_iters + \
                        self.online_ewc_gamma * self.prev_task_fishers.get(
                            name, torch.zeros_like(param.grad))
                else:
                    name = name + f'_prev_task{self.ewc_task_count}'
                    self.prev_task_params[name] = param.detach().clone()
                    self.prev_task_fishers[name] = \
                        fisher / self.ewc_estimate_fisher_iters

        self.ewc_task_count += 1

    def kl_with_optimal_actor(self, task_id):
        if task_id >= self.ewc_task_count:
            return -1.0
        else:
            rollouts = self.task_rollouts[task_id]
            kls = []
            for rollout in rollouts:
                with utils.eval_mode(self):
                    mu, _, _, log_std = self.actor(
                        torch.Tensor(rollout['obs']).to(device=self.device),
                        compute_pi=False, compute_log_pi=False)

                optimal_dist = Independent(Normal(
                    torch.cat(rollout['mu']), torch.cat(rollout['log_std']).exp()), 1)
                dist = Independent(Normal(mu, log_std.exp()), 1)
                kl = torch.distributions.kl_divergence(optimal_dist, dist)
                kls.append(kl)

            return torch.mean(torch.cat(kls))

    def update(self, replay_buffer, logger, step, **kwargs):
        obs, action, reward, next_obs, not_done = replay_buffer.sample(self.batch_size)

        logger.log('train/batch_reward', reward.mean(), step)

        critic_loss = self.compute_critic_loss(obs, action, reward, next_obs, not_done, **kwargs)
        # TODO (chongyi zheng): delete this block
        # critic_ewc_loss = self._compute_ewc_loss(self.critic.named_parameters())
        # critic_loss = critic_loss + self.ewc_lambda * critic_ewc_loss
        self.update_critic(critic_loss, logger, step)

        if step % self.actor_update_freq == 0:
            log_pi, actor_loss, alpha_loss = self.compute_actor_and_alpha_loss(obs, **kwargs)
            actor_ewc_loss = self._compute_ewc_loss(self.actor.named_parameters())
            actor_loss = actor_loss + self.ewc_lambda * actor_ewc_loss
            # TODO (chongyi zheng): delete this block
            # alpha_ewc_loss = self._compute_ewc_loss(iter([('log_alpha', self.log_alpha)]))
            # alpha_loss = alpha_loss + self.ewc_lambda * alpha_ewc_loss

            self.update_actor_and_alpha(log_pi, actor_loss, logger, step, alpha_loss=alpha_loss)

        if step % self.critic_target_update_freq == 0:
            utils.soft_update_params(self.critic, self.critic_target,
                                     self.critic_tau)

    def save(self, model_dir, step):
        super().save(model_dir, step)
        torch.save(
            self.prev_task_params,
            '%s/prev_task_params_%s.pt' % (model_dir, step)
        )

    def load(self, model_dir, step):
        super().load(model_dir, step)
        self.prev_task_params = torch.load(
            '%s/prev_task_params_%s.pt' % (model_dir, step)
        )
