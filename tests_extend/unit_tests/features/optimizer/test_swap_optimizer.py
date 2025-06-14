# Copyright (c) 2025, Huawei Technologies Co., Ltd. All rights reserved.
# Copyright (c) 2023, NVIDIA CORPORATION. All rights reserved.
from functools import partial
from time import sleep
import os
import copy
import itertools
from unittest import mock

import pytest
import torch
import torch_npu
from mindspeed import megatron_adaptor

from megatron.training.arguments import parse_args
from megatron.training.global_vars import set_args
from megatron.core.models.gpt import GPTModel
from megatron.core.models.gpt.gpt_layer_specs import get_gpt_layer_local_spec
from megatron.core.optimizer import OptimizerConfig, get_megatron_optimizer
from megatron.core.timers import DummyTimer
from megatron.core.tensor_parallel import model_parallel_cuda_manual_seed
from megatron.core.transformer import TransformerConfig
from megatron.training.training import get_model
from megatron.training.utils import unwrap_model

from tests_extend.unit_tests.common import DistributedTest
from tests_extend.commons import set_random_seed, initialize_model_parallel


def initialize_gpt_model(pre_process=True, post_process=True, seed=0, **config_kwargs):
    torch.manual_seed(seed)
    model_parallel_cuda_manual_seed(seed)

    default_config_kwargs = dict(num_layers=8, hidden_size=512, num_attention_heads=32, use_cpu_initialization=True)
    default_config_kwargs.update(**config_kwargs)
    transformer_config = TransformerConfig(**default_config_kwargs)
    model = GPTModel(config=transformer_config, transformer_layer_spec=get_gpt_layer_local_spec(), vocab_size=1024,
                     max_sequence_length=64, pre_process=pre_process, post_process=post_process)

    model.bfloat16()
    with torch.no_grad():
        for p in model.parameters():
            p.random_()
    return model


def init_mock_args(args, use_distributed_optimizer=False, swap_optimizer=False):
    args.data_parallel_random_init = False
    args.virtual_pipeline_model_parallel_size = None
    args.bf16 = True
    args.accumulate_allreduce_grads_in_fp32 = True
    args.use_distributed_optimizer = use_distributed_optimizer
    args.ddp_bucket_size = None
    args.swap_optimizer = swap_optimizer
    return args


def setup_model_and_optimizer(seed, use_distributed_optimizer=False):
    model = get_model(partial(initialize_gpt_model, seed=seed, bf16=True))
    set_random_seed(seed)
    config = OptimizerConfig(lr=1e-4, bf16=True, params_dtype=torch.bfloat16,
                             use_distributed_optimizer=use_distributed_optimizer)
    config.timers = Timers()
    optimizer = get_megatron_optimizer(config, model)

    for group in optimizer.optimizer.param_groups:
        for p in group['params']:
            if len(optimizer.optimizer.state[p]) == 0:
                optimizer.optimizer.state[p]['exp_avg'] = torch.rand_like(p.data)
                optimizer.optimizer.state[p]['exp_avg_sq'] = torch.rand_like(p.data)
    optimizer.reload_model_params()
    return unwrap_model(model), optimizer


class Timers:
    def __init__(self, *args, **kwargs):
        self._dummy_timer = DummyTimer()

    def __call__(self, *args, **kwargs):
        return self._dummy_timer


class TestDistributedOptimizer(DistributedTest):
    world_size = 8

    @pytest.mark.parametrize("is_deterministic", [False])
    @pytest.mark.parametrize("overlap_grad_reduce", [True, False])
    @pytest.mark.parametrize("overlap_param_gather", [True, False])
    @pytest.mark.parametrize("tp_pp", [(4, 1), (2, 2), (8, 1)])
    def test_swap_optimizer(self, tp_pp, is_deterministic, overlap_grad_reduce, overlap_param_gather):
        args = parse_args(None, True)
        args.npu_deterministic = is_deterministic
        args.overlap_grad_reduce = overlap_grad_reduce
        args.overlap_param_gather = overlap_param_gather
        set_args(args)

        # truth
        ret = init_mock_args(args, use_distributed_optimizer=True)
        initialize_model_parallel(tensor_model_parallel_size=tp_pp[0], pipeline_model_parallel_size=tp_pp[1])
        _, optimizer = setup_model_and_optimizer(seed=5, use_distributed_optimizer=True)
        for _ in range(10):
            for float16_group in optimizer.model_float16_groups:
                for p in float16_group:
                    p.grad = torch.randn_like(p.data, dtype=p.data.dtype)
            optimizer.step()
            if overlap_param_gather:
                for model_chunk in optimizer.model_chunks:
                    model_chunk.start_param_sync(force_sync=True)
                torch.cuda.synchronize()
        truth_params = copy.deepcopy(list(itertools.chain(*optimizer.model_float16_groups)))

        # swap_optimizer
        ret = init_mock_args(args, use_distributed_optimizer=True, swap_optimizer=True)
        initialize_model_parallel(tensor_model_parallel_size=tp_pp[0], pipeline_model_parallel_size=tp_pp[1])
        _, optimizer = setup_model_and_optimizer(seed=5, use_distributed_optimizer=True)
        for _ in range(10):
            for float16_group in optimizer.model_float16_groups:
                for p in float16_group:
                    p.grad = torch.randn_like(p.data, dtype=p.data.dtype)
            optimizer.step()
            if overlap_param_gather:
                for model_chunk in optimizer.model_chunks:
                    model_chunk.start_param_sync(force_sync=True)
                torch.cuda.synchronize()
        swap_optimizer_params = copy.deepcopy(list(itertools.chain(*optimizer.model_float16_groups)))

        for p, swap_optimizer_p in zip(truth_params, swap_optimizer_params):
            if is_deterministic:
                assert torch.allclose(p.data, swap_optimizer_p.data, rtol=0, atol=0)
            else:
                assert torch.allclose(p.data, swap_optimizer_p.data, rtol=0.005, atol=0.005)
