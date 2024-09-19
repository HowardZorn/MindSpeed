# Copyright (c) 2024, Huawei Technologies.
# All rights reserved.
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

from typing import List, Optional
import torch
import torchair
from torch.library import Library, impl
from mindspeed.op_builder.builder import MindSpeedOpBuilder, AS_LIBRARY
torch_npu_api_version = None
try:
    from torchair import ge
    from torchair import register_fx_node_ge_converter
    from torchair.ge import Tensor, TensorSpec, DataType
except ImportError:
    ge, Tensor, TensorSpec, DataType = None, None, None, None
    from torchair.ge_concrete_graph.fx2ge_converter import register_fx_node_ge_converter
    torch_npu_api_version = 1
else:
    torch_npu_api_version = 2


class AllToAllAllGatherBatchMatMulOpBuilder(MindSpeedOpBuilder):
    OP_NAME = "npu_alltoall_allgather_bmm"
    OP_PROTO = "npu_alltoall_allgather_bmm(Tensor x, Tensor weight, *, Tensor? bias=None, \
        str group_ep, int group_ep_worldsize, \
        str group_tp, int group_tp_worldsize, \
        int shard_type=0, int act_type=0, \
        bool need_allgather_out=False, \
        bool need_activation_feature=False) -> (Tensor, Tensor, Tensor)"

    def __init__(self):
        super(AllToAllAllGatherBatchMatMulOpBuilder, self).__init__(self.OP_NAME)
        self.register_op_proto(self.OP_PROTO)
        self.register_op_ir()

    def sources(self):
        return ['ops/csrc/cann/npu_all_to_all_all_gather_bmm.cpp']

    def include_paths(self):
        paths = super().include_paths()
        paths += ['ops/csrc/cann/inc']
        return paths

    def cxx_args(self):
        args = super().cxx_args()
        args += [
            '-Wno-sign-compare',
            '-Wno-deprecated-declarations',
            '-Wno-return-type',
            "-D__FILENAME__='\"$$(notdir $$(abspath $$<))\"'"
        ]
        return args
    
    def register_op_ir(self):
        @impl(AS_LIBRARY, "npu_alltoall_allgather_bmm", "Meta")
        def npu_alltoall_allgather_bmm_forward(x, weight, *, bias=None,
                                               group_ep, group_ep_worldsize, group_tp, group_tp_worldsize,
                                               shard_type=0, act_type=0,
                                               need_allgather_out=False, need_activation_feature=False):
            batch = weight.size(0)
            m = x.size(1) * group_ep_worldsize
            if shard_type == 1:
                m *= group_tp_worldsize
            n = weight.size(2)
            k = weight.size(1)
            empty_tensor = x.new_empty((0))
            return (x.new_empty((batch, m, n)),
                    x.new_empty((batch, m, k)) if need_allgather_out else empty_tensor,
                    x.new_empty((batch, m, n)) if need_activation_feature else empty_tensor)
        
        @register_fx_node_ge_converter(torch.ops.mindspeed.npu_alltoall_allgather_bmm.default)
        def convert_npu_alltoall_allgather_bmm(
            x: Tensor,
            weight: Tensor,
            *,
            bias: Optional[Tensor] = None,
            group_ep: str,
            group_ep_worldsize: int,
            group_tp: str,
            group_tp_worldsize: int,
            shard_type: Optional[int] = 0,
            act_type: Optional[int] = 0,
            need_allgather_out: Optional[bool] = False,
            need_activation_feature: Optional[bool] = False,
            meta_outputs: List[TensorSpec] = None):
            '''"npu_alltoall_allgather_bmm(Tensor x, Tensor weight, str group_ep, str group_tp,
                int ep_world_size, int tp_world_size, *, Tensor? bias=None, int x_shard_type=0, int act_type=0,
                bool need_allgather_out=False, bool need_activation_feature=False) -> (Tensor, Tensor, Tensor)"'''
            if torch_npu_api_version != 2:
                raise ValueError(f"torch_npu_api_version {torch_npu_api_version} unsupport")
            return AllToAllAllGatherBatchMatmul(x,
                                                weight,
                                                bias=bias,
                                                group_ep=group_ep,
                                                group_ep_worldsize=group_ep_worldsize,
                                                group_tp=group_tp,
                                                group_tp_worldsize=group_tp_worldsize,
                                                shard_type=shard_type,
                                                act_type=act_type,
                                                need_allgather_out=need_allgather_out,
                                                need_activation_feature=need_activation_feature)


def AllToAllAllGatherBatchMatmul(
    x: Tensor,
    weight: Tensor,
    *,
    bias: Optional[Tensor] = None,
    group_ep: str,
    group_ep_worldsize: int,
    group_tp: str,
    group_tp_worldsize: int,
    shard_type: Optional[int] = 0,
    act_type: Optional[int] = 0,
    need_allgather_out: Optional[bool] = False,
    need_activation_feature: Optional[bool] = False):
    """REG_OP(AlltoAllAllGatherBatchMatMul)\n
    .INPUT(x, TensorType({DT_FLOAT16, DT_BF16}))\n
    .INPUT(weight, TensorType({DT_FLOAT16, DT_BF16}))\n
    .OPTIONAL_INPUT(bias, TensorType({DT_FLOAT16, DT_BF16, DT_FLOAT32}))\n
    .OUTPUT(y1, TensorType({DT_FLOAT16, DT_BF16}))\n
    .OUTPUT(y2, TensorType({DT_FLOAT16, DT_BF16}))\n
    .OUTPUT(y3, TensorType({DT_FLOAT16, DT_BF16}))\n
    .REQUIRED_ATTR(group_ep, String)\n
    .REQUIRED_ATTR(group_tp, String)\n
    .REQUIRED_ATTR(ep_world_size, int)\n
    .REQUIRED_ATTR(tp_world_size, int)\n
    .ATTR(x_shard_type, Int, 1)\n
    .ATTR(act_type, Int, 0)\n
    .ATTR(need_allgather_out, Bool, False)\n
    .ATTR(need_activation_feature, Bool, False)\n
    .OP_END_FACTORY_REG(AlltoAllAllGatherBatchMatMul)
    use to construct Opdesc
    """
    transpose_weight = False
    return torchair.ge.custom_op(
        "AlltoAllAllGatherBatchMatMul",
        inputs={
            "x": x,
            "weight": weight,
            "bias": bias
        },
        attrs={
            "group_ep": ge.attr.Str(group_ep),
            "group_tp": ge.attr.Str(group_tp),
            "ep_world_size": ge.attr.Int(group_ep_worldsize),
            "tp_world_size": ge.attr.Int(group_tp_worldsize),
            "x_shard_type": ge.attr.Int(shard_type),
            "act_type": ge.attr.Int(act_type),
            "transpose_weight": ge.attr.Bool(transpose_weight),
            "output_y2_flag": ge.attr.Bool(need_allgather_out),
            "output_y3_flag": ge.attr.Bool(need_activation_feature)
        },
        outputs=["y1", "y2", "y3"]
    )