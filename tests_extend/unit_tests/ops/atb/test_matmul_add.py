# Copyright (c) 2024, Huawei Technologies Co., Ltd.  All rights reserved.
import pytest
import torch
import torch_npu
from mindspeed.ops.npu_matmul_add import npu_matmul_add_fp32


class TestMatmulAdd():
    def test_matmul_add(self):
        for _ in range(10):
            x = torch.rand((4096, 8192), dtype=torch.float16).npu()
            weight = torch.rand((4096, 8192), dtype=torch.float16).npu()
            c = torch.rand((8192, 8192), dtype=torch.float32).npu()

            product = torch.mm(x.T, weight)
            result = product + c

            x = x.clone().detach()
            weight = weight.clone().detach()
            c = c.clone().detach()
            npu_matmul_add_fp32(weight, x, c)

            assert torch.allclose(result, c, rtol=0.005, atol=0.005)
