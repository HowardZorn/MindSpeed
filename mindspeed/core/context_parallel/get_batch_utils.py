# Copyright (c) 2025, Huawei Technologies Co., Ltd. All rights reserved.
# Copyright (c) 2022-2024, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
import torch
from mindspeed.core.context_parallel import get_args
from mindspeed.core.context_parallel import mpu
from mindspeed.core.tensor_parallel_y_union_cp import TensorParallelYUnionCP
from mindspeed.model.transformer import set_attention_mask, get_attention_mask
from mindspeed.core.context_parallel.model_parallel_utils import (get_context_parallel_for_hybrid_ulysses_world_size,
                                             get_context_parallel_for_hybrid_ulysses_rank,
                                             get_context_parallel_for_hybrid_ring_world_size,
                                             get_context_parallel_for_hybrid_ring_rank)
from mindspeed.core.context_parallel.utils import (set_scheduling_info,
                                                   set_remapped_seq_order,
                                                   adaptive_reschedule_task,
                                                   get_adaptive_cp_mask_list_by_user,
                                                   get_adaptive_cp_grid_mask_by_user,
                                                   generate_adaptive_cp_mask_list_by_user,
                                                   generate_adaptive_cp_grid_mask_by_user)

_ACTUAL_SEQ_LEN = None
_REARRANGE_IDX_TENSOR = None


def get_actual_seq_len():
    global _ACTUAL_SEQ_LEN
    return _ACTUAL_SEQ_LEN


def set_actual_seq_len(actual_seq_len):
    global _ACTUAL_SEQ_LEN
    _ACTUAL_SEQ_LEN = actual_seq_len


def get_ring_degree():
    args = get_args()
    cp_size = args.context_parallel_size
    if cp_size == 1:
        return 1

    if args.context_parallel_algo == 'megatron_cp_algo':
        return cp_size
    elif args.context_parallel_algo == 'ulysses_cp_algo':
        return 1
    else:
        return args.ring_degree


def _broadcast(item):
    if item is not None:
        torch.distributed.broadcast(item, mpu.get_tensor_model_parallel_src_rank(), group=mpu.get_tensor_model_parallel_group())


def broadcast_dynamic(item):
    if item is not None:
        item = item.npu()
        item_len = torch.tensor(item.numel(), device=torch.cuda.current_device())
        _broadcast(item_len)
        _broadcast(item)
    else:
        item_len = torch.empty((), dtype=torch.int64, device=torch.cuda.current_device())
        _broadcast(item_len)
        item = torch.empty([item_len.item()], dtype=torch.int64, device=torch.cuda.current_device())
        _broadcast(item)

    return item


def generate_rearrange_idx_tensor(tp_y_cp_size):
    global _REARRANGE_IDX_TENSOR
    if _REARRANGE_IDX_TENSOR is None:
        rearrange_index = []
        for i in range(tp_y_cp_size):
            rearrange_index.extend([i, 2 * tp_y_cp_size - 1 - i])
        _REARRANGE_IDX_TENSOR = torch.tensor(rearrange_index, device='cpu', pin_memory=True).to(device='npu', non_blocking=True)
    return _REARRANGE_IDX_TENSOR


def batch_index(seq1d, seq_len):
    from bisect import bisect_right
    end_points = list(range(seq_len, seq1d[-1] + 1, seq_len))
    indexes = [0] + [bisect_right(seq1d, p) for p in end_points]
    seq_batch = [seq1d[indexes[i]:indexes[i + 1]] for i in range(len(indexes) - 1)]
    return [[elem - i * seq_len for elem in seq] for i, seq in enumerate(seq_batch)]


