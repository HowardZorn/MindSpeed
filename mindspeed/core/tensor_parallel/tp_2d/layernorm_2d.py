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
from torch.cuda.amp import custom_bwd
from torch.cuda.amp import custom_fwd
from torch.nn import Parameter

from mindspeed.core.tensor_parallel.tp_2d.group_api_2d import CollectiveCommIntf
from mindspeed.core.tensor_parallel.tp_2d.group_api_2d import TPYCollectiveComm

from mindspeed.core.tensor_parallel.tp_2d.utils import divide


class LayerNorm2D(torch.nn.Module):
    """LayerNorm2D layer with row and column parallelism.

    Arguments:
        hidden_size (int): input normalized size from an expected input of size
        eps: a value added to the denominator for numerical stability. Default: 1e-5
        bias: (bool, optional): Whether to add a bias, defaults to ``True``.
        dtype: (:class:`torch.dtype`, optional): The dtype of parameters, defaults to None.
        last_dim_split_comm_intf: Reduce scatter comm intf.
    """

    def __init__(
        self,
        hidden_size: int,
        eps: float = 1e-5,
        bias: bool = True,
        dtype=None,
        last_dim_split_comm_intf: CollectiveCommIntf = TPYCollectiveComm(),
    ) -> None:
        super(LayerNorm2D, self).__init__()
        # layer norm config
        self.hidden_size = hidden_size
        self.epsilon = eps

        # parallel setting
        self.last_dim_split_comm_intf = last_dim_split_comm_intf
        self.rs_comm_world_sz = self.last_dim_split_comm_intf.get_comm_group_world_size()
        # partitioning dimension
        self.partitioned_dim = divide(hidden_size, self.rs_comm_world_sz)
        # create parameters
        factory_kwargs = {"device": torch.cuda.current_device(), "dtype": dtype}

        # [H/(xy)]
        self.weight = Parameter(torch.ones(self.partitioned_dim, **factory_kwargs))
        if bias:
            # [H/(xy)]
            self.bias = Parameter(torch.zeros(self.partitioned_dim, **factory_kwargs))
        else:
            self.bias = None

        # set sequence parallelism flag on weight and bias parameters
        setattr(self.weight, "2d_tp", True)
        setattr(self.bias, "2d_tp", True)

    def forward(self, x: Tensor) -> Tensor:
        return _ParallelLayerNorm2D.apply(
            x,
            self.weight,
            self.bias,
            self.epsilon,
            self.hidden_size,
            self.last_dim_split_comm_intf,
        )


class _ParallelLayerNorm2D(torch.autograd.Function):
    @staticmethod
    @custom_fwd
    def forward(
        ctx: Any,
        input_: Tensor,
        weight,
        bias,
        epsilon,
        hidden_size: int,
        last_dim_split_comm_intf: CollectiveCommIntf
    ) -> Tensor:
        """

        :param ctx:
        :param input_:  [s/(cp*x), b,  H/y]
        :param weight:  [H/(xy)]
        :param bias:    [H/(xy)]
        :param epsilon:
        :param hidden_size: H
        :param last_dim_split_comm_intf:
        :return:
        """
        # [s/(cp*x), b,  H/y]---> [s/(cp*x), b,  1]
        e_x = torch.sum(input_, dim=-1, keepdim=True)
        # [s/(cp*x), b,  1]
        handle_ex = torch.distributed.all_reduce(
            e_x, group=last_dim_split_comm_intf.get_comm_group(), async_op=True
        )

        # [s/(cp*x), b,  H/y]---> [s/(cp*x), b,  1]
        var_x = torch.sum(input_.float().pow(2), dim=-1, keepdim=True)
        if handle_ex:
            handle_ex.wait()

        handle_var = torch.distributed.all_reduce(
            var_x, group=last_dim_split_comm_intf.get_comm_group(), async_op=True
        )

        input_.sub_(e_x.div_(hidden_size))
        e_x.mul_(e_x)
        if handle_var:
            handle_var.wait()

        var_x = torch.rsqrt(var_x.div_(hidden_size).sub_(e_x).add_(epsilon))

        ctx.hidden_size = hidden_size
        ctx.last_dim_split_comm_intf = last_dim_split_comm_intf
        # [s/(cp*x), b,  H/y] * [s/(cp*x), b,  1] --> [s/(cp*x), b,  H/y]
        norm_x = torch.mul(input_, var_x)

        if bias is not None:
            # bias + weight * norm, [H/y] + [H/y] *  [s/(cp*x), b,  H/y]
            output = torch.addcmul(bias, weight, norm_x)
        else:
            output = torch.mul(weight, norm_x)

        ctx.save_for_backward(norm_x, var_x, bias, weight)
        return output

    @staticmethod
    @custom_bwd
    def backward(ctx: Any, output_grad: Tensor) -> Tuple[Tensor, ...]:
        x, var_x, bias, weight = ctx.saved_tensors
        # calculate grad_bias
        if bias is None:
            grad_bias = None
        else:
            grad_bias = output_grad.sum(dim=(0, 1))

        # calculate grad_input
        grad_norm_x = torch.mul(output_grad, weight)
        output_grad_sum = torch.sum(grad_norm_x, dim=-1, keepdim=True)
        handle_grad_sum = torch.distributed.all_reduce(
            output_grad_sum, group=ctx.last_dim_split_comm_intf.get_comm_group(), async_op=True
        )
        output_grad_mul_x_sum = torch.sum(grad_norm_x * x, dim=-1, keepdim=True)

        # calculate grad_weight
        grad_weight = torch.mul(output_grad, x)
        grad_weight = grad_weight.sum(dim=(0, 1))

        if handle_grad_sum:
            handle_grad_sum.wait()

        handle_grad_mul_x = torch.distributed.all_reduce(
            output_grad_mul_x_sum, group=ctx.last_dim_split_comm_intf.get_comm_group(), async_op=True
        )
        output_grad_sum.div_(ctx.hidden_size)
        grad_input = grad_norm_x.sub(output_grad_sum)
        if handle_grad_mul_x:
            handle_grad_mul_x.wait()

        grad_input = (grad_input - x * (output_grad_mul_x_sum / ctx.hidden_size)) * var_x
        return grad_input, grad_weight, grad_bias, None, None, None
