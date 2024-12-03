# coding=utf-8
# Copyright (c) 2024, Huawei Technologies Co., Ltd. All rights reserved.
# Copyright (c) 2022-2024, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
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
"""Expert parallel groups."""
import os
from functools import wraps
from typing import Optional
from datetime import timedelta

import torch
import megatron
from megatron.core.parallel_state import get_context_parallel_world_size, get_nccl_options

from mindspeed.core.simple_parallel_cfg import SimpleParallelCfg
from mindspeed.core.tensor_parallel_y_union_cp import TensorParallelYUnionCP

_CONTEXT_PARALLEL_GROUP_FOR_SEND_RECV_OVERLAP = None
_CONTEXT_PARALLEL_GROUP_FOR_HYBRID_ULYSSES = None
_CONTEXT_PARALLEL_GROUP_FOR_HYBRID_RING = None
_PIPELINE_MODEL_PARALLEL_GROUP_FOR_NEW_STREAM = None

_CONTEXT_PARALLEL_RANKS_FOR_HYBRID_ULYSSES = None
_CONTEXT_PARALLEL_RANKS_FOR_HYBRID_RING = None

_CONTEXT_PARALLEL_RANKS_FOR_RING_INTRA_WINDOW = None
_CONTEXT_PARALLEL_RANKS_FOR_RING_INTER_WINDOW_KV = None
_CONTEXT_PARALLEL_RANKS_FOR_RING_INTER_WINDOW_DKV = None
_CONTEXT_PARALLEL_GROUP_FOR_RING_INTRA_WINDOW = None
_CONTEXT_PARALLEL_GROUP_FOR_RING_INTRA_WINDOW_SEND_RECV_OVERLAP = None

_TP_X_PARALLEL_RING_RANKS = None
_TP_Y_PARALLEL_RING_RANKS = None

_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1 = None
_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2 = None
_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM1 = None
_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM2 = None
_TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM1 = None
_TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM2 = None
_TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND2_DIM1 = None
_TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND2_DIM2 = None
_TP_X_SD_RCV_OVERLAP_GROUP = None
_TP_Y_SD_RCV_OVERLAP_GROUP = None
_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_RANK = None
_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_RANK = None
_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_WORLD_SIZE = None
_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_WORLD_SIZE = None

_TENSOR_AND_CONTEXT_PARALLEL_GROUP = None
_TENSOR_AND_CONTEXT_PARALLEL_GLOBAL_RANKS = None