def get_batch_on_this_cp_rank(batch):
    """ Slice batch input along sequence dimension into multiple chunks,
        which are parallelized across GPUs in a context parallel group.
    """

    # With causal masking, each token only attends to its prior tokens. Simply split
    # sequence into CP chunks can result in severe load imbalance. That's to say, chunks
    # at the end of sequence have bigger workload than others. To address this issue,
    # we split sequence into 2*CP ranks. Assuming CP=2, we then get 4 chunks, chunk_0
    # and chunk_3 are assigned to GPU0, chunk_1 and chunk_2 are assigned to GPU1, so
    # that we can get balanced workload among GPUs in a context parallel group.
    args = get_args()

    cp_size = args.context_parallel_size

    if cp_size == 1:
        return batch

    tp_y_cp_size = TensorParallelYUnionCP().get_parallel_group_world_size() if args.tp_2d else args.context_parallel_size
    if not tp_y_cp_size > 1:
        return batch

    cp_expanded_by_2d_tp = args.tp_y > 1
    if args.reset_attention_mask and args.attention_mask_type == 'causal':
        batch = _get_batch_on_this_cp_rank_in_megatron_cp_eod_padding(batch, get_actual_seq_len())
    elif args.context_parallel_algo == 'megatron_cp_algo':
        if args.attention_mask_type == 'general':
            batch = _get_batch_on_this_cp_rank_in_megatron_cp_general(batch)
        elif cp_expanded_by_2d_tp:
            batch = _get_batch_on_this_tp_y_cp_rank_in_megatron_cp(batch)
        else:
            batch = _get_batch_on_this_cp_rank_in_megatron_cp(batch)
    elif args.context_parallel_algo == 'ulysses_cp_algo':
        batch = _get_batch_on_this_cp_rank_in_ulysses_cp(batch)
    elif args.context_parallel_algo == 'hybrid_cp_algo':
        if args.attention_mask_type == 'general':
            batch = _get_batch_on_this_cp_rank_in_hybrid_cp_general(batch)
        else:
            batch = _get_batch_on_this_cp_rank_in_hybrid_cp(batch)
    elif args.context_parallel_algo == 'adaptive_cp_algo':
        batch = _get_batch_on_this_cp_rank_in_adaptive_cp(batch)
    elif args.context_parallel_algo == 'hybrid_adaptive_cp_algo':
        batch = _get_batch_on_this_cp_rank_in_hybrid_adaptive_cp(batch)
    return batch


def _get_batch_on_this_cp_rank_in_megatron_cp_eod_padding(batch, actual_seq_len):
    def get_index(batched_actual_seq_len, cp_size, cp_rank):
        full_indices = list(range(len(batched_actual_seq_len) * batched_actual_seq_len[0][-1]))
        batched_index = []
        start = 0
        offset = 0
        for actual_seq_len in batched_actual_seq_len:
            for end in actual_seq_len:
                end = end + offset
                chunk_size = (end - start) // (2 * cp_size)
                batched_index.extend(full_indices[start + cp_rank * chunk_size: start + (cp_rank + 1) * chunk_size])
                batched_index.extend(full_indices[end - (cp_rank + 1) * chunk_size: end - cp_rank * chunk_size])
                start = end
            offset += actual_seq_len[-1]

        return torch.tensor(batched_index, device='npu')

    cp_rank = mpu.get_context_parallel_rank()
    cp_size = mpu.get_context_parallel_world_size()
    args = get_args()

    actual_seq_len_lst = list(actual_seq_len * get_ring_degree())
    batched_index = batch_index(actual_seq_len_lst, args.seq_length)
    index = get_index(batched_index, cp_size, cp_rank)

    for key, val in batch.items():
        if key == 'attention_mask':
            continue
        if val is not None:
            seq_dim = 1 if key != 'attention_mask' else 2
            bsz = val.shape[0]
            val = val.view(-1, *val.shape[seq_dim + 1:])
            val = val.index_select(0, index)
            val = val.view(bsz, -1, *val.shape[seq_dim + 1:])

        batch[key] = val

    return batch


def _get_batch_on_this_cp_rank_in_megatron_cp_general(batch):
    cp_rank = mpu.get_context_parallel_rank()
    cp_size = mpu.get_context_parallel_world_size()

    attention_mask = get_attention_mask()
    if attention_mask is not None:
        if len(attention_mask.shape) != 2:
            raise AssertionError("The fusion attention operator currently only support 2D attention mask.")
        seq_dim = 0
        mask_row = attention_mask.chunk(cp_size, dim=seq_dim)[cp_rank].contiguous()
        if get_args().attention_mask_on_cpu:
            mask_list = [m.contiguous().npu(non_blocking=True) for m in mask_row.chunk(cp_size, dim=1)]
        else:
            mask_list = [m.contiguous() for m in mask_row.chunk(cp_size, dim=1)]
        batch['attention_mask'] = mask_list
        set_attention_mask(mask_list)

    for key, val in batch.items():
        if key != 'attention_mask' and val is not None:
            seq_dim = 1
            val = val.chunk(cp_size, dim=seq_dim)[cp_rank].contiguous()
            batch[key] = val

    return batch


