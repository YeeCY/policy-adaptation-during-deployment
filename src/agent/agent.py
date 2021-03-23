import itertools
import numpy as np
import torch
import torch.nn.functional as F
import utils

from agent.network import Actor, Critic, CURL, FwdFunction, InvFunction, RotFunction

LOG_FREQ = 10000


def make_agent(obs_shape, action_shape, args):
    if args.use_ensemble:
        agent = SacSSEnsembleAgent(
            obs_shape=obs_shape,
            action_shape=action_shape,
            hidden_dim=args.hidden_dim,
            discount=args.discount,
            init_temperature=args.init_temperature,
            alpha_lr=args.alpha_lr,
            alpha_beta=args.alpha_beta,
            actor_lr=args.actor_lr,
            actor_beta=args.actor_beta,
            actor_log_std_min=args.actor_log_std_min,
            actor_log_std_max=args.actor_log_std_max,
            actor_update_freq=args.actor_update_freq,
            critic_lr=args.critic_lr,
            critic_beta=args.critic_beta,
            critic_tau=args.critic_tau,
            critic_target_update_freq=args.critic_target_update_freq,
            encoder_feature_dim=args.encoder_feature_dim,
            encoder_lr=args.encoder_lr,
            encoder_tau=args.encoder_tau,
            use_fwd=args.use_fwd,
            use_inv=args.use_inv,
            ss_lr=args.ss_lr,
            ss_update_freq=args.ss_update_freq,
            ss_stop_shared_layers_grad=args.ss_stop_shared_layers_grad,
            batch_size=args.batch_size,
            num_layers=args.num_layers,
            num_shared_layers=args.num_shared_layers,
            num_filters=args.num_filters,
            curl_latent_dim=args.curl_latent_dim,
            num_ensem_comps=args.num_ensem_comps,
        )
    else:
        agent = SacSSAgent(
            obs_shape=obs_shape,
            action_shape=action_shape,
            hidden_dim=args.hidden_dim,
            discount=args.discount,
            init_temperature=args.init_temperature,
            alpha_lr=args.alpha_lr,
            alpha_beta=args.alpha_beta,
            actor_lr=args.actor_lr,
            actor_beta=args.actor_beta,
            actor_log_std_min=args.actor_log_std_min,
            actor_log_std_max=args.actor_log_std_max,
            actor_update_freq=args.actor_update_freq,
            critic_lr=args.critic_lr,
            critic_beta=args.critic_beta,
            critic_tau=args.critic_tau,
            critic_target_update_freq=args.critic_target_update_freq,
            encoder_feature_dim=args.encoder_feature_dim,
            encoder_lr=args.encoder_lr,
            encoder_tau=args.encoder_tau,
            use_rot=args.use_rot,
            use_inv=args.use_inv,
            use_curl=args.use_curl,
            ss_lr=args.ss_lr,
            ss_update_freq=args.ss_update_freq,
            batch_size=args.batch_size,
            num_layers=args.num_layers,
            num_shared_layers=args.num_shared_layers,
            num_filters=args.num_filters,
            curl_latent_dim=args.curl_latent_dim,
        )

    return agent


