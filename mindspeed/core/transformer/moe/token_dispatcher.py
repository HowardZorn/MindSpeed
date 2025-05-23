# Copyright (c) 2024; NVIDIA CORPORATION. All rights reserved.
# Copyright (c) 2024, Huawei Technologies Co., Ltd.  All rights reserved.
import torch

from megatron.core import parallel_state, tensor_parallel
from megatron.core.tensor_parallel.mappings import (
    _gather_along_first_dim,
    gather_from_sequence_parallel_region,
    reduce_scatter_to_sequence_parallel_region,
)
from megatron.core.transformer.moe.moe_utils import (
    get_capacity,
    permute,
    sort_chunks_by_idxs,
    unpermute,
)
from megatron.core.transformer.transformer_config import TransformerConfig


def preprocess(self, routing_map: torch.Tensor) -> torch.Tensor:
    """
    Preprocess token routing map for AlltoAll communication and token permutation.

    This method computes the number of tokens assigned to each expert based on the routing_map.
    It also initializes the necessary data structures for AlltoAll communication, such as input
    and output splits, and the mapping between global tokens and local experts.

    Args:
        routing_map (torch.Tensor): The mapping of tokens to experts, with shape
            [num_tokens, num_experts].

    Returns:
        torch.Tensor: Tensor containing the number of tokens assigned to local expert.
    """
    num_local_tokens_per_expert = routing_map.sum(dim=0).long()

    if self.drop_and_pad:
        num_tokens = routing_map.size(0) * self.config.moe_router_topk
        self.capacity = get_capacity(
            num_tokens=num_tokens,
            num_experts=self.num_experts,
            capacity_factor=self.config.moe_expert_capacity_factor,
        )
        self.num_out_tokens = self.capacity * self.num_experts

        num_tokens_per_local_expert = torch.full(
            (self.num_local_experts,),
            self.capacity * self.tp_size * self.ep_size,
            dtype=torch.long,
        )
        # [tp_size * ep_size, num_local_experts].
        self.num_global_tokens_per_local_expert_cpu = torch.full(
            (self.num_experts * self.tp_size,), self.capacity, dtype=torch.long
        )
        return num_tokens_per_local_expert
    elif self.config.moe_expert_capacity_factor is not None:
        # Token drop but no pad. A synchronization is needed before the first
        # permutation to get the `num_out_tokens` CPU value.
        self.num_out_tokens = num_local_tokens_per_expert.sum().to(
            torch.device("cpu"), non_blocking=True
        )
        self.cuda_sync_point = "before_permutation_1"
    else:
        # Dropless
        self.num_out_tokens = routing_map.size(0) * self.config.moe_router_topk
        if self.ep_size > 1 or self.num_local_experts > 1:
            # Token dropless and enable ep. A synchronization is needed before expert parallel
            # AlltoAll communication to get the `input_splits` and `output_splits` CPU values.
            self.cuda_sync_point = "before_ep_alltoall"
        else:
            # Token dropless and no ep. A synchronization is needed before the token_permutation()
            # function returns to get the `tokens_per_expert` CPU value.
            self.cuda_sync_point = "before_finish"

    if self.ep_size > 1 or self.tp_size > 1:
        # ===================================================
        # Calculate input_splits, output_splits for alltoall/allgather in variable size.
        # ===================================================
        self.input_splits = (
            num_local_tokens_per_expert.reshape(self.ep_size, self.num_local_experts)
            .sum(axis=1)
            .to(torch.device("cpu"), non_blocking=True)
            .numpy()
        )
        # Gather the global distribution of tokens across ranks.
        # num_global_tokens_per_expert represents the number of tokens sent to each
        # expert by all ranks.
        num_global_tokens_per_expert = (
            gather_from_sequence_parallel_region(
                num_local_tokens_per_expert, group=self.tp_ep_group
            )
            .reshape(self.ep_size, self.tp_size, self.num_experts)
            .transpose(0, 1)
        )
        # [tp_size, ep_size, num_experts] -> [tp_size, ep_size, num_local_experts]
        num_global_tokens_per_local_expert = num_global_tokens_per_expert[
            :, :, self.local_expert_indices[0] : self.local_expert_indices[-1] + 1
        ].contiguous()
        # [tp_size, ep_size, num_local_experts] -> [tp_size, ep_size]
        num_global_tokens_per_rank = num_global_tokens_per_local_expert.sum(axis=2)
        # [tp_size, ep_size] -> [ep_size]
        # self.output_splits represents the number of tokens received by the current rank
        # from other EP rank.
        self.output_splits = (
            num_global_tokens_per_rank[self.tp_rank]
            .to(torch.device("cpu"), non_blocking=True)
            .numpy()
        )
        # [tp_size, ep_size] -> [tp_size]
        # self.output_splits_tp represents the number of tokens received by the current
        # rank from other TP rank.
        self.output_splits_tp = (
            num_global_tokens_per_rank.sum(axis=1)
            .to(torch.device("cpu"), non_blocking=True)
            .numpy()
        )
        # [tp_size, ep_size, num_local_experts] -> [num_local_experts]
        num_tokens_per_local_expert = num_global_tokens_per_local_expert.sum(dim=(0, 1))
    else:
        num_global_tokens_per_local_expert = num_local_tokens_per_expert.reshape(
            self.num_experts
        )
        num_tokens_per_local_expert = num_local_tokens_per_expert

    if self.num_local_experts > 1:
        self.num_global_tokens_per_local_expert_cpu = num_global_tokens_per_local_expert.view(
            -1, self.num_local_experts
        ).to(torch.device("cpu"), non_blocking=True)

    return num_tokens_per_local_expert
