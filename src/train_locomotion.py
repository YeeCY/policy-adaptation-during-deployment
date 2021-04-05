import torch
import numpy as np
import os

from arguments import parse_args
from env import make_atari_env, make_locomotion_env, make_single_metaworld_env
from agent import make_agent
import utils
import buffers
import time
from logger import Logger
from video import VideoRecorder


def evaluate(env, agent, video, num_episodes, logger, step):
    """Evaluate agent"""
    episode_rewards = []
    episode_success_rates = []
    episode_fwd_pred_vars = []
    episode_inv_pred_vars = []
    for episode in range(num_episodes):
        obs = env.reset()
        video.init(enabled=(episode == 0))
        done = False
        episode_reward = 0
        obs_buf = []
        next_obs_buf = []
        action_buf = []
        success_buf = []
        while not done:
            with utils.eval_mode(agent):
                action = agent.act(obs, sample=False)
            next_obs, reward, done, info = env.step(action)
            if hasattr(env, 'max_path_length') and env.curr_path_length > env.max_path_length:  # metaworld
                done = True

            obs_buf.append(obs)
            next_obs_buf.append(next_obs)
            action_buf.append(action)
            episode_reward += reward
            if info.get('success') is not None:
                success_buf.append(info.get('success'))

            video.record(env)
            obs = next_obs
        episode_rewards.append(episode_reward)
        if len(success_buf) > 0:
            episode_success_rates.append(np.sum(success_buf) / len(success_buf))

        if agent.use_fwd:
            episode_fwd_pred_vars.append(np.mean(
                agent.ss_preds_var(
                    np.asarray(obs_buf, dtype=obs.dtype),
                    np.asarray(next_obs_buf, dtype=obs.dtype),
                    np.asarray(action_buf, dtype=action.dtype))
            ))
        if agent.use_inv:
            episode_inv_pred_vars.append(np.mean(
                agent.ss_preds_var(
                    np.asarray(obs_buf, dtype=obs.dtype),
                    np.asarray(next_obs_buf, dtype=obs.dtype),
                    np.asarray(action_buf, dtype=action.dtype))
            ))
        video.save('%d.mp4' % step)
    logger.log('eval/episode_reward', np.mean(episode_rewards), step)
    if len(episode_success_rates) > 0:
        logger.log('eval/episode_success_rate', np.mean(episode_success_rates), step)
    if agent.use_fwd:
        logger.log('eval/episode_ss_pred_var', np.mean(episode_fwd_pred_vars), step)
    if agent.use_inv:
        logger.log('eval/episode_ss_pred_var', np.mean(episode_inv_pred_vars), step)
    logger.dump(step, ty='eval')


