# coding=utf-8
# Copyright (c) 2024, Huawei Technologies Co., Ltd. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Copyright (c) 2022, NVIDIA CORPORATION. All rights reversed.
# Copyright (c) Huawei Technologies Co., Ltd. 2024. All rights reserved.
import contextlib
import torch
from functools import wraps
from megatron.core.enums import ModelType
from megatron.training import get_args
from megatron.core import parallel_state
from megatron.core.utils import get_attr_wrapped_model, get_model_config, get_model_type
from megatron.core.transformer.moe.router import MoEAuxLossAutoScaler
from mindspeed.core.pipeline_parallel import flexible_schedules
from mindspeed.core.pipeline_parallel.ripipe_schedules import forward_backward_ripipe_pipelining

LOSS_BACKWARD_SCALE = torch.tensor(1.0)


def get_forward_backward_func_wrapper(get_forward_backward_func):
    @wraps(get_forward_backward_func)
    def wrapper(*args, **kwargs):
        arguments = get_args()
        if arguments.optimize_send_recv_comm and arguments.num_layers_per_virtual_pipeline_stage is None:
            return flexible_schedules.forward_backward_pipelining_without_interleaving

        if arguments.automated_pipeline_perf and arguments.pp_schedule_list:
            return flexible_schedules.forward_backward_pipelining_without_interleaving

        if arguments.recompute_in_bubble or arguments.recompute_in_advance:
            return forward_backward_ripipe_pipelining

        if parallel_state.get_pipeline_model_parallel_world_size() > 1 \
            and parallel_state.get_virtual_pipeline_model_parallel_world_size() is not None \
            and arguments.use_nanopipe:
            return flexible_schedules.forward_backward_pipelining_with_interleaving_nano_pipe
        return get_forward_backward_func(*args, **kwargs)
    return wrapper


def forward_step(
    forward_step_func,
    data_iterator,
    model,
    num_microbatches,
    input_tensor,
    forward_data_store,
    config,
    collect_non_loss_data=False,
    checkpoint_activations_microbatch=None,
    is_first_microbatch=False,
):

    """Forward step for passed-in model.

    If first stage, input tensor is obtained from data_iterator, otherwise
    passed-in input_tensor is used.

    Returns output tensor."""
    if config.timers is not None:
        config.timers('forward-compute', log_level=2).start()

    if is_first_microbatch and hasattr(model, 'set_is_first_microbatch'):
        model.set_is_first_microbatch()

    unwrap_output_tensor = False
    if not isinstance(input_tensor, list):
        input_tensor = [input_tensor]
        unwrap_output_tensor = True

    set_input_tensor = get_attr_wrapped_model(model, "set_input_tensor")
    set_input_tensor(input_tensor)

    if config.enable_autocast:
        context_manager = torch.autocast("cuda", dtype=config.autocast_dtype)
    else:
        context_manager = contextlib.nullcontext()
    with context_manager:
        if checkpoint_activations_microbatch is None:
            output_tensor, loss_func = forward_step_func(data_iterator, model)
        else:
            output_tensor, loss_func = forward_step_func(
                data_iterator, model, checkpoint_activations_microbatch
            )

    if parallel_state.is_pipeline_last_stage():
        if not collect_non_loss_data:
            output_tensor = loss_func(output_tensor)
            loss, loss_reduced = output_tensor
            output_tensor = loss / num_microbatches
            forward_data_store.append(loss_reduced)
        else:
            data = loss_func(output_tensor, non_loss_data=True)
            forward_data_store.append(data)

    if config.timers is not None:
        config.timers('forward-compute').stop()

    # Set the loss scale for the auxiliary loss of the MoE layer.
    # Since we use a trick to do backward on the auxiliary loss, we need to set the scale explicitly.
    if hasattr(config, 'num_moe_experts') and config.num_moe_experts is not None:
        # Calculate the loss scale based on the grad_scale_func if available, else default to 1.
        loss_scale = (
            config.grad_scale_func(LOSS_BACKWARD_SCALE)
            if config.grad_scale_func is not None
            else torch.tensor(1.0)
        )
        # Set the loss scale
        MoEAuxLossAutoScaler.set_loss_scale(loss_scale / num_microbatches)

    # If T5 model (or other model with encoder and decoder)
    # and in decoder stack, then send encoder_hidden_state
    # downstream as well.
    model_type = get_model_type(model)
    if (
        parallel_state.is_pipeline_stage_after_split()
        and model_type == ModelType.encoder_and_decoder
    ):
        return [output_tensor, input_tensor[-1]]
    if unwrap_output_tensor:
        return output_tensor
    return [output_tensor]
 
 
def get_tensor_shapes_wrapper(get_tensor_shapes):
    @wraps(get_tensor_shapes)
    def wrapper(*args, **kwargs):
        # [s, b, h]
        tensor_shapes = get_tensor_shapes(*args, **kwargs)
        arguments = get_args()
        if arguments.tp_2d:
            tensor_shapes = [[tensor_shape[0] // arguments.tp_x, tensor_shape[1], tensor_shape[2] // arguments.tp_y]
                             for tensor_shape in tensor_shapes]
 
        return tensor_shapes
    return wrapper
