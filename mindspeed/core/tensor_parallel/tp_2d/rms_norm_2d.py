# Copyright 2024 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
from typing import Any
from typing import Tuple

import torch
import torch.distributed as dist
from torch import Tensor
from torch import nn
from torch.cuda.amp import custom_bwd
from torch.cuda.amp import custom_fwd
from mindspeed.core.tensor_parallel.tp_2d.group_api_2d import CollectiveCommIntf
from mindspeed.core.tensor_parallel.tp_2d.group_api_2d import TPYCollectiveComm

from mindspeed.core.tensor_parallel.tp_2d.utils import divide


class RMSNorm2D(torch.nn.Module):

    def __init__(self,
                 hidden_size: int,
                 eps: float = 1e-6,
                 last_dim_split_comm_intf: CollectiveCommIntf = TPYCollectiveComm()):
        """RMS Normaliation 2d module

        Args:
            hidden_size (int): The width of input, i.e. hidden size
            eps (float): epsilon to use for the norm, default to 1e-6
            last_dim_split_comm_intf: All-reduce at last dim comm intf.
        """
        super().__init__()
        self.eps = eps
        self.hidden_size = hidden_size
        self.last_dim_split_comm_intf = last_dim_split_comm_intf
        self.last_dim_split_comm_world_sz = self.last_dim_split_comm_intf.get_comm_group_world_size()
        # partitioning dimension
        self.partitioned_dim = divide(hidden_size, self.last_dim_split_comm_world_sz)
        self.weight = nn.Parameter(torch.ones(self.partitioned_dim))

        setattr(self.weight, "2d_tp", True)

    def forward(self, x):
        return _ParallelRMSNorm2D.apply(
            x,
            self.weight,
            self.eps,
            self.hidden_size,
            self.last_dim_split_comm_intf,
        )


class _ParallelRMSNorm2D(torch.autograd.Function):
    @staticmethod
    @custom_fwd
    def forward(
            ctx: Any,
            input_: Tensor,
            weight,
            epsilon,
            hidden_size: int,
            last_dim_split_comm_intf: CollectiveCommIntf,
    ) -> Tensor:
        # input_ inner: [s/cp, b, h/xy]
        # input_ outer: [s/(cp*x), b, h/y]
        ctx.last_dim_split_comm_intf = last_dim_split_comm_intf
        ctx.hidden_size = hidden_size
        pow_mean = input_.float().pow(2).mean(-1, keepdim=True)
        torch.distributed.all_reduce(pow_mean, group=last_dim_split_comm_intf.get_comm_group())
        var_x = torch.rsqrt(pow_mean.div_(last_dim_split_comm_intf.get_comm_group_world_size()) + epsilon)
        norm_x = torch.mul(input_, var_x).type_as(input_)
        ctx.save_for_backward(norm_x, var_x, weight)
        return norm_x * weight

    @staticmethod
    @custom_bwd
    def backward(ctx: Any, output_grad: Tensor) -> Tuple[Tensor, ...]:
        x, var_x, weight = ctx.saved_tensors
        grad_norm_x = torch.mul(output_grad, weight)
        output_grad_mul_x_sum = torch.sum(grad_norm_x * x, dim=-1, keepdim=True)
        handle_grad = torch.distributed.all_reduce(
            output_grad_mul_x_sum, group=ctx.last_dim_split_comm_intf.get_comm_group(), async_op=True)
        # calculate grad_weight
        grad_weight = torch.mul(output_grad, x)
        if handle_grad:
            handle_grad.wait()
        grad_input = (grad_norm_x - x * (output_grad_mul_x_sum / ctx.hidden_size)) * var_x
        return grad_input, grad_weight, None, None, None
