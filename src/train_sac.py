import torch
import numpy as np
import os
from collections import deque
import copy


from arguments import parse_args
from environment import make_atari_env, make_locomotion_env, make_single_metaworld_env, make_continual_metaworld_env
from environment.metaworld_utils import MultiEnvWrapper
from agent import make_agent
import utils
import buffers
import time
from logger import Logger
from video import VideoRecorder


def evaluate(env, agent, video, num_episodes, logger, step):
    """Evaluate agent"""
    if isinstance(env, MultiEnvWrapper):
        assert env.env_names is not None, "Environment name must exist!"

        for task_id, task_name in enumerate(env.env_names):
            episode_rewards = []
            episode_successes = []
            episode_fwd_pred_vars = []
            episode_inv_pred_vars = []
            for episode in enumerate(num_episodes):
                obs = env.reset(sample_task=(episode == 0))
                video.init(enabled=(episode == 0))
                done = False
                episode_reward = 0
                obs_buf = []
                next_obs_buf = []
                action_buf = []
                is_successes = []
                while not done:
                    with utils.eval_mode(agent):
                        if 'mh' in args.algo:
                            action, _ = agent.act(obs, sample=False, head_idx=task_id)
                        else:
                            action, _ = agent.act(obs, sample=False)
                    next_obs, reward, done, info = env.step(action)

                    obs_buf.append(obs)
                    next_obs_buf.append(next_obs)
                    action_buf.append(action)
                    episode_reward += reward
                    if info.get('success') is not None:
                        is_successes.append(info.get('success'))

                    video.record(env)
                    obs = next_obs
                episode_rewards.append(episode_reward)
                episode_successes.append(np.any(is_successes).astype(np.float))

                if getattr(agent, 'use_fwd', False):
                    episode_fwd_pred_vars.append(np.mean(
                        agent.ss_preds_var(
                            np.asarray(obs_buf, dtype=obs.dtype),
                            np.asarray(next_obs_buf, dtype=obs.dtype),
                            np.asarray(action_buf, dtype=action.dtype))
                    ))
                if getattr(agent, 'use_inv', False):
                    episode_inv_pred_vars.append(np.mean(
                        agent.ss_preds_var(
                            np.asarray(obs_buf, dtype=obs.dtype),
                            np.asarray(next_obs_buf, dtype=obs.dtype),
                            np.asarray(action_buf, dtype=action.dtype))
                    ))
                video.save('%s_%d.mp4' % (task_name, step))
            logger.log('eval/episode_reward', np.mean(episode_rewards), step, sw_prefix=task_name + '_')
            if len(episode_successes) > 0:
                logger.log('eval/success_rate', np.mean(episode_successes), step)
            if getattr(agent, 'use_fwd', False):
                logger.log('eval/episode_ss_pred_var', np.mean(episode_fwd_pred_vars), step)
            if getattr(agent, 'use_inv', False):
                logger.log('eval/episode_ss_pred_var', np.mean(episode_inv_pred_vars), step)
            log_info = {
                'eval/task_name': task_name
            }
            logger.dump(step, ty='eval', info=log_info)


