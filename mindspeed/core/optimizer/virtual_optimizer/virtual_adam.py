# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
# Copyright (c) 2025, Huawei Technologies Co., Ltd. All rights reserved.

import torch
import torch_npu

from mindspeed.optimizer.adamw import adamw


def virtual_optimizer_replace(optimizer, virtual_allocator):
    if not optimizer.state:
        return
    for group in optimizer.param_groups:
        for p in group['params']:
            state = optimizer.state[p]
            state["exp_avg"] = virtual_allocator.copy2swap(state["exp_avg"])
            state["exp_avg_sq"] = virtual_allocator.copy2swap(state["exp_avg_sq"])


def virtual_optimizer_step_impl(self, closure=None):
    loss = None
    if closure is not None:
        with torch.enable_grad():
            loss = closure()

    for group in self.param_groups:
        params_with_grad = []
        grads = []
        exp_avgs = []
        exp_avg_sqs = []
        state_sums = []
        max_exp_avg_sqs = []
        state_steps = []
        amsgrad = group['amsgrad']
        beta1, beta2 = group['betas']

        if 'step' in group:
            group['step'] += 1
            if group['step'].is_cpu:
                group['step'] = group['step'].cuda()
        else:
            group['step'] = torch.tensor(1, dtype=torch.int64, device=torch.cuda.current_device())

        for p in group['params']:
            if p.grad is None:
                continue
            params_with_grad.append(p)
            if p.grad.is_sparse:
                raise RuntimeError('AdamW does not support sparse gradients')
            grads.append(p.grad)

            state = self.state[p]

            # State initialization
            if len(state) == 0:
                state['exp_avg'], state['exp_avg_sq'] = self.virtual_allocator.init_exp(p)
                if amsgrad:
                    # Maintains max of all exp. moving avg. of sq. grad. values
                    state['max_exp_avg_sq'] = torch.zeros_like(p, memory_format=torch.preserve_format)

            exp_avgs.append(state['exp_avg'])
            exp_avg_sqs.append(state['exp_avg_sq'])

            if amsgrad:
                max_exp_avg_sqs.append(state['max_exp_avg_sq'])

        adamw(params_with_grad,
            grads,
            exp_avgs,
            exp_avg_sqs,
            max_exp_avg_sqs,
            group['step'],
            amsgrad=amsgrad,
            beta1=beta1,
            beta2=beta2,
            lr=group['lr'],
            weight_decay=group['weight_decay'],
            eps=group['eps'],
            maximize=group['maximize'])

    self.virtual_allocator.print_swap_size(self.print_swap_flag)
    return loss


class VirtualAllocator:
    def __init__(self, pp_rank, pp_stages, virtual_optimizer_size=None):
        """
        Initializes an VirtualAllocator instance.

        Args:
            pp_rank (int): pipeline model rank number.
            pp_stages (int): 
            virtual_optimizer_size (float, List[float], Optional): 
        """
        self.pp_stages = pp_stages
        self.pp_rank = pp_rank
        self.virtual_optimizer_size = virtual_optimizer_size
        self.swap_size_this_pp_rank = self.get_swap_memory_size()[self.pp_rank] * (1024 ** 3)
        self.actually_swap_size = 0
        self.print_flag = False

    def get_swap_memory_size(self):
        """
        Obtain the swap size of each pipeline rank based on the given parameters.
        """
        swap_sizes = self.virtual_optimizer_size
        if swap_sizes is not None:
            if len(swap_sizes) == 1:
                return [swap_sizes[0] for _ in range(self.pp_stages)]
            elif len(swap_sizes) == self.pp_stages:
                return swap_sizes
            else:
                raise ValueError("Virtual_optimizer swap size do not match pp_stages or swap ALL.")
        else:
            return [0 for _ in range(self.pp_stages)]

    def init_exp(self, p: torch.Tensor):
        """
        Create exp_avg and exp_avg_sq based on input param.
        
        Args:
            p (tensor): param tensor.
        """
        exp_avg = self.create(p)
        exp_avg_sq = self.create(p)
        return exp_avg, exp_avg_sq

    def create(self, p: torch.Tensor):
        """
        Select the tensor construction mode based on the size of each PP swap.

        Args:
            p (tensor): param tensor.
        """
        if self.swap_size_this_pp_rank > 0:
            return self.get_swap_memory(p)
        else:
            return self.get_npu_memory(p)
    
    def copy2swap(self, input_: torch.Tensor):
        """
        Copy input_ to swap tensor.

        Args:
            input_ (torch.Tensor): Switch input_ to swap type.
        """
        swap = self.create(input_)
        swap.copy_(input_)
        return swap

    def get_swap_memory(self, p: torch.Tensor):
        """
        Construct tensors in virtual memory. 

        Args:
            p (tensor): param tensor.
        """
        if not hasattr(torch_npu, "empty_with_swapped_memory"):
            return self.get_npu_memory(p)
        try:
            swap_tensor = torch_npu.empty_with_swapped_memory(p.size(), device=p.device)
            setattr(swap_tensor, "swap_tensor", True)
            setattr(swap_tensor.data, "swap_tensor", True)
            swap_tensor.zero_()
            tensor_size = p.numel() * p.element_size()
            self.actually_swap_size += tensor_size / 1024 / 1024
            self.swap_size_this_pp_rank -= tensor_size
            return swap_tensor
        except Exception as e:
            print(f"[Warning] Swap memory alloc failed: {e}")
            return self.get_npu_memory(p)
    
    def get_npu_memory(self, p: torch.Tensor):
        """
        Construct tensors in hbm memory. 

        Args:
            p (tensor): param tensor.
        """
        return torch.zeros_like(p, memory_format=torch.preserve_format)
    
    def print_swap_size(self, print_swap_flag):
        """
        Print swap size when `print_swap_flag` is true.

        Args:
            print_swap_flag (bool): Whether to print the swap size.
        """
        if print_swap_flag:
            print(f"[Swap virtual-optimizer Summary: Rank {torch.distributed.get_rank()}] Swap {self.actually_swap_size:.5f} MB")
