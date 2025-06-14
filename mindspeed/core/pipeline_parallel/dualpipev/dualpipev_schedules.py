# Copyright (c) 2022, NVIDIA CORPORATION. All rights reserved.
# Copyright (c) 2025, Huawei Technologies Co., Ltd. All rights reserved.

import contextlib
from functools import wraps
from typing import Iterator, List, Union

import torch

from megatron.core import parallel_state
from megatron.core.enums import ModelType
from megatron.training import get_args
from megatron.core.transformer.moe.router import MoEAuxLossAutoScaler
from megatron.core.utils import (
    get_attr_wrapped_model,
    get_model_config,
    get_model_type,
)
from megatron.core.pipeline_parallel.schedules import clear_embedding_activation_buffer, deallocate_output_tensor
from megatron.core import ModelParallelConfig
from megatron.core.pipeline_parallel.p2p_communication import _communicate
from megatron.core.pipeline_parallel.schedules import backward_step, set_current_microbatch, custom_backward, finish_embedding_wgrad_compute
from megatron.core.models.gpt import GPTModel
from mindspeed.core.transformer.moe.moe_feature.fb_overlap.gpt_model import gpt_model_backward
from mindspeed.core.transformer.moe.moe_feature.fb_overlap.transformer_layer import P2PCommParams
from mindspeed.core.transformer.moe.moe_feature.fb_overlap.modules.weight_grad_store import WeightGradStore
from mindspeed.core.transformer.moe.moe_feature.fb_overlap.modules.utils import detach_tensor
from mindspeed.core.transformer.moe.moe_feature.fb_overlap.vpp_schedules import forward_step_func_wrapper


def detach_tensor(tensor, checkpoint_forward=False):
    if checkpoint_forward:
        return tensor
    if tensor is None:
        return None
    detached_tensor = tensor.detach()
    detached_tensor.requires_grad = True
    return detached_tensor

# Types
Shape = Union[List[int], torch.Size]
LOSS_BACKWARD_SCALE = torch.tensor(1.0)

_DUALPIPE_CHUNK = None
_POST_CHUNK = None


def set_dualpipe_chunk(chunkid):
    """set_dualpipe_chunk for fp16forward patch"""
    global _DUALPIPE_CHUNK
    _DUALPIPE_CHUNK = chunkid


def get_dualpipe_chunk():
    global _DUALPIPE_CHUNK
    if _DUALPIPE_CHUNK is not None:
        return _DUALPIPE_CHUNK
    else:
        raise AssertionError("_DUALPIPE_CHUNK is None")


def set_post_process_flag(flag):
    """set_postprocess_chunk for mtp position id"""
    global _POST_CHUNK
    _POST_CHUNK = flag


def get_post_process_flag():
    global _POST_CHUNK
    if _POST_CHUNK is not None:
        return _POST_CHUNK
    else:
        raise AssertionError("_POST_CHUNK is None")


def is_dualpipev_last_stgae(model_chunk_id):
    return parallel_state.is_pipeline_first_stage() and model_chunk_id == 1


def send_forward(output_tensor: torch.Tensor, tensor_shape, config: ModelParallelConfig, model_chunk_id, async_op=False) -> None:
    """Send tensor to next rank in pipeline (forward send).

    See _communicate for argument details.
    """
    tensor_send_next, tensor_send_prev = None, None
    if model_chunk_id == 0:
        if parallel_state.is_pipeline_last_stage():
            return None
        tensor_send_next = output_tensor
    else:
        if parallel_state.is_pipeline_first_stage():
            return None
        tensor_send_prev = output_tensor

    if config.timers is not None:
        config.timers('forward-send', log_level=2).start()

    _, _, fwd_wait_handles = _communicate(
        tensor_send_next=tensor_send_next,
        tensor_send_prev=tensor_send_prev,
        recv_prev=False,
        recv_next=False,
        tensor_shape=tensor_shape,
        config=config,
        wait_on_reqs=(not async_op)
    )
    if config.timers is not None:
        config.timers('forward-send').stop()

    return fwd_wait_handles


def send_backward(input_tensor_grad: torch.Tensor, tensor_shape, config: ModelParallelConfig, model_chunk_id, async_op=False) -> None:
    """Send tensor to next rank in pipeline (forward send).

    See _communicate for argument details.
    """

    tensor_send_next, tensor_send_prev = None, None
    if model_chunk_id == 0:
        if parallel_state.is_pipeline_first_stage():
            return None
        tensor_send_prev = input_tensor_grad
    else:
        if parallel_state.is_pipeline_last_stage():
            return None
        tensor_send_next = input_tensor_grad

    if config.timers is not None:
        config.timers('backward-send', log_level=2).start()
    _, _, reqs = _communicate(
        tensor_send_next=tensor_send_next,
        tensor_send_prev=tensor_send_prev,
        recv_prev=False,
        recv_next=False,
        tensor_shape=tensor_shape,
        config=config,
        wait_on_reqs=(not async_op)
    )
    if config.timers is not None:
        config.timers('backward-send').stop()
    return reqs


def recv_forward(tensor_shape: Shape, config: ModelParallelConfig, model_chunk_id, async_op=False) -> torch.Tensor:
    """ Receive tensor from previous rank in pipeline (forward receive).

    See _communicate for argument details.
    """
    recv_prev, recv_next = False, False
    if model_chunk_id == 0:
        recv_prev = True
    else:
        recv_next = True

    if (parallel_state.is_pipeline_first_stage() and recv_prev) or (parallel_state.is_pipeline_last_stage() and recv_next):
        fwd_wait_handles = None
        return None, fwd_wait_handles
    else:
        if config.timers is not None:
            config.timers('forward-recv', log_level=2).start()
        tensor_recv_prev, tensor_recv_next, fwd_wait_handles = _communicate(
            tensor_send_next=None,
            tensor_send_prev=None,
            recv_prev=recv_prev,
            recv_next=recv_next,
            tensor_shape=tensor_shape,
            config=config,
            wait_on_reqs=(not async_op),
        )
        if config.timers is not None:
            config.timers('forward-recv').stop()

    if recv_prev:
        return tensor_recv_prev, fwd_wait_handles
    else:
        return tensor_recv_next, fwd_wait_handles


def recv_backward(tensor_shape: Shape, config: ModelParallelConfig, model_chunk_id, async_op=False) -> torch.Tensor:
    """Receive tensor from next rank in pipeline (backward receive).

    See _communicate for argument details.
    """
    recv_prev, recv_next = False, False
    if model_chunk_id == 0:
        recv_next = True
    else:
        recv_prev = True

    if (parallel_state.is_pipeline_first_stage() and recv_prev) or (parallel_state.is_pipeline_last_stage() and recv_next):
        output_tensor_grad = None
        bwd_wait_handles = None
        return output_tensor_grad, bwd_wait_handles
    else:

        if config.timers is not None:
            config.timers('backward-recv', log_level=2).start()
        tensor_recv_prev, tensor_recv_next, bwd_wait_handles = _communicate(
            tensor_send_next=None,
            tensor_send_prev=None,
            recv_prev=recv_prev,
            recv_next=recv_next,
            tensor_shape=tensor_shape,
            config=config,
            wait_on_reqs=(not async_op)
        )
        if config.timers is not None:
            config.timers('backward-recv').stop()

    if recv_prev:
        return tensor_recv_prev, bwd_wait_handles
    else:
        return tensor_recv_next, bwd_wait_handles


