# Copyright 2024 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
import abc

import torch
import torch.distributed
from mindspeed.core.simple_parallel_cfg import SimpleParallelCfg


class MindspeedParallelGroup:
    def __init__(
        self,
        parallel_cfg: SimpleParallelCfg = None,
        pg_name: str = None,
        overlap_gp_name: str = None,
        nccl_comm_cfgs=None,
    ):
        """Parallel group interface, any type of parallelism class can implement this class.

        :param parallel_cfg: Parallel configuration.
        :param pg_name: parallel process group name.
        :param overlap_gp_name: overlap process name, for the send/rcv parallel.
        :param nccl_comm_cfgs:
        """
        self._pg_name = pg_name
        self._overlap_pg_name = overlap_gp_name
        self._group, self._global_ranks, self._overlap_group = self.init_group(
            parallel_cfg, pg_name, overlap_gp_name, nccl_comm_cfgs
        )

    @staticmethod
    @abc.abstractmethod
    def init_group(
        parallel_cfg: SimpleParallelCfg,
        pg_name: str,
        overlap_gp_name: str = None,
        nccl_comm_cfgs=None,
    ):
        raise NotImplementedError

    @property
    def group(self):
        return self._group

    @property
    def overlap_group(self):
        return self._overlap_group

    @property
    def global_ranks(self):
        return self._global_ranks

    def get_parallel_rank(self):
        if torch.distributed.is_available() and torch.distributed.is_initialized():
            return torch.distributed.get_rank(group=self.group)
        else:
            raise AssertionError("The distribution is not available or not initialized.") 

    def get_parallel_group_world_size(self):
        if torch.distributed.is_available() and torch.distributed.is_initialized():
            return torch.distributed.get_world_size(group=self.group)
        else:
            return 0
