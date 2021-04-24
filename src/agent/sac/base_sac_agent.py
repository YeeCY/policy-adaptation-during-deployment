import torch
import numpy as np
import utils
import torch.nn.functional as F

from agent.network import ActorMlp, CriticMlp


class SacMlpAgent:
    def __init__(
            self,
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
    ):
        self.obs_shape = obs_shape
        self.action_shape = action_shape
        self.action_range = action_range
        self.device = device
        self.hidden_dim = hidden_dim
        self.discount = discount
        self.init_temperature = init_temperature
        self.alpha_lr = alpha_lr
        self.actor_lr = actor_lr
        self.actor_log_std_min = actor_log_std_min
        self.actor_log_std_max = actor_log_std_max
        self.actor_update_freq = actor_update_freq
        self.critic_lr = critic_lr
        self.critic_tau = critic_tau
        self.critic_target_update_freq = critic_target_update_freq
        self.batch_size = batch_size

        self.training = False

        self._setup_agent()

        self.train()

    def _setup_agent(self):
        self.actor = ActorMlp(
            self.obs_shape, self.action_shape, self.hidden_dim,
            self.actor_log_std_min, self.actor_log_std_max
        ).to(self.device)

        self.critic = CriticMlp(
            self.obs_shape, self.action_shape, self.hidden_dim
        ).to(self.device)

        self.critic_target = CriticMlp(
            self.obs_shape, self.action_shape, self.hidden_dim
        ).to(self.device)

        self.critic_target.load_state_dict(self.critic.state_dict())

        # TODO (chongyi zheng): delete this line
        # tie encoders between actor and criticp
        # self.actor.encoder.copy_conv_weights_from(self.critic.encoder)

        self.log_alpha = torch.tensor(np.log(self.init_temperature)).to(self.device)
        self.log_alpha.requires_grad = True
        # set target entropy to -|A|
        self.target_entropy = -np.prod(self.action_shape)

        # sac optimizers
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=self.actor_lr)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=self.critic_lr)

        self.log_alpha_optimizer = torch.optim.Adam([self.log_alpha], lr=self.alpha_lr)

    def train(self, training=True):
        self.training = training
        self.actor.train(training)
        self.critic.train(training)
        self.critic_target.train(training)

    @property
    def alpha(self):
        return self.log_alpha.exp()

    def act(self, obs, sample=False):
        with torch.no_grad():
            obs = torch.FloatTensor(obs).to(self.device)
            obs = obs.unsqueeze(0)
            mu, pi, _, _ = self.actor(obs, compute_log_pi=False)
            action = pi if sample else mu
            action = action.clamp(*self.action_range)
            assert action.ndim == 2 and action.shape[0] == 1

        return utils.to_np(action[0])

    def compute_critic_loss(self, obs, action, reward, next_obs, not_done):
        with torch.no_grad():
            _, policy_action, log_pi, _ = self.actor(next_obs)
            target_Q1, target_Q2 = self.critic_target(next_obs, policy_action)
            target_V = torch.min(target_Q1,
                                 target_Q2) - self.alpha.detach() * log_pi
            target_Q = reward + (not_done * self.discount * target_V)

        # get current Q estimates
        current_Q1, current_Q2 = self.critic(obs, action)
        critic_loss = F.mse_loss(current_Q1, target_Q) + F.mse_loss(current_Q2, target_Q)

        return critic_loss

    def update_critic(self, critic_loss, logger, step):
        # Optimize the critic
        logger.log('train_critic/loss', critic_loss, step)
        self.critic_optimizer.zero_grad()
        critic_loss.backward()
        self.critic_optimizer.step()

    def compute_actor_and_alpha_loss(self, obs, compute_alpha_loss=True):
        _, pi, log_pi, log_std = self.actor(obs)
        actor_Q1, actor_Q2 = self.critic(obs, pi)

        actor_Q = torch.min(actor_Q1, actor_Q2)
        actor_loss = (self.alpha.detach() * log_pi - actor_Q).mean()

        alpha_loss = None
        if compute_alpha_loss:
            self.log_alpha_optimizer.zero_grad()
            alpha_loss = (self.alpha * (-log_pi - self.target_entropy).detach()).mean()

        return log_pi, actor_loss, alpha_loss

    def update_actor_and_alpha(self, log_pi, actor_loss, logger, step, alpha_loss=None):
        logger.log('train_actor/loss', actor_loss, step)
        logger.log('train_actor/target_entropy', self.target_entropy, step)
        logger.log('train_actor/entropy', -log_pi.mean(), step)

        # optimize the actor
        self.actor_optimizer.zero_grad()
        actor_loss.backward()
        self.actor_optimizer.step()

        if isinstance(alpha_loss, torch.Tensor):
            logger.log('train_alpha/loss', alpha_loss, step)
            logger.log('train_alpha/value', self.alpha, step)

            self.log_alpha_optimizer.zero_grad()
            alpha_loss.backward()
            self.log_alpha_optimizer.step()

    def update(self, replay_buffer, logger, step):
        # obs, action, reward, next_obs, not_done, ensem_kwargs = replay_buffer.sample_ensembles(
        #     self.batch_size, num_ensembles=self.num_ensem_comps)
        obs, action, reward, next_obs, not_done = replay_buffer.sample(self.batch_size)
        # obs, action, reward, next_obs, not_done, obs_aug, next_obs_aug = replay_buffer.sample(
        #     self.batch_size)

        logger.log('train/batch_reward', reward.mean(), step)

        critic_loss = self.compute_critic_loss(obs, action, reward, next_obs, not_done)
        self.update_critic(critic_loss, logger, step)

        if step % self.actor_update_freq == 0:
            log_pi, actor_loss, alpha_loss = self.compute_actor_and_alpha_loss(obs)
            self.update_actor_and_alpha(log_pi, actor_loss, logger, step, alpha_loss=alpha_loss)

        if step % self.critic_target_update_freq == 0:
            utils.soft_update_params(self.critic, self.critic_target,
                                     self.critic_tau)

    def save(self, model_dir, step):
        torch.save(
            self.actor.state_dict(), '%s/actor_%s.pt' % (model_dir, step)
        )
        torch.save(
            self.critic.state_dict(), '%s/critic_%s.pt' % (model_dir, step)
        )

    def load(self, model_dir, step):
        self.actor.load_state_dict(
            torch.load('%s/actor_%s.pt' % (model_dir, step))
        )
        self.critic.load_state_dict(
            torch.load('%s/critic_%s.pt' % (model_dir, step))
        )