import os
from typing import List, Optional, Callable
import logging
import types
from functools import wraps

import torch
from megatron.training import get_args
from megatron.core.optimizer.optimizer import ChainedOptimizer, MegatronOptimizer
from megatron.core.optimizer.optimizer_config import OptimizerConfig
from megatron.core.transformer.module import MegatronModule
from megatron.core.optimizer import _get_param_groups_and_buffers, _get_megatron_optimizer_based_on_param_groups
from megatron.core import mpu
from megatron.core.utils import is_te_min_version, log_single_rank

logger = logging.getLogger(__name__)


@torch.no_grad()
def prepare_grads(self) -> bool:
    """Pre-processing gradients before the optimizer step, returns whether inf/nan is found."""
    timers = self.config.timers

    # Copy gradients from model params to main params.
    if timers is not None:
        timers('optimizer-copy-to-main-grad', log_level=1).start(
            barrier=self.config.barrier_with_L1_time
        )
    self._copy_model_grads_to_main_grads()
    if timers is not None:
        timers('optimizer-copy-to-main-grad').stop()

    if self.config.reuse_fp32_param:
        # bf16 -> fp32
        self.fp16_tensor_convert_to_fp32_tensor()

    # Do unscale, check for inf, and update grad scaler only for
    # the case that grad scaler is provided.
    if self.grad_scaler:

        # Unscale and check for inf/nan.
        if timers is not None:
            timers('optimizer-unscale-and-check-inf', log_level=1).start(
                barrier=self.config.barrier_with_L1_time
            )
        found_inf_flag = self._unscale_main_grads_and_check_for_nan()
        if timers is not None:
            timers('optimizer-unscale-and-check-inf').stop()

        # We are done with scaling gradients
        # so we can update the loss scale.
        self.grad_scaler.update(found_inf_flag)

        return found_inf_flag

    return False


@torch.no_grad()
def step_with_ready_grads(self) -> bool:
    """Step the optimizer with ready gradients, return successful."""
    timers = self.config.timers
    # Step the optimizer.
    if timers is not None:
        timers('optimizer-inner-step', log_level=1).start(
            barrier=self.config.barrier_with_L1_time
        )
    self.optimizer.step()
    if timers is not None:
        timers('optimizer-inner-step').stop()

    # Update params from main params.
    if timers is not None:
        timers('optimizer-copy-main-to-model-params', log_level=1).start(
            barrier=self.config.barrier_with_L1_time
        )
    if self.config.reuse_fp32_param:
        # fp32 -> bf16 + res
        self.fp32_tensor_convert_to_fp16_tensor()
    else:
        self._copy_main_params_to_model_params()
    if timers is not None:
        timers('optimizer-copy-main-to-model-params').stop()

    return True


@torch.no_grad()
def mixed_precision_optimizer_step(self):
    # Copy gradients from model params to main params.
    timers = self.config.timers
    timers('optimizer-copy-to-main-grad', log_level=1).start(
        barrier=self.config.barrier_with_L1_time)
    self._copy_model_grads_to_main_grads()
    timers('optimizer-copy-to-main-grad').stop()
    if self.config.reuse_fp32_param:
        # bf16 -> fp32
        self.fp16_tensor_convert_to_fp32_tensor()

    # Do unscale, check for inf, and update grad scaler only for
    # the case that grad scaler is provided.
    if self.grad_scaler:

        # Unscale and check for inf/nan.
        timers('optimizer-unscale-and-check-inf', log_level=1).start(
            barrier=self.config.barrier_with_L1_time)
        found_inf_flag = self._unscale_main_grads_and_check_for_nan()
        timers('optimizer-unscale-and-check-inf').stop()

        # We are done with scaling gradients
        # so we can update the loss scale.
        self.grad_scaler.update(found_inf_flag)

        # If we found inf/nan, skip the update.
        if found_inf_flag:
            return False, None, None

    # Clip the main gradients.
    timers('optimizer-clip-main-grad', log_level=1).start(
        barrier=self.config.barrier_with_L1_time)
    grad_norm = None
    if self.config.clip_grad > 0.0:
        grad_norm = self.clip_grad_norm(self.config.clip_grad)
    timers('optimizer-clip-main-grad').stop()


    # Count the zeros in the grads.
    timers('optimizer-count-zeros', log_level=1).start(
        barrier=self.config.barrier_with_L1_time)
    num_zeros_in_grad = self.count_zeros() if \
        self.config.log_num_zeros_in_grad else None
    timers('optimizer-count-zeros').stop()

    # Step the optimizer.
    timers('optimizer-inner-step', log_level=1).start(
        barrier=self.config.barrier_with_L1_time)
    self.optimizer.step()
    timers('optimizer-inner-step').stop()

    # Update params from main params.
    timers('optimizer-copy-main-to-model-params', log_level=1).start(
        barrier=self.config.barrier_with_L1_time)
    if self.config.reuse_fp32_param:
        # fp32 -> bf16 + res
        self.fp32_tensor_convert_to_fp16_tensor()
    else:
        self._copy_main_params_to_model_params()
    timers('optimizer-copy-main-to-model-params').stop()

    # Successful update.
    return True, grad_norm, num_zeros_in_grad


