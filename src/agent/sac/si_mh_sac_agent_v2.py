import torch

import utils
from agent.sac import MultiHeadSacMlpAgentV2, SiSacMlpAgentV2


class SiMultiHeadSacMlpAgentV2(MultiHeadSacMlpAgentV2, SiSacMlpAgentV2):
    """Adapt from https://github.com/GMvandeVen/continual-learning"""
    def __init__(self,
                 obs_shape,
                 action_shape,
                 action_range,
                 device,
                 hidden_dim=400,
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
                 si_c=1.0,
                 si_epsilon=0.1,
                 ):
        MultiHeadSacMlpAgentV2.__init__(self, obs_shape, action_shape, action_range, device, hidden_dim, discount,
                                        init_temperature, alpha_lr, actor_lr, actor_log_std_min, actor_log_std_max,
                                        actor_update_freq, critic_lr, critic_tau, critic_target_update_freq,
                                        batch_size)

        SiSacMlpAgentV2.__init__(self, obs_shape, action_shape, action_range, device, hidden_dim, discount,
                                 init_temperature, alpha_lr, actor_lr, actor_log_std_min, actor_log_std_max,
                                 actor_update_freq, critic_lr, critic_tau, critic_target_update_freq, batch_size,
                                 si_c, si_epsilon)

    def _save_init_params(self):
        # set prev_task_params as weight initializations
        for name, param in self.actor.named_common_parameters():
            if param.requires_grad:
                self.prev_task_params[name] = param.detach().clone()
                self.prev_params[name] = param.detach().clone()

    def update_omegas(self):
        for name, param in self.actor.named_common_parameters():
            if param.requires_grad:
                prev_param = self.prev_task_params[name]
                current_param = param.detach().clone()
                delta_param = current_param - prev_param
                current_omega = self.params_w[name] / (delta_param ** 2 + self.si_epsilon)

                self.prev_task_params[name] = current_param
                self.omegas[name] = current_omega + self.omegas.get(name, torch.zeros_like(param))

        # clear importance buffers for the next task
        self.params_w = {}

    def _estimate_importance(self):
        for name, param in self.actor.named_common_parameters():
            if param.requires_grad:
                self.params_w[name] = \
                    -param.grad.detach() * (param.detach() - self.prev_params[name]) + \
                    self.params_w.get(name, torch.zeros_like(param))
                self.prev_params[name] = param.detach().clone()

    def update(self, replay_buffer, logger, step, **kwargs):
        obs, action, reward, next_obs, not_done = replay_buffer.sample(self.batch_size)

        logger.log('train/batch_reward', reward.mean(), step)

        critic_loss = self.compute_critic_loss(obs, action, reward, next_obs, not_done, **kwargs)
        # TODO (chongyi zheng): delete this block
        # critic_si_surrogate_loss = self._compute_surrogate_loss(
        #     self.critic.named_common_parameters())
        # critic_loss = critic_loss + self.si_c * critic_si_surrogate_loss
        self.update_critic(critic_loss, logger, step)

        if step % self.actor_update_freq == 0:
            log_pi, actor_loss, alpha_loss = self.compute_actor_and_alpha_loss(obs, **kwargs)
            actor_si_surrogate_loss = self._compute_surrogate_loss(
                self.actor.named_common_parameters())
            actor_loss = actor_loss + self.si_c * actor_si_surrogate_loss
            # TODO (chongyi zheng): delete this block
            # alpha_si_surrogate_loss = self._compute_surrogate_loss(iter([('log_alpha', self.log_alpha)]))
            # alpha_loss = alpha_loss + self.si_c * alpha_si_surrogate_loss

            self.update_actor_and_alpha(log_pi, actor_loss, logger, step, alpha_loss=alpha_loss)

        if step % self.critic_target_update_freq == 0:
            utils.soft_update_params(self.critic, self.critic_target,
                                     self.critic_tau)

        # estimate weight importance
        self._estimate_importance()
