import acl
import torch
import torch_npu

from megatron.core.parallel_state import (get_expert_model_parallel_group, get_expert_tensor_and_model_parallel_group, 
                                            get_expert_tensor_and_model_parallel_world_size,
                                            get_tensor_model_parallel_group, 
                                            get_tensor_model_parallel_world_size)
from megatron.core.transformer.moe.moe_layer import MoELayer
from megatron.training import get_args
from mindspeed.core.transformer.moe.legacy_a2a_token_dispatcher import cann_version_check
from mindspeed.core.transformer.moe.moe_utils import AG_SHARED_EXPERTS_INPUTS
from mindspeed.core.transformer.moe.comm_utils import async_all_gather, async_reduce_scatter
from mindspeed.core.transformer.moe.moe_utils import (forward_func, backward_func, set_gemm_backward_need_tensors,
                                                      get_rs_global_hidden_states_grad_with_handle)


class MoELayerOverlapAllGather(torch.autograd.Function):
    @staticmethod
    def forward(ctx, hidden_states, moe_layer: MoELayer):
        args = get_args()
        save_tensors = []
        ctx.input_shape = hidden_states.shape
        # input detach graph, leaf node
        hidden_states = hidden_states.detach()
        hidden_states.requires_grad = True

        shared_experts_input = hidden_states
        shared_experts_allgather_handle = None

        # router
        (probs, routing_map), _ = forward_func(moe_layer.router, hidden_states)

        # after router, do 2 allgather
        global_routing_map_tuple = None
        global_probs_tuple = None
        if moe_layer.config.sequence_parallel or (moe_layer.config.expert_model_parallel_size > 1):
            if isinstance(routing_map, tuple):
                global_routing_map, gr_handle = routing_map
            else:
                _, global_routing_map, gr_handle = async_all_gather(routing_map, get_expert_tensor_and_model_parallel_group())

            global_routing_map_tuple = (global_routing_map, gr_handle)

            _, global_probs, gp_handle = async_all_gather(
                probs, get_expert_tensor_and_model_parallel_group()
            )

            global_probs_tuple = (global_probs, gp_handle)

        # 专家 ep group allgather hidden_states
        global_hidden_states_tuple = None
        if moe_layer.config.sequence_parallel or moe_layer.config.expert_model_parallel_size > 1:
            if '910B' in acl.get_soc_name():
                _, global_hidden_states, ghs_handle = async_all_gather(
                    hidden_states,
                    get_expert_tensor_and_model_parallel_group(),
                )
            else:
                _, global_hidden_states, ghs_handle = async_all_gather(
                    shared_experts_input,
                    get_expert_model_parallel_group()
                    if shared_experts_allgather_handle
                    else get_expert_tensor_and_model_parallel_group(),
                    shared_experts_allgather_handle
                )
            global_hidden_states = global_hidden_states.view(-1, global_hidden_states.shape[-1])
            global_hidden_states_tuple = (global_hidden_states, ghs_handle)

        # shared experts
        shared_experts_rs_handle = None
        share_experts_output = None
        rs_share_experts_output = None
        if args.n_shared_experts or args.moe_shared_expert_intermediate_size:
            if shared_experts_allgather_handle is not None:
                shared_experts_allgather_handle.wait()
            (share_experts_output), _ = forward_func(
                moe_layer.shared_experts, hidden_states
            )

            share_experts_output = share_experts_output[0]
            rs_share_experts_output = share_experts_output

        token_permutation_input = (
            global_routing_map_tuple,
            global_probs_tuple,
            global_hidden_states_tuple
        )

        # dispatch input
        save_tensors.append(probs)

        moe_layer.token_dispatcher.hidden_shape = hidden_states.shape
        (dispatched_input, tokens_per_expert, local_map, reversed_local_input_permutation_mapping), *token_permutation_input = forward_func(
            moe_layer.token_dispatcher.token_permutation, token_permutation_input
        )

        save_tensors.append(local_map)
        save_tensors.append(reversed_local_input_permutation_mapping)

        global_probs_detach, global_hidden_states_detach = token_permutation_input[1][0], token_permutation_input[2][0]

        global_hidden_states_detach.untyped_storage().resize_(0)
        if cann_version_check:
            global_probs_detach.untyped_storage().resize_(0)
        save_tensors.append(global_probs_detach)
        save_tensors.append(global_hidden_states_detach)

        expert_input = (dispatched_input, tokens_per_expert)

        def func(dispatched_input, tokens_per_expert):
            expert_output, mlp_bias = moe_layer.experts(dispatched_input, tokens_per_expert, None)
            output, mlp_bias = moe_layer.token_dispatcher.token_unpermutation(
                expert_output, mlp_bias, reversed_local_input_permutation_mapping
            )
            return output, mlp_bias

        (output, mlp_bias), *_ = forward_func(func, expert_input)
        ctx.token_unpermutation_output_shape = output.shape
        save_tensors.append(dispatched_input)
        save_tensors.append(hidden_states)
        save_tensors.append(output)
        save_tensors.append(share_experts_output)
        ctx.save_for_backward(*save_tensors)

        if args.n_shared_experts or args.moe_shared_expert_intermediate_size:
            if shared_experts_rs_handle is not None:
                shared_experts_rs_handle.wait()
            output_rs = output + rs_share_experts_output
            rs_share_experts_output.untyped_storage().resize_(0)
            share_experts_output.untyped_storage().resize_(0)
        else:
            output_rs = output.detach()

        return output_rs, mlp_bias

    @staticmethod
    def backward(ctx, *args):
        (scores, local_map, reversed_local_input_permutation_mapping,
         global_probs_detach, global_hidden_states_detach, dispatched_input,
         input_, output, share_experts_graph) = ctx.saved_tensors

        token_unpermutation_output_shape = ctx.token_unpermutation_output_shape

        ag_share_experts_grad_input = args[0]
        ag_share_experts_handle = None

        args = None
        if ag_share_experts_handle is not None:
            ag_share_experts_handle.wait()

        if share_experts_graph is not None:
            # 反向 —— 共享专家
            share_experts_graph.backward(ag_share_experts_grad_input)
        if '910B' in acl.get_soc_name() or share_experts_graph is None:
            from mindspeed.core.transformer.moe.moe_utils import set_ag_tp_hidden_status
            set_ag_tp_hidden_status(input_)

        ag_experts_grad_input = ag_share_experts_grad_input.view(token_unpermutation_output_shape)
        ag_share_experts_grad_input = None
        # token 重排反向 function set
        set_gemm_backward_need_tensors((dispatched_input, global_hidden_states_detach, local_map, reversed_local_input_permutation_mapping))

        # 反向 —— token 反重排 expert
        output.backward(ag_experts_grad_input)

        global_probs_grad = global_probs_detach.grad

        _, rs_global_probs_grad, rs_global_probs_grad_handle = async_reduce_scatter(
            global_probs_grad, get_expert_tensor_and_model_parallel_group()
        )
        rs_global_probs_grad_handle.wait()
        global_probs_grad.untyped_storage().resize_(0)

        # 反向 —— router
        backward_func(scores, rs_global_probs_grad)

        rs_global_hidden_states_grad, rs_handle = get_rs_global_hidden_states_grad_with_handle()
        rs_handle.wait()

        rs_global_hidden_states_grad = rs_global_hidden_states_grad.view(ctx.input_shape)
        # expert grad + shared expert grad
        rs_global_hidden_states_grad += input_.grad
        return rs_global_hidden_states_grad, None