def initialize_model_parallel_wrapper(initialize_model_parallel):
    @wraps(initialize_model_parallel)
    def wrapper(
            tensor_model_parallel_size: int = 1,
            pipeline_model_parallel_size: int = 1,
            virtual_pipeline_model_parallel_size: Optional[int] = None,
            pipeline_model_parallel_split_rank: Optional[int] = None,
            use_sharp: bool = False,
            context_parallel_size: int = 1,
            expert_model_parallel_size: int = 1,
            nccl_communicator_config_path: Optional[str] = None,
            distributed_timeout_minutes: int = 30,
            order: str = "tp-cp-ep-dp-pp",
    ):
        from megatron.training.utils import print_rank_0

        if order == "tp-cp-ep-dp-pp":
            # Megatron doesn't allow ep & cp combination, set ep to 1 to bypass that, ep related groups will be regenerated
            initialize_model_parallel(
                tensor_model_parallel_size,
                pipeline_model_parallel_size,
                virtual_pipeline_model_parallel_size,
                pipeline_model_parallel_split_rank,
                use_sharp,
                context_parallel_size,
                1,
                nccl_communicator_config_path,
                distributed_timeout_minutes,
                order
            )

            rank = torch.distributed.get_rank()
            world_size: int = torch.distributed.get_world_size()
            num_tensor_model_parallel_groups: int = world_size // tensor_model_parallel_size
            num_pipeline_model_parallel_groups: int = world_size // pipeline_model_parallel_size
            data_parallel_size: int = world_size // (
                    tensor_model_parallel_size * pipeline_model_parallel_size * context_parallel_size
            )

            if data_parallel_size * context_parallel_size % expert_model_parallel_size != 0:
                raise RuntimeError(
                    f"data_parallel_size * context_parallel_size ({data_parallel_size * context_parallel_size}) is not "
                    f"divisible by expert_model_parallel_size "
                )

            nccl_comm_cfgs = {}
            if nccl_communicator_config_path is not None:
                import yaml

                with open(nccl_communicator_config_path, "r") as stream:
                    nccl_comm_cfgs = yaml.safe_load(stream)

            all_data_parallel_group_ranks = []
            all_data_parallel_group_ranks_with_cp = []
            for i in range(pipeline_model_parallel_size):
                start_rank = i * num_pipeline_model_parallel_groups
                end_rank = (i + 1) * num_pipeline_model_parallel_groups
                for j in range(context_parallel_size * tensor_model_parallel_size):
                    ranks = range(
                        start_rank + j, end_rank, context_parallel_size * tensor_model_parallel_size
                    )
                    all_data_parallel_group_ranks.append(list(ranks))
                for j in range(tensor_model_parallel_size):
                    ranks_with_cp = range(
                        start_rank + j, end_rank, tensor_model_parallel_size
                    )
                    all_data_parallel_group_ranks_with_cp.append(list(ranks_with_cp))

            timeout = timedelta(minutes=distributed_timeout_minutes)

            # # Regenerate ep related groups because ep is set to 1 in initialize_model_parallel func
            rank_generator = megatron.core.parallel_state.RankGenerator(
                tp=tensor_model_parallel_size,
                ep=expert_model_parallel_size,
                dp=data_parallel_size * context_parallel_size,
                pp=pipeline_model_parallel_size,
                cp=1,
                order=order,
            )
            for ranks in rank_generator.get_ranks('tp-ep-pp', independent_ep=True):
                group = torch.distributed.new_group(
                    ranks, timeout=timeout,
                    pg_options=get_nccl_options('mp_exp', nccl_comm_cfgs)
                )
                if rank in ranks:
                    megatron.core.parallel_state._MODEL_AND_EXPERT_PARALLEL_GROUP = group

            all_tensor_and_expert_group_ranks = []
            for ranks in rank_generator.get_ranks('tp-ep', independent_ep=True):
                all_tensor_and_expert_group_ranks.append(list(ranks))
                group = torch.distributed.new_group(
                    ranks, timeout=timeout, pg_options=get_nccl_options('tp_exp', nccl_comm_cfgs)
                )
                if rank in ranks:
                    megatron.core.parallel_state._TENSOR_AND_EXPERT_PARALLEL_GROUP = group

            all_ep_groups = []
            for ranks in rank_generator.get_ranks('ep', independent_ep=True):
                all_ep_groups.append(list(ranks))
                group = torch.distributed.new_group(
                    ranks, pg_options=get_nccl_options('exp', nccl_comm_cfgs)
                )
                if rank in ranks:
                    megatron.core.parallel_state._EXPERT_MODEL_PARALLEL_GROUP = group

            all_dp_modulo_exp_group_ranks = []
            for ranks in rank_generator.get_ranks('dp', independent_ep=True):
                all_dp_modulo_exp_group_ranks.append(list(ranks))
                group = torch.distributed.new_group(
                    ranks, timeout=timeout, pg_options=get_nccl_options('dp_modulo_exp', nccl_comm_cfgs)
                )
                group_gloo = torch.distributed.new_group(ranks, backend="gloo")
                if rank in ranks:
                    megatron.core.parallel_state._DATA_MODULO_EXPERT_PARALLEL_GROUP = group
                    megatron.core.parallel_state._DATA_MODULO_EXPERT_PARALLEL_GROUP_GLOO = group_gloo

            for ranks in rank_generator.get_ranks('dp-cp', independent_ep=True):
                # Lazy initialization of the group
                if get_context_parallel_world_size() > 1:
                    group = torch.distributed.new_group(
                        ranks,
                        timeout=timeout,
                        pg_options=get_nccl_options('dp_modulo_exp_cp', nccl_comm_cfgs),
                    )
                    group_gloo = torch.distributed.new_group(ranks, backend="gloo")
                else:
                    group = megatron.core.parallel_state._DATA_MODULO_EXPERT_PARALLEL_GROUP
                    group_gloo = megatron.core.parallel_state._DATA_MODULO_EXPERT_PARALLEL_GROUP_GLOO
                if rank in ranks:
                    megatron.core.parallel_state._DATA_MODULO_EXPERT_PARALLEL_GROUP_WITH_CP = group
                    megatron.core.parallel_state._DATA_MODULO_EXPERT_PARALLEL_GROUP_WITH_CP_GLOO = group_gloo

            all_tp_groups = []
            for i in range(num_tensor_model_parallel_groups):
                ranks = range(i * tensor_model_parallel_size, (i + 1) * tensor_model_parallel_size)
                all_tp_groups.append(list(ranks))

            print_rank_0(f"all tp gourps {all_tp_groups}")
            print_rank_0(f"all ep groups {all_ep_groups}")
            print_rank_0(f"all dp groups {all_data_parallel_group_ranks}")
            print_rank_0(f"all_dp_modulo_exp_group_ranks {all_dp_modulo_exp_group_ranks}")
            print_rank_0(f"all_tensor_and_expert_group_ranks {all_tensor_and_expert_group_ranks}")
            print_rank_0(f"all_data_parallel_group_ranks_with_cp {all_data_parallel_group_ranks_with_cp}")

        else:
            initialize_model_parallel(
                tensor_model_parallel_size,
                pipeline_model_parallel_size,
                virtual_pipeline_model_parallel_size,
                pipeline_model_parallel_split_rank,
                use_sharp,
                context_parallel_size,
                expert_model_parallel_size,
                nccl_communicator_config_path,
                distributed_timeout_minutes,
                order
            )

        initialize_context_parallel_group_for_send_recv_overlap(
            tensor_model_parallel_size,
            pipeline_model_parallel_size,
            context_parallel_size,
            nccl_comm_cfgs
        )

        initialize_context_parallel_group_for_hybrid_cp(
            tensor_model_parallel_size,
            pipeline_model_parallel_size,
            context_parallel_size,
            nccl_comm_cfgs
        )

        initialize_context_parallel_group_for_double_ring(
            tensor_model_parallel_size,
            pipeline_model_parallel_size,
            context_parallel_size,
            nccl_comm_cfgs
        )

        global _PIPELINE_MODEL_PARALLEL_GROUP_FOR_NEW_STREAM
        if _PIPELINE_MODEL_PARALLEL_GROUP_FOR_NEW_STREAM is not None:
            raise AttributeError('Pipeline parallel group for new stream is already initialized')
        num_pipeline_model_parallel_groups: int = world_size // pipeline_model_parallel_size
        for i in range(num_pipeline_model_parallel_groups):
            ranks = range(i, world_size, num_pipeline_model_parallel_groups)
            group = torch.distributed.new_group(
                ranks, pg_options=megatron.core.parallel_state.get_nccl_options('pp', nccl_comm_cfgs)
            )
            if rank in ranks:
                _PIPELINE_MODEL_PARALLEL_GROUP_FOR_NEW_STREAM = group

        from megatron.training import get_args
        args = get_args()
        nd1_dim1_sz = args.nd1_dim1_size if args.use_nd_matmul else args.tp_x
        nd2_dim1_sz = args.nd2_dim1_size if args.use_nd_matmul else args.tp_y
        initialize_ndmm_parallel_group(
            nccl_comm_cfgs,
            tensor_model_parallel_size=tensor_model_parallel_size,
            nd1_dim1_size=nd1_dim1_sz,
            nd2_dim1_size=nd2_dim1_sz,
        )
        if args.tp_2d:
            tp_y_cp_group = TensorParallelYUnionCP(
                parallel_cfg=SimpleParallelCfg(
                    dp=data_parallel_size,
                    pp=pipeline_model_parallel_size,
                    tp=tensor_model_parallel_size,
                    cp=context_parallel_size,
                    ep=expert_model_parallel_size,
                    tp_x=get_args().tp_x,
                    tp_y=get_args().tp_y,
                ),
                pg_name="tp-y-cp",
                overlap_gp_name="tp-y-cp-overlap",
                nccl_comm_cfgs=nccl_comm_cfgs
            )
            print(f'tp_y_cp_group.global_ranks={tp_y_cp_group.global_ranks} for rank {rank}')
    return wrapper