def optimizer_config_init_wrapper(init_func):
    @wraps(init_func)
    def optimizer_config_init(*args, **kwargs):
        init_func(*args, **kwargs)
        self = args[0]
        args = get_args()
        self.reuse_fp32_param = args.reuse_fp32_param if hasattr(args, "reuse_fp32_param") else False

    return optimizer_config_init


def get_megatron_optimizer_func_wrapper(func):
    @wraps(func)
    def get_megatron_optimizer_func(*args, **kwargs):
        chained_optimizer = func(*args, **kwargs)
        args = get_args()
        if hasattr(chained_optimizer, "chained_optimizers"):
            for optim in chained_optimizer.chained_optimizers:
                optim.optimizer.ema_decay = args.ema_decay
            return chained_optimizer
        if hasattr(chained_optimizer, "optimizer"):
            chained_optimizer.optimizer.ema_decay = args.ema_decay
            return chained_optimizer
        return chained_optimizer

    return get_megatron_optimizer_func


def reuse_fp32_param_init_wrapper(init_func):
    @wraps(init_func)
    def reuse_fp32_param_init(*args, **kwargs):
        init_func(*args, **kwargs)
        self = args[0]
        args = get_args()
        self.reuse_fp32_param = args.reuse_fp32_param if hasattr(args, "reuse_fp32_param") else False
        if self.reuse_fp32_param:
            self.res_float16_groups = []
            self.float16_float32_groups = []
            self.int32_float32_groups = []
            for float16_params_this_group, fp32_from_float16_group in zip(self.float16_groups, self.fp32_from_float16_groups):
                res_float16_params_this_group = []
                float16_float32_params_this_group = []
                int32_float32_params_this_group = []
                for i, (_, fp32_from_fp16_param) in enumerate(zip(float16_params_this_group, fp32_from_float16_group)):
                    res_float16_params_this_group.append(
                        torch.empty((fp32_from_fp16_param.numel() * 1), dtype=torch.bfloat16, device=fp32_from_fp16_param.device))
                    float16_float32_params_this_group.append(
                        torch.empty((fp32_from_fp16_param.numel() * 2), dtype=torch.bfloat16, device=fp32_from_fp16_param.device))
                    int32_float32_params_this_group.append(
                        torch.empty((fp32_from_fp16_param.numel() * 1), dtype=torch.int32, device=fp32_from_fp16_param.device))
                    init_and_reuse_storage_of_tensors(fp32_from_float16_group[i],  
                                float16_float32_params_this_group[-1],
                                res_float16_params_this_group[-1],
                                float16_params_this_group[i],
                                int32_float32_params_this_group[-1]
                        )
                self.res_float16_groups.append(res_float16_params_this_group)
                self.float16_float32_groups.append(float16_float32_params_this_group)
                self.int32_float32_groups.append(int32_float32_params_this_group)
            self._copy_model_params_to_main_params = _copy_model_params_to_main_params
            if args.npu_deterministic:
                self.fp16_tensor_convert_to_fp32_tensor = types.MethodType(fp16_tensor_convert_to_fp32_tensor_deterministic, self)
                self.fp32_tensor_convert_to_fp16_tensor = types.MethodType(fp32_tensor_convert_to_fp16_tensor_deterministic, self)    
            else:
                self.fp16_tensor_convert_to_fp32_tensor = types.MethodType(fp16_tensor_convert_to_fp32_tensor, self)
                self.fp32_tensor_convert_to_fp16_tensor = types.MethodType(fp32_tensor_convert_to_fp16_tensor, self)    
    return reuse_fp32_param_init