def _get_batch_on_this_tp_y_cp_rank_in_megatron_cp(batch):
    cp_rank = mpu.get_context_parallel_rank()
    cp_size = mpu.get_context_parallel_world_size()

    tp_y_cp_size = TensorParallelYUnionCP().get_parallel_group_world_size()
    rearrange_idx_tensor = generate_rearrange_idx_tensor(tp_y_cp_size)

    for key, val in batch.items():
        if key == 'attention_mask' or val is None:
            continue

        seq_dim = 1
        b = val.shape[0]

        # [b, s] -> [b, 2*tp_y_cp_sz, s/(2*tp_y_cp_sz)]
        val = val.view(
            *val.shape[0:seq_dim],
            2 * tp_y_cp_size,
            val.shape[seq_dim] // (2 * tp_y_cp_size),
            *val.shape[(seq_dim + 1):],
        )

        val = val.index_select(seq_dim, index=rearrange_idx_tensor)

        # [b, 2 * tp_y_cp_sz, s / (2 * tp_y_cp_sz)] -> [b, cp, s/cp]
        val = val.view(
            *val.shape[0:seq_dim],
            cp_size,
            val.shape[seq_dim] // cp_size,
            *val.shape[(seq_dim + 1):],
        )
        # [b, 1, s/cp] -> [b, s/cp]
        val = val[:, cp_rank].view(b, -1)
        batch[key] = val

    return batch


def _get_batch_on_this_cp_rank_in_megatron_cp(batch):
    cp_rank = mpu.get_context_parallel_rank()
    cp_size = mpu.get_context_parallel_world_size()
    for key, val in batch.items():
        if key == 'attention_mask':
            continue
        if val is not None:
            seq_dim = 1 if key != 'attention_mask' else 2
            val = val.view(
                *val.shape[0:seq_dim],
                2 * cp_size,
                val.shape[seq_dim] // (2 * cp_size),
                *val.shape[(seq_dim + 1):],
            )
            index = torch.tensor([cp_rank, (2 * cp_size - cp_rank - 1)], device=val.device)
            val = val.index_select(seq_dim, index)
            val = val.view(*val.shape[0:seq_dim], -1, *val.shape[(seq_dim + 2):])
            batch[key] = val

    return batch


def _get_batch_on_this_cp_rank_in_ulysses_cp(batch):
    cp_rank = mpu.get_context_parallel_rank()
    cp_size = mpu.get_context_parallel_world_size()
    for key, val in batch.items():
        if key == 'attention_mask':
            continue
        if val is not None:
            seq_dim = 1 if key != 'attention_mask' else 2
            val = val.chunk(cp_size, dim=seq_dim)[cp_rank].contiguous()
            batch[key] = val

    return batch


def _get_batch_on_this_cp_rank_in_hybrid_cp_general(batch):
    u_size = get_context_parallel_for_hybrid_ulysses_world_size()
    r_size = get_context_parallel_for_hybrid_ring_world_size()

    u_rank = get_context_parallel_for_hybrid_ulysses_rank()
    r_rank = get_context_parallel_for_hybrid_ring_rank()

    attention_mask = get_attention_mask()
    if attention_mask is not None:
        if len(attention_mask.shape) != 2:
            raise AssertionError("The fusion attention operator currently only support 2D attention mask.")
        seq_dim = 0
        mask_row = attention_mask.chunk(r_size, dim=seq_dim)[r_rank].contiguous()
        if get_args().attention_mask_on_cpu:
            mask_list = [m.contiguous().npu(non_blocking=True) for m in mask_row.chunk(r_size, dim=1)]
        else:
            mask_list = [m.contiguous() for m in mask_row.chunk(r_size, dim=1)]
        batch['attention_mask'] = mask_list
        set_attention_mask(mask_list)

    for key, val in batch.items():
        if key != 'attention_mask' and val is not None:
            seq_dim = 1
            val = val.chunk(r_size, dim=seq_dim)[r_rank].contiguous()
            val = val.chunk(u_size, dim=seq_dim)[u_rank].contiguous()
            batch[key] = val

    return batch