def get_ring_group_for_intra_window():
    global _CONTEXT_PARALLEL_GROUP_FOR_RING_INTRA_WINDOW
    return _CONTEXT_PARALLEL_GROUP_FOR_RING_INTRA_WINDOW


def get_ring_group_for_intra_window_send_recv_overlap():
    global _CONTEXT_PARALLEL_GROUP_FOR_RING_INTRA_WINDOW_SEND_RECV_OVERLAP
    return _CONTEXT_PARALLEL_GROUP_FOR_RING_INTRA_WINDOW_SEND_RECV_OVERLAP


def get_ring_ranks_for_intra_window():
    global _CONTEXT_PARALLEL_RANKS_FOR_RING_INTRA_WINDOW
    assert _CONTEXT_PARALLEL_RANKS_FOR_RING_INTRA_WINDOW is not None
    return _CONTEXT_PARALLEL_RANKS_FOR_RING_INTRA_WINDOW


def get_ring_ranks_for_inter_window_kv():
    global _CONTEXT_PARALLEL_RANKS_FOR_RING_INTER_WINDOW_KV
    assert _CONTEXT_PARALLEL_RANKS_FOR_RING_INTER_WINDOW_KV is not None
    return _CONTEXT_PARALLEL_RANKS_FOR_RING_INTER_WINDOW_KV


def get_ring_ranks_for_inter_window_dkv():
    global _CONTEXT_PARALLEL_RANKS_FOR_RING_INTER_WINDOW_DKV
    assert _CONTEXT_PARALLEL_RANKS_FOR_RING_INTER_WINDOW_DKV is not None
    return _CONTEXT_PARALLEL_RANKS_FOR_RING_INTER_WINDOW_DKV


def initialize_context_parallel_group_for_send_recv_overlap(
        tensor_model_parallel_size,
        pipeline_model_parallel_size,
        context_parallel_size,
        nccl_comm_cfgs
):
    from megatron.training import get_args
    if not get_args().use_cp_send_recv_overlap:
        return
    # when tp_y > 1, use TensorParallelYUnionCP
    if get_args().tp_2d and get_args().tp_y > 1:
        return
    rank = torch.distributed.get_rank()
    world_size: int = torch.distributed.get_world_size()
    num_pipeline_model_parallel_groups: int = world_size // pipeline_model_parallel_size
    data_parallel_size: int = world_size // (
            tensor_model_parallel_size * pipeline_model_parallel_size * context_parallel_size
    )
    global _CONTEXT_PARALLEL_GROUP_FOR_SEND_RECV_OVERLAP
    for i in range(pipeline_model_parallel_size):
        for j in range(data_parallel_size):
            start_rank = (
                    i * num_pipeline_model_parallel_groups
                    + j * tensor_model_parallel_size * context_parallel_size
            )
            end_rank = (
                    i * num_pipeline_model_parallel_groups
                    + (j + 1) * tensor_model_parallel_size * context_parallel_size
            )
            for k in range(tensor_model_parallel_size):
                ranks = range(start_rank + k, end_rank, tensor_model_parallel_size)
                group_send_recv_overlap = torch.distributed.new_group(
                    ranks, pg_options=megatron.core.parallel_state.get_nccl_options('cp2', nccl_comm_cfgs)
                )
                if rank in ranks:
                    _CONTEXT_PARALLEL_GROUP_FOR_SEND_RECV_OVERLAP = group_send_recv_overlap


