export PYTHONPATH=$HOME/policy-adaptation-during-deployment/
export PWD=$HOME/policy-adaptation-during-deployment
export CUDA_VISIBLE_DEVICES=3

declare -a seeds=(0 1 2)

for seed in "${seeds[@]}"; do
  xvfb-run -a -s "-screen 0 1400x900x24" python $PWD/src/train_ppo.py \
    --env_names Walker2d-v3 HalfCheetah-v3 Hopper-v3 \
    --env_type mujoco \
    --algo ewc_ppo_mlp \
    --train_steps_per_task 1000000 \
    --save_freq 10 \
    --eval_freq 10 \
    --discount 0.99 \
    --batch_size 32 \
    --ppo_num_rollout_steps_per_process 2048 \
    --ppo_num_processes 1 \
    --ppo_use_clipped_critic_loss \
    --ppo_use_proper_time_limits \
    --ppo_ewc_lambda 5 \
    --seed $seed \
    --work_dir $PWD/vec_logs/walker2d_halfcheetah_hopper/ewc_lambda5/$seed \
    --save_model
done