def send_forward_recv_forward(
    output_tensor: torch.Tensor,
    tensor_shape: Shape,
    config: ModelParallelConfig,
    model_chunk_id,
    async_op=False
) -> torch.Tensor:
    """Batched recv from previous rank and send to next rank in pipeline.

    See _communicate for argument details.
    """
    recv_prev, recv_next = False, False
    tensor_send_next, tensor_send_prev = None, None
    if model_chunk_id == 0:
        if not parallel_state.is_pipeline_last_stage():
            tensor_send_next = output_tensor
        if not parallel_state.is_pipeline_first_stage():
            recv_prev = True
    if model_chunk_id == 1:
        if not parallel_state.is_pipeline_first_stage():
            tensor_send_prev = output_tensor
        if not parallel_state.is_pipeline_last_stage():
            recv_next = True

    if config.timers is not None:
        config.timers('forward-send-forward-recv', log_level=2).start()
    tensor_recv_prev, tensor_recv_next, fwd_wait_handles = _communicate(
        tensor_send_next=tensor_send_next,
        tensor_send_prev=tensor_send_prev,
        recv_prev=recv_prev,
        recv_next=recv_next,
        tensor_shape=tensor_shape,
        wait_on_reqs=(not async_op),
        config=config
    )
    if config.timers is not None:
        config.timers('forward-send-forward-recv').stop()

    if model_chunk_id == 0:
        if not parallel_state.is_pipeline_first_stage():
            return tensor_recv_prev, fwd_wait_handles
        else:
            return None, fwd_wait_handles
    else:
        if not parallel_state.is_pipeline_last_stage():
            return tensor_recv_next, fwd_wait_handles
        else:
            return None, fwd_wait_handles


def send_forward_recv_slave_forward(
    output_tensor: torch.Tensor,
    tensor_shape: Shape,
    config: ModelParallelConfig,
    model_chunk_id,
    async_op=False,
) -> torch.Tensor:
    """Batched recv from previous rank and send to next rank in pipeline.
    See _communicate for argument details.
    """
    recv_prev, recv_next = False, False
    tensor_send_next, tensor_send_prev = None, None
    if model_chunk_id == 0:
        if parallel_state.is_pipeline_last_stage():
            return None, None
        tensor_send_next = output_tensor
        recv_next = True
    if model_chunk_id == 1:
        if parallel_state.is_pipeline_first_stage():
            return None, None
        tensor_send_prev = output_tensor
        recv_prev = True
    if config.timers is not None:
        config.timers('forward-send-slave-forward-recv', log_level=2).start()
    tensor_recv_prev, tensor_recv_next, fwd_wait_handles = _communicate(
        tensor_send_next=tensor_send_next,
        tensor_send_prev=tensor_send_prev,
        recv_prev=recv_prev,
        recv_next=recv_next,
        tensor_shape=tensor_shape,
        wait_on_reqs=(not async_op),
        config=config,
    )
    if config.timers is not None:
        config.timers('forward-send-slave-forward-recv').stop()

    if model_chunk_id == 0:
        return tensor_recv_next, fwd_wait_handles
    else:
        return tensor_recv_prev, fwd_wait_handles