def main(args):
    # Initialize environment
    if args.env_type == 'atari':
        # environment = make_atari_env(
        #     env_name=args.env_name,
        #     seed=args.seed,
        #     action_repeat=args.action_repeat,
        #     frame_stack=args.frame_stack
        # )
        # eval_env = make_atari_env(
        #     env_name=args.env_name,
        #     seed=args.seed,
        #     action_repeat=args.action_repeat,
        #     frame_stack=args.frame_stack
        # )
        # environment = make_continual_atari_env(
        #     env_names=args.env_names,
        #     seed=args.seed,
        #     action_repeat=args.action_repeat,
        #     frame_stack=args.frame_stack
        # )
        # eval_env = make_continual_atari_env(
        #     env_names=args.env_names,
        #     seed=args.seed,
        #     action_repeat=args.action_repeat,
        #     frame_stack=args.frame_stack
        # )
        pass
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
        # environment = make_single_metaworld_env(
        #     env_name=args.env_name,
        #     seed=args.seed
        # )
        # eval_env = make_single_metaworld_env(
        #     env_name=args.env_name,
        #     seed=args.seed
        # )
        env = make_continual_metaworld_env(
            env_names=args.env_names,
            seed=args.seed
        )
        # eval_env = copy.deepcopy(env)
        eval_env = make_continual_metaworld_env(
            env_names=args.env_names,
            seed=args.seed
        )

    utils.set_seed_everywhere(args.seed)
    utils.make_dir(args.work_dir)
    model_dir = utils.make_dir(os.path.join(args.work_dir, 'model'))
    video_dir = utils.make_dir(os.path.join(args.work_dir, 'video'))
    video = VideoRecorder(video_dir if args.save_video else None, args.env_type,
                          height=448, width=448, camera_id=args.video_camera_id)

    # Prepare agent
    # assert torch.cuda.is_available(), 'must have cuda enabled'
    device = torch.device(args.device)

    if args.env_type == 'atari':
        # replay_buffer = buffers.FrameStackReplayBuffer(
        #     obs_space=environment.observation_space,
        #     action_space=environment.action_space,
        #     capacity=args.replay_buffer_capacity,
        #     frame_stack=args.frame_stack,
        #     device=device,
        #     optimize_memory_usage=True,
        # )
        # from stable_baselines3.common.buffers import ReplayBuffer
        # replay_buffer = ReplayBuffer(
        #     args.replay_buffer_capacity,
        #     environment.observation_space,
        #     environment.action_space,
        #     device,
        #     optimize_memory_usage=True,
        # )
        replay_buffer = buffers.ReplayBuffer(
            obs_space=env.observation_space,
            action_space=env.action_space,
            capacity=args.replay_buffer_capacity,
            device=device,
            optimize_memory_usage=True,
        )
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
        action_space=env.all_action_spaces if 'mh' in args.algo else env.action_space,
        device=device,
        args=args
    )

    logger = Logger(args.work_dir,
                    log_frequency=args.log_freq,
                    action_repeat=args.action_repeat,
                    save_tb=args.save_tb)

    # log arguments
    args_dict = vars(args)
    logger.log_and_dump_arguments(args_dict)

    episode, episode_reward, episode_step, episode_successes, done, info = 0, 0, 0, [], True, {}
    total_steps = 0
    recent_success = deque(maxlen=100)
    recent_episode_reward = deque(maxlen=100)
    start_time = time.time()
    # for step in range(args.train_steps + 1):
    #     # (chongyi zheng): we can also evaluate and save model when current episode is not finished
    #     # Evaluate agent periodically
    #     if step % args.eval_freq == 0:
    #         print('Evaluating:', args.work_dir)
    #         logger.log('eval/episode', episode, step)
    #         evaluate(eval_env, agent, video, args.num_eval_episodes, logger, step)
    #
    #     # Save agent periodically
    #     if step % args.save_freq == 0 and step > 0:
    #         if args.save_model:
    #             agent.save(model_dir, step)
    #
    #     if done:
    #         if step > 0:
    #             logger.log('train/duration', time.time() - start_time, step)
    #             start_time = time.time()
    #             logger.dump(step, ty='train', save=(step > args.init_steps))
    #
    #         recent_episode_reward.append(episode_reward)
    #         logger.log('train/recent_episode_reward', np.mean(recent_episode_reward), step)
    #         logger.log('train/episode_reward', episode_reward, step)
    #
    #         obs = environment.reset()
    #         episode_reward = 0
    #         episode_step = 0
    #         episode += 1
    #
    #         logger.log('train/episode', episode, step)
    #
    #     # Sample action for data collection
    #     if step < args.init_steps:
    #         action = environment.action_space.sample()
    #     else:
    #         # with utils.eval_mode(agent):
    #         action = agent.act(obs, False)
    #
    #     if 'dqn' in args.algo:
    #         agent.on_step(step, args.train_steps, logger)
    #
    #     # Run training update
    #     if step >= args.init_steps and step % args.train_freq == 0:
    #         # TODO (chongyi zheng): Do we need multiple updates after initial data collection?
    #         # num_updates = args.init_steps if step == args.init_steps else 1
    #         # for _ in range(num_updates):
    #         # 	agent.update(replay_buffer, logger, step)
    #         for _ in range(args.num_train_iters):
    #             agent.update(replay_buffer, logger, step)
    #
    #     # Take step
    #     next_obs, reward, done, _ = environment.step(action)
    #     # replay_buffer.add(obs, action, reward, next_obs, done)
    #     replay_buffer.add(np.expand_dims(obs, axis=0),
    #                       np.expand_dims(next_obs, axis=0),
    #                       np.expand_dims(action, axis=0),
    #                       np.expand_dims(reward, axis=0),
    #                       np.expand_dims(done, axis=0))
    #     episode_reward += reward
    #     obs = next_obs
    #     episode_step += 1
    train_steps_per_task = args.train_steps_per_task
    # if isinstance(environment, MultiEnvWrapper):
    #     for step in range(train_steps_per_task * environment.num_tasks):
    #         # (chongyi zheng): we can also evaluate and save model when current episode is not finished
    #         # Evaluate agent periodically
    #         if step % args.eval_freq_per_task == 0:
    #             print('Evaluating:', args.work_dir)
    #             logger.log('eval/episode', episode, step)
    #             evaluate(eval_env, agent, video, args.num_eval_episodes, logger, step)
    #
    #         # Save agent periodically
    #         if step % args.save_freq == 0 and step > 0:
    #             if args.save_model:
    #                 train_steps_per_task = args.train_steps_per_task
    if isinstance(env, MultiEnvWrapper):
        for task_id in range(env.num_tasks):
            obs = env.reset(sample_task=True)

            for task_step in range(train_steps_per_task):
                # Evaluate agent periodically
                if total_steps % args.eval_freq == 0:
                    print('Evaluating:', args.work_dir)
                    logger.log('eval/episode', episode, total_steps)
                    evaluate(eval_env, agent, video, args.num_eval_episodes, logger, total_steps)

                # Save agent periodically
                if total_steps % args.save_freq == 0 and total_steps > 0:
                    if args.save_model:
                        agent.save(model_dir, total_steps)

                # # (chongyi zheng): force reset outside done = True when step reach train_steps_per_task
                # if task_step >= train_steps_per_task:
                #     obs = env.reset(sample_task=True)
                #
                #     if 'ewc' in args.algo:
                #         agent.estimate_fisher(replay_buffer)
                #     elif 'si' in args.algo:
                #         agent.update_omegas()
                #     elif 'agem' in args.algo:
                #         agent.construct_memory(replay_buffer)
                #
                #     agent.reset_target_critic()
                #     replay_buffer.reset()

                if done:
                    success = np.any(episode_successes).astype(np.float)
                    recent_success.append(success)
                    recent_episode_reward.append(episode_reward)

                    logger.log(f'train/episode_success', success, total_steps)
                    logger.log(f'train/recent_success_rate', np.mean(recent_success), total_steps)
                    logger.log('train/episode_reward', episode_reward, total_steps)
                    logger.log('train/recent_episode_reward', np.mean(recent_episode_reward), total_steps)
                    logger.log('train/episode', episode, total_steps)

                    if total_steps > 0:
                        # save non-scalar info
                        log_info = {
                            'train/task_name': info['task_name']
                        }
                        logger.log('train/duration', time.time() - start_time, total_steps)
                        start_time = time.time()
                        # logger.dump(step, ty='train', save=(step > args.init_steps), info=log_info)
                        logger.dump(total_steps, ty='train', save=(task_step > args.init_steps), info=log_info)

                    obs = env.reset()
                    episode_reward = 0
                    episode_step = 0
                    episode_successes.clear()
                    episode += 1

                # Sample action for data collection
                if task_step < args.init_steps:
                    action = np.array(env.action_space.sample())
                else:
                    with utils.eval_mode(agent):
                        if 'mh' in args.algo:
                            action = agent.act(obs, sample=True, head_idx=task_id)
                        else:
                            action = agent.act(obs, sample=True)

                if 'dqn' in args.algo:
                    agent.on_step(task_step, train_steps_per_task, logger)

                # Run training update
                if task_step >= args.init_steps:
                    # TODO (chongyi zheng): Do we need multiple updates after initial data collection?
                    # num_updates = args.init_steps if step == args.init_steps else 1
                    for _ in range(args.num_train_iters):
                        if 'mh' in args.algo:
                            agent.update(replay_buffer, logger, total_steps, head_idx=task_id)
                        else:
                            agent.update(replay_buffer, logger, total_steps)

                # Take step
                next_obs, reward, done, info = env.step(action)

                if info.get('success') is not None:
                    episode_successes.append(info.get('success'))

                replay_buffer.add(obs, action, reward, next_obs, done)
                episode_reward += reward
                obs = next_obs
                episode_step += 1
                total_steps += 1

            if 'ewc' in args.algo:
                agent.estimate_fisher(replay_buffer)
            elif 'si' in args.algo:
                agent.update_omegas()
            elif 'agem' in args.algo:
                agent.construct_memory(replay_buffer)

            agent.reset_target_critic()
            replay_buffer.reset()

    print('Final evaluating:', args.work_dir)
    evaluate(eval_env, agent, video, args.num_eval_episodes, logger,
             train_steps_per_task * env.num_tasks)


if __name__ == '__main__':
    args = parse_args()
    main(args)