def initialize_context_parallel_group_for_hybrid_cp(
        tensor_model_parallel_size,
        pipeline_model_parallel_size,
        context_parallel_size,
        nccl_comm_cfgs
):
    from megatron.training import get_args
    if (not hasattr(get_args(), 'context_parallel_algo') or
            (
                    get_args().context_parallel_algo != 'hybrid_cp_algo' and get_args().context_parallel_algo != 'hybrid_adaptive_cp_algo')):
        return

    rank = torch.distributed.get_rank()
    world_size: int = torch.distributed.get_world_size()
    num_pipeline_model_parallel_groups: int = world_size // pipeline_model_parallel_size
    data_parallel_size: int = world_size // (
            tensor_model_parallel_size * pipeline_model_parallel_size * context_parallel_size
    )

    ulysses_degree = get_args().ulysses_degree_in_cp
    assert (context_parallel_size > ulysses_degree and context_parallel_size % ulysses_degree == 0)
    ring_degree = context_parallel_size // ulysses_degree

    global _CONTEXT_PARALLEL_GROUP_FOR_HYBRID_ULYSSES
    global _CONTEXT_PARALLEL_RANKS_FOR_HYBRID_ULYSSES
    global _CONTEXT_PARALLEL_GROUP_FOR_HYBRID_RING
    global _CONTEXT_PARALLEL_RANKS_FOR_HYBRID_RING
    for i in range(pipeline_model_parallel_size):
        for j in range(data_parallel_size):
            start_rank = (
                    i * num_pipeline_model_parallel_groups
                    + j * tensor_model_parallel_size * context_parallel_size
            )
            end_rank = (
                    i * num_pipeline_model_parallel_groups
                    + (j + 1) * tensor_model_parallel_size * context_parallel_size
            )
            for k in range(tensor_model_parallel_size):
                # cp ranks
                ranks = list(range(start_rank + k, end_rank, tensor_model_parallel_size))
                # ulysses cp ranks. 
                # Ulysses need higher communication bandwidth than Ring.
                # Try to put Ulysses ranks in the same node.
                for m in range(ring_degree):
                    ulysses_ranks = [ranks[idx] for idx in range(m * ulysses_degree, (m + 1) * ulysses_degree)]
                    ulysses_group = torch.distributed.new_group(
                        ulysses_ranks,
                        pg_options=megatron.core.parallel_state.get_nccl_options('cp_ulysses', nccl_comm_cfgs)
                    )
                    if rank in ulysses_ranks:
                        _CONTEXT_PARALLEL_GROUP_FOR_HYBRID_ULYSSES = ulysses_group
                        _CONTEXT_PARALLEL_RANKS_FOR_HYBRID_ULYSSES = ulysses_ranks

                # ring cp ranks
                for m in range(ulysses_degree):
                    ring_ranks = [ranks[idx] for idx in range(m, len(ranks), ulysses_degree)]
                    ring_group = torch.distributed.new_group(
                        ring_ranks, pg_options=megatron.core.parallel_state.get_nccl_options('cp_ring', nccl_comm_cfgs)
                    )
                    if rank in ring_ranks:
                        _CONTEXT_PARALLEL_GROUP_FOR_HYBRID_RING = ring_group
                        _CONTEXT_PARALLEL_RANKS_FOR_HYBRID_RING = ring_ranks