def _copy_model_params_to_main_params():
    pass


def init_and_reuse_storage_of_tensors(
        fp32_tensor,
        bf16_fp32_tensor,
        res_tensor,
        bf16_tensor,
        int32_tensor
):
    """
    init a list of tensor with length of 2*fp32_tensor.numel() in bf16 to share the same storage.
    Args:
        fp32_tensor: original fp32 tensor.
        bf16_fp32_tensor: a bf16 tensor share the same storage with original list of fp32 tensors.
        res_tensor: a bf16 tensor that store the residual value of fp32 to bf16, shares a half of the
        storage with bf16_fp32_tensor.
        bf16_tensor: a bf16 tensor that store the value from fp32, shares another half of the
        storage with bf16_fp32_tensor.
        int32_tensors: a list of int32 tensors share the same storages with original list of fp32 tensors.
    """
    from mindspeed.op_builder import AlgorithmOpBuilder
    reuse_data_ptr = AlgorithmOpBuilder().load().reuse_data_ptr
    reuse_data_ptr(bf16_fp32_tensor, fp32_tensor, 0)
    reuse_data_ptr(int32_tensor, fp32_tensor, 0)
    fp32_tensors_to_bf16_tensors([int32_tensor], [bf16_fp32_tensor])
    reuse_data_ptr(res_tensor, bf16_fp32_tensor, 0)
    reuse_data_ptr(bf16_tensor, bf16_fp32_tensor, res_tensor.numel())


def fp16_tensor_convert_to_fp32_tensor(self):
    for int32_float32_group, float16_param_group in zip(
            self.int32_float32_groups, self.float16_float32_groups):
        bf16_tensors_to_fp32_tensors(int32_float32_group, float16_param_group)


def fp32_tensor_convert_to_fp16_tensor(self):
    for int32_float32_param_group, float16_param_group in zip(
        self.int32_float32_groups, self.float16_float32_groups):
        fp32_tensors_to_bf16_tensors(int32_float32_param_group, float16_param_group)


def fp32_tensors_to_bf16_tensors(int32_tensors, bf16_fp32_tensors):
    """
    fp32(0p0p0p0p) -> bf16(pppp) + res(0000)
    rearrange the storage of bf16_fp32_tensors so that recover the fp32_tensors.
    Args:
        int32_tensors: a list of int32 tensors share the same storages with original list of fp32 tensors.
        bf16_fp32_tensors: a list of bf16 tensors share the same storages with original list of fp32 tensors.
    Returns:
        None
    """
    for int32_tensor, bf16_fp32_tensor in zip(int32_tensors, bf16_fp32_tensors):
        if bf16_fp32_tensor.numel() == 0:
            return  
        int32_tensor.add_(32768)
        bf16_fp32_tensor.copy_(bf16_fp32_tensor.view(-1, 2).transpose(1, 0).reshape(-1).contiguous())


def bf16_tensors_to_fp32_tensors(int32_tensors, bf16_fp32_tensors):
    """
    res(0000) + bf16(pppp) -> fp32(0p0p0p0p)
    rearrange the storage of bf16_fp32_tensors so that recover the fp32_tensors.
    Args:
        int32_tensors: a list of int32 tensors share the same storages with original list of fp32 tensors.
        bf16_fp32_tensors: a list of bf16 tensors share the same storages with original list of fp32 tensors.
    Returns:
        None
    """
    for int32_tensor, bf16_fp32_tensor in zip(int32_tensors, bf16_fp32_tensors):
        if bf16_fp32_tensor.numel() == 0:
            return
        bf16_fp32_tensor.copy_(bf16_fp32_tensor.view(2, -1).transpose(1, 0).reshape(-1).contiguous())
        int32_tensor.sub_(32768)