# TODO (chongyi zheng): delete this SAC Agent
class SacSSAgent(object):
    """
    SAC with an auxiliary self-supervised task.
    Based on https://github.com/denisyarats/pytorch_sac_ae
    """
    def __init__(
        self,
        obs_shape,
        action_shape,
        hidden_dim=256,
        discount=0.99,
        init_temperature=0.01,
        alpha_lr=1e-3,
        alpha_beta=0.9,
        actor_lr=1e-3,
        actor_beta=0.9,
        actor_log_std_min=-10,
        actor_log_std_max=2,
        actor_update_freq=2,
        critic_lr=1e-3,
        critic_beta=0.9,
        critic_tau=0.005,
        critic_target_update_freq=2,
        encoder_feature_dim=50,
        encoder_lr=1e-3,
        encoder_tau=0.005,
        use_rot=False,
        use_inv=False,
        use_curl=False,
        ss_lr=1e-3,
        ss_update_freq=1,
        batch_size=128,
        num_layers=4,
        num_shared_layers=4,
        num_filters=32,
        curl_latent_dim=128,
    ):
        self.discount = discount
        self.critic_tau = critic_tau
        self.encoder_tau = encoder_tau
        self.actor_update_freq = actor_update_freq
        self.critic_target_update_freq = critic_target_update_freq
        self.ss_update_freq = ss_update_freq
        self.batch_size = batch_size
        self.use_rot = use_rot
        self.use_inv = use_inv
        self.use_curl = use_curl
        self.curl_latent_dim = curl_latent_dim

        assert num_layers >= num_shared_layers, 'num shared layers cannot exceed total amount'

        self.actor = Actor(
            obs_shape, action_shape, hidden_dim,
            encoder_feature_dim, actor_log_std_min, actor_log_std_max,
            num_layers, num_filters, num_layers
        ).cuda()

        self.critic = Critic(
            obs_shape, action_shape, hidden_dim,
            encoder_feature_dim, num_layers, num_filters, num_layers
        ).cuda()

        self.critic_target = Critic(
            obs_shape, action_shape, hidden_dim,
            encoder_feature_dim, num_layers, num_filters, num_layers
        ).cuda()

        self.critic_target.load_state_dict(self.critic.state_dict())

        # tie encoders between actor and critic
        self.actor.encoder.copy_conv_weights_from(self.critic.encoder)

        self.log_alpha = torch.tensor(np.log(init_temperature)).cuda()
        self.log_alpha.requires_grad = True
        # set target entropy to -|A|
        self.target_entropy = -np.prod(action_shape)

        # self-supervision
        self.rot = None
        self.inv = None
        self.curl = None
        self.ss_encoder = None

        if use_rot or use_inv:
            self.ss_encoder = make_encoder(
                obs_shape, encoder_feature_dim, num_layers,
                num_filters, num_shared_layers
            ).cuda()
            self.ss_encoder.copy_conv_weights_from(self.critic.encoder, num_shared_layers)
            
            # rotation
            if use_rot:
                self.rot = RotFunction(encoder_feature_dim, hidden_dim).cuda()
                self.rot.apply(utils.weight_init)

            # inverse dynamics
            if use_inv:
                self.inv = InvFunction(encoder_feature_dim, action_shape[0], hidden_dim).cuda()
                self.inv.apply(utils.weight_init)
            
        # curl
        if use_curl:
            self.curl = CURL(obs_shape, encoder_feature_dim,
                self.curl_latent_dim, self.critic, self.critic_target, output_type='continuous').cuda()

        # ss optimizers
        self.init_ss_optimizers(encoder_lr, ss_lr)

        # sac optimizers
        self.actor_optimizer = torch.optim.Adam(
            self.actor.parameters(), lr=actor_lr, betas=(actor_beta, 0.999)
        )

        self.critic_optimizer = torch.optim.Adam(
            self.critic.parameters(), lr=critic_lr, betas=(critic_beta, 0.999)
        )

        self.log_alpha_optimizer = torch.optim.Adam(
            [self.log_alpha], lr=alpha_lr, betas=(alpha_beta, 0.999)
        )

        self.train()
        self.critic_target.train()

    def init_ss_optimizers(self, encoder_lr=1e-3, ss_lr=1e-3):
        if self.ss_encoder is not None:
            self.encoder_optimizer = torch.optim.Adam(
                self.ss_encoder.parameters(), lr=encoder_lr
            )
        if self.use_rot:
            self.rot_optimizer = torch.optim.Adam(
                self.rot.parameters(), lr=ss_lr
            )
        if self.use_inv:
            self.inv_optimizer = torch.optim.Adam(
                self.inv.parameters(), lr=ss_lr
            )
        if self.use_curl:
            self.encoder_optimizer = torch.optim.Adam(
                self.critic.encoder.parameters(), lr=encoder_lr
            )
            self.curl_optimizer = torch.optim.Adam(
                self.curl.parameters(), lr=ss_lr
            )
    
    def train(self, training=True):
        self.training = training
        self.actor.train(training)
        self.critic.train(training)
        if self.ss_encoder is not None:
            self.ss_encoder.train(training)
        if self.rot is not None:
            self.rot.train(training)
        if self.inv is not None:
            self.inv.train(training)
        if self.curl is not None:
            self.curl.train(training)

    @property
    def alpha(self):
        return self.log_alpha.exp()

    def select_action(self, obs):
        with torch.no_grad():
            obs = torch.FloatTensor(obs).cuda()
            obs = obs.unsqueeze(0)
            mu, _, _, _ = self.actor(
                obs, compute_pi=False, compute_log_pi=False
            )
            return mu.cpu().data.numpy().flatten()

    def sample_action(self, obs):
        with torch.no_grad():
            obs = torch.FloatTensor(obs).cuda()
            obs = obs.unsqueeze(0)
            mu, pi, _, _ = self.actor(obs, compute_log_pi=False)
            return pi.cpu().data.numpy().flatten()

    def update_critic(self, obs, action, reward, next_obs, not_done, L, step):
        with torch.no_grad():
            _, policy_action, log_pi, _ = self.actor(next_obs)
            target_Q1, target_Q2 = self.critic_target(next_obs, policy_action)
            target_V = torch.min(target_Q1,
                                 target_Q2) - self.alpha.detach() * log_pi
            target_Q = reward + (not_done * self.discount * target_V)

        # get current Q estimates
        current_Q1, current_Q2 = self.critic(obs, action)
        critic_loss = F.mse_loss(current_Q1,
                                 target_Q) + F.mse_loss(current_Q2, target_Q)
        L.log('train_critic/loss', critic_loss, step)

        # Optimize the critic
        self.critic_optimizer.zero_grad()
        critic_loss.backward()
        self.critic_optimizer.step()


    def update_actor_and_alpha(self, obs, L=None, step=None, update_alpha=True):
        # detach encoder, so we don't update it with the actor loss
        _, pi, log_pi, log_std = self.actor(obs, detach_encoder=True)
        actor_Q1, actor_Q2 = self.critic(obs, pi, detach_encoder=True)

        actor_Q = torch.min(actor_Q1, actor_Q2)
        actor_loss = (self.alpha.detach() * log_pi - actor_Q).mean()

        if L is not None:
            L.log('train_actor/loss', actor_loss, step)
            entropy = 0.5 * log_std.shape[1] * (1.0 + np.log(2 * np.pi)
                                                ) + log_std.sum(dim=-1)

        # optimize the actor
        self.actor_optimizer.zero_grad()
        actor_loss.backward()
        self.actor_optimizer.step()

        if update_alpha:
            self.log_alpha_optimizer.zero_grad()
            alpha_loss = (self.alpha * (-log_pi - self.target_entropy).detach()).mean()

            if L is not None:
                L.log('train_alpha/loss', alpha_loss, step)
                L.log('train_alpha/value', self.alpha, step)

            alpha_loss.backward()
            self.log_alpha_optimizer.step()

    def update_rot(self, obs, L=None, step=None):
        assert obs.shape[-1] == 84

        obs, label = utils.rotate(obs)
        h = self.ss_encoder(obs)
        pred_rotation = self.rot(h)
        rot_loss = F.cross_entropy(pred_rotation, label)

        self.encoder_optimizer.zero_grad()
        self.rot_optimizer.zero_grad()
        rot_loss.backward()

        self.encoder_optimizer.step()
        self.rot_optimizer.step()

        if L is not None:
            L.log('train_rot/rot_loss', rot_loss, step)

        return rot_loss.item()

    def update_inv(self, obs, next_obs, action, L=None, step=None):
        assert obs.shape[-1] == 84 and next_obs.shape[-1] == 84

        h = self.ss_encoder(obs)
        h_next = self.ss_encoder(next_obs)

        pred_action = self.inv(h, h_next)
        inv_loss = F.mse_loss(pred_action, action)

        self.encoder_optimizer.zero_grad()
        self.inv_optimizer.zero_grad()
        inv_loss.backward()

        self.encoder_optimizer.step()
        self.inv_optimizer.step()

        if L is not None:
            L.log('train_inv/inv_loss', inv_loss, step)

        return inv_loss.item()

    def update_curl(self, obs_anchor, obs_pos, L=None, step=None, ema=False):
        assert obs_anchor.shape[-1] == 84 and obs_pos.shape[-1] == 84

        z_a = self.curl.encode(obs_anchor)
        z_pos = self.curl.encode(obs_pos, ema=True)
        
        logits = self.curl.compute_logits(z_a, z_pos)
        labels = torch.arange(logits.shape[0]).long().cuda()
        curl_loss = F.cross_entropy(logits, labels)
        
        self.encoder_optimizer.zero_grad()
        self.curl_optimizer.zero_grad()
        curl_loss.backward()

        self.encoder_optimizer.step()
        self.curl_optimizer.step()
        if L is not None:
            L.log('train/curl_loss', curl_loss, step)

        if ema:
            utils.soft_update_params(
                self.critic.encoder, self.critic_target.encoder,
                self.encoder_tau
            )

        return curl_loss.item()

    def update(self, replay_buffer, L, step):
        if self.use_curl:
            obs, action, reward, next_obs, not_done, curl_kwargs = replay_buffer.sample_curl(self.batch_size)
        else:
            obs, action, reward, next_obs, not_done = replay_buffer.sample(self.batch_size)
        
        L.log('train/batch_reward', reward.mean(), step)

        self.update_critic(obs, action, reward, next_obs, not_done, L, step)

        if step % self.actor_update_freq == 0:
            self.update_actor_and_alpha(obs, L, step)

        if step % self.critic_target_update_freq == 0:
            utils.soft_update_params(
                self.critic.Q1, self.critic_target.Q1, self.critic_tau
            )
            utils.soft_update_params(
                self.critic.Q2, self.critic_target.Q2, self.critic_tau
            )
            utils.soft_update_params(
                self.critic.encoder, self.critic_target.encoder,
                self.encoder_tau
            )
        
        if self.rot is not None and step % self.ss_update_freq == 0:
            self.update_rot(obs, L, step)

        if self.inv is not None and step % self.ss_update_freq == 0:
            self.update_inv(obs, next_obs, action, L, step)

        if self.curl is not None and step % self.ss_update_freq == 0:
            obs_anchor, obs_pos = curl_kwargs["obs_anchor"], curl_kwargs["obs_pos"]
            self.update_curl(obs_anchor, obs_pos, L, step)

    def save(self, model_dir, step):
        torch.save(
            self.actor.state_dict(), '%s/actor_%s.pt' % (model_dir, step)
        )
        torch.save(
            self.critic.state_dict(), '%s/critic_%s.pt' % (model_dir, step)
        )
        if self.rot is not None:
            torch.save(
                self.rot.state_dict(),
                '%s/rot_%s.pt' % (model_dir, step)
            )
        if self.inv is not None:
            torch.save(
                self.inv.state_dict(),
                '%s/inv_%s.pt' % (model_dir, step)
            )
        if self.curl is not None:
            torch.save(
                self.curl.state_dict(),
                '%s/curl_%s.pt' % (model_dir, step)
            )
        if self.ss_encoder is not None:
            torch.save(
                self.ss_encoder.state_dict(),
                '%s/ss_encoder_%s.pt' % (model_dir, step)
            )

    def load(self, model_dir, step):
        self.actor.load_state_dict(
            torch.load('%s/actor_%s.pt' % (model_dir, step))
        )
        self.critic.load_state_dict(
            torch.load('%s/critic_%s.pt' % (model_dir, step))
        )
        if self.rot is not None:
            self.rot.load_state_dict(
                torch.load('%s/rot_%s.pt' % (model_dir, step))
            )
        if self.inv is not None:
            self.inv.load_state_dict(
                torch.load('%s/inv_%s.pt' % (model_dir, step))
            )
        if self.curl is not None:
            self.curl.load_state_dict(
                torch.load('%s/curl_%s.pt' % (model_dir, step))
            )
        if self.ss_encoder is not None:
            self.ss_encoder.load_state_dict(
                torch.load('%s/ss_encoder_%s.pt' % (model_dir, step))
            )


