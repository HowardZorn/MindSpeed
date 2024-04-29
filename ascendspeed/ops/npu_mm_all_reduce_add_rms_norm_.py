import torch
import torch_npu
from torch.library import impl
from ascendspeed.op_builder.npu_inplace_mm_all_reduce_add_rms_norm_builder import InplaceMatmulAllReduceAddRmsNormOpBuilder
from ascendspeed.op_builder.builder import AS_LIBRARY

ascendspeed_ops = InplaceMatmulAllReduceAddRmsNormOpBuilder().load()


@impl(AS_LIBRARY, "npu_mm_all_reduce_add_rms_norm_", "PrivateUse1")
def npu_mm_all_reduce_add_rms_norm_single_(x1,
                                           x2,
                                           residual,
                                           gamma,
                                           hcom,
                                           reduce_op='sum',
                                           epsilon=1e-06,
                                           bias=None,
                                           antiquant_scale=None,
                                           antiquant_offset=None,
                                           dequant_scale=None,
                                           antiquant_group_size=0,
                                           comm_turn=0):
    if x1 is None:
        raise AssertionError('x1 must not be None.')
    if x2 is None:
        raise AssertionError('x2 must not be None.')
    if residual is None:
        raise AssertionError('residual must not be None.')
    if gamma is None:
        raise AssertionError('gamma must not be None.')
    y, normOut = ascendspeed_ops.npu_mm_all_reduce_add_rms_norm_(x1,
                                                                 x2,
                                                                 residual,
                                                                 gamma,
                                                                 hcom,
                                                                 reduce_op,
                                                                 epsilon,
                                                                 bias,
                                                                 antiquant_scale,
                                                                 antiquant_offset,
                                                                 dequant_scale,
                                                                 antiquant_group_size,
                                                                 comm_turn)
    return (y.view(residual.shape), normOut.view(residual.shape))


def npu_mm_all_reduce_add_rms_norm_(*args, **kwargs):
    return torch.ops.ascendspeed.npu_mm_all_reduce_add_rms_norm_(*args, **kwargs)