def fp16_tensor_convert_to_fp32_tensor_deterministic(self):
    for int32_float32_group, float16_param_group, fp32_from_float16_group in zip(
        self.int32_float32_groups, self.float16_float32_groups, self.fp32_from_float16_groups):
        bf16_tensors_to_fp32_tensors_deterministic(int32_float32_group, float16_param_group, fp32_from_float16_group, self.optimizer)


def fp32_tensor_convert_to_fp16_tensor_deterministic(self):
    for int32_float32_param_group, float16_param_group, fp32_from_float16_group in zip(
        self.int32_float32_groups, self.float16_float32_groups, self.fp32_from_float16_groups):
        fp32_tensors_to_bf16_tensors_deterministic(int32_float32_param_group, float16_param_group, fp32_from_float16_group, self.optimizer)


def fp32_tensors_to_bf16_tensors_deterministic(int32_tensors, bf16_fp32_tensors, fp32_tensors, optimizer):
    for int32_tensor, bf16_fp32_tensor, fp32_tensor in zip(int32_tensors, bf16_fp32_tensors, fp32_tensors):
        if bf16_fp32_tensor.numel() == 0:
            return  
        odd_even_tensor = ((int32_tensor & 131071) == 32768).int()
        int32_tensor.add_(32768)
        optimizer_exp_avg_save_sign(optimizer, fp32_tensor, int32_tensor, odd_even_tensor)
        bf16_fp32_tensor.copy_(bf16_fp32_tensor.view(-1, 2).transpose(1, 0).reshape(-1).contiguous())


def bf16_tensors_to_fp32_tensors_deterministic(int32_tensors, bf16_fp32_tensors, fp32_tensors, optimizer):
    for int32_tensor, bf16_fp32_tensor, fp32_tensor in zip(int32_tensors, bf16_fp32_tensors, fp32_tensors):
        if bf16_fp32_tensor.numel() == 0:
            return
        bf16_fp32_tensor.copy_(bf16_fp32_tensor.view(2, -1).transpose(1, 0).reshape(-1).contiguous())
        optimizer_exp_avg_load_sign(optimizer, fp32_tensor, int32_tensor)
        int32_tensor.sub_(32768)


def optimizer_exp_avg_save_sign(optimizer, fp32_param, int32_tensor, odd_even_tensor):
    if "exp_avg_sq" in optimizer.state[fp32_param]:
        int32_tensor.sub_(odd_even_tensor)
        sign_tensor = torch.sign(odd_even_tensor - 0.5).reshape(optimizer.state[fp32_param]["exp_avg_sq"].shape)
        optimizer.state[fp32_param]["exp_avg_sq"].mul_(sign_tensor)


def optimizer_exp_avg_load_sign(optimizer, fp32_param, int32_tensor):
    if "exp_avg_sq" in optimizer.state[fp32_param]:
        odd_even_tensor = (torch.sign(optimizer.state[fp32_param]["exp_avg_sq"]) > 0).reshape(-1)
        optimizer.state[fp32_param]["exp_avg_sq"].abs_()
        int32_tensor.add_(odd_even_tensor)


