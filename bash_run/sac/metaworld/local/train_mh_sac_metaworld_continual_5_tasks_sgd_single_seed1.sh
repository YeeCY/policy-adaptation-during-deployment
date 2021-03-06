#!/bin/bash

SCRIPT_DIR=$(dirname "$BASH_SOURCE")
PROJECT_DIR=$(realpath "$SCRIPT_DIR/../../../..")

export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$HOME/.mujoco/mujoco200/bin:/usr/lib/nvidia-418
export PYTHONPATH=$PROJECT_DIR

declare -a all_env_names=(window-close-v2 button-press-topdown-v2 peg-insert-side-v2 door-open-v2 hammer-v2)
declare -a seeds=(1)

for env_names in "${all_env_names[@]}"; do
  for seed in "${seeds[@]}"; do
    export CUDA_VISIBLE_DEVICES="$seed"
    nohup \
    python $PROJECT_DIR/src/train_sac.py \
      --env_names $env_names \
      --env_type metaworld \
      --algo mh_sac_mlp \
      --train_steps_per_task 500000 \
      --eval_freq 10 \
      --num_eval_episodes 10 \
      --discount 0.99 \
      --sac_init_steps 1000 \
      --sac_num_expl_steps_per_process 1000 \
      --sac_num_processes 1 \
      --sac_num_train_iters 1000 \
      --seed $seed \
      --save_video \
      --work_dir $PROJECT_DIR/vec_logs/mh_sac_mlp_metaworld_5_tasks_single/sgd/$env_names/$seed \
      > $PROJECT_DIR/terminal_logs/mh_sac_mlp_metaworld_5_tasks_single-sgd-"$env_names"-seed"$seed".log 2>&1 &
  done
done