def _get_batch_on_this_cp_rank_in_hybrid_cp(batch):
    u_size = get_context_parallel_for_hybrid_ulysses_world_size()
    r_size = get_context_parallel_for_hybrid_ring_world_size()

    u_rank = get_context_parallel_for_hybrid_ulysses_rank()
    r_rank = get_context_parallel_for_hybrid_ring_rank()

    for key, val in batch.items():
        if key == 'attention_mask':
            continue
        if val is not None:
            seq_dim = 1 if key != 'attention_mask' else 2
            val = val.view(
                *val.shape[0:seq_dim],
                2 * r_size,
                val.shape[seq_dim] // (2 * r_size),
                *val.shape[(seq_dim + 1):],
            )
            index = torch.tensor([r_rank, (2 * r_size - r_rank - 1)], device=val.device)
            val = val.index_select(seq_dim, index)
            val = val.view(*val.shape[0:seq_dim], -1, *val.shape[(seq_dim + 2):])
            val = val.chunk(u_size, dim=seq_dim)[u_rank].contiguous()
            batch[key] = val

    return batch


def _get_batch_on_this_cp_rank_in_adaptive_cp(batch):
    args = get_args()
    cp_rank = mpu.get_context_parallel_rank()
    cp_size = mpu.get_context_parallel_world_size()

    attention_mask = get_attention_mask()
    if args.adaptive_cp_manually_set_mask_list:
        remapped_seq_order = list(range(args.seq_length))
        generate_adaptive_cp_grid_mask_by_user(cp_size)
        grid_mask = get_adaptive_cp_grid_mask_by_user()
        scheduling = adaptive_reschedule_task(grid_mask, cp_size)
        generate_adaptive_cp_mask_list_by_user(remapped_seq_order, scheduling, cp_rank, cp_size)
        mask_list = get_adaptive_cp_mask_list_by_user()
    else:
        if attention_mask is None:
            raise AssertionError("Do not use adaptive cp with full mask")
        if len(attention_mask.shape) != 2:
            raise AssertionError("The fusion attention operator currently only support 2D attention mask.")
        from mindspeed.core.context_parallel.utils import AdaptiveCpOps
        adaptive_cp_ops = AdaptiveCpOps()
        remapped_seq_order, scheduling = adaptive_cp_ops.get_adaptive_cp_info(attention_mask, cp_size)
        mask_list = adaptive_cp_ops.get_mask_list(attention_mask, scheduling, remapped_seq_order, cp_rank, cp_size)

    batch['attention_mask'] = mask_list
    set_attention_mask(mask_list)
    set_scheduling_info(torch.distributed.get_rank(), scheduling)
    set_remapped_seq_order(remapped_seq_order)

    for key, val in batch.items():
        if key != 'attention_mask' and val is not None:
            seq_dim = 1
            per = val.shape[seq_dim] // cp_size
            index = torch.tensor(remapped_seq_order[cp_rank * per:(cp_rank + 1) * per], device=val.device,
                                 dtype=torch.int)
            val = val.index_select(seq_dim, index)
            batch[key] = val
    return batch


def _get_batch_on_this_cp_rank_in_hybrid_adaptive_cp(batch):
    args = get_args()
    ulys_size = get_context_parallel_for_hybrid_ulysses_world_size()
    adap_size = get_context_parallel_for_hybrid_ring_world_size()
    ulys_rank = get_context_parallel_for_hybrid_ulysses_rank()
    adap_rank = get_context_parallel_for_hybrid_ring_rank()

    attention_mask = get_attention_mask()
    if args.adaptive_cp_manually_set_mask_list:
        remapped_seq_order = list(range(args.seq_length))
        generate_adaptive_cp_grid_mask_by_user(adap_size)
        grid_mask = get_adaptive_cp_grid_mask_by_user()
        scheduling = adaptive_reschedule_task(grid_mask, adap_size)
        generate_adaptive_cp_mask_list_by_user(remapped_seq_order, scheduling, adap_rank, adap_size)
        mask_list = get_adaptive_cp_mask_list_by_user()
    else:
        if attention_mask is None:
            raise AssertionError("Do not use adaptive cp with full mask")
        if len(attention_mask.shape) != 2:
            raise AssertionError("The fusion attention operator currently only support 2D attention mask.")
        from mindspeed.core.context_parallel.utils import AdaptiveCpOps
        adaptive_cp_ops = AdaptiveCpOps()
        remapped_seq_order, scheduling = adaptive_cp_ops.get_adaptive_cp_info(attention_mask, adap_size)
        mask_list = adaptive_cp_ops.get_mask_list(attention_mask, scheduling, remapped_seq_order, adap_rank, adap_size)

    batch['attention_mask'] = mask_list
    set_scheduling_info(torch.distributed.get_rank(), scheduling)
    set_remapped_seq_order(remapped_seq_order)
    set_attention_mask(mask_list)

    for key, val in batch.items():
        if key != 'attention_mask' and val is not None:
            seq_dim = 1
            per = val.shape[seq_dim] // adap_size // ulys_size
            which_per = adap_rank * ulys_size + ulys_rank
            index = torch.tensor(remapped_seq_order[which_per * per:(which_per + 1) * per], device=val.device)
            val = val.index_select(seq_dim, index)
            batch[key] = val

    return batch


