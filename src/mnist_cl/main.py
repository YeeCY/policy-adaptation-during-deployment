import argparse
import numpy as np
import torch
import os

from src.mnist_cl import evaluate
from src.mnist_cl.data import get_multitask_experiment
from src.mnist_cl.train import train_cl
from src.mnist_cl.ewc_classifier import EwcClassifier
from src.mnist_cl.si_classifier import SiClassifier
from src.mnist_cl.agem_classifier import AgemClassifier
from src.mnist_cl.cmaml_classifier import CmamlClassfier
from src.mnist_cl import callbacks as cb
from src.mnist_cl.param_stamp import get_param_stamp


def main(args):
    cuda = torch.cuda.is_available()
    device = torch.device("cuda" if cuda else "cpu")
    print("CUDA is {}used".format("" if cuda else "NOT(!!) "))

    os.makedirs(args.result_dir, exist_ok=True)

    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if cuda:
        torch.cuda.manual_seed(args.seed)

    train_datasets, test_datasets, config, classes_per_task = get_multitask_experiment(
        name=args.dataset, scenario=args.scenario, num_tasks=args.num_tasks, data_dir=args.data_dir,
        verbose=True, exception=True)

    if args.ewc:
        model = EwcClassifier(
            config['size'], config['channels'], config['classes'], hidden_units=args.hidden_units,
            lam=args.ewc_lambda, fisher_sample_size=args.ewc_fisher_sample_size,
            online=args.ewc_online, gamma=args.ewc_gamma, device=device)
    elif args.si:
        model = SiClassifier(
            config['size'], config['channels'], config['classes'], hidden_units=args.hidden_units,
            c=args.si_c, epsilon=args.si_epsilon, device=device)
    elif args.agem:
        model = AgemClassifier(
            config['size'], config['channels'], config['classes'], hidden_units=args.hidden_units,
            memory_budget=args.agem_memory_budget, device=device)
    elif args.cmaml:
        model = CmamlClassfier(
            config['size'], config['channels'], config['classes'], hidden_units=args.hidden_units,
            fast_lr=args.cmaml_fast_lr, meta_lr=args.cmaml_meta_lr, memory_budget=args.cmaml_memory_budget,
            grad_clip_norm=args.cmaml_grad_clip_norm, first_order=args.cmaml_first_order, device=device)
    else:
        raise RuntimeError("Unknown algorithm")

    # # Store in model whether, how many and in what way to store exemplars
    # if isinstance(model, ExemplarHandler) and (args.use_exemplars or args.add_exemplars or args.replay=="exemplars"):
    #     model.memory_budget = args.budget
    #     model.norm_exemplars = args.norm_exemplars
    #     model.herding = args.herding

    # # Synpatic Intelligence (SI)
    # if isinstance(model, ContinualLearner):
    #     model.si_c = args.si_c if args.si else 0
    #     if args.si:
    #         model.epsilon = args.epsilon

    print("\nParameter-stamp...")
    param_stamp = get_param_stamp(
        args, verbose=True, replay=True if (not args.replay == "none") else False)
    #
    # # Print some model-characteristics on the screen
    # if verbose:
    #     # -main model
    #     utils.print_model_info(model, title="MAIN MODEL")
    #     # -generator
    #     if generator is not None:
    #         utils.print_model_info(generator, title="GENERATOR")

    solver_loss_cbs = [
        cb._solver_loss_cb(log=args.loss_log_intervals, visdom=None, model=model, tasks=args.num_tasks,
                           iters_per_task=args.iters, replay=False if args.replay == "none" else True)
    ]

    train_cl(
        model, train_datasets, replay_mode=args.replay, scenario=args.scenario, classes_per_task=classes_per_task,
        iters=args.iters, batch_size=args.batch_size, loss_cbs=solver_loss_cbs)

    precs = [evaluate.validate(
        model, test_datasets[i], verbose=False, test_size=None, task=i + 1, with_exemplars=False,
        allowed_classes=list(range(classes_per_task * i, classes_per_task * (i + 1)))
        if args.scenario == "task" else None
    ) for i in range(args.num_tasks)]
    average_precs = sum(precs) / args.num_tasks

    # -print on screen
    print("\n Precision on test-set")
    for i in range(args.num_tasks):
        print(" - Task {}: {:.4f}".format(i + 1, precs[i]))
    print('=> Average precision over all {} tasks: {:.4f}\n'.format(args.num_tasks, average_precs))

    # Average precision on full test set
    output_file = open("{}/prec-{}.txt".format(args.result_dir, param_stamp), 'w')
    output_file.write('{}\n'.format(average_precs))
    output_file.close()


if __name__ == "__main__":
    def str2bool(v):
        if isinstance(v, bool):
            return v
        if v.lower() in ('yes', 'true', 't', 'y', '1'):
            return True
        elif v.lower() in ('no', 'false', 'f', 'n', '0'):
            return False
        else:
            raise argparse.ArgumentTypeError('Boolean value expected.')

    parser = argparse.ArgumentParser()

    # common
    parser.add_argument('--dataset', type=str, default='splitMNIST', choices=['permMNIST', 'splitMNIST'])
    parser.add_argument('--scenario', type=str, default='class', choices=['task', 'domain', 'class'])
    parser.add_argument('--num_tasks', type=int, default=5, help='number of tasks')  # splitMNIST = 5, permMNIST = 10
    parser.add_argument('--data_dir', type=str, default='./datasets', help="default: %(default)s")
    parser.add_argument('--result_dir', type=str, default='./results', help="default: %(default)s")
    parser.add_argument('--seed', type=int, default=0, help='random seed (for each random-module used)')

    parser.add_argument('--iters', type=int, default=500, help="# batches to optimize solver")  # splitMNIST = 2000, permMNIST = 5000
    parser.add_argument('--lr', type=float, default=0.001, help="learning rate")  # splitMNIST = 0.001, permMNIST = 0.0001
    parser.add_argument('--batch_size', type=int, default=128, help="batch size")
    parser.add_argument('--hidden_units', type=int, default=400, help="fully connected layer hidden units")  # splitMNIST = 400, permMNIST = 1000
    parser.add_argument('--loss_log_intervals', type=int, default=200, metavar="N", help="# iters after which to plot loss")

    # exemplars
    replay_choices = ['none', 'exemplars']
    parser.add_argument('--replay', type=str, default='none', choices=replay_choices)

    # ewc
    parser.add_argument('--ewc', type=str2bool, default=False)
    parser.add_argument('--ewc_lambda', type=float, default=500)
    parser.add_argument('--ewc_fisher_sample_size', type=int)
    parser.add_argument('--ewc_online', type=str2bool, default=False)
    parser.add_argument('--ewc_gamma', type=float, default=1.0)

    # si
    parser.add_argument('--si', type=str2bool, default=False)
    parser.add_argument('--si_c', type=float, default=1.0)
    parser.add_argument('--si_epsilon', type=float, default=0.1)

    # agem
    parser.add_argument('--agem', type=str2bool, default=False)
    parser.add_argument('--agem_memory_budget', type=int, default=1000)

    # cmaml
    parser.add_argument('--cmaml', type=str2bool, default=False)
    parser.add_argument('--cmaml_fast_lr', type=float, default=0.0003)
    parser.add_argument('--cmaml_meta_lr', type=float, default=0.001)
    parser.add_argument('--cmaml_memory_budget', type=int, default=1000)
    parser.add_argument('--cmaml_grad_clip_norm', type=float, default=2.0)
    parser.add_argument('--cmaml_first_order', type=str2bool, default=True)

    args = parser.parse_args()

    main(args)