def initialize_context_parallel_group_for_double_ring(
        tensor_model_parallel_size,
        pipeline_model_parallel_size,
        context_parallel_size,
        nccl_comm_cfgs,
):
    from megatron.training import get_args
    args = get_args()
    if args.tp_2d:
        return
    if context_parallel_size == 1 or args.context_parallel_algo not in ['megatron_cp_algo', 'hybrid_cp_algo']:
        return

    use_hybrid_cp = args.context_parallel_algo == 'hybrid_cp_algo' and args.ulysses_degree_in_cp > 1

    rank = torch.distributed.get_rank()
    world_size: int = torch.distributed.get_world_size()
    num_pipeline_model_parallel_groups: int = world_size // pipeline_model_parallel_size
    data_parallel_size: int = world_size // (
            tensor_model_parallel_size * pipeline_model_parallel_size * context_parallel_size
    )

    def _initialize_helper(
            rank,
            ring_global_ranks,
            window_size
    ):
        from megatron.training import get_args
        global _CONTEXT_PARALLEL_RANKS_FOR_RING_INTRA_WINDOW
        global _CONTEXT_PARALLEL_RANKS_FOR_RING_INTER_WINDOW_KV
        global _CONTEXT_PARALLEL_RANKS_FOR_RING_INTER_WINDOW_DKV
        global _CONTEXT_PARALLEL_GROUP_FOR_RING_INTRA_WINDOW
        global _CONTEXT_PARALLEL_GROUP_FOR_RING_INTRA_WINDOW_SEND_RECV_OVERLAP

        ring_size = len(ring_global_ranks)
        inter_size = ring_size // window_size
        for wid in range(inter_size):
            intra_ranks = [ring_global_ranks[idx] for idx in range(wid * window_size, (wid + 1) * window_size)]
            intra_group = torch.distributed.new_group(intra_ranks)
            intra_group_for_send_recv_overlap = None
            if args.use_cp_send_recv_overlap:
                intra_group_for_send_recv_overlap = torch.distributed.new_group(intra_ranks)

            if rank in intra_ranks:
                _CONTEXT_PARALLEL_RANKS_FOR_RING_INTRA_WINDOW = intra_ranks
                _CONTEXT_PARALLEL_GROUP_FOR_RING_INTRA_WINDOW = intra_group
                _CONTEXT_PARALLEL_GROUP_FOR_RING_INTRA_WINDOW_SEND_RECV_OVERLAP = intra_group_for_send_recv_overlap

        for inner_id in range(window_size):
            inter_ranks = [ring_global_ranks[idx] for idx in range(inner_id, ring_size, window_size)]
            if rank in inter_ranks:
                _CONTEXT_PARALLEL_RANKS_FOR_RING_INTER_WINDOW_KV = inter_ranks
                break

        for inner_id in range(window_size):
            inter_dkv_ranks = []
            cur_rank = ring_global_ranks[inner_id]
            cur_idx = inner_id
            cur_window = 0
            while cur_rank not in inter_dkv_ranks:
                inter_dkv_ranks.append(cur_rank)
                cur_window = (cur_window + 1) % inter_size
                window_start = cur_window * window_size
                cur_idx = window_start + (cur_idx + 1) % window_size
                cur_rank = ring_global_ranks[cur_idx]

            if rank in inter_dkv_ranks:
                _CONTEXT_PARALLEL_RANKS_FOR_RING_INTER_WINDOW_DKV = inter_dkv_ranks
                break


    for i in range(pipeline_model_parallel_size):
        for j in range(data_parallel_size):
            start_rank = (
                    i * num_pipeline_model_parallel_groups
                    + j * tensor_model_parallel_size * context_parallel_size
            )
            end_rank = (
                    i * num_pipeline_model_parallel_groups
                    + (j + 1) * tensor_model_parallel_size * context_parallel_size
            )
            for k in range(tensor_model_parallel_size):
                cp_ranks = range(start_rank + k, end_rank, tensor_model_parallel_size)

                if use_hybrid_cp:
                    ulysses_degree = get_args().ulysses_degree_in_cp
                    assert (context_parallel_size > ulysses_degree and context_parallel_size % ulysses_degree == 0)
                    # ring cp ranks
                    for m in range(ulysses_degree):
                        ring_ranks = [cp_ranks[idx] for idx in range(m, len(cp_ranks), ulysses_degree)]

                        _initialize_helper(rank, ring_ranks, args.cp_window_size)
                else:
                    _initialize_helper(rank, cp_ranks, args.cp_window_size)


def get_context_parallel_group_for_send_recv_overlap(check_initialized=True):
    """Get the context parallel group for send-recv overlap the caller rank belongs to."""
    if check_initialized:
        assert (
                _CONTEXT_PARALLEL_GROUP_FOR_SEND_RECV_OVERLAP is not None
        ), 'context parallel group for send-recv overlap is not initialized'
    return _CONTEXT_PARALLEL_GROUP_FOR_SEND_RECV_OVERLAP


def get_context_parallel_next_rank():
    """Return the global rank that follows the caller in the context parallel"""
    import megatron.core.parallel_state as ps
    assert ps._CONTEXT_PARALLEL_GLOBAL_RANKS is not None, "Context parallel group is not initialized"
    rank_in_context = ps.get_context_parallel_rank()
    world_size = ps.get_context_parallel_world_size()
    return ps._CONTEXT_PARALLEL_GLOBAL_RANKS[(rank_in_context + 1) % world_size]


def get_context_parallel_prev_rank():
    """Return the global rank that preceeds the caller in the context parallel"""
    import megatron.core.parallel_state as ps
    assert ps._CONTEXT_PARALLEL_GLOBAL_RANKS is not None, "Context parallel group is not initialized"
    rank_in_context = ps.get_context_parallel_rank()
    world_size = ps.get_context_parallel_world_size()
    return ps._CONTEXT_PARALLEL_GLOBAL_RANKS[(rank_in_context - 1) % world_size]


def get_pipeline_parallel_group_for_new_stream():
    if _PIPELINE_MODEL_PARALLEL_GROUP_FOR_NEW_STREAM is None:
        raise AttributeError('Pipeline parallel group of backward is not initialized')
    return _PIPELINE_MODEL_PARALLEL_GROUP_FOR_NEW_STREAM


def get_context_parallel_group_for_hybrid_ulysses(check_initialized=True):
    """Get the context parallel group for hybrid ulysses the caller rank belongs to."""
    if check_initialized:
        assert (
                _CONTEXT_PARALLEL_GROUP_FOR_HYBRID_ULYSSES is not None
        ), 'context parallel group for hybrid ulysses is not initialized'
    return _CONTEXT_PARALLEL_GROUP_FOR_HYBRID_ULYSSES


def get_context_parallel_for_hybrid_ulysses_world_size():
    return torch.distributed.get_world_size(group=get_context_parallel_group_for_hybrid_ulysses())