class SacSSEnsembleAgent:
    def __init__(
            self,
            obs_shape,
            action_shape,
            hidden_dim=256,
            discount=0.99,
            init_temperature=0.01,
            alpha_lr=1e-3,
            alpha_beta=0.9,
            actor_lr=1e-3,
            actor_beta=0.9,
            actor_log_std_min=-10,
            actor_log_std_max=2,
            actor_update_freq=2,
            critic_lr=1e-3,
            critic_beta=0.9,
            critic_tau=0.005,
            critic_target_update_freq=2,
            encoder_feature_dim=50,
            encoder_lr=1e-3,
            encoder_tau=0.005,
            use_fwd=False,
            use_inv=False,
            ss_lr=1e-3,
            ss_update_freq=1,
            ss_stop_shared_layers_grad=False,  # (chongyi zheng)
            batch_size=128,  # (chongyi zheng)
            num_layers=4,
            num_shared_layers=4,
            num_filters=32,
            curl_latent_dim=128,
            num_ensem_comps=4,  # (chongyi zheng)
    ):
        self.discount = discount
        self.critic_tau = critic_tau
        self.encoder_tau = encoder_tau
        self.actor_update_freq = actor_update_freq
        self.critic_target_update_freq = critic_target_update_freq
        self.ss_update_freq = ss_update_freq
        self.ss_stop_shared_layers_grad = ss_stop_shared_layers_grad
        self.batch_size = batch_size
        self.use_fwd = use_fwd
        self.use_inv = use_inv
        self.curl_latent_dim = curl_latent_dim
        self.num_ensem_comps = num_ensem_comps

        assert num_layers >= num_shared_layers, 'num shared layers cannot exceed total amount'

        self.actor = Actor(
            obs_shape, action_shape, hidden_dim,
            encoder_feature_dim, actor_log_std_min, actor_log_std_max,
            num_layers, num_filters, num_layers
        ).cuda()

        self.critic = Critic(
            obs_shape, action_shape, hidden_dim,
            encoder_feature_dim, num_layers, num_filters, num_layers
        ).cuda()

        self.critic_target = Critic(
            obs_shape, action_shape, hidden_dim,
            encoder_feature_dim, num_layers, num_filters, num_layers
        ).cuda()

        self.critic_target.load_state_dict(self.critic.state_dict())

        # tie encoders between actor and critic
        self.actor.encoder.copy_conv_weights_from(self.critic.encoder)

        self.log_alpha = torch.tensor(np.log(init_temperature)).cuda()
        self.log_alpha.requires_grad = True
        # set target entropy to -|A|
        self.target_entropy = -np.prod(action_shape)

        # self-supervision ensembles
        self.ss_encoders = []
        self.fwds = []
        self.invs = []
        self.encoder_optimizer = None
        self.fwd_optimizer = None
        self.inv_optimizer = None

        if self.use_inv or self.use_fwd:
            # self-supervised encoder ensemble
            for _ in range(self.num_ensem_comps):
                ss_encoder = make_encoder(
                    obs_shape, encoder_feature_dim, num_layers,
                    num_filters, num_shared_layers
                ).cuda()
                ss_encoder.copy_conv_weights_from(self.critic.encoder, num_shared_layers)
                self.ss_encoders.append(ss_encoder)

            if self.use_fwd:
                # forward dynamics predictor ensemble
                for _ in range(self.num_ensem_comps):
                    fwd = FwdFunction(encoder_feature_dim, action_shape[0], hidden_dim).cuda()
                    # fwd.apply(weight_init)
                    self.fwds.append(fwd)

            if self.use_inv:
                # inverse dynamics predictor ensemble
                for _ in range(self.num_ensem_comps):
                    inv = InvFunction(encoder_feature_dim, action_shape[0], hidden_dim).cuda()
                    # inv.apply(weight_init)  # different initialization for each component
                    self.invs.append(inv)

        # ss optimizers
        self.init_ss_optimizers(encoder_lr, ss_lr)

        # sac optimizers
        self.actor_optimizer = torch.optim.Adam(
            self.actor.parameters(), lr=actor_lr, betas=(actor_beta, 0.999)
        )

        self.critic_optimizer = torch.optim.Adam(
            self.critic.parameters(), lr=critic_lr, betas=(critic_beta, 0.999)
        )

        self.log_alpha_optimizer = torch.optim.Adam(
            [self.log_alpha], lr=alpha_lr, betas=(alpha_beta, 0.999)
        )

        self.train()
        self.critic_target.train()

    def init_ss_optimizers(self, encoder_lr=1e-3, ss_lr=1e-3):
        if len(self.ss_encoders) > 0:
            ss_encoder_params = []
            for ss_encoder in self.ss_encoders:
                ss_encoder_params += list(ss_encoder.parameters())
            self.encoder_optimizer = torch.optim.Adam(ss_encoder_params, lr=encoder_lr)
        if self.use_fwd:
            fwd_params = []
            for fwd in self.fwds:
                fwd_params += list(fwd.parameters())
            self.fwd_optimizer = torch.optim.Adam(fwd_params, lr=ss_lr)

        if self.use_inv:
            inv_params = []
            for inv in self.invs:
                inv_params += list(inv.parameters())
            self.inv_optimizer = torch.optim.Adam(inv_params, lr=ss_lr)

    def train(self, training=True):
        self.training = training
        self.actor.train(training)
        self.critic.train(training)
        if len(self.ss_encoders) > 0:
            for ss_encoder in self.ss_encoders:
                ss_encoder.train(training)
        if self.use_fwd:
            for fwd in self.fwds:
                fwd.train(training)
        if self.use_inv:
            for inv in self.invs:
                inv.train(training)

    @property
    def alpha(self):
        return self.log_alpha.exp()

    def select_action(self, obs):
        with torch.no_grad():
            obs = torch.FloatTensor(obs).cuda()
            obs = obs.unsqueeze(0)
            mu, _, _, _ = self.actor(
                obs, compute_pi=False, compute_log_pi=False
            )
            return mu.cpu().data.numpy().flatten()

    def sample_action(self, obs):
        with torch.no_grad():
            obs = torch.FloatTensor(obs).cuda()
            obs = obs.unsqueeze(0)
            mu, pi, _, _ = self.actor(obs, compute_log_pi=False)
            return pi.cpu().data.numpy().flatten()

    def ss_preds_var(self, obs, next_obs, action):
        # TODO (chongyi zheng):
        #  do we need next_obs (forward) or action (inverse) - measure the prediction error,
        #  or we just need to predictions - measure the prediction variance?
        #  Maybe statistical hypothesis testing like: https://arxiv.org/abs/1902.09434
        assert obs.shape == next_obs.shape and obs.shape[0] == next_obs.shape[0] == action.shape[0], \
            "invalid transitions shapes!"

        with torch.no_grad():
            obs = torch.FloatTensor(obs).cuda() if not isinstance(obs, torch.Tensor) else obs.cuda()
            next_obs = torch.FloatTensor(next_obs).cuda() if not isinstance(next_obs, torch.Tensor) else next_obs.cuda()
            action = torch.FloatTensor(action).cuda() if not isinstance(action, torch.Tensor) else action.cuda()

            if len(obs.size()) == 3:
                obs = obs.unsqueeze(0)
                next_obs = next_obs.unsqueeze(0)
                action = action.unsqueeze(0)

            # prediction variances
            preds = []
            if self.use_fwd:
                for ss_encoder, fwd in zip(self.ss_encoders, self.fwds):
                    h = ss_encoder(obs)

                    preds.append(fwd(h, action))

            if self.use_inv:
                for ss_encoder, inv in zip(self.ss_encoders, self.invs):
                    h = ss_encoder(obs)
                    h_next = ss_encoder(next_obs)

                    preds.append(inv(h, h_next))

            # (chongyi zheng): the same as equation (1) in https://arxiv.org/abs/1906.04161
            pred_vars = torch.var(torch.stack(preds), dim=0).sum(dim=-1)

            return pred_vars.cpu().data.numpy()

    def update_critic(self, obs, action, reward, next_obs, not_done, L, step):
        with torch.no_grad():
            _, policy_action, log_pi, _ = self.actor(next_obs)
            target_Q1, target_Q2 = self.critic_target(next_obs, policy_action)
            target_V = torch.min(target_Q1,
                                 target_Q2) - self.alpha.detach() * log_pi
            target_Q = reward + (not_done * self.discount * target_V)

        # get current Q estimates
        current_Q1, current_Q2 = self.critic(obs, action)
        critic_loss = F.mse_loss(current_Q1, target_Q) + F.mse_loss(current_Q2, target_Q)
        L.log('train_critic/loss', critic_loss, step)

        # Optimize the critic
        self.critic_optimizer.zero_grad()
        critic_loss.backward()
        self.critic_optimizer.step()

    def update_actor_and_alpha(self, obs, L=None, step=None, update_alpha=True):
        # detach encoder, so we don't update it with the actor loss
        _, pi, log_pi, log_std = self.actor(obs, detach_encoder=True)
        actor_Q1, actor_Q2 = self.critic(obs, pi, detach_encoder=True)

        actor_Q = torch.min(actor_Q1, actor_Q2)
        actor_loss = (self.alpha.detach() * log_pi - actor_Q).mean()

        entropy = 0.5 * log_std.shape[1] * (1.0 + np.log(2 * np.pi)
                                            ) + log_std.sum(dim=-1)

        if L is not None:
            L.log('train_actor/loss', actor_loss, step)

        # optimize the actor
        self.actor_optimizer.zero_grad()
        actor_loss.backward()
        self.actor_optimizer.step()

        if update_alpha:
            self.log_alpha_optimizer.zero_grad()
            alpha_loss = (self.alpha * (-log_pi - self.target_entropy).detach()).mean()

            if L is not None:
                L.log('train_alpha/loss', alpha_loss, step)
                L.log('train_alpha/value', self.alpha, step)

            alpha_loss.backward()
            self.log_alpha_optimizer.step()

    def update_ss_preds(self, obs, next_obs, action, L=None, step=None):
        assert obs.shape[-1] == 84 and next_obs.shape[-1] == 84

        fwd_losses = []
        inv_losses = []
        # (chongyi zheng): split transitions for each component of the ensemble
        num_samples_each_slice = obs.shape[0] // self.num_ensem_comps
        for idx, (ss_encoder, fwd, inv) in enumerate(itertools.zip_longest(self.ss_encoders, self.fwds, self.invs)):
            obs_slice = obs[idx * num_samples_each_slice:(idx + 1) * num_samples_each_slice]
            next_obs_slice = next_obs[idx * num_samples_each_slice:(idx + 1) * num_samples_each_slice]
            action_slice = action[idx * num_samples_each_slice:(idx + 1) * num_samples_each_slice]

            if self.use_fwd:
                # only back propagate the gradients from first predictor in the ensemble to shared encoder
                if self.ss_stop_shared_layers_grad and idx != 0:
                    h = ss_encoder(obs_slice, detach=True)
                else:
                    h = ss_encoder(obs_slice)
                h_next = ss_encoder(next_obs_slice).detach()  # stop all gradients

                pred_h_next = fwd(h, action_slice)
                fwd_loss = F.mse_loss(pred_h_next, h_next)
                fwd_losses.append(fwd_loss)

            if self.use_inv:
                # only back propagate the gradients from first predictor in the ensemble to shared encoder
                if self.ss_stop_shared_layers_grad and idx != 0:
                    h = ss_encoder(obs_slice, detach=True)
                    h_next = ss_encoder(next_obs_slice, detach=True)
                else:
                    h = ss_encoder(obs_slice)
                    h_next = ss_encoder(next_obs_slice)

                pred_action = inv(h, h_next)
                inv_loss = F.mse_loss(pred_action, action_slice)
                inv_losses.append(inv_loss)

            if L is not None:
                if self.use_fwd:
                    L.log('train_fwd_ensem/loss_{}'.format(idx), fwd_loss, step)
                if self.use_inv:
                    L.log('train_inv_ensem/loss_{}'.format(idx), inv_loss, step)

        if self.use_fwd:
            mean_fwd_loss = torch.mean(torch.stack(fwd_losses))
            self.encoder_optimizer.zero_grad()
            self.fwd_optimizer.zero_grad()
            mean_fwd_loss.backward()

            self.encoder_optimizer.step()
            self.fwd_optimizer.step()

        if self.use_inv:
            mean_inv_loss = torch.mean(torch.stack(inv_losses))
            self.encoder_optimizer.zero_grad()
            self.inv_optimizer.zero_grad()
            mean_inv_loss.backward()

            self.encoder_optimizer.step()
            self.inv_optimizer.step()

    def update(self, replay_buffer, L, step):
        obs, action, reward, next_obs, not_done, ensem_kwargs = replay_buffer.sample_ensembles(
            self.batch_size, num_ensembles=self.num_ensem_comps)
        # obs, action, reward, next_obs, not_done = replay_buffer.sample(self.batch_size)

        L.log('train/batch_reward', reward.mean(), step)

        self.update_critic(obs, action, reward, next_obs, not_done, L, step)

        if step % self.actor_update_freq == 0:
            self.update_actor_and_alpha(obs, L, step)

        if step % self.critic_target_update_freq == 0:
            utils.soft_update_params(
                self.critic.Q1, self.critic_target.Q1, self.critic_tau
            )
            utils.soft_update_params(
                self.critic.Q2, self.critic_target.Q2, self.critic_tau
            )
            utils.soft_update_params(
                self.critic.encoder, self.critic_target.encoder,
                self.encoder_tau
            )

        if (self.use_fwd or self.use_inv) and step % self.ss_update_freq == 0:
            self.update_ss_preds(ensem_kwargs['obses'], ensem_kwargs['next_obses'], ensem_kwargs['actions'], L, step)
            # self.update_ss_preds(obs, next_obs, action, L, step)
            with torch.no_grad():
                ss_preds_var = self.ss_preds_var(obs, next_obs, action)
                L.log('train/batch_ss_preds_var', ss_preds_var.mean(), step)

    def save(self, model_dir, step):
        torch.save(
            self.actor.state_dict(), '%s/actor_%s.pt' % (model_dir, step)
        )
        torch.save(
            self.critic.state_dict(), '%s/critic_%s.pt' % (model_dir, step)
        )
        if len(self.ss_encoders) > 0:
            for idx, ss_encoder in enumerate(self.ss_encoders):
                torch.save(
                    ss_encoder.state_dict(),
                    '%s/ss_encoder_comp%d_%s.pt' % (model_dir, idx, step)
                )
        if self.use_fwd:
            for idx, fwd in enumerate(self.fwds):
                torch.save(
                    fwd.state_dict(),
                    '%s/fwd_comp%d_%s.pt' % (model_dir, idx, step)
                )
        if self.use_inv:
            for idx, inv in enumerate(self.invs):
                torch.save(
                    inv.state_dict(),
                    '%s/inv_comp%d_%s.pt' % (model_dir, idx, step)
                )

    def load(self, model_dir, step):
        self.actor.load_state_dict(
            torch.load('%s/actor_%s.pt' % (model_dir, step))
        )
        self.critic.load_state_dict(
            torch.load('%s/critic_%s.pt' % (model_dir, step))
        )
        if len(self.ss_encoders) > 0:
            for idx, ss_encoder in enumerate(self.ss_encoders):
                ss_encoder.load_state_dict(
                    torch.load('%s/ss_encoder_comp%d_%s.pt' % (model_dir, idx, step))
                )
        if self.use_fwd:
            for idx, fwd in enumerate(self.fwds):
                fwd.load_state_dict(
                    torch.load('%s/fwd_comp%d_%s.pt' % (model_dir, idx, step))
                )
        if self.use_inv:
            for idx, inv in enumerate(self.invs):
                inv.load_state_dict(
                    torch.load('%s/inv_comp%d_%s.pt' % (model_dir, idx, step))
                )