def generate_dualpipev_schedule(pp_size, num_microbatches):
    num_microbatches = num_microbatches * 2
    num_warmup_stages = [0] * pp_size
    num_interleaved_forward_stages = [0] * pp_size
    num_1b1w1f_stages = [0] * pp_size
    num_overlap_stages = [0] * pp_size
    num_1b1overlap_stages = [0] * pp_size
    num_interleaved_backward_stages = [0] * pp_size
    num_cooldown_stages = [0] * pp_size

    pp_size *= 2
    for i in range(pp_size // 2):
        num_warmup_stages[i] = pp_size - 2 - i * 2

        num_interleaved_forward_stages[i] = i + 1  # 每个单位是一组1f1f

        num_1b1w1f_stages[i] = pp_size // 2 - i - 1

        num_overlap_stages[i] = num_microbatches - pp_size * 2 + i * 2 + 2

        num_1b1overlap_stages[i] = (pp_size // 2 - i - 1) * 2

        num_interleaved_backward_stages[i] = i + 1

        num_cooldown_stages[i] = [i + 1, pp_size - 2 * i - 2, i + 1]

    schedule_all_stages = {
        'warmup': num_warmup_stages,
        'interleaved_forward': num_interleaved_forward_stages,
        '1b1w1f': num_1b1w1f_stages,
        'overlap': num_overlap_stages,
        '1b1overlap': num_1b1overlap_stages,
        'interleaved_backward': num_interleaved_backward_stages,
        'cooldown': num_cooldown_stages
    }

    return schedule_all_stages


def forward_step_no_model_graph(
    forward_step_func,
    model_chunk_id,
    data_iterator,
    model,
    num_microbatches,
    input_tensor,
    forward_data_store,
    config,
    collect_non_loss_data=False,
    checkpoint_activations_microbatch=None,
    is_first_microbatch=False,
    current_microbatch=None,
):
    if config.timers is not None:
        config.timers('forward-compute', log_level=2).start()

    if is_first_microbatch and hasattr(model, 'set_is_first_microbatch'):
        model.set_is_first_microbatch()
    if current_microbatch is not None:
        set_current_microbatch(model, current_microbatch)

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

    num_tokens = torch.tensor(0, dtype=torch.int)
    if is_dualpipev_last_stgae(model_chunk_id):
        if not collect_non_loss_data:
            outputs = loss_func(output_tensor)
            if len(outputs) == 3:
                output_tensor, num_tokens, loss_reduced = outputs
                if not config.calculate_per_token_loss:
                    output_tensor /= num_tokens
                    output_tensor /= num_microbatches
            else:
                # preserve legacy loss averaging behavior (ie, over the number of microbatches)
                assert len(outputs) == 2
                output_tensor, loss_reduced = outputs
                output_tensor /= num_microbatches
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
            config.grad_scale_func(torch.ones(1, device=output_tensor.device))
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
        return [output_tensor, input_tensor[-1]], num_tokens

    if unwrap_output_tensor:
        return output_tensor, num_tokens
    return [output_tensor], num_tokens


def backward_step_with_model_graph(input_tensor, output_tensor, output_tensor_grad, model_type, config, model_graph=None):
    """Backward step through passed-in output tensor.

    If last stage, output_tensor_grad is None, otherwise gradient of loss
    with respect to stage's output tensor.

    Returns gradient of loss with respect to input tensor (None if first
    stage)."""

    # NOTE: This code currently can handle at most one skip connection. It
    # needs to be modified slightly to support arbitrary numbers of skip
    # connections.

    if config.timers is not None:
        config.timers('backward-compute', log_level=2).start()

    # Retain the grad on the input_tensor.
    unwrap_input_tensor_grad = False
    if not isinstance(input_tensor, list):
        input_tensor = [input_tensor]
        unwrap_input_tensor_grad = True
    for x in input_tensor:
        if x is not None:
            x.retain_grad()

    if not isinstance(output_tensor, list):
        output_tensor = [output_tensor]
    if not isinstance(output_tensor_grad, list):
        output_tensor_grad = [output_tensor_grad]

    # Backward pass.
    if output_tensor_grad[0] is None and config.grad_scale_func is not None and model_graph is None:
        output_tensor[0] = config.grad_scale_func(output_tensor[0])

    if config.deallocate_pipeline_outputs:
        if model_graph is None:
            custom_backward(output_tensor[0], output_tensor_grad[0])
        else:
            layer_output_grad = gpt_model_backward(
                output_tensor_grad[0], model_graph)
    else:
        torch.autograd.backward(
            output_tensor[0], grad_tensors=output_tensor_grad[0])

    # Collect the grad of the input_tensor.
    input_tensor_grad = [None]
    if input_tensor is not None:
        input_tensor_grad = []
        if model_graph is not None:
            input_tensor_grad.append(layer_output_grad)
        else:
            for x in input_tensor:
                if x is None:
                    input_tensor_grad.append(None)
                else:
                    input_tensor_grad.append(x.grad)

    # Handle single skip connection if it exists (encoder_hidden_state in
    # model with encoder and decoder).
    if (
            parallel_state.get_pipeline_model_parallel_world_size() > 1
            and parallel_state.is_pipeline_stage_after_split()
            and model_type == ModelType.encoder_and_decoder
    ):
        if output_tensor_grad[1] is not None:
            input_tensor_grad[-1].add_(output_tensor_grad[1])
    if unwrap_input_tensor_grad:
        input_tensor_grad = input_tensor_grad[0]

    if config.timers is not None:
        config.timers('backward-compute').stop()

    return input_tensor_grad


def forward_step_with_model_graph(
    forward_step_func,
    model_chunk_id,
    data_iterator,
    model,
    num_microbatches,
    input_tensor,
    forward_data_store,
    config,
    collect_non_loss_data=False,
    checkpoint_activations_microbatch=None,
    is_first_microbatch=False,
    current_microbatch=None,
    extra_block_kwargs=None,
):
    """Forward step for passed-in model.

    If it is the first stage, the input tensor is obtained from the data_iterator.
    Otherwise, the passed-in input_tensor is used.

    Args:
        forward_step_func (callable): The forward step function for the model that takes the
            data iterator as the first argument, and model as the second.
            This user's forward step is expected to output a tuple of two elements:
                1. The output object from the forward step. This output object needs to be a
                    tensor or some kind of collection of tensors. The only hard requirement
                    for this object is that it needs to be acceptible as input into the second
                    function.
                2. A function to reduce (optionally) the output from the forward step. This
                    could be a reduction over the loss from the model, it could be a function that
                    grabs the output from the model and reformats, it could be a function that just
                    passes through the model output. This function must have one of the following
                    patterns, and depending on the pattern different things happen internally.
                        a. A tuple of reduced loss and some other data. Note that in this case
                            the first argument is divided by the number of global microbatches,
                            assuming it is a loss, so that the loss is stable as a function of
                            the number of devices the step is split across.
                        b. A triple of reduced loss, number of tokens, and some other data. This
                            is similar to case (a), but the loss is further averaged across the
                            number of tokens in the batch. If the user is not already averaging
                            across the number of tokens, this pattern is useful to use.
                        c. Any arbitrary data the user wants (eg a dictionary of tensors, a list
                            of tensors, etc in the case of inference). To trigger case 3 you need
                            to specify `collect_non_loss_data=True` and you may also want to
                            specify `forward_only=True` in the call to the parent forward_backward
                            function.
        data_iterator (iterator): The data iterator.
        model (nn.Module): The model to perform the forward step on.
        num_microbatches (int): The number of microbatches.
        input_tensor (Tensor or list[Tensor]): The input tensor(s) for the forward step.
        forward_data_store (list): The list to store the forward data. If you go down path 2.a or
            2.b for the return of your forward reduction function then this will store only the
            final dimension of the output, for example the metadata output by the loss function.
            If you go down the path of 2.c then this will store the entire output of the forward
            reduction function applied to the model output.
        config (object): The configuration object.
        collect_non_loss_data (bool, optional): Whether to collect non-loss data. Defaults to False.
            This is the path to use if you want to collect arbitrary output from the model forward,
            such as with inference use cases. Defaults to False.
        checkpoint_activations_microbatch (int, optional): The microbatch to checkpoint activations.
            Defaults to None.
        is_first_microbatch (bool, optional): Whether it is the first microbatch. Defaults to False.
        current_microbatch (int, optional): The current microbatch. Defaults to None.

    Returns:
        Tensor or list[Tensor]: The output object(s) from the forward step.
        Tensor: The number of tokens.
    """
    if config.timers is not None:
        config.timers('forward-compute', log_level=2).start()

    if is_first_microbatch and hasattr(model, 'set_is_first_microbatch'):
        model.set_is_first_microbatch()
    if current_microbatch is not None:
        set_current_microbatch(model, current_microbatch)

    unwrap_output_tensor = False
    if not isinstance(input_tensor, list):
        input_tensor = [input_tensor]
        unwrap_output_tensor = True

    set_input_tensor = get_attr_wrapped_model(model, "set_input_tensor")
    set_input_tensor(input_tensor)
    block = get_attr_wrapped_model(model, 'decoder')

    if config.enable_autocast:
        context_manager = torch.autocast("cuda", dtype=config.autocast_dtype)
    else:
        context_manager = contextlib.nullcontext()
    with context_manager:
        if checkpoint_activations_microbatch is None:
            output_tensor, loss_func = forward_step_func(
                data_iterator, model, extra_block_kwargs=extra_block_kwargs)
        else:
            output_tensor, loss_func = forward_step_func(
                data_iterator, model, checkpoint_activations_microbatch, extra_block_kwargs=extra_block_kwargs
            )

    num_tokens = torch.tensor(0, dtype=torch.int)

    if is_dualpipev_last_stgae(model_chunk_id):
        if not collect_non_loss_data:
            outputs = loss_func(output_tensor)
            if len(outputs) == 3:
                output_tensor, num_tokens, loss_reduced = outputs
                if not config.calculate_per_token_loss:
                    output_tensor /= num_tokens
                    output_tensor /= num_microbatches
            else:
                # preserve legacy loss averaging behavior (ie, over the number of microbatches)
                assert len(outputs) == 2
                output_tensor, loss_reduced = outputs
                output_tensor /= num_microbatches
            forward_data_store.append(loss_reduced)
        else:
            data = loss_func(output_tensor, non_loss_data=True)
            forward_data_store.append(data)

    next_info = None
    model_graph = block.get_fwd_layer_graphs()
    if extra_block_kwargs is not None:
        next_info = block.get_pp_comm_output()
    output_tensor = (output_tensor, model_graph, next_info) if next_info is not None else (
        output_tensor, model_graph)

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
        return [output_tensor, input_tensor[-1]], num_tokens

    if unwrap_output_tensor:
        return output_tensor, num_tokens
    return [output_tensor], num_tokens


shared_embedding = None


def get_shared_embedding_from_dual_chunk():
    assert shared_embedding is not None
    return shared_embedding


def _unwrap_megatron_model(model):
    """
    Remove consecutive 'module.' prefixes from the model based on the state_dict's first key.
    This method only removes 'module.' from the beginning of the key and ignores other occurrences.
    """
    first_key = list(dict((model).named_parameters()).keys())[0]
    while first_key.startswith("module."):
        (model) = (model).module
        first_key = first_key[len("module."):]  # 更新键，去掉一个module.
    return (model)


def set_shared_embedding_from_dual_chunk(model1, model2):
    global shared_embedding
    if shared_embedding is not None:
        return
    model1 = _unwrap_megatron_model(model1)
    model2 = _unwrap_megatron_model(model2)
    if model1.pre_process:
        shared_embedding = model1.embedding.word_embeddings.weight
    elif model2.pre_process:
        shared_embedding = model2.embedding.word_embeddings.weight



def forward_backward_pipelining_with_cutinhalf(
    *,
    forward_step_func,
    data_iterator: Union[Iterator, List[Iterator]],
    model: Union[torch.nn.Module, List[torch.nn.Module]],
    num_microbatches: int,
    seq_length: int,
    micro_batch_size: int,
    decoder_seq_length: int = None,
    forward_only: bool = False,
    collect_non_loss_data: bool = False,
    first_val_step: bool = None,
):
    args = get_args()

    set_shared_embedding_from_dual_chunk(model[0], model[1])
    assert (
        isinstance(model, list) and len(model) == 2
    ), 'Dualpipe Schedule only support chunk model for two consecutive chunks'
    assert (
        isinstance(data_iterator, list) and len(data_iterator) == 2
    ), 'Dualpipe Schedule only support two data_iterators'

    # should overide forward step func with extra_block_kwargs passed in
    forward_step_func = forward_step_func_wrapper(forward_step_func)

    config = get_model_config(model[0])
    config.batch_p2p_comm = False

    # Needed only when gradients are finalized in M-Core
    if config.finalize_model_grads_func is not None and not forward_only:
        embedding_module = clear_embedding_activation_buffer(config, model)

    if config.timers is not None:
        config.timers('forward-backward',
                      log_level=1).start(barrier=config.barrier_with_L1_time)

    # Disable async grad reductions
    no_sync_func = config.no_sync_func
    if isinstance(no_sync_func, list):

        def multi_no_sync():
            stack = contextlib.ExitStack()
            for model_chunk_no_sync_func in config.no_sync_func:
                stack.enter_context(model_chunk_no_sync_func())
            return stack

        no_sync_func = multi_no_sync
    if no_sync_func is None:
        no_sync_func = contextlib.nullcontext
    no_sync_context = None

    if config.grad_sync_func is not None and not isinstance(config.grad_sync_func, list):
        config.grad_sync_func = [config.grad_sync_func for _ in model]

    if config.param_sync_func is not None and not isinstance(config.param_sync_func, list):
        config.param_sync_func = [config.param_sync_func for _ in model]

    # Disable config.grad_sync_func and config.param_sync_func if only running forward passes.
    # They will be re-enabled at the end of this function.
    grad_sync_func, param_sync_func = None, None
    if forward_only:
        grad_sync_func, param_sync_func = config.grad_sync_func, config.param_sync_func
        config.grad_sync_func, config.param_sync_func = None, None

    def disable_grad_sync():
        """Disable asynchronous grad reductions"""
        nonlocal no_sync_context
        if no_sync_context is None:
            no_sync_context = no_sync_func()
            no_sync_context.__enter__()

    def enable_grad_sync():
        """Enable asynchronous grad reductions"""
        nonlocal no_sync_context
        if no_sync_context is not None:
            no_sync_context.__exit__(None, None, None)
            no_sync_context = None

    disable_grad_sync()


    # Compute number of steps for each stage
    pp_size = parallel_state.get_pipeline_model_parallel_world_size()
    rank = parallel_state.get_pipeline_model_parallel_rank()
    schedule = generate_dualpipev_schedule(pp_size, num_microbatches)

    model_type = get_model_type(model[0])

    tensor_shape = [seq_length, micro_batch_size, config.hidden_size]
    tensor_shape[0] = tensor_shape[0] // parallel_state.get_context_parallel_world_size()
    if config.sequence_parallel:
        tensor_shape[0] = tensor_shape[0] // parallel_state.get_tensor_model_parallel_world_size()

    total_num_tokens = torch.tensor(0, dtype=torch.int).cuda()
    input_tensors = [[], []]
    output_tensors = [[], []]
    model_graphs = [[], []]
    logits_inputs = []
    forward_data_store = []

    master_chunk_id = 0
    slave_chunk_id = 1

    master_cur_microbatch = 0
    slave_cur_microbatch = num_microbatches
    master_microbatch_max = num_microbatches
    slave_microbatch_max = num_microbatches * 2

    set_dualpipe_chunk(master_chunk_id)

    checkpoint_activations_microbatch = None

    def forward_step_helper(model_chunk_id, current_microbatch, checkpoint_activations_microbatch,
                            is_first_microbatch=False, extra_block_kwargs=None):

        input_tensor = input_tensors[model_chunk_id][-1][1]
        output_tensor, num_tokens = forward_step_with_model_graph(
            forward_step_func,
            model_chunk_id,
            data_iterator[model_chunk_id],
            model[model_chunk_id],
            num_microbatches,
            input_tensor,
            forward_data_store,
            config,
            collect_non_loss_data,
            checkpoint_activations_microbatch,
            is_first_microbatch,
            current_microbatch=current_microbatch,
            extra_block_kwargs=extra_block_kwargs
        )

        if isinstance(output_tensor, tuple):
            if len(output_tensor) == 2:
                output_tensor_, model_graph = output_tensor
            elif len(output_tensor) == 3:
                output_tensor_, model_graph, pp_comm_output = output_tensor

            if is_dualpipev_last_stgae(model_chunk_id):
                logits_inputs.append(model_graph[-1].unperm2_graph[1])
            model_graphs[model_chunk_id].append(model_graph)
        else:
            output_tensor_ = output_tensor
        output_tensors[model_chunk_id].append(output_tensor_)

        if extra_block_kwargs is not None:
            input_tensors[1 - model_chunk_id].pop(0)
            output_tensors[1 - model_chunk_id].pop(0)

        nonlocal total_num_tokens
        total_num_tokens += num_tokens.item()

        # if forward-only, no need to save tensors for a backward pass
        if forward_only:
            input_tensors[model_chunk_id].pop()
            output_tensors[model_chunk_id].pop()

        return output_tensor

    def check_pipeline_stage(model_chunk_id, fwd_send_only):
        send_next, recv_next, send_prev, recv_prev = True, True, True, True
        if parallel_state.is_pipeline_first_stage():
            send_prev, recv_prev = False, False
        if parallel_state.is_pipeline_last_stage():
            send_next, recv_next = False, False

        if model_chunk_id == 0:
            return P2PCommParams(send_next=send_next, recv_next=not fwd_send_only and recv_next), P2PCommParams(send_next=send_next, recv_next=recv_next)
        else:
            return P2PCommParams(send_prev=send_prev, recv_prev=not fwd_send_only and recv_prev), P2PCommParams(send_prev=send_prev, recv_prev=recv_prev)

    input_tensor = recv_forward(tensor_shape, config, master_chunk_id)[0]

    fwd_wait_handles_warmup = None
    # Run warmup forward passes
    for i in range(schedule['warmup'][rank]):

        if args.moe_fb_overlap:
            input_tensors[master_chunk_id].append(
                (master_cur_microbatch, input_tensor))
            output_tensor_warmup, _ = forward_step_helper(master_chunk_id, master_cur_microbatch, checkpoint_activations_microbatch,
                                                          is_first_microbatch=(i == 0))
        else:
            output_tensor_warmup, num_tokens = forward_step_no_model_graph(
                forward_step_func,
                master_chunk_id,
                data_iterator[master_chunk_id],
                model[master_chunk_id],
                num_microbatches,
                input_tensor,
                forward_data_store,
                config,
                collect_non_loss_data,
                checkpoint_activations_microbatch,
                is_first_microbatch=(i == 0),
                current_microbatch=master_cur_microbatch
            )

            total_num_tokens += num_tokens.item()
            input_tensors[master_chunk_id].append(
                (master_cur_microbatch, input_tensor))
            output_tensors[master_chunk_id].append(output_tensor_warmup)

        master_cur_microbatch += 1

        if i != schedule['warmup'][rank] - 1:
            input_tensor, _ = send_forward_recv_forward(
                output_tensor_warmup, tensor_shape, config, master_chunk_id)
            deallocate_output_tensor(
                output_tensor_warmup, config.deallocate_pipeline_outputs)
        else:
            input_tensor, _ = recv_forward(
                tensor_shape, config, master_chunk_id)
            fwd_wait_handles_warmup = send_forward(
                output_tensor_warmup, tensor_shape, config, master_chunk_id, async_op=True)

    # Run interleaved forward passes for two model chunk
    fwd_wait_handles = None
    fwd_wait_handles_slave_chunk = None
    fwd_wait_handles_send = None
    for i in range(schedule['interleaved_forward'][rank]):

        if fwd_wait_handles is not None:
            for req in fwd_wait_handles.values():
                req.wait()
            fwd_wait_handles = None

        is_first_microbatch = parallel_state.is_pipeline_last_stage() and (i == 0)
        set_dualpipe_chunk(master_chunk_id)

        if args.moe_fb_overlap:
            input_tensors[master_chunk_id].append(
                (master_cur_microbatch, input_tensor))
            output_tensor, _ = forward_step_helper(master_chunk_id, master_cur_microbatch, checkpoint_activations_microbatch,
                                                   is_first_microbatch=is_first_microbatch)
        else:
            output_tensor, num_tokens = forward_step_no_model_graph(
                forward_step_func,
                master_chunk_id,
                data_iterator[master_chunk_id],
                model[master_chunk_id],
                num_microbatches,
                input_tensor,
                forward_data_store,
                config,
                collect_non_loss_data,
                checkpoint_activations_microbatch,
                is_first_microbatch=is_first_microbatch,
                current_microbatch=master_cur_microbatch
            )

            total_num_tokens += num_tokens.item()
            input_tensors[master_chunk_id].append(
                (master_cur_microbatch, input_tensor))
            output_tensors[master_chunk_id].append(output_tensor)

        master_cur_microbatch += 1

        if not parallel_state.is_pipeline_last_stage() and fwd_wait_handles_send is not None:
            for req in fwd_wait_handles_send.values():
                req.wait()
            deallocate_output_tensor(
                output_tensor_send, config.deallocate_pipeline_outputs)
            fwd_wait_handles_send = None

        if parallel_state.is_pipeline_last_stage():
            input_tensor_slave_chunk = detach_tensor(output_tensor)

            input_tensor, fwd_wait_handles = recv_forward(
                tensor_shape, config, master_chunk_id, async_op=True)
        else:
            input_tensor_slave_chunk, _ = recv_forward(
                tensor_shape, config, slave_chunk_id)

            input_tensor, fwd_wait_handles = recv_forward(
                tensor_shape, config, master_chunk_id, async_op=True)

        if fwd_wait_handles_warmup is not None:
            for req in fwd_wait_handles_warmup.values():
                req.wait()
            deallocate_output_tensor(
                output_tensor_warmup, config.deallocate_pipeline_outputs)
            fwd_wait_handles_warmup = None

        if fwd_wait_handles_slave_chunk is not None:
            for req in fwd_wait_handles_slave_chunk.values():
                req.wait()
            deallocate_output_tensor(
                output_tensor_slave_chunk, config.deallocate_pipeline_outputs)
            fwd_wait_handles_slave_chunk = None

        set_dualpipe_chunk(slave_chunk_id)

        if args.moe_fb_overlap:
            input_tensors[slave_chunk_id].append(
                (slave_cur_microbatch, input_tensor_slave_chunk))
            output_tensor_slave_chunk, _ = forward_step_helper(
                slave_chunk_id, slave_cur_microbatch, checkpoint_activations_microbatch)
        else:
            output_tensor_slave_chunk, num_tokens = forward_step_no_model_graph(
                forward_step_func,
                slave_chunk_id,
                data_iterator[slave_chunk_id],
                model[slave_chunk_id],
                num_microbatches,
                input_tensor_slave_chunk,
                forward_data_store,
                config,
                collect_non_loss_data,
                checkpoint_activations_microbatch,
                current_microbatch=slave_cur_microbatch,
            )

            input_tensors[slave_chunk_id].append(
                (slave_cur_microbatch, input_tensor_slave_chunk))
            total_num_tokens += num_tokens.item()
            output_tensors[slave_chunk_id].append(output_tensor_slave_chunk)

        slave_cur_microbatch += 1

        if i == schedule['interleaved_forward'][rank] - 1:

            firstFB_no_overlp = False
            firstFB_no_overlp_handle = None
            if not forward_only:
                # last rank not overlap first F&B
                if parallel_state.is_pipeline_last_stage():
                    firstFB_no_overlp = True
                    output_tensor_grad_bwd, firstFB_no_overlp_handle = recv_backward(
                        tensor_shape, config, slave_chunk_id, async_op=True)
                else:
                    output_tensor_grad_bwd, _ = recv_backward(
                        tensor_shape, config, slave_chunk_id)

        fwd_wait_handles_slave_chunk = send_forward(output_tensor_slave_chunk,
                                                    tensor_shape, config, slave_chunk_id, async_op=True)

        if not parallel_state.is_pipeline_last_stage():
            output_tensor_send = output_tensor
            fwd_wait_handles_send = send_forward(
                output_tensor_send, tensor_shape, config, master_chunk_id, async_op=True)

    if fwd_wait_handles is not None:
        for req in fwd_wait_handles.values():
            req.wait()
        fwd_wait_handles = None

    # Run 1b1w1f stages for slave chunk
    bwd_wait_handles = None
    for _ in range(schedule['1b1w1f'][rank]):

        if not forward_only:
            WeightGradStore.start_decouple()

            if args.moe_fb_overlap:

                if is_dualpipev_last_stgae(slave_chunk_id):
                    input_tensor_bwd = logits_inputs.pop(0)
                    output_tensor_bwd = output_tensors[slave_chunk_id][0]
                    model_graph = None

                    output_tensor_grad_bwd = backward_step_with_model_graph(
                        input_tensor_bwd, output_tensor_bwd, output_tensor_grad_bwd, model_type, config, model_graph
                    )

                input_tensor_bwd = input_tensors[slave_chunk_id].pop(0)[1]
                output_tensor_bwd = output_tensors[slave_chunk_id].pop(0)
                model_graph = model_graphs[slave_chunk_id].pop(0)

                input_tensor_grad = backward_step_with_model_graph(
                    input_tensor_bwd, output_tensor_bwd, output_tensor_grad_bwd, model_type, config, model_graph
                )

            else:
                input_tensor_bwd = input_tensors[slave_chunk_id].pop(0)[1]
                output_tensor_bwd = output_tensors[slave_chunk_id].pop(0)

                input_tensor_grad = backward_step(
                    input_tensor_bwd, output_tensor_bwd, output_tensor_grad_bwd, model_type, config
                )

            WeightGradStore.end_decouple()

            # If asynchronous, the memory will rise.
            bwd_wait_handles = send_backward(input_tensor_grad,
                                            tensor_shape, config, slave_chunk_id)

        if fwd_wait_handles_slave_chunk is not None:
            for req in fwd_wait_handles_slave_chunk.values():
                req.wait()
            deallocate_output_tensor(
                output_tensor_slave_chunk, config.deallocate_pipeline_outputs)
            fwd_wait_handles_slave_chunk = None
        if fwd_wait_handles_send is not None:
            for req in fwd_wait_handles_send.values():
                req.wait()
            deallocate_output_tensor(
                output_tensor, config.deallocate_pipeline_outputs)
            fwd_wait_handles_send = None

        # If asynchronous, the memory will rise.
        input_tensor_slave_chunk, recv_forward_handle = recv_forward(
            tensor_shape, config, slave_chunk_id)

        # 1w: Weight Grad Compute
        WeightGradStore.pop()

        if recv_forward_handle is not None:
            for req in recv_forward_handle.values():
                req.wait()
            recv_forward_handle = None

        # 1F: Forward pass
        set_dualpipe_chunk(slave_chunk_id)

        if args.moe_fb_overlap:
            input_tensors[slave_chunk_id].append(
                (slave_cur_microbatch, input_tensor_slave_chunk))
            output_tensor_slave_chunk, _ = forward_step_helper(
                slave_chunk_id, slave_cur_microbatch, checkpoint_activations_microbatch)
        else:
            output_tensor_slave_chunk, num_tokens = forward_step_no_model_graph(
                forward_step_func,
                slave_chunk_id,
                data_iterator[slave_chunk_id],
                model[slave_chunk_id],
                num_microbatches,
                input_tensor_slave_chunk,
                forward_data_store,
                config,
                collect_non_loss_data,
                checkpoint_activations_microbatch,
                current_microbatch=slave_cur_microbatch
            )

            input_tensors[slave_chunk_id].append(
                (slave_cur_microbatch, input_tensor_slave_chunk))
            total_num_tokens += num_tokens.item()
            output_tensors[slave_chunk_id].append(output_tensor_slave_chunk)

        slave_cur_microbatch += 1

        if not forward_only:
            output_tensor_grad_bwd, _ = recv_backward(
                tensor_shape, config, slave_chunk_id)

        fwd_wait_handles_slave_chunk = send_forward(output_tensor_slave_chunk,
                                                    tensor_shape, config, slave_chunk_id, async_op=True)

    fwd_wait_handles_recv = None
    # Run overlaping f&bw stages
    fwd_model_chunk_id = master_chunk_id
    bwd_model_chunk_id = slave_chunk_id
    for _ in range(schedule['overlap'][rank] + schedule['1b1overlap'][rank] + schedule['interleaved_backward'][rank]):
        only_bwd = False
        if fwd_model_chunk_id == master_chunk_id and master_cur_microbatch == master_microbatch_max:
            only_bwd = True
        if fwd_model_chunk_id == slave_chunk_id and slave_cur_microbatch == slave_microbatch_max:
            only_bwd = True

        if args.moe_fb_overlap and not firstFB_no_overlp and not forward_only:
            if not only_bwd:
                if fwd_wait_handles is not None:
                    for req in fwd_wait_handles.values():
                        req.wait()
                    fwd_wait_handles = None
                if fwd_wait_handles_recv is not None:
                    for req in fwd_wait_handles_recv.values():
                        req.wait()
                    fwd_wait_handles_recv = None
                if bwd_wait_handles is not None:
                    for req in bwd_wait_handles.values():
                        req.wait()
                    bwd_wait_handles = None

                if not parallel_state.is_pipeline_last_stage() or fwd_model_chunk_id == master_chunk_id:
                    deallocate_output_tensor(
                        output_tensor, config.deallocate_pipeline_outputs)

                fwd_microbatch = master_cur_microbatch if fwd_model_chunk_id == master_chunk_id else slave_cur_microbatch
                set_dualpipe_chunk(fwd_model_chunk_id)

                fwd_send_only = False
                if fwd_model_chunk_id == slave_chunk_id and master_cur_microbatch == master_microbatch_max:
                    fwd_send_only = True

                extra_block_kwargs = {}
                if is_dualpipev_last_stgae(bwd_model_chunk_id):
                    input_tensor_bwd = logits_inputs.pop(0)
                    output_tensor_bwd = output_tensors[bwd_model_chunk_id][0]
                    model_graph = None

                    input_tensor_grad = backward_step_with_model_graph(
                        input_tensor_bwd, output_tensor_bwd, output_tensor_grad_bwd, model_type, config, model_graph
                    )

                    extra_block_kwargs.setdefault(
                        'bwd_block_output_grad', input_tensor_grad)

                else:
                    extra_block_kwargs.setdefault(
                        'bwd_block_output_grad', output_tensor_grad_bwd)

                fwd_pp_comm_params, bwd_pp_comm_params = check_pipeline_stage(
                    fwd_model_chunk_id, fwd_send_only)
                fwd_pp_comm_params.config, bwd_pp_comm_params.config = config, config
                fwd_pp_comm_params.tensor_shape, bwd_pp_comm_params.tensor_shape = tensor_shape, tensor_shape

                extra_block_kwargs.setdefault(
                    'bwd_block_graphs', model_graphs[bwd_model_chunk_id].pop(0))
                extra_block_kwargs.setdefault(
                    'pp_comm_params', fwd_pp_comm_params)
                extra_block_kwargs.setdefault(
                    'bwd_pp_comm_params', bwd_pp_comm_params)

                input_tensors[fwd_model_chunk_id].append(
                    (fwd_microbatch, input_tensor))

                output_tensor, model_graph, pp_comm_output = forward_step_helper(fwd_model_chunk_id, fwd_microbatch, checkpoint_activations_microbatch,
                                                                                 extra_block_kwargs=extra_block_kwargs)

                if parallel_state.is_pipeline_last_stage() and fwd_model_chunk_id == master_chunk_id:
                    input_tensor = output_tensor
                    output_tensor_grad_bwd = pp_comm_output.input_tensor_grad
                else:
                    input_tensor, fwd_wait_handles = pp_comm_output.input_tensor, pp_comm_output.fwd_wait_handles
                    output_tensor_grad_bwd, bwd_wait_handles = pp_comm_output.output_tensor_grad, pp_comm_output.bwd_wait_handles

                if fwd_model_chunk_id == master_chunk_id:
                    master_cur_microbatch += 1
                else:
                    slave_cur_microbatch += 1

                    if fwd_wait_handles_slave_chunk is not None:
                        for req in fwd_wait_handles_slave_chunk.values():  # 同步上个阶段最后一个slave前向send
                            req.wait()
                        deallocate_output_tensor(
                            output_tensor_slave_chunk, config.deallocate_pipeline_outputs)
                        fwd_wait_handles_slave_chunk = None
            else:
                if fwd_wait_handles is not None:
                    for req in fwd_wait_handles.values():
                        req.wait()
                    fwd_wait_handles = None
                if bwd_wait_handles is not None:
                    for req in bwd_wait_handles.values():
                        req.wait()
                    bwd_wait_handles = None
                deallocate_output_tensor(
                    output_tensor, config.deallocate_pipeline_outputs)

                if bwd_model_chunk_id == slave_chunk_id and slave_cur_microbatch < slave_microbatch_max:
                    input_tensor, fwd_wait_handles_recv = recv_forward(
                        tensor_shape, config, slave_chunk_id, async_op=True)

                if is_dualpipev_last_stgae(bwd_model_chunk_id):
                    input_tensor_bwd = logits_inputs.pop(0)
                    output_tensor_bwd = output_tensors[bwd_model_chunk_id][0]
                    model_graph = None

                    output_tensor_grad_bwd = backward_step_with_model_graph(
                        input_tensor_bwd, output_tensor_bwd, output_tensor_grad_bwd, model_type, config, model_graph
                    )

                input_tensor_bwd = input_tensors[bwd_model_chunk_id].pop(0)[1]
                output_tensor_bwd = output_tensors[bwd_model_chunk_id].pop(0)
                model_graph = model_graphs[bwd_model_chunk_id].pop(0)

                input_tensor_grad = backward_step_with_model_graph(
                    input_tensor_bwd, output_tensor_bwd, output_tensor_grad_bwd, model_type, config, model_graph
                )

                if parallel_state.is_pipeline_last_stage() and fwd_model_chunk_id == master_chunk_id:
                    output_tensor_grad_bwd = input_tensor_grad
                else:
                    # send_backward_recv_slave_backward
                    output_tensor_grad_bwd, bwd_wait_handles = send_forward_recv_slave_forward(input_tensor_grad,
                                                                                               tensor_shape, config, fwd_model_chunk_id)

        else:
            firstFB_no_overlp = False
            if not only_bwd:
                fwd_microbatch = master_cur_microbatch if fwd_model_chunk_id == master_chunk_id else slave_cur_microbatch
                set_dualpipe_chunk(fwd_model_chunk_id)

                if args.moe_fb_overlap:
                    input_tensors[fwd_model_chunk_id].append(
                        (fwd_microbatch, input_tensor))
                    output_tensor, _ = forward_step_helper(
                        fwd_model_chunk_id, fwd_microbatch, checkpoint_activations_microbatch)
                else:
                    output_tensor, num_tokens = forward_step_no_model_graph(
                        forward_step_func,
                        fwd_model_chunk_id,
                        data_iterator[fwd_model_chunk_id],
                        model[fwd_model_chunk_id],
                        num_microbatches,
                        input_tensor,
                        forward_data_store,
                        config,
                        collect_non_loss_data,
                        checkpoint_activations_microbatch,
                        current_microbatch=fwd_microbatch
                    )
                    input_tensors[fwd_model_chunk_id].append(
                        (fwd_microbatch, input_tensor))
                    total_num_tokens += num_tokens.item()
                    output_tensors[fwd_model_chunk_id].append(output_tensor)

                if fwd_model_chunk_id == master_chunk_id:
                    master_cur_microbatch += 1
                    fwd_send_only = False
                else:
                    slave_cur_microbatch += 1
                    fwd_send_only = (master_cur_microbatch ==
                                     master_microbatch_max)

                if fwd_send_only:
                    fwd_wait_handles = send_forward(
                        output_tensor, tensor_shape, config, fwd_model_chunk_id, async_op=True)
                else:
                    if parallel_state.is_pipeline_last_stage() and fwd_model_chunk_id == master_chunk_id:
                        input_tensor = detach_tensor(output_tensor)
                    else:
                        input_tensor, fwd_wait_handles = send_forward_recv_slave_forward(
                            output_tensor, tensor_shape, config, fwd_model_chunk_id, async_op=True)

                if not forward_only:
                    if firstFB_no_overlp_handle is not None:
                        for req in firstFB_no_overlp_handle.values():
                            req.wait()
                        firstFB_no_overlp_handle = None

                    if bwd_wait_handles is not None:
                        for req in bwd_wait_handles.values():
                            req.wait()
                        bwd_wait_handles = None

                    if args.moe_fb_overlap:
                        if is_dualpipev_last_stgae(bwd_model_chunk_id):
                            input_tensor_bwd = logits_inputs.pop(0)
                            output_tensor_bwd = output_tensors[bwd_model_chunk_id][0]
                            model_graph = None

                            output_tensor_grad_bwd = backward_step_with_model_graph(
                                input_tensor_bwd, output_tensor_bwd, output_tensor_grad_bwd, model_type, config, model_graph
                            )

                        input_tensor_bwd = input_tensors[bwd_model_chunk_id].pop(0)[
                            1]
                        output_tensor_bwd = output_tensors[bwd_model_chunk_id].pop(
                            0)
                        model_graph = model_graphs[bwd_model_chunk_id].pop(0)

                        input_tensor_grad = backward_step_with_model_graph(
                            input_tensor_bwd, output_tensor_bwd, output_tensor_grad_bwd, model_type, config, model_graph
                        )
                    else:
                        input_tensor_bwd = input_tensors[bwd_model_chunk_id].pop(0)[
                            1]
                        output_tensor_bwd = output_tensors[bwd_model_chunk_id].pop(
                            0)

                        if parallel_state.is_pipeline_last_stage() and fwd_model_chunk_id == slave_chunk_id:
                            deallocate_output_tensor(output_tensor_bwd, config.deallocate_pipeline_outputs)

                        input_tensor_grad = backward_step(
                            input_tensor_bwd, output_tensor_bwd, output_tensor_grad_bwd, model_type, config
                        )

                    if parallel_state.is_pipeline_last_stage() and fwd_model_chunk_id == master_chunk_id:
                        output_tensor_grad_bwd = input_tensor_grad
                    else:
                        #  send_backward_recv_slave_backward
                        output_tensor_grad_bwd, bwd_wait_handles = send_forward_recv_slave_forward(input_tensor_grad,
                                                                                                tensor_shape, config, fwd_model_chunk_id, async_op=True)

                if fwd_wait_handles is not None:
                    for req in fwd_wait_handles.values():
                        req.wait()
                    fwd_wait_handles = None
                    deallocate_output_tensor(
                        output_tensor, config.deallocate_pipeline_outputs)

                if fwd_wait_handles_slave_chunk is not None:
                    for req in fwd_wait_handles_slave_chunk.values():  # 同步上个阶段最后一个slave前向send
                        req.wait()
                    deallocate_output_tensor(
                        output_tensor_slave_chunk, config.deallocate_pipeline_outputs)
                    fwd_wait_handles_slave_chunk = None

            # only run backward
            else:
                if bwd_model_chunk_id == slave_chunk_id and slave_cur_microbatch < slave_microbatch_max:
                    input_tensor, _ = recv_forward(
                        tensor_shape, config, slave_chunk_id)

                if not forward_only:
                    if bwd_wait_handles is not None:
                        for req in bwd_wait_handles.values():
                            req.wait()
                        bwd_wait_handles = None

                    if args.moe_fb_overlap:
                        if is_dualpipev_last_stgae(bwd_model_chunk_id):
                            input_tensor_bwd = logits_inputs.pop(0)
                            output_tensor_bwd = output_tensors[bwd_model_chunk_id][0]
                            model_graph = None

                            output_tensor_grad_bwd = backward_step_with_model_graph(
                                input_tensor_bwd, output_tensor_bwd, output_tensor_grad_bwd, model_type, config, model_graph
                            )

                        input_tensor_bwd = input_tensors[bwd_model_chunk_id].pop(0)[
                            1]
                        output_tensor_bwd = output_tensors[bwd_model_chunk_id].pop(
                            0)
                        model_graph = model_graphs[bwd_model_chunk_id].pop(0)

                        input_tensor_grad = backward_step_with_model_graph(
                            input_tensor_bwd, output_tensor_bwd, output_tensor_grad_bwd, model_type, config, model_graph
                        )
                    else:
                        input_tensor_bwd = input_tensors[bwd_model_chunk_id].pop(0)[
                            1]
                        output_tensor_bwd = output_tensors[bwd_model_chunk_id].pop(
                            0)

                        if parallel_state.is_pipeline_last_stage() and fwd_model_chunk_id == slave_chunk_id:
                            deallocate_output_tensor(output_tensor_bwd, config.deallocate_pipeline_outputs)

                        input_tensor_grad = backward_step(
                            input_tensor_bwd, output_tensor_bwd, output_tensor_grad_bwd, model_type, config
                        )

                    if parallel_state.is_pipeline_last_stage() and fwd_model_chunk_id == master_chunk_id:
                        output_tensor_grad_bwd = input_tensor_grad
                    else:
                        #  send_backward_recv_slave_backward
                        output_tensor_grad_bwd, bwd_wait_handles = send_forward_recv_slave_forward(input_tensor_grad,
                                                                                                tensor_shape, config, fwd_model_chunk_id)

        # swap fwd & bwd chunks
        fwd_model_chunk_id, bwd_model_chunk_id = bwd_model_chunk_id, fwd_model_chunk_id

    if not forward_only:
        # Run cooldown phases
        merged_input_tensors = []
        merged_output_tensors = []
        while len(input_tensors[0]) > 0 or len(input_tensors[1]) > 0:
            if len(input_tensors[bwd_model_chunk_id]) > 0:
                merged_input_tensors.append(
                    input_tensors[bwd_model_chunk_id].pop(0))
                merged_output_tensors.append(
                    (output_tensors[bwd_model_chunk_id].pop(0), bwd_model_chunk_id))

            if len(input_tensors[1 - bwd_model_chunk_id]) > 0:
                merged_input_tensors.append(
                    input_tensors[1 - bwd_model_chunk_id].pop(0))
                merged_output_tensors.append(
                    (output_tensors[1 - bwd_model_chunk_id].pop(0), 1 - bwd_model_chunk_id))

        bwd_wait_handles_recv = None
        for i in range(pp_size):

            if bwd_wait_handles is not None:
                for req in bwd_wait_handles.values():
                    req.wait()
                bwd_wait_handles = None
            if bwd_wait_handles_recv is not None:
                for req in bwd_wait_handles_recv.values():
                    req.wait()
                bwd_wait_handles_recv = None

            input_tensor_bwd = merged_input_tensors.pop(0)[1]
            output_tensor_bwd, bwd_model_chunk_id = merged_output_tensors.pop(0)

            if args.dualpipev_dw_detach:
                WeightGradStore.start_decouple()

            if args.moe_fb_overlap:
                model_graph = model_graphs[bwd_model_chunk_id].pop(0)

                input_tensor_grad = backward_step_with_model_graph(
                    input_tensor_bwd, output_tensor_bwd, output_tensor_grad_bwd, model_type, config, model_graph
                )
            else:
                if parallel_state.is_pipeline_last_stage() and bwd_model_chunk_id == master_chunk_id:
                    deallocate_output_tensor(output_tensor_bwd, config.deallocate_pipeline_outputs)
                input_tensor_grad = backward_step(
                    input_tensor_bwd, output_tensor_bwd, output_tensor_grad_bwd, model_type, config
                )

            if args.dualpipev_dw_detach:
                WeightGradStore.end_decouple()

            if i == pp_size - 1:
                bwd_wait_handles = send_backward(input_tensor_grad,
                                                tensor_shape, config, bwd_model_chunk_id, async_op=True)
            elif i >= schedule['cooldown'][rank][0] - 1:
                bwd_wait_handles = send_backward(input_tensor_grad,
                                                tensor_shape, config, bwd_model_chunk_id, async_op=True)
                output_tensor_grad_bwd, bwd_wait_handles_recv = recv_backward(
                    tensor_shape, config, bwd_model_chunk_id, async_op=True)
            else:
                if parallel_state.is_pipeline_last_stage() and (1 - bwd_model_chunk_id) == master_chunk_id:
                    output_tensor_grad_bwd = input_tensor_grad
                else:
                    #  send_backward_recv_slave_backward
                    output_tensor_grad_bwd, bwd_wait_handles = send_forward_recv_slave_forward(input_tensor_grad,
                                                                                            tensor_shape, config, 1 - bwd_model_chunk_id)

            WeightGradStore.flush_chunk_grad()
            if i >= schedule['cooldown'][rank][0] - 1:
                WeightGradStore.pop_single()

            if args.overlap_grad_reduce and i == rank - 1:
                enable_grad_sync()
                if config.grad_sync_func is not None:
                    config.grad_sync_func[1](model[1].parameters())
                disable_grad_sync()

            if args.overlap_grad_reduce and i == 0 and rank == 0:
                enable_grad_sync()
                if config.grad_sync_func is not None:
                    config.grad_sync_func[1](model[1].parameters())
                disable_grad_sync()


        for _ in range(schedule['cooldown'][rank][2] - 1):
            WeightGradStore.pop_single()

        assert WeightGradStore.weight_grad_queue.empty()

        if bwd_wait_handles is not None:
            for req in bwd_wait_handles.values():
                req.wait()
            bwd_wait_handles = None

        if args.overlap_grad_reduce:
            enable_grad_sync()
            if config.grad_sync_func is not None:
                config.grad_sync_func[0](model[0].parameters())


        if config.finalize_model_grads_func is not None and not forward_only:

            # If defer_embedding_wgrad_compute is enabled we need to do the
            # weight gradient GEMM's here.
            finish_embedding_wgrad_compute(config, embedding_module)

            # Finalize model grads (perform full grad all-reduce / reduce-scatter for
            # data parallelism, layernorm all-reduce for sequence parallelism, and
            # embedding all-reduce for pipeline parallelism).
            config.finalize_model_grads_func(
                model, total_num_tokens if config.calculate_per_token_loss else None
            )

    # Restore config.grad_sync_func and config.param_sync_func.
    if forward_only:
        config.grad_sync_func, config.param_sync_func = grad_sync_func, param_sync_func

    return forward_data_store