def get_context_parallel_for_hybrid_ulysses_rank():
    return torch.distributed.get_rank(group=get_context_parallel_group_for_hybrid_ulysses())


def get_context_parallel_group_for_hybrid_ring(check_initialized=True):
    """Get the context parallel group for hybrid ring the caller rank belongs to."""
    if check_initialized:
        assert (
                _CONTEXT_PARALLEL_GROUP_FOR_HYBRID_RING is not None
        ), 'context parallel group for hybrid ring is not initialized'
    return _CONTEXT_PARALLEL_GROUP_FOR_HYBRID_RING


def get_context_parallel_for_hybrid_ring_world_size():
    return torch.distributed.get_world_size(group=get_context_parallel_group_for_hybrid_ring())


def get_context_parallel_for_hybrid_ring_rank():
    return torch.distributed.get_rank(group=get_context_parallel_group_for_hybrid_ring())


def get_context_parallel_for_hybrid_ring_global_ranks():
    assert (_CONTEXT_PARALLEL_GROUP_FOR_HYBRID_RING is not None
            ), 'context parallel group for hybrid ring is not initialized'
    global _CONTEXT_PARALLEL_RANKS_FOR_HYBRID_RING
    return _CONTEXT_PARALLEL_RANKS_FOR_HYBRID_RING


def get_tp_x_ring_global_ranks():
    global _TP_X_PARALLEL_RING_RANKS
    assert (_TP_X_PARALLEL_RING_RANKS is not None), 'TP-X parallel group for ring is not initialized'
    return _TP_X_PARALLEL_RING_RANKS


def get_tp_y_ring_global_ranks():
    global _TP_Y_PARALLEL_RING_RANKS
    assert (_TP_Y_PARALLEL_RING_RANKS is not None), 'TP-Y parallel group for ring is not initialized'
    return _TP_Y_PARALLEL_RING_RANKS


def destroy_model_parallel_wrapper(destroy_model_parallel):
    @wraps(destroy_model_parallel)
    def wrapper():
        destroy_model_parallel()

        global _CONTEXT_PARALLEL_GROUP_FOR_SEND_RECV_OVERLAP
        global _PIPELINE_MODEL_PARALLEL_GROUP_FOR_NEW_STREAM
        global _CONTEXT_PARALLEL_GROUP_FOR_HYBRID_RING
        global _CONTEXT_PARALLEL_GROUP_FOR_HYBRID_ULYSSES
        global _CONTEXT_PARALLEL_RANKS_FOR_HYBRID_RING
        global _CONTEXT_PARALLEL_RANKS_FOR_HYBRID_ULYSSES
        global _TP_X_PARALLEL_RING_RANKS
        global _TP_Y_PARALLEL_RING_RANKS
        global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1
        global _TP_X_SD_RCV_OVERLAP_GROUP
        global _TP_Y_SD_RCV_OVERLAP_GROUP
        global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2
        global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_RANK
        global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_RANK
        global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_WORLD_SIZE
        global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_WORLD_SIZE
        global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM1
        global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM2
        global _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM1
        global _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM2
        global _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND2_DIM1
        global _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND2_DIM2
        global _TENSOR_AND_CONTEXT_PARALLEL_GROUP
        global _TENSOR_AND_CONTEXT_PARALLEL_GLOBAL_RANKS
        _CONTEXT_PARALLEL_GROUP_FOR_SEND_RECV_OVERLAP = None
        _PIPELINE_MODEL_PARALLEL_GROUP_FOR_NEW_STREAM = None
        _CONTEXT_PARALLEL_GROUP_FOR_HYBRID_RING = None
        _CONTEXT_PARALLEL_GROUP_FOR_HYBRID_ULYSSES = None
        _CONTEXT_PARALLEL_RANKS_FOR_HYBRID_RING = None
        _CONTEXT_PARALLEL_RANKS_FOR_HYBRID_ULYSSES = None
        _TENSOR_AND_CONTEXT_PARALLEL_GROUP = None
        _TENSOR_AND_CONTEXT_PARALLEL_GLOBAL_RANKS = None
        _TP_X_PARALLEL_RING_RANKS = None
        _TP_Y_PARALLEL_RING_RANKS = None
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1 = None
        _TP_X_SD_RCV_OVERLAP_GROUP = None
        _TP_Y_SD_RCV_OVERLAP_GROUP = None
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2 = None
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_RANK = None
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_RANK = None
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_WORLD_SIZE = None
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_WORLD_SIZE = None
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM1 = None
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM2 = None
        _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM1 = None
        _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM2 = None
        _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND2_DIM1 = None
        _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND2_DIM2 = None

    return wrapper


def get_tensor_model_parallel_group_for_nd1_dim1(check_initialized=True):
    if check_initialized and _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1 is None:
        raise AssertionError('tensor model parallel group for nd1 dim1 is not initialized')
    return _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1


def get_tp_x_sd_rcv_overlap_group(check_initialized=True):
    if check_initialized and _TP_X_SD_RCV_OVERLAP_GROUP is None:
        raise AssertionError('tp-x send recv overlap group is not initialized')
    return _TP_X_SD_RCV_OVERLAP_GROUP


def get_tp_y_sd_rcv_overlap_group(check_initialized=True):
    if check_initialized and _TP_Y_SD_RCV_OVERLAP_GROUP is None:
        raise AssertionError('tp-y send recv overlap group is not initialized')
    return _TP_Y_SD_RCV_OVERLAP_GROUP