class DrQSACSSEnsembleAgent:
    def __init__(
        self,
        obs_shape,
        action_shape,
        action_range,
        device,
        hidden_dim=256,
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
        encoder_feature_dim=50,
        encoder_lr=1e-3,
        encoder_tau=0.005,
        use_fwd=False,
        use_inv=False,
        ss_lr=1e-3,
        ss_update_freq=1,
        ss_stop_shared_layers_grad=False,  # (chongyi zheng)
        batch_size=128,  # (chongyi zheng)
        num_layers=4,
        num_filters=32,
        curl_latent_dim=128,
        num_ensem_comps=4,  # (chongyi zheng)
    ):
        self.action_range = action_range
        self.device = device
        self.discount = discount
        self.critic_tau = critic_tau
        self.encoder_tau = encoder_tau
        self.actor_update_freq = actor_update_freq
        self.critic_target_update_freq = critic_target_update_freq
        self.ss_update_freq = ss_update_freq
        self.ss_stop_shared_layers_grad = ss_stop_shared_layers_grad
        self.batch_size = batch_size
        self.use_fwd = use_fwd
        self.use_inv = use_inv
        self.curl_latent_dim = curl_latent_dim
        self.num_ensem_comps = num_ensem_comps

        self.actor = Actor(
            obs_shape, action_shape, hidden_dim,
            encoder_feature_dim, actor_log_std_min, actor_log_std_max,
            num_layers, num_filters
        ).to(self.device)

        self.critic = Critic(
            obs_shape, action_shape, hidden_dim,
            encoder_feature_dim, num_layers, num_filters
        ).to(self.device)

        self.critic_target = Critic(
            obs_shape, action_shape, hidden_dim,
            encoder_feature_dim, num_layers, num_filters
        ).to(self.device)

        self.critic_target.load_state_dict(self.critic.state_dict())

        # tie encoders between actor and critic
        self.actor.encoder.copy_conv_weights_from(self.critic.encoder)

        self.log_alpha = torch.tensor(np.log(init_temperature)).to(self.device)
        self.log_alpha.requires_grad = True
        # set target entropy to -|A|
        self.target_entropy = -np.prod(action_shape)

        # self-supervision ensembles
        self.ss_encoders = []
        self.fwds = []
        self.invs = []
        self.encoder_optimizer = None
        self.fwd_optimizer = None
        self.inv_optimizer = None

        # TODO (chongyi zheng): update this code block
        if self.use_inv or self.use_fwd:
            # self-supervised encoder ensemble
            for _ in range(self.num_ensem_comps):
                ss_encoder = make_encoder(
                    obs_shape, encoder_feature_dim, num_layers,
                    num_filters
                ).to(self.device)
                ss_encoder.copy_conv_weights_from(self.critic.encoder)
                self.ss_encoders.append(ss_encoder)

            if self.use_fwd:
                # forward dynamics predictor ensemble
                for _ in range(self.num_ensem_comps):
                    fwd = FwdFunction(encoder_feature_dim, action_shape[0], hidden_dim).cuda()
                    # fwd.apply(weight_init)
                    self.fwds.append(fwd)

            if self.use_inv:
                # inverse dynamics predictor ensemble
                for _ in range(self.num_ensem_comps):
                    inv = InvFunction(encoder_feature_dim, action_shape[0], hidden_dim).cuda()
                    # inv.apply(weight_init)  # different initialization for each component
                    self.invs.append(inv)

        # ss optimizers
        self.init_ss_optimizers(encoder_lr, ss_lr)

        # sac optimizers
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=actor_lr)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=critic_lr)

        self.log_alpha_optimizer = torch.optim.Adam([self.log_alpha], lr=alpha_lr)

        self.train()
        self.critic_target.train()

    def init_ss_optimizers(self, encoder_lr=1e-3, ss_lr=1e-3):
        if len(self.ss_encoders) > 0:
            ss_encoder_params = []
            for ss_encoder in self.ss_encoders:
                ss_encoder_params += list(ss_encoder.parameters())
            self.encoder_optimizer = torch.optim.Adam(ss_encoder_params, lr=encoder_lr)
        if self.use_fwd:
            fwd_params = []
            for fwd in self.fwds:
                fwd_params += list(fwd.parameters())
            self.fwd_optimizer = torch.optim.Adam(fwd_params, lr=ss_lr)

        if self.use_inv:
            inv_params = []
            for inv in self.invs:
                inv_params += list(inv.parameters())
            self.inv_optimizer = torch.optim.Adam(inv_params, lr=ss_lr)

    def train(self, training=True):
        self.training = training
        self.actor.train(training)
        self.critic.train(training)

        # TODO (chongyi zheng): update this code block
        if len(self.ss_encoders) > 0:
            for ss_encoder in self.ss_encoders:
                ss_encoder.train(training)
        if self.use_fwd:
            for fwd in self.fwds:
                fwd.train(training)
        if self.use_inv:
            for inv in self.invs:
                inv.train(training)

    @property
    def alpha(self):
        return self.log_alpha.exp()

    def act(self, obs, sample=False):
        obs = torch.FloatTensor(obs).to(self.device)
        obs = obs.unsqueeze(0)
        dist = self.actor(obs)
        action = dist.sample() if sample else dist.mean
        action = action.clamp(*self.action_range)
        assert action.ndim == 2 and action.shape[0] == 1
        return utils.to_np(action[0])

    # TODO (chongyi zheng): update this function
    def ss_preds_var(self, obs, next_obs, action):
        # TODO (chongyi zheng):
        #  do we need next_obs (forward) or action (inverse) - measure the prediction error,
        #  or we just need to predictions - measure the prediction variance?
        #  task inference: threshold or statistical hypothesis testing like: https://arxiv.org/abs/1902.09434
        assert obs.shape == next_obs.shape and obs.shape[0] == next_obs.shape[0] == action.shape[0], \
            "invalid transitions shapes!"

        with torch.no_grad():
            obs = torch.FloatTensor(obs).cuda() if not isinstance(obs, torch.Tensor) else obs.cuda()
            next_obs = torch.FloatTensor(next_obs).cuda() if not isinstance(next_obs, torch.Tensor) else next_obs.cuda()
            action = torch.FloatTensor(action).cuda() if not isinstance(action, torch.Tensor) else action.cuda()

            if len(obs.size()) == 3:
                obs = obs.unsqueeze(0)
                next_obs = next_obs.unsqueeze(0)
                action = action.unsqueeze(0)

            # prediction variances
            preds = []
            if self.use_fwd:
                for ss_encoder, fwd in zip(self.ss_encoders, self.fwds):
                    h = ss_encoder(obs)

                    preds.append(fwd(h, action))

            if self.use_inv:
                for ss_encoder, inv in zip(self.ss_encoders, self.invs):
                    h = ss_encoder(obs)
                    h_next = ss_encoder(next_obs)

                    preds.append(inv(h, h_next))

            # (chongyi zheng): the same as equation (1) in https://arxiv.org/abs/1906.04161
            pred_vars = torch.var(torch.stack(preds), dim=0).sum(dim=-1)

            return pred_vars.cpu().data.numpy()

    def update_critic(self, obs, obs_aug, action, reward, next_obs, next_obs_aug,
                      not_done, L, step):
        with torch.no_grad():
            dist = self.actor(next_obs)
            next_action = dist.rsample()
            log_prob = dist.log_prob(next_action).sum(-1, keepdim=True)
            target_Q1, target_Q2 = self.critic_target(next_obs, next_action)
            target_V = torch.min(target_Q1, target_Q2) - self.alpha.detach() * log_prob
            target_Q = reward + (not_done * self.discount * target_V)

            dist_aug = self.actor(next_obs_aug)
            next_action_aug = dist_aug.rsample()
            log_prob_aug = dist_aug.log_prob(next_action_aug).sum(-1, keepdim=True)
            target_Q1, target_Q2 = self.critic_target(next_obs_aug, next_action_aug)
            target_V = torch.min(target_Q1, target_Q2) - self.alpha.detach() * log_prob_aug
            target_Q_aug = reward + (not_done * self.discount * target_V)

            target_Q = (target_Q + target_Q_aug) / 2  # K = 2

        # get current Q estimates
        current_Q1, current_Q2 = self.critic(obs, action)
        critic_loss = F.mse_loss(current_Q1, target_Q) + F.mse_loss(current_Q2, target_Q)

        Q1_aug, Q2_aug = self.critic(obs_aug, action)
        critic_loss += F.mse_loss(Q1_aug, target_Q) + F.mse_loss(Q2_aug, target_Q)  # M = 2
        L.log('train_critic/loss', critic_loss, step)

        # Optimize the critic
        self.critic_optimizer.zero_grad()
        critic_loss.backward()
        self.critic_optimizer.step()

        self.critic.log(L, step)

    def update_actor_and_alpha(self, obs, L, step, update_alpha=True):
        # detach conv filters, so we don't update them with the actor loss
        dist = self.actor(obs, detach_encoder=True)
        action = dist.rsample()
        log_prob = dist.log_prob(action).sum(-1, keepdim=True)
        # detach conv filters, so we don't update them with the actor loss
        actor_Q1, actor_Q2 = self.critic(obs, action, detach_encoder=True)

        actor_Q = torch.min(actor_Q1, actor_Q2)
        actor_loss = (self.alpha.detach() * log_prob - actor_Q).mean()

        L.log('train_actor/loss', actor_loss, step)
        L.log('train_actor/target_entropy', self.target_entropy, step)
        L.log('train_actor/entropy', -log_prob.mean(), step)

        # optimize the actor
        self.actor_optimizer.zero_grad()
        actor_loss.backward()
        self.actor_optimizer.step()

        self.actor.log(L, step)

        if update_alpha:
            self.log_alpha_optimizer.zero_grad()
            alpha_loss = (self.alpha * (-log_prob - self.target_entropy).detach()).mean()

            L.log('train_alpha/loss', alpha_loss, step)
            L.log('train_alpha/value', self.alpha, step)

            alpha_loss.backward()
            self.log_alpha_optimizer.step()

    # TODO (chongyi zheng): update this function
    def update_ss_preds(self, obs, next_obs, action, L, step):
        assert obs.shape[-1] == 84 and next_obs.shape[-1] == 84

        fwd_losses = []
        inv_losses = []
        # (chongyi zheng): split transitions for each component of the ensemble
        num_samples_each_slice = obs.shape[0] // self.num_ensem_comps
        for idx, (ss_encoder, fwd, inv) in enumerate(itertools.zip_longest(self.ss_encoders, self.fwds, self.invs)):
            obs_slice = obs[idx * num_samples_each_slice:(idx + 1) * num_samples_each_slice]
            next_obs_slice = next_obs[idx * num_samples_each_slice:(idx + 1) * num_samples_each_slice]
            action_slice = action[idx * num_samples_each_slice:(idx + 1) * num_samples_each_slice]

            if self.use_fwd:
                # only back propagate the gradients from first predictor in the ensemble to shared encoder
                if self.ss_stop_shared_layers_grad and idx != 0:
                    h = ss_encoder(obs_slice, detach=True)
                else:
                    h = ss_encoder(obs_slice)
                h_next = ss_encoder(next_obs_slice).detach()  # stop all gradients

                pred_h_next = fwd(h, action_slice)
                fwd_loss = F.mse_loss(pred_h_next, h_next)
                fwd_losses.append(fwd_loss)

            if self.use_inv:
                # only back propagate the gradients from first predictor in the ensemble to shared encoder
                if self.ss_stop_shared_layers_grad and idx != 0:
                    h = ss_encoder(obs_slice, detach=True)
                    h_next = ss_encoder(next_obs_slice, detach=True)
                else:
                    h = ss_encoder(obs_slice)
                    h_next = ss_encoder(next_obs_slice)

                pred_action = inv(h, h_next)
                inv_loss = F.mse_loss(pred_action, action_slice)
                inv_losses.append(inv_loss)

            if self.use_fwd:
                L.log('train_fwd_ensem/loss_{}'.format(idx), fwd_loss, step)
            if self.use_inv:
                L.log('train_inv_ensem/loss_{}'.format(idx), inv_loss, step)

        if self.use_fwd:
            mean_fwd_loss = torch.mean(torch.stack(fwd_losses))
            self.encoder_optimizer.zero_grad()
            self.fwd_optimizer.zero_grad()
            mean_fwd_loss.backward()

            self.encoder_optimizer.step()
            self.fwd_optimizer.step()

        if self.use_inv:
            mean_inv_loss = torch.mean(torch.stack(inv_losses))
            self.encoder_optimizer.zero_grad()
            self.inv_optimizer.zero_grad()
            mean_inv_loss.backward()

            self.encoder_optimizer.step()
            self.inv_optimizer.step()

    def update(self, replay_buffer, L, step):
        # obs, action, reward, next_obs, not_done, ensem_kwargs = replay_buffer.sample_ensembles(
        #     self.batch_size, num_ensembles=self.num_ensem_comps)
        # obs, action, reward, next_obs, not_done = replay_buffer.sample(self.batch_size)
        obs, action, reward, next_obs, not_done, obs_aug, next_obs_aug = replay_buffer.sample(
            self.batch_size)

        L.log('train/batch_reward', reward.mean(), step)

        self.update_critic(obs, obs_aug, action, reward, next_obs,
                           next_obs_aug, not_done, L, step)

        # TODO (chongyi zheng): which version is better?
        # if step % self.actor_update_freq == 0:
        #     self.update_actor_and_alpha(obs, L, step)
        #
        # if step % self.critic_target_update_freq == 0:
        #     utils.soft_update_params(
        #         self.critic.Q1, self.critic_target.Q1, self.critic_tau
        #     )
        #     utils.soft_update_params(
        #         self.critic.Q2, self.critic_target.Q2, self.critic_tau
        #     )
        #     utils.soft_update_params(
        #         self.critic.encoder, self.critic_target.encoder,
        #         self.encoder_tau
        #     )
        if step % self.actor_update_freq == 0:
            self.update_actor_and_alpha(obs, L, step)

        if step % self.critic_target_update_freq == 0:
            utils.soft_update_params(self.critic, self.critic_target,
                                     self.critic_tau)

        if (self.use_fwd or self.use_inv) and step % self.ss_update_freq == 0:
            # self.update_ss_preds(ensem_kwargs['obses'], ensem_kwargs['next_obses'], ensem_kwargs['actions'], L, step)
            self.update_ss_preds(obs, next_obs, action, L, step)
            ss_preds_var = self.ss_preds_var(obs, next_obs, action)
            L.log('train/batch_ss_preds_var', ss_preds_var.mean(), step)

    def save(self, model_dir, step):
        torch.save(
            self.actor.state_dict(), '%s/actor_%s.pt' % (model_dir, step)
        )
        torch.save(
            self.critic.state_dict(), '%s/critic_%s.pt' % (model_dir, step)
        )
        if len(self.ss_encoders) > 0:
            for idx, ss_encoder in enumerate(self.ss_encoders):
                torch.save(
                    ss_encoder.state_dict(),
                    '%s/ss_encoder_ensem%d_%s.pt' % (model_dir, idx, step)
                )
        if self.use_fwd:
            for idx, fwd in enumerate(self.fwds):
                torch.save(
                    fwd.state_dict(),
                    '%s/fwd_ensem%d_%s.pt' % (model_dir, idx, step)
                )
        if self.use_inv:
            for idx, inv in enumerate(self.invs):
                torch.save(
                    inv.state_dict(),
                    '%s/inv_ensem%d_%s.pt' % (model_dir, idx, step)
                )

    def load(self, model_dir, step):
        self.actor.load_state_dict(
            torch.load('%s/actor_%s.pt' % (model_dir, step))
        )
        self.critic.load_state_dict(
            torch.load('%s/critic_%s.pt' % (model_dir, step))
        )
        if len(self.ss_encoders) > 0:
            for idx, ss_encoder in enumerate(self.ss_encoders):
                ss_encoder.load_state_dict(
                    torch.load('%s/ss_encoder_ensem%d_%s.pt' % (model_dir, idx, step))
                )
        if self.use_fwd:
            for idx, fwd in enumerate(self.fwds):
                fwd.load_state_dict(
                    torch.load('%s/fwd_ensem%d_%s.pt' % (model_dir, idx, step))
                )
        if self.use_inv:
            for idx, inv in enumerate(self.invs):
                inv.load_state_dict(
                    torch.load('%s/inv_ensem%d_%s.pt' % (model_dir, idx, step))
                )
