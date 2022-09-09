import gym
import numpy as np
import os
import platform

import torch as th
import torch.nn as nn

from stable_baselines3.common.vec_env import SubprocVecEnv, VecVideoRecorder, DummyVecEnv
from stable_baselines3.common.monitor import Monitor
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.logger import configure
from stable_baselines3.common.torch_layers import FlattenExtractor, NatureCNN

from cspn import CSPN, print_cspn_params
from sb3 import CspnActor, CspnSAC, EntropyLoggingSAC, CspnPolicy
from utils import non_existing_folder_name


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', '-s', type=int, nargs='+', required=True)
    parser.add_argument('--mlp_actor', action='store_true', help='Use a MLP actor')
    parser.add_argument('--num_envs', type=int, default=1, help='Number of parallel environments to run.')
    parser.add_argument('--timesteps', type=int, default=int(1e6), help='Total timesteps to train model.')
    parser.add_argument('--save_interval', type=int, help='Save model and a video every save_interval timesteps.')
    parser.add_argument('--log_interval', type=int, default=4, help='Log interval')
    parser.add_argument('--env_name', '-env', type=str, required=True, help='Gym environment to train on.')
    parser.add_argument('--device', '-dev', type=str, default='cuda', help='Device to run on. cpu or cuda.')
    parser.add_argument('--proj_name', '-proj', type=str, default='test_proj', help='Project name for WandB')
    parser.add_argument('--run_name', '-name', type=str, default='test_run',
                        help='Name of this run for WandB. The seed will be automatically appended. ')
    parser.add_argument('--log_dir', type=str, default='../../cspn_rl_experiments',
                        help='Directory to save logs to.')
    parser.add_argument('--model_path', type=str,
                        help='Path to the pretrained model.')
    parser.add_argument('--no_wandb', action='store_true', help="Don't log this run in WandB")
    parser.add_argument('--no_video', action='store_true',
                        help="Don't record videos of the agent. NOTE: This requires "
                             "LD_PRELOAD=$CONDA_PREFIX/lib/libGLEW.so to be set.")
    # SAC arguments
    parser.add_argument('--ent_coef', type=float, default=0.1, help='Entropy temperature')
    parser.add_argument('--learning_rate', '-lr', type=float, default=3e-4, help='Learning rate')
    parser.add_argument('--learning_starts', type=int, default=1000,
                        help='Nr. of steps to act randomly in the beginning.')
    parser.add_argument('--buffer_size', type=int, default=300_000, help='replay buffer size')
    parser.add_argument('--joint_fail_prob', type=float, default=0.05, help="Joints can fail with this probability")
    # CSPN arguments
    parser.add_argument('--repetitions', '-R', type=int, default=3, help='Number of parallel CSPNs to learn at once. ')
    parser.add_argument('--cspn_depth', '-D', type=int,
                        help='Depth of the CSPN. If not provided, maximum will be used (ceil of log2(inputs)).')
    parser.add_argument('--num_dist', '-I', type=int, default=3, help='Number of Gauss dists per pixel.')
    parser.add_argument('--num_sums', '-S', type=int, default=3, help='Number of sums per RV in each sum layer.')
    parser.add_argument('--dropout', type=float, default=0.0, help='Dropout to apply')
    parser.add_argument('--no_relu', action='store_true',
                        help='Don\'t use inner ReLU activations in the layers providing '
                             'the CSPN parameters from the conditional.')
    parser.add_argument('--feat_layers', type=int, nargs='+',
                        help='List of sizes of the CSPN feature layers.')
    parser.add_argument('--sum_param_layers', type=int, nargs='+',
                        help='List of sizes of the CSPN sum param layers.')
    parser.add_argument('--dist_param_layers', type=int, nargs='+',
                        help='List of sizes of the CSPN dist param layers.')
    parser.add_argument('--objective', '-obj', type=str, help='Entropy objective to maximize.')
    # Entropy arguments
    parser.add_argument('--recurs_sample_size', type=int, default=5,
                        help='Number of samples to approximate recursive entropy with. ')
    parser.add_argument('--naive_sample_size', type=int, default=50,
                        help='Number of samples to approximate naive entropy with. ')
    args = parser.parse_args()

    if not args.save_interval:
        args.save_interval = args.timesteps

    if args.timesteps == 0:
        learn = False
    else:
        learn = True
        assert args.timesteps >= args.save_interval, "Total timesteps cannot be lower than save_interval!"
        assert args.timesteps % args.save_interval == 0, "save_interval must be a divisor of total timesteps."

    if args.model_path:
        assert os.path.exists(args.model_path), f"The model_path doesn't exist! {args.model_path}"
    if args.log_dir:
        assert os.path.exists(args.log_dir), f"The log_dir doesn't exist! {args.log_dir}"

    if not args.no_wandb:
        import wandb
        from wandb.integration.sb3 import WandbCallback
        wandb.login(key=os.environ['WANDB_API_KEY'])

    for seed in args.seed:
        print(f"Seed: {seed}")
        np.random.seed(seed)
        th.manual_seed(seed)

        run_name_seed = f"{args.run_name}_s{seed}"
        log_path = os.path.join(args.log_dir, args.proj_name)
        log_path = os.path.join(log_path, non_existing_folder_name(log_path, run_name_seed))
        monitor_path = os.path.join(log_path, "monitor")
        model_path = os.path.join(log_path, "models")
        video_path = os.path.join(log_path, "video")
        for d in [log_path, monitor_path, model_path, video_path]:
            os.makedirs(d, exist_ok=True)

        if not args.no_wandb:
            run = wandb.init(
                dir=log_path,
                project=args.proj_name,
                name=run_name_seed,
                group=args.run_name,
                sync_tensorboard=True,
                monitor_gym=True,
                reinit=True,
                force=True,
                settings=wandb.Settings(start_method="fork"),
            )

        env = make_vec_env(
            env_id=args.env_name,
            n_envs=args.num_envs,
            monitor_dir=monitor_path,
            # vec_env_cls=SubprocVecEnv,
            # vec_env_kwargs={'start_method': 'fork'},
        )
        if env.observation_space.dtype == np.float64:
            env.observation_space = gym.spaces.Box(
                low=env.observation_space.low, high=env.observation_space.high, dtype=np.float32
            )
        if not args.no_video:
            # Without env as a VecVideoRecorder we need the env var LD_PRELOAD=$CONDA_PREFIX/lib/libGLEW.so
            env = VecVideoRecorder(env, video_folder=video_path,
                                   record_video_trigger=lambda x: x % args.save_interval == 0,
                                   video_length=200)

        if args.model_path:
            model = CspnSAC.load(args.model_path, env)
            # model.tensorboard_log = None
            # model.vi_aux_resp_grad_mode = args.vi_aux_resp_grad_mode
            # model_name = f"sac_loadedpretrained_{args.env}_{args.proj_name}_{run_name_seed}"
            model.seed = seed
            model.learning_starts = args.learning_starts
            # model.learning_rate = args.learning_rate
            sac_kwargs = {
                'env': model.env,
                'seed': model.seed,
                'verbose': model.verbose,
                'ent_coef': model.ent_coef,
                'learning_starts': model.learning_starts,
                'device': model.device,
                'learning_rate': model.learning_rate,
                'policy_kwargs': {
                    'cspn_args': {
                        'R': model.actor.cspn.config.R,
                        'D': model.actor.cspn.config.D,
                        'I': model.actor.cspn.config.I,
                        'S': model.actor.cspn.config.S,
                        'dropout': model.actor.cspn.config.dropout,
                        'sum_param_layers': model.actor.cspn.config.sum_param_layers,
                        'dist_param_layers': model.actor.cspn.config.dist_param_layers,
                        'cond_layers_inner_act': model.actor.cspn.config.cond_layers_inner_act,
                        'vi_ent_approx_sample_size': model.actor.vi_ent_approx_sample_size,
                    }
                }
            }
        else:
            sac_kwargs = {
                'env': env,
                'seed': seed,
                'verbose': 2,
                'ent_coef': args.ent_coef,
                'learning_starts': args.learning_starts,
                'device': args.device,
                'learning_rate': args.learning_rate,
                'buffer_size': args.buffer_size,
            }
            if args.mlp_actor:
                model = EntropyLoggingSAC("MlpPolicy", **sac_kwargs)
            else:
                cspn_args = {
                    'R': args.repetitions,
                    'D': args.cspn_depth,
                    'I': args.num_dist,
                    'S': args.num_sums,
                    'dropout': args.dropout,
                    'feat_layers': args.feat_layers,
                    'sum_param_layers': args.sum_param_layers,
                    'dist_param_layers': args.dist_param_layers,
                    'cond_layers_inner_act': nn.Identity if args.no_relu else nn.ReLU,
                    'entropy_objective': args.objective,
                    'recurs_ent_approx_sample_size': args.recurs_sample_size,
                    'naive_ent_approx_sample_size': args.naive_sample_size,
                }
                sac_kwargs['policy_kwargs'] = {
                    'joint_failure_prob': args.joint_fail_prob,
                    'actor_cspn_args': cspn_args,
                    'features_extractor_class': NatureCNN if len(env.observation_space.shape) > 1 else FlattenExtractor,
                }
                model = CspnSAC(policy=CspnPolicy, **sac_kwargs)
            # model_name = f"sac_{'mlp' if args.mlp_actor else 'cspn'}_{args.env_name}_{args.exp_name}_s{seed}"

        if not args.no_wandb:
            run.config.update({
                **sac_kwargs,
                'machine': platform.node(),
            })
            callback = WandbCallback(
                gradient_save_freq=10000,
                model_save_path=model_path,
                model_save_freq=args.save_interval,
                verbose=2,
            )
        else:
            callback = None

        logger = configure(log_path, ["stdout", "csv", "tensorboard"])
        logger.output_formats[0].max_length = 50
        model.set_logger(logger)

        print(model.actor)
        print(model.critic)
        if isinstance(model.actor, CspnActor):
            print_cspn_params(model.actor.cspn)
        else:
            print(f"Actor MLP has {sum(p.numel() for p in model.actor.parameters() if p.requires_grad)} parameters.")

        # noinspection PyTypeChecker
        model.learn(
            total_timesteps=args.timesteps,
            log_interval=args.log_interval,
            reset_num_timesteps=not args.model_path,
            tb_log_name=f"{args.proj_name}/{run_name_seed}",
            callback=callback,
        )
        run.finish()