def get_tensor_model_parallel_group_for_nd1_dim2(check_initialized=True):
    if check_initialized and _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2 is None:
        raise AssertionError('tensor model parallel group for nd1 dim2 is not initialized')
    return _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2


def get_tensor_model_parallel_group_for_nd2_dim1(check_initialized=True):
    if check_initialized and _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM1 is None:
        raise AssertionError('tensor model parallel group for nd2 dim1 is not initialized')
    return _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM1


def get_tensor_model_parallel_group_for_nd1_dim1_rank():
    global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_RANK
    if _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_RANK is None:
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_RANK = torch.distributed.get_rank(
            group=get_tensor_model_parallel_group_for_nd1_dim1())

    return _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_RANK


def get_tensor_model_parallel_group_for_nd1_dim2_rank():
    global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_RANK
    if _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_RANK is None:
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_RANK = torch.distributed.get_rank(
            group=get_tensor_model_parallel_group_for_nd1_dim2())

    return _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_RANK


def get_tensor_model_parallel_group_for_nd1_dim1_world_size():
    global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_WORLD_SIZE
    if _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_WORLD_SIZE is None:
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_WORLD_SIZE = torch.distributed.get_world_size(
            group=get_tensor_model_parallel_group_for_nd1_dim1())

    return _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_WORLD_SIZE


def get_tensor_model_parallel_group_for_nd1_dim2_world_size():
    global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_WORLD_SIZE
    if _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_WORLD_SIZE is None:
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_WORLD_SIZE = torch.distributed.get_world_size(
            group=get_tensor_model_parallel_group_for_nd1_dim2())

    return _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_WORLD_SIZE


def get_tensor_model_parallel_group_for_nd2_dim2(check_initialized=True):
    if check_initialized and _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM2 is None:
        raise AssertionError('tensor model parallel group for nd2 dim2 is not initialized')
    return _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM2


def get_tensor_model_parallel_world_size_for_nd1_dim1():
    global _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM1
    if _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM1 is None:
        _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM1 = torch.distributed.get_world_size(
            group=get_tensor_model_parallel_group_for_nd1_dim1()
        )
    return _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM1


def get_tensor_model_parallel_world_size_for_nd1_dim2():
    global _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM2
    if _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM2 is None:
        _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM2 = torch.distributed.get_world_size(
            group=get_tensor_model_parallel_group_for_nd1_dim2()
        )
    return _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM2


def get_tensor_model_parallel_world_size_for_nd2_dim1():
    global _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND2_DIM1
    if _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND2_DIM1 is None:
        _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND2_DIM1 = torch.distributed.get_world_size(
            group=get_tensor_model_parallel_group_for_nd2_dim1()
        )
    return _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND2_DIM1


def get_tensor_model_parallel_world_size_for_nd2_dim2():
    global _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND2_DIM2
    if _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND2_DIM2 is None:
        _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND2_DIM2 = torch.distributed.get_world_size(
            group=get_tensor_model_parallel_group_for_nd2_dim2()
        )
    return _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND2_DIM2


