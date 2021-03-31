from agent.dqn_agent import DqnCnnSSEnsembleAgent
from agent.sac_agent import SacMlpSSEnsembleAgent, SacCnnSSEnsembleAgent

ALGOS = [
    'dqn_cnn_ss_ensem',
    'sac_cnn_ss_ensem',
    'sac_mlp_ss_ensem',
]


def make_agent(obs_shape, action_shape, action_range, device, args):
    kwargs = {
        'obs_shape': obs_shape,
        'action_shape': action_shape,
        'discount': args.discount,
        'use_fwd': args.use_fwd,
        'use_inv': args.use_inv,
        'ss_lr': args.ss_lr,
        'ss_update_freq': args.ss_update_freq,
        'batch_size': args.batch_size,
        'device': device,
    }

    if args.algo == 'dqn_cnn_ss_ensem':
        kwargs['feature_dim'] = args.encoder_feature_dim
        kwargs['exploration_fraction'] = args.exploration_fraction
        kwargs['exploration_initial_eps'] = args.exploration_initial_eps
        kwargs['target_update_interval'] = args.target_update_interval
        kwargs['max_grad_norm'] = args.max_grad_norm
        kwargs['q_net_lr'] = args.q_net_lr
        kwargs['q_net_tau'] = args.q_net_tau

        agent = DqnCnnSSEnsembleAgent(**kwargs)
    elif 'sac' in args.algo:
        kwargs['hidden_dim'] = args.hidden_dim
        kwargs['init_temperature'] = args.init_temperature
        kwargs['alpha_lr'] = args.alpha_lr
        kwargs['actor_lr'] = args.actor_lr
        kwargs['actor_beta'] = args.actor_beta
        kwargs['actor_log_std_min'] = args.actor_log_std_min
        kwargs['actor_log_std_max'] = args.actor_log_std_max
        kwargs['actor_update_freq'] = args.actor_update_freq
        kwargs['critic_lr'] = args.critic_lr
        kwargs['critic_tau'] = args.critic_tau
        kwargs['critic_target_update_freq'] = args.critic_target_update_freq
        kwargs['use_fwd'] = args.use_fwd
        kwargs['use_inv'] = args.use_inv
        kwargs['ss_lr'] = args.ss_lr
        kwargs['ss_update_freq'] = args.ss_update_freq
        kwargs['num_ensem_comps'] = args.num_ensem_comps

        if args.algo == 'sac_cnn_ss_ensem':
            kwargs['encoder_feature_dim'] = args.encoder_feature_dim
            kwargs['encoder_lr'] = args.encoder_lr
            kwargs['encoder_tau'] = args.encoder_tau
            kwargs['ss_stop_shared_layers_grad'] = args.ss_stop_shared_layers_grad
            kwargs['num_layers'] = args.num_layers
            kwargs['num_shared_layers'] = args.num_shared_layers
            kwargs['num_filters'] = args.num_filters
            kwargs['curl_latent_dim'] = args.curl_latent_dim
            agent = SacCnnSSEnsembleAgent(**kwargs)
        elif args.algo == 'sac_mlp_ss_ensem':
            kwargs['action_range'] = action_range
            agent = SacMlpSSEnsembleAgent(**kwargs)
        else:
            raise ValueError(f"Unknown algorithm {args.algo}")
    else:
        raise ValueError(f"Unknown algorithm {args.algo}")

    return agent