def get_megatron_optimizer_disable_gloo(
    config: OptimizerConfig,
    model_chunks: List[MegatronModule],
    no_weight_decay_cond: Optional[Callable] = None,
    scale_lr_cond: Optional[Callable] = None,
    lr_mult: float = 1.0,
    use_gloo_process_groups: bool = True,
) -> MegatronOptimizer:
    """Retrieve the Megatron optimizer for model chunks.

    We use separate optimizers for expert parameters and non-expert parameters.

    Args:
        config (OptimizerConfig): optimizer configuration object.
        model_chunks (List[MegatronModule]): model chunks to get optimizer for.
        no_weight_decay_cond (func, optional): function to determine whether a parameter
            should not perform weight decay. Defaults to None.
        scale_lr_cond (func, optional): function to determine whether a parameter
            should have a scaled learning rate. Defaults to None.
        lr_mult (float, optional): learning rate multiplier for parameters that
            satisfy scale_lr_cond. Defaults to 1.0.

    Returns:
        Instance of MegatronOptimizer.
    """
    args = get_args()
    log_single_rank(logger, logging.INFO, f'Setting up optimizer with config {config}')

    # Separate out first model chunk if overlapping param AG with optimizer step.
    if config.overlap_param_gather_with_optimizer_step:
        all_dense_model_chunks = [[model_chunks[0]], model_chunks[1:]]
        overlap_param_gather_with_optimizer_step_flags = [True, False]
    else:
        all_dense_model_chunks = [model_chunks]
        overlap_param_gather_with_optimizer_step_flags = [False]
    model_parallel_rank = torch.distributed.get_rank(mpu.get_model_parallel_group())

    if torch.distributed.get_world_size(
        mpu.get_data_parallel_group(with_context_parallel=True, partial_data_parallel=False)
    ) > torch.distributed.get_world_size(
        mpu.get_data_parallel_group(with_context_parallel=True, partial_data_parallel=True)
    ):
        distributed_optimizer_instance_id = torch.distributed.get_rank(
            mpu.get_inter_partial_data_parallel_group()
        )
    else:
        distributed_optimizer_instance_id = 0

    optimizers = []
    model_chunk_offset = 0
    for dense_model_chunks, overlap_param_gather_with_optimizer_step in zip(
        all_dense_model_chunks, overlap_param_gather_with_optimizer_step_flags
    ):
        param_groups, buffers = _get_param_groups_and_buffers(
            dense_model_chunks,
            model_chunk_offset=model_chunk_offset,
            config=config,
            no_weight_decay_cond=no_weight_decay_cond,
            scale_lr_cond=scale_lr_cond,
            lr_mult=lr_mult,
            filter_fn=lambda g: not g['is_expert_parallel'],
            buffer_name='buffers',
        )
        for model_chunk in dense_model_chunks:
            model_chunk.overlap_param_gather_with_optimizer_step = (
                overlap_param_gather_with_optimizer_step
            )

        # Pass Gloo process groups into optimizer only if needed.
        if not args.disable_gloo_group:
            data_parallel_group_gloo = mpu.get_data_parallel_group_gloo(
                with_context_parallel=True, partial_data_parallel=True
            )
        else:
            data_parallel_group_gloo = None
        optimizers.append(
            _get_megatron_optimizer_based_on_param_groups(
                config,
                model_chunks=dense_model_chunks,
                param_groups=param_groups,
                per_model_buffers=buffers,
                model_parallel_group=mpu.get_model_parallel_group(),
                data_parallel_group=mpu.get_data_parallel_group(
                    with_context_parallel=True, partial_data_parallel=True
                ),
                data_parallel_group_gloo=data_parallel_group_gloo,
                data_parallel_group_idx=model_parallel_rank,
                distributed_optimizer_instance_id=distributed_optimizer_instance_id,
            )
        )
        model_chunk_offset += 1

    moe_param_groups, moe_buffers = _get_param_groups_and_buffers(
        model_chunks,
        model_chunk_offset=0,
        config=config,
        no_weight_decay_cond=no_weight_decay_cond,
        scale_lr_cond=scale_lr_cond,
        lr_mult=lr_mult,
        filter_fn=lambda g: g['is_expert_parallel'],
        buffer_name='expert_parallel_buffers',
    )
    if len(moe_param_groups) > 0:
        model_parallel_rank = torch.distributed.get_rank(
            mpu.get_expert_tensor_model_pipeline_parallel_group()
        )
        # Pass Gloo process groups into optimizer only if needed.
        if not args.disable_gloo_group:
            data_parallel_group_gloo = mpu.get_expert_data_parallel_group_gloo()
        else:
            data_parallel_group_gloo = None
        optimizers.append(
            _get_megatron_optimizer_based_on_param_groups(
                config,
                model_chunks=model_chunks,
                param_groups=moe_param_groups,
                per_model_buffers=moe_buffers,
                model_parallel_group=mpu.get_expert_tensor_model_pipeline_parallel_group(),
                data_parallel_group=mpu.get_expert_data_parallel_group(),
                data_parallel_group_gloo=data_parallel_group_gloo,
                data_parallel_group_idx=model_parallel_rank,
            )
        )

    if len(optimizers) == 1:
        return optimizers[0]

    return ChainedOptimizer(optimizers)