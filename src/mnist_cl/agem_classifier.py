import torch
from torch import nn
from torch.nn import functional as F
from torch import optim
import numpy as np

import utils


class AgemClassifier(nn.Module):
    def __init__(self, image_size, image_channels, classes, hidden_units=400, lr=0.001,
                 memory_budget=2000, ref_grad_batch_size=128, device=None):

        super().__init__()
        self.image_size = image_size
        self.image_channels = image_channels
        self.classes = classes
        self.hidden_units = hidden_units
        self.lr = lr
        self.memory_budget = memory_budget
        self.ref_grad_batch_size = ref_grad_batch_size

        # flatten image to 2D-tensor
        self.trunk = nn.Sequential(
            nn.Flatten(),
            nn.Linear(self.image_channels * self.image_size ** 2, self.hidden_units),
            nn.ReLU(),
            nn.Linear(self.hidden_units, self.hidden_units),
            nn.ReLU(),
            nn.Linear(self.hidden_units, self.classes)
        )

        self.optimizer = optim.Adam(self.parameters(), lr=self.lr)

        self.to(device)

        self.agem_task_count = 0
        self.agem_memories = {}

    def device(self):
        return next(self.parameters()).device

    def is_on_cuda(self):
        return next(self.parameters()).is_cuda

    def forward(self, x):
        return self.trunk(x)

    # def reduce_exemplar_sets(self, m):
    #     for y, p_y in enumerate(self.exemplar_sets):
    #         self.exemplar_sets[y] = p_y[:m]

    def _adjust_memory_size(self, size):
        # for y, p_y in enumerate(self.exemplar_sets):
        #     self.exemplar_sets[y] = p_y[:m]
        # for key, val in self.agem_memories.items():
        #     self.agem_memories[key] = val[:size]
        for mem in self.agem_memories.values():
            mem['x'] = mem['x'][:size]
            mem['y'] = mem['y'][:size]

    # def construct_exemplar_set(self, dataset, n):
    #     # set model to eval()-mode
    #     mode = self.training
    #     self.eval()
    #
    #     n_max = len(dataset)
    #     exemplar_set = []
    #
    #     # if self.herding:
    #     #     # compute features for each example in [dataset]
    #     #     first_entry = True
    #     #     dataloader = utils.get_data_loader(dataset, 128, cuda=self._is_on_cuda())
    #     #     for (image_batch, _) in dataloader:
    #     #         image_batch = image_batch.to(self._device())
    #     #         with torch.no_grad():
    #     #             feature_batch = self.feature_extractor(image_batch).cpu()
    #     #         if first_entry:
    #     #             features = feature_batch
    #     #             first_entry = False
    #     #         else:
    #     #             features = torch.cat([features, feature_batch], dim=0)
    #     #     if self.norm_exemplars:
    #     #         features = F.normalize(features, p=2, dim=1)
    #     #
    #     #     # calculate mean of all features
    #     #     class_mean = torch.mean(features, dim=0, keepdim=True)
    #     #     if self.norm_exemplars:
    #     #         class_mean = F.normalize(class_mean, p=2, dim=1)
    #     #
    #     #     # one by one, select exemplar that makes mean of all exemplars as close to [class_mean] as possible
    #     #     exemplar_features = torch.zeros_like(features[:min(n, n_max)])
    #     #     list_of_selected = []
    #     #     for k in range(min(n, n_max)):
    #     #         if k>0:
    #     #             exemplar_sum = torch.sum(exemplar_features[:k], dim=0).unsqueeze(0)
    #     #             features_means = (features + exemplar_sum)/(k+1)
    #     #             features_dists = features_means - class_mean
    #     #         else:
    #     #             features_dists = features - class_mean
    #     #         index_selected = np.argmin(torch.norm(features_dists, p=2, dim=1))
    #     #         if index_selected in list_of_selected:
    #     #             raise ValueError("Exemplars should not be repeated!!!!")
    #     #         list_of_selected.append(index_selected)
    #     #
    #     #         exemplar_set.append(dataset[index_selected][0].numpy())
    #     #         exemplar_features[k] = copy.deepcopy(features[index_selected])
    #     #
    #     #         # make sure this example won't be selected again
    #     #         features[index_selected] = features[index_selected] + 10000
    #     # else:
    #     indeces_selected = np.random.choice(n_max, size=min(n, n_max), replace=False)
    #     for k in indeces_selected:
    #         exemplar_set.append(dataset[k][0].numpy())
    #
    #     # add this [exemplar_set] as a [n]x[ich]x[isz]x[isz] to the list of [exemplar_sets]
    #     self.exemplar_sets.append(np.array(exemplar_set))
    #
    #     # set mode of model back
    #     self.train(mode=mode)

    def _compute_ref_grad(self, active_classes=None):
        # (chongyi zheng): We compute reference gradients for actor and critic separately
        # assert isinstance(named_parameters, Iterable), "'named_parameters' must be a iterator"
        #
        # si_losses = []
        # for name, param in named_parameters:
        #     if param.requires_grad:
        #         prev_param = self.prev_task_params[name]
        #         omega = self.omegas.get(name, torch.zeros_like(param))
        #         si_loss = torch.sum(omega * (param - prev_param) ** 2)
        #         si_losses.append(si_loss)
        #
        # return torch.sum(torch.stack(si_losses))

        if not self.agem_memories:
            return None

        # sample memory transitions
        # sample memory transitions
        concat_x = []
        concat_y = []
        for mem in self.agem_memories.values():
            concat_x.append(mem['x'])
            concat_y.append(mem['y'])

        concat_x = torch.cat(concat_x)
        concat_y = torch.cat(concat_y)

        perm_idxs = np.random.permutation(concat_x.shape[0])
        sample_idxs = np.random.randint(0, len(concat_x), size=self.ref_grad_batch_size)
        x_ = concat_x[perm_idxs][sample_idxs].to(self.device())
        y_ = concat_y[perm_idxs][sample_idxs].to(self.device())
        # x_ = concat_x[sample_idxs].to(self.device())
        # y_ = concat_y[sample_idxs].to(self.device())

        # y_ = [y_]
        # active_classes = [active_classes]

        # Run model (if [x_] is not a list with separate replay per task and there is no task-specific mask)
        y_hat_all = self(x_)

        # Loop to evalute predictions on replay according to each previous task
        # y_hats = []
        # for replay_id in range(self.agem_task_count):
        #     # -if needed (e.g., Task-IL or Class-IL scenario), remove predictions for classes not in replayed task
        #     y_hats.append(y_hat_all[:, active_classes[replay_id]])
        # y_hats = y_hat_all[:, np.array(active_classes)[:-2]]
        y_hats = y_hat_all[:, active_classes]

        # Calculate losses
        loss = F.cross_entropy(y_hats, y_, reduction='mean')
        loss.backward()

        # reorganize the gradient of the memory transitions as a single vector
        ref_grad = []
        for name, param in self.named_parameters():
            if param.requires_grad:
                ref_grad.append(param.grad.detach().clone().flatten())
                # ref_grad.append(param.grad.view(-1))
        ref_grad = torch.cat(ref_grad)
        # reset gradients (with A-GEM, gradients of memory transitions should only be used as inequality constraint)
        self.optimizer.zero_grad()

        # # sample memory transitions
        # obses = []
        # actions = []
        # rewards = []
        # next_obses = []
        # not_dones = []
        # for mem in self.agem_memories.values():
        #     idxs = np.random.randint(0, len(mem['rewards']))
        #     obses.append(mem['obses'][idxs])
        #     actions.append(mem['actions'][idxs])
        #     rewards.append(mem['rewards'][idxs])
        #     next_obses.append(mem['next_obses'][idxs])
        #     not_dones.append(mem['not_dones'][idxs])
        #
        # obses = torch.cat(obses)
        # actions = torch.cat(actions)
        # rewards = torch.cat(rewards)
        # next_obses = torch.cat(next_obses)
        # not_dones = torch.cat(not_dones)
        #
        # # reference critic gradients
        # ref_critic_loss = self.compute_critic_loss(obses, actions, rewards, next_obses, not_dones)
        # ref_critic_loss.backward()
        #
        # # reorganize the gradient of the memory transitions as a single vector
        # ref_critic_grad = []
        # for name, param in self.critic.named_parameters():
        #     if param.requires_grad:
        #         ref_critic_grad.append(param.grad.detach().clone().flatten())
        # ref_critic_grad = torch.cat(ref_critic_grad)
        # # reset gradients (with A-GEM, gradients of memory transitions should only be used as inequality constraint)
        # self.critic_optimizer.zero_grad()
        #
        # # reference actor and alpha gradients
        # _, ref_actor_loss, ref_alpha_loss = self.compute_actor_and_alpha_loss(
        #     obses, compute_alpha_loss=compute_alpha_ref_grad)
        # ref_actor_grad = []
        # for name, param in self.actor.named_parameters():
        #     if param.requires_grad:
        #         ref_actor_grad.append(param.grad.detach().clone().flatten())
        # ref_actor_grad = torch.cat(ref_actor_grad)
        # self.actor_optimizer.zero_grad()
        #
        # ref_alpha_grad = None
        # if compute_alpha_ref_grad:
        #     ref_alpha_grad = self.log_alpha.grad.detach().clone()
        #     self.log_alpha_optimizer.zero_grad()

        return ref_grad

    def _project_grad(self, ref_grad):
        if ref_grad is None:
            return

        grad = []
        for name, param in self.named_parameters():
            if param.requires_grad:
                grad.append(param.grad.flatten())
                # grad.append(param.grad.view(-1))
        grad = torch.cat(grad)

        # inequality constrain
        angle = (grad * ref_grad).sum()
        if angle < 0:
            # project the gradient of the current transitions onto the gradient of the memory transitions ...
            proj_grad = grad - (angle / (ref_grad * ref_grad).sum()) * ref_grad
            # replace all the gradients within the model with this projected gradient
            idx = 0
            for _, param in self.named_parameters():
                if param.requires_grad:
                    num_param = param.numel()  # number of parameters in [p]
                    param.grad.copy_(proj_grad[idx:idx + num_param].reshape(param.shape))
                    # param.grad.copy_(proj_grad[idx:idx + num_param].view_as(param))
                    idx += num_param

    def construct_memory(self, dataset):
        memory_size_per_task = self.memory_budget // (self.agem_task_count + 1)
        self._adjust_memory_size(memory_size_per_task)

        size_max = len(dataset)
        indeces_selected = np.random.choice(size_max, size=min(memory_size_per_task, size_max), replace=False)
        xs = []
        ys = []
        for k in indeces_selected:
            xs.append(dataset[k][0])
            ys.append(dataset[k][1])

        self.agem_memories[self.agem_task_count] = {
            'x': torch.cat(xs),
            'y': torch.tensor(ys)
        }

        self.agem_task_count += 1

    def train_a_batch(self, x, y, active_classes=None):
        # Set model to training-mode
        self.train()

        # Reset optimizer
        self.optimizer.zero_grad()


        ##--(1)-- REPLAYED DATA --##

        # if x_ is not None:
        ref_grad = self._compute_ref_grad(active_classes=active_classes)

        # Calculate total replay loss
        # loss_replay = None if (x_ is None) else sum(loss_replay) / n_replays

        # If using A-GEM, calculate and store averaged gradient of replayed data
        # if x_ is not None:
        #     # Perform backward pass to calculate gradient of replayed batch (if not yet done)
        #     loss_replay.backward()
        #
        #     # Reorganize the gradient of the replayed batch as a single vector
        #     grad_rep = []
        #     for p in self.parameters():
        #         if p.requires_grad:
        #             grad_rep.append(p.grad.view(-1))
        #     grad_rep = torch.cat(grad_rep)
        #     # Reset gradients (with A-GEM, gradients of replayed batch should only be used as inequality constraint)
        #     self.optimizer.zero_grad()

        ##--(2)-- CURRENT DATA --##
        if x is not None:
            # Run model
            y_hat = self(x)
            # -if needed, remove predictions for classes not in current task
            if active_classes is not None:
                class_entries = active_classes[-1] if type(active_classes[0])==list else active_classes
                y_hat = y_hat[:, class_entries]

            # Calculate prediction loss
            # -multiclass prediction loss
            predL = None if y is None else F.cross_entropy(input=y_hat, target=y, reduction='mean')

            # Weigh losses
            loss_cur = predL

            # Calculate training-precision
            precision = None if y is None else (y == y_hat.max(1)[1]).sum().item() / x.size(0)

        else:
            precision = predL = None
            # -> it's possible there is only "replay" [e.g., for offline with task-incremental learning]


        # Combine loss from current and replayed batch
        loss_total = loss_cur

        ##--(3)-- ALLOCATION LOSSES --##
        # Backpropagate errors (if not yet done)
        loss_total.backward()

        # If using A-GEM, potentially change gradient:
        # if x_ is not None:
        #     # -reorganize gradient (of current batch) as single vector
        #     grad_cur = []
        #     for p in self.parameters():
        #         if p.requires_grad:
        #             grad_cur.append(p.grad.view(-1))
        #     grad_cur = torch.cat(grad_cur)
        #     # -check inequality constrain
        #     angle = (grad_cur * grad_rep).sum()
        #     if angle < 0:
        #         # -if violated, project the gradient of the current batch onto the gradient of the replayed batch ...
        #         length_rep = (grad_rep*grad_rep).sum()
        #         grad_proj = grad_cur-(angle/length_rep)*grad_rep
        #         # -...and replace all the gradients within the model with this projected gradient
        #         index = 0
        #         for p in self.parameters():
        #             if p.requires_grad:
        #                 n_param = p.numel()  # number of parameters in [p]
        #                 p.grad.copy_(grad_proj[index:index+n_param].view_as(p))
        #                 index += n_param
        self._project_grad(ref_grad)

        # Take optimization-step
        self.optimizer.step()

        # Return the dictionary with different training-loss split in categories
        return {
            'loss_total': loss_total.item(),
            'pred': predL.item() if predL is not None else 0,
            'precision': precision if precision is not None else 0.,
        }

