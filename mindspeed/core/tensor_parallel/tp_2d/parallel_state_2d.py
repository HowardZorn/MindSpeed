# Copyright (c) 2025, Huawei Technologies Co., Ltd. All rights reserved.

from typing import Optional, Callable, List
from logging import getLogger

import torch

from mindspeed.core.simple_parallel_cfg import SimpleParallelCfg
from mindspeed.core.tensor_parallel_y_union_cp import TensorParallelYUnionCP

LOG = getLogger(__name__)

_CONTEXT_PARALLEL_GROUP_FOR_SEND_RECV_OVERLAP = None

_TP_X_PARALLEL_RING_RANKS = None
_TP_Y_PARALLEL_RING_RANKS = None

_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1 = None
_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2 = None
_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM1 = None
_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM2 = None
_TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM1 = None
_TP_X_SD_RCV_OVERLAP_GROUP = None
_TP_Y_SD_RCV_OVERLAP_GROUP = None
_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_RANK = None
_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_RANK = None
_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM1_WORLD_SIZE = None
_TENSOR_MODEL_PARALLEL_GROUP_FOR_ND1_DIM2_WORLD_SIZE = None


def _ensure_var_is_not_initialized(var, name):
    """Make sure the input variable is not None."""
    if var is not None:
        raise AssertionError('{} is already initialized.'.format(name))
        
        
def get_nccl_options(pg_name, nccl_comm_cfgs):
    """Set the NCCL process group options.

    Args:
        pg_name (str): process group name
        nccl_comm_cfgs (dict): nccl communicator configurations

    When an option (e.g., max_ctas) is not found in the config, use the NCCL default setting.
    """
    if pg_name in nccl_comm_cfgs:
        nccl_options = torch.distributed.ProcessGroupNCCL.Options()
        nccl_options.config.cga_cluster_size = nccl_comm_cfgs[pg_name].get('cga_cluster_size', 4)
        nccl_options.config.max_ctas = nccl_comm_cfgs[pg_name].get('max_ctas', 32)
        nccl_options.config.min_ctas = nccl_comm_cfgs[pg_name].get('min_ctas', 1)
        return nccl_options
    else:
        return None


def initialize_model_parallel_impl(
        tensor_model_parallel_size: int = 1,
        pipeline_model_parallel_size: int = 1,
        virtual_pipeline_model_parallel_size: Optional[int] = None,
        pipeline_model_parallel_split_rank: Optional[int] = None,
        pipeline_model_parallel_comm_backend: Optional[str] = None,
        use_sharp: bool = False,
        context_parallel_size: int = 1,
        hierarchical_context_parallel_sizes: Optional[List[int]] = None,
        expert_model_parallel_size: int = 1,
        num_distributed_optimizer_instances: int = 1,
        expert_tensor_parallel_size: Optional[int] = None,
        nccl_communicator_config_path: Optional[str] = None,
        distributed_timeout_minutes: int = 30,
        order: str = "tp-cp-ep-dp-pp",
        encoder_tensor_model_parallel_size: int = 0,
        encoder_pipeline_model_parallel_size: Optional[int] = 0,
        get_embedding_ranks: Optional[Callable[[List[int], Optional[int]], List[int]]] = None,
        get_position_embedding_ranks: Optional[Callable[[List[int], Optional[int]], List[int]]] = None,
        create_gloo_process_groups: bool = True,
        config=None
):
    world_size = torch.distributed.get_world_size()
    data_parallel_size = world_size // (
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

    initialize_ndmm_parallel_group(
        nccl_comm_cfgs,
        tensor_model_parallel_size=tensor_model_parallel_size,
        nd1_dim1_size=config.tp_x,
        nd2_dim1_size=config.tp_y,
        config=config
    )
    if config.tp_2d:
        tp_y_cp_group = TensorParallelYUnionCP(
            parallel_cfg=SimpleParallelCfg(
                dp=data_parallel_size,
                pp=pipeline_model_parallel_size,
                tp=tensor_model_parallel_size,
                cp=context_parallel_size,
                ep=expert_model_parallel_size,
                tp_x=config.tp_x,
                tp_y=config.tp_y,
            ),
            pg_name="tp-y-cp",
            overlap_gp_name="tp-y-cp-overlap",
            nccl_comm_cfgs=nccl_comm_cfgs
        )
        print(f'tp_y_cp_group.global_ranks={tp_y_cp_group.global_ranks} for rank {torch.distributed.get_rank()}')


def get_tp_x_ring_global_ranks():
    global _TP_X_PARALLEL_RING_RANKS
    if _TP_X_PARALLEL_RING_RANKS is None:
        raise AssertionError('TP-X parallel group for ring is not initialized')
    return _TP_X_PARALLEL_RING_RANKS


def get_tp_y_ring_global_ranks():
    global _TP_Y_PARALLEL_RING_RANKS
    if _TP_Y_PARALLEL_RING_RANKS is None:
        raise AssertionError('TP-Y parallel group for ring is not initialized')
    return _TP_Y_PARALLEL_RING_RANKS


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


def get_tensor_model_parallel_world_size_for_nd1_dim1():
    global _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM1
    if _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM1 is None:
        _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM1 = torch.distributed.get_world_size(
            group=get_tensor_model_parallel_group_for_nd1_dim1()
        )
    return _TENSOR_MODEL_PARALLEL_WORLD_SIZE_FOR_ND1_DIM1


def initialize_ndmm_parallel_group(
        nccl_comm_cfgs: dict,
        tensor_model_parallel_size: int = 1,
        nd1_dim1_size: int = 1,
        nd2_dim1_size: int = 1,
        config=None
) -> None:

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
            "tensor_model_parallel_size can't divisible by nd1_dim1_size"
        )

    if tensor_model_parallel_size % nd2_dim1_size != 0:
        raise RuntimeError(
            "tensor_model_parallel_size can't divisible by nd2_dim1_size"
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
                ranks, pg_options=get_nccl_options('nd1_dim1', nccl_comm_cfgs)
            )
            if config.enable_overlap_ag_with_matmul or config.enable_backward_overlap_ag_with_matmul:
                tp_x_ag_overlap_group = torch.distributed.new_group(
                    ranks, pg_options=get_nccl_options('ag_x_sd_rcv_overlap', nccl_comm_cfgs)
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
                ranks, pg_options=get_nccl_options('nd1_dim2', nccl_comm_cfgs)
            )
            if config.enable_overlap_ag_with_matmul or config.enable_backward_overlap_ag_with_matmul:
                tp_y_ag_overlap_group = torch.distributed.new_group(
                    ranks, pg_options=get_nccl_options('ag_y_sd_rcv_overlap', nccl_comm_cfgs)
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
                ranks, pg_options=get_nccl_options('nd2_dim1', nccl_comm_cfgs)
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
                ranks, pg_options=get_nccl_options('nd2_dim2', nccl_comm_cfgs)
            )
            if rank in ranks:
                _TENSOR_MODEL_PARALLEL_GROUP_FOR_ND2_DIM2 = group