def get_batch_on_this_tp_rank(data_iterator):
    args = get_args()

    if mpu.get_tensor_model_parallel_rank() == 0:
        if data_iterator is not None:
            data = next(data_iterator)
        else:
            data = None

        batch = {
            'tokens': data["tokens"].cuda(non_blocking=True),
            'labels': data["labels"].cuda(non_blocking=True),
            'loss_mask': data["loss_mask"].cuda(non_blocking=True),
            'attention_mask': None if "attention_mask" not in data else data["attention_mask"].cuda(non_blocking=True),
            'position_ids': data["position_ids"].cuda(non_blocking=True)
        }

        if args.pipeline_model_parallel_size == 1:
            _broadcast(batch['tokens'])
            _broadcast(batch['labels'])
            _broadcast(batch['loss_mask'])
            _broadcast(batch['attention_mask'])
            _broadcast(batch['position_ids'])

        elif mpu.is_pipeline_first_stage():
            _broadcast(batch['tokens'])
            _broadcast(batch['attention_mask'])
            _broadcast(batch['position_ids'])

        elif mpu.is_pipeline_last_stage():
            _broadcast(batch['labels'])
            _broadcast(batch['loss_mask'])
            _broadcast(batch['attention_mask'])
            if args.reset_attention_mask:
                _broadcast(batch['position_ids'])

        elif args.reset_attention_mask:
            _broadcast(batch['position_ids'])

        if args.reset_attention_mask:
            actual_seq_len = broadcast_dynamic(data['actual_seq_len'])
            if args.attention_mask_type == 'causal':
                actual_seq_len /= get_ring_degree()
            set_actual_seq_len(actual_seq_len)

    else:
        tokens = torch.empty((args.micro_batch_size, args.seq_length), dtype=torch.int64,
                             device=torch.cuda.current_device())
        labels = torch.empty((args.micro_batch_size, args.seq_length), dtype=torch.int64,
                             device=torch.cuda.current_device())
        loss_mask = torch.empty((args.micro_batch_size, args.seq_length), dtype=torch.float32,
                                device=torch.cuda.current_device())
        if args.create_attention_mask_in_dataloader:
            attention_mask = torch.empty(
                (args.micro_batch_size, 1, args.seq_length, args.seq_length), dtype=torch.bool,
                device=torch.cuda.current_device()
            )
        else:
            attention_mask = None
        position_ids = torch.empty((args.micro_batch_size, args.seq_length), dtype=torch.int64,
                                   device=torch.cuda.current_device())

        if args.pipeline_model_parallel_size == 1:
            _broadcast(tokens)
            _broadcast(labels)
            _broadcast(loss_mask)
            _broadcast(attention_mask)
            _broadcast(position_ids)

        elif mpu.is_pipeline_first_stage():
            labels = None
            loss_mask = None

            _broadcast(tokens)
            _broadcast(attention_mask)
            _broadcast(position_ids)

        elif mpu.is_pipeline_last_stage():
            tokens = None

            _broadcast(labels)
            _broadcast(loss_mask)
            _broadcast(attention_mask)
            if args.reset_attention_mask:
                _broadcast(position_ids)
            else:
                position_ids = None

        elif args.reset_attention_mask:
            _broadcast(position_ids)

        batch = {
            'tokens': tokens,
            'labels': labels,
            'loss_mask': loss_mask,
            'attention_mask': attention_mask,
            'position_ids': position_ids
        }

        if args.reset_attention_mask:
            actual_seq_len = broadcast_dynamic(None)
            if args.attention_mask_type == 'causal':
                actual_seq_len /= get_ring_degree()
            set_actual_seq_len(actual_seq_len)

    return batch