def main(args):
    if args.env_type == 'atari':
        env = make_atari_env(
            env_name=args.env_name,
            action_repeat=args.action_repeat,
            frame_stack=args.frame_stack
        )
        eval_env = make_atari_env(
            env_name=args.env_name,
            action_repeat=args.action_repeat,
            frame_stack=args.frame_stack
        )
    elif args.env_type == 'dmc_locomotion':
        env = make_locomotion_env(
            env_name=args.env_name,
            seed=args.seed,
            episode_length=args.episode_length,
            from_pixels=args.pixel_obs,
            action_repeat=args.action_repeat,
            obs_height=args.obs_height,
            obs_width=args.obs_width,
            camera_id=args.env_camera_id,
            mode=args.mode
        )
        eval_env = make_locomotion_env(
            env_name=args.env_name,
            seed=args.seed,
            episode_length=args.episode_length,
            from_pixels=args.pixel_obs,
            action_repeat=args.action_repeat,
            obs_height=args.obs_height,
            obs_width=args.obs_width,
            camera_id=args.env_camera_id,
            mode=args.mode
        )
    elif args.env_type == 'metaworld':
        env = make_single_metaworld_env(
            env_name=args.env_name
        )
        eval_env = make_single_metaworld_env(
            env_name=args.env_name
        )

    # Initialize environment
    utils.set_seed_everywhere(args.seed, env=env, eval_env=eval_env)
    utils.make_dir(args.work_dir)
    model_dir = utils.make_dir(os.path.join(args.work_dir, 'model'))
    video_dir = utils.make_dir(os.path.join(args.work_dir, 'video'))
    video = VideoRecorder(video_dir if args.save_video else None, args.env_type,
                          height=448, width=448, camera_id=args.video_camera_id)

    # Prepare agent
    assert torch.cuda.is_available(), 'must have cuda enabled'
    device = torch.device(args.device)

    if args.env_type == 'atari':
        # replay_buffer = buffers.FrameStackReplayBuffer(
        #     obs_space=env.observation_space,
        #     action_space=env.action_space,
        #     capacity=args.replay_buffer_capacity,
        #     frame_stack=args.frame_stack,
        #     device=device,
        #     optimize_memory_usage=True,
        # )
        from stable_baselines3.common.buffers import ReplayBuffer
        replay_buffer = ReplayBuffer(
            args.replay_buffer_capacity,
            env.observation_space,
            env.action_space,
            device,
            optimize_memory_usage=True,
        )
        # replay_buffer = buffers.ReplayBuffer(
        #     obs_space=env.observation_space,
        #     action_space=env.action_space,
        #     capacity=args.replay_buffer_capacity,
        #     device=device,
        #     optimize_memory_usage=True,
        # )
    elif args.env_type == 'dmc_locomotion' or 'metaworld':
        replay_buffer = buffers.ReplayBuffer(
            obs_space=env.observation_space,
            action_space=env.action_space,
            capacity=args.replay_buffer_capacity,
            device=device,
            optimize_memory_usage=True,
        )
    agent = make_agent(
        obs_space=env.observation_space,
        action_space=env.action_space,
        device=device,
        args=args
    )

    logger = Logger(args.work_dir,
                    log_frequency=args.log_freq,
                    action_repeat=args.action_repeat,
                    save_tb=args.save_tb)
    episode, episode_reward, episode_step, done = 0, 0, 0, True
    success = []
    obs = env.reset()
    start_time = time.time()
    for step in range(args.train_steps + 1):
        # (chongyi zheng): we can also evaluate and save model when current episode is not finished
        # Evaluate agent periodically
        if step % args.eval_freq == 0:
            print('Evaluating:', args.work_dir)
            logger.log('eval/episode', episode, step)
            evaluate(eval_env, agent, video, args.num_eval_episodes, logger, step)

        # Save agent periodically
        if step % args.save_freq == 0 and step > 0:
            if args.save_model:
                agent.save(model_dir, step)

        if done:
            if step > 0:
                logger.log('train/duration', time.time() - start_time, step)
                start_time = time.time()
                logger.dump(step, ty='train', save=(step > args.init_steps))

            logger.log('train/episode_reward', episode_reward, step)

            obs = env.reset()
            episode_reward = 0
            episode_step = 0
            episode += 1
            if len(success) > 0:
                success_rate = np.sum(success) / len(success)
                logger.log('train/success_rate', success_rate, step)
                success.clear()

            logger.log('train/episode', episode, step)

        # Sample action for data collection
        if step < args.init_steps:
            action = env.action_space.sample()
        else:
            with utils.eval_mode(agent):
                action = agent.act(obs, sample=True)

        if 'dqn' in args.algo:
            agent.on_step(step, args.train_steps, logger)

        # Run training update
        if step >= args.init_steps and step % args.train_freq == 0:
            # TODO (chongyi zheng): Do we need multiple updates after initial data collection?
            # num_updates = args.init_steps if step == args.init_steps else 1
            # for _ in range(num_updates):
            # 	agent.update(replay_buffer, logger, step)
            for _ in range(args.num_train_iters):
                agent.update(replay_buffer, logger, step)

        # Take step
        next_obs, reward, done, info = env.step(action)
        if hasattr(env, 'max_path_length') and env.curr_path_length > env.max_path_length:  # metaworld
            done = True
        if info.get('success') is not None:
            success.append(info.get('success'))

        replay_buffer.add(obs, action, reward, next_obs, done)
        # replay_buffer.add(np.expand_dims(obs, axis=0),
        #                   np.expand_dims(next_obs, axis=0),
        #                   np.expand_dims(action, axis=0),
        #                   np.expand_dims(reward, axis=0),
        #                   np.expand_dims(done, axis=0))
        episode_reward += reward
        obs = next_obs
        episode_step += 1


if __name__ == '__main__':
    args = parse_args()
    main(args)