def initialize_ndmm_parallel_group(
        nccl_comm_cfgs: dict,
        tensor_model_parallel_size: int = 1,
        nd1_dim1_size: int = 1,
        nd2_dim1_size: int = 1,
) -> None:
    import megatron.core.parallel_state as ps
    from megatron.training import get_args
    from megatron.training.global_vars import _ensure_var_is_not_initialized

    args = get_args()
    if not (args.use_nd_matmul or args.tp_2d):
        return

    global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1
    _ensure_var_is_not_initialized(
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1, 'nd1_dim1'
    )

    global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2
    _ensure_var_is_not_initialized(
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2, 'nd1_dim2'
    )

    global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM1
    _ensure_var_is_not_initialized(
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM1, 'nd2_dim1'
    )

    global _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM2
    _ensure_var_is_not_initialized(
        _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM2, 'nd2_dim2'
    )

    global _TP_X_PARALLEL_RING_RANKS
    _ensure_var_is_not_initialized(_TP_X_PARALLEL_RING_RANKS, 'tp_x_ring_ranks')

    global _TP_Y_PARALLEL_RING_RANKS
    _ensure_var_is_not_initialized(_TP_Y_PARALLEL_RING_RANKS, 'tp_y_ring_ranks')

    global _TP_X_SD_RCV_OVERLAP_GROUP
    _ensure_var_is_not_initialized(_TP_X_SD_RCV_OVERLAP_GROUP, 'tp_x_overlap_ranks')

    global _TP_Y_SD_RCV_OVERLAP_GROUP
    _ensure_var_is_not_initialized(_TP_Y_SD_RCV_OVERLAP_GROUP, 'tp_y_overlap_ranks')

    if tensor_model_parallel_size % nd1_dim1_size != 0:
        raise RuntimeError(
            f"tensor_model_parallel_size can't divisible by nd1_dim1_size"
        )

    if tensor_model_parallel_size % nd2_dim1_size != 0:
        raise RuntimeError(
            f"tensor_model_parallel_size can't divisible by nd2_dim1_size"
        )

    rank = torch.distributed.get_rank()
    world_size: int = torch.distributed.get_world_size()
    num_tensor_model_parallel_group: int = world_size // tensor_model_parallel_size

    tp_nd1_dim1_groups = []
    tp_nd1_dim2_groups = []
    tp_nd2_dim1_groups = []
    tp_nd2_dim2_groups = []
    for i in range(num_tensor_model_parallel_group):
        for j in range(tensor_model_parallel_size // nd1_dim1_size):
            ranks = range(
                i * tensor_model_parallel_size + j * nd1_dim1_size,
                i * tensor_model_parallel_size + (j + 1) * nd1_dim1_size
            )
            tp_nd1_dim1_groups.append(list(ranks))
            group = torch.distributed.new_group(
                ranks, pg_options=ps.get_nccl_options('nd1_dim1', nccl_comm_cfgs)
            )
            if args.enable_overlap_ag_with_matmul or args.enable_backward_overlap_ag_with_matmul:
                tp_x_ag_overlap_group = torch.distributed.new_group(
                    ranks, pg_options=ps.get_nccl_options('ag_x_sd_rcv_overlap', nccl_comm_cfgs)
                )
            else:
                tp_x_ag_overlap_group = None
            if rank in ranks:
                _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1 = group
                _TP_X_SD_RCV_OVERLAP_GROUP = tp_x_ag_overlap_group
                _TP_X_PARALLEL_RING_RANKS = ranks

        nd1_dim2_size = tensor_model_parallel_size // nd1_dim1_size
        for j in range(tensor_model_parallel_size // nd1_dim2_size):
            ranks = range(
                i * tensor_model_parallel_size + j,
                (i + 1) * tensor_model_parallel_size,
                nd1_dim1_size
            )
            tp_nd1_dim2_groups.append(list(ranks))
            group = torch.distributed.new_group(
                ranks, pg_options=ps.get_nccl_options('nd1_dim2', nccl_comm_cfgs)
            )
            if args.enable_overlap_ag_with_matmul or args.enable_backward_overlap_ag_with_matmul:
                tp_y_ag_overlap_group = torch.distributed.new_group(
                    ranks, pg_options=ps.get_nccl_options('ag_y_sd_rcv_overlap', nccl_comm_cfgs)
                )
            else:
                tp_y_ag_overlap_group = None
            if rank in ranks:
                _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2 = group
                _TP_Y_SD_RCV_OVERLAP_GROUP = tp_y_ag_overlap_group
                _TP_Y_PARALLEL_RING_RANKS = ranks

        for j in range(tensor_model_parallel_size // nd2_dim1_size):
            ranks = range(
                i * tensor_model_parallel_size + j * nd2_dim1_size,
                i * tensor_model_parallel_size + (j + 1) * nd2_dim1_size
            )
            tp_nd2_dim1_groups.append(list(ranks))
            group = torch.distributed.new_group(
                ranks, pg_options=ps.get_nccl_options('nd2_dim1', nccl_comm_cfgs)
            )
            if rank in ranks:
                _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM1 = group

        nd2_dim2_size = tensor_model_parallel_size // nd2_dim1_size
        for j in range(tensor_model_parallel_size // nd2_dim2_size):
            ranks = range(
                i * tensor_model_parallel_size + j,
                (i + 1) * tensor_model_parallel_size,
                nd2_dim1_size
            )
            tp_nd2_dim2_groups.append(list(ranks))
            group = torch.distributed.new_group(
                ranks, pg_options=ps.get_nccl_options('nd2_dim2', nccl_comm_cfgs)
            )
            if rank in ranks:
                _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM2 = group


def get_data_parallel_group_gloo_replace(with_context_parallel=False):
    """Get the data parallel group-gloo the caller rank belongs to."""
    import megatron.core.parallel_state as ps

    if with_context_parallel:
        assert (
            ps._DATA_PARALLEL_GROUP_WITH_CP_GLOO is None
        ), 'data parallel group-gloo with context parallel combined should be None when args.disable_gloo_group is True'
        return ps._DATA_PARALLEL_GROUP_WITH_CP
    else:
        assert ps._DATA_PARALLEL_GROUP_GLOO is None, 'data parallel group-gloo should be None when args.disable_gloo_group is True'
        return ps._DATA_PARALLEL_GROUP


def get_data_modulo_expert_parallel_group_gloo_replace(with_context_parallel=False):
    import megatron.core.parallel_state as ps

    if with_context_parallel:
        assert (
            ps._DATA_MODULO_EXPERT_PARALLEL_GROUP_WITH_CP_GLOO is None
        ), 'data modulo expert parallel group-gloo with context parallel is not initialized'
        return ps._DATA_MODULO_EXPERT_PARALLEL_GROUP_WITH_CP
    else:
        assert (
            ps._DATA_MODULO_EXPERT_PARALLEL_GROUP_GLOO is None
        ), 'data modulo expert parallel group-gloo should be None when args.disable_gloo_group is True'
        return ps._DATA_MODULO_EXPERT_PARALLEL_GROUP


def new_group_wrapper(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        from megatron.training import get_args
        if get_args().disable_gloo_group:
            if "backend" in kwargs and kwargs["backend"] == "gloo":
                return None
        return fn(*args, **kwargs)
    return wrapper