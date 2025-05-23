# Copyright (c) 2023, NVIDIA CORPORATION. All rights reserved.
# Copyright (c) 2025, Huawei Technologies Co., Ltd. All rights reserved.
import torch
import torch_npu


class RMSNorm(torch.nn.Module):

    def __init__(self,
                 dim: int,
                 eps: float = 1e-6,
                 sequence_parallel: bool = False,
                 config=None):
        """RMS Normaliation module

        Args:
            dim (int): The width of input, i.e. hidden size
            eps (float): epsilon to use for the norm, default to 1e-6
            sequence_parallel (bool): Set to true if sequence parallelism is being used,
              this marks the weights as needing to be allreduced.
        """
        super().__init__()
        self.eps = eps
        self.weight = torch.nn.Parameter(torch.ones(dim))
        self.config = config

        setattr(self.weight, 'sequence_parallel', sequence_parallel)

    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)

    def unfused_rmsnorm(self, x):
        output = self._norm(x.float()).type_as(x)
        return output * self.weight

    def fused_rmsnorm(self, x):
        return torch_npu.npu_rms_norm(x, self.weight, epsilon=self.eps)[0]

    def forward(self, x):
        if self.config.use_fused_rmsnorm:
            return self.fused_rmsnorm(x)
        return self.unfused_rmsnorm(x)
