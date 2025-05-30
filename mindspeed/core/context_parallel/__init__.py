from megatron.core import mpu
from megatron.core.parallel_state import get_context_parallel_global_ranks
from megatron.training import get_args
from megatron.core.parallel_state import get_nccl_options
from megatron.core.transformer.enums import AttnMaskType
from megatron.core.fusions.fused_softmax import FusedScaleMaskSoftmax
from megatron.core.utils import divide
from megatron.core.transformer.dot_product_attention import DotProductAttention