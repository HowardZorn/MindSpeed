# Copyright (c) 2025, Huawei Technologies.
# All rights reserved.
import torch
import torch.nn.functional as F
from mindspeed.core.transformer.moe.moe_feature import parallel_state
from mindspeed.core.fusions.fused_bias_swiglu import fused_swiglu
from mindspeed.core.transformer.moe.moe_feature import grouped_gemm_util as gg
from mindspeed.model.transformer import should_recompute_activation
from mindspeed.core.tensor_parallel.random import CheckpointWithoutOutput


class TpExtendEpGmmExpertsImpl:
    """
    support gemm_fusion、 activation recompute、TpExtendEp
    """

    def __init__(self, num_local_experts, config=None):
        """adjust the logic for generate expert weight to avoid splitting by tp_size

        Args:
            num_local_experts: experts in device
            config: TransformerConfig
        """
        self.num_local_experts = num_local_experts
        self.config = config

        self.weight1 = None
        self.weight2 = None
        self.activation_checkpoint_manager = None

        tp_size = parallel_state._MPU_EXPERT_TENSOR_PARALLEL_WORLD_SIZE
        # set tp size to 1 before GMM init to aviod weight sharding
        parallel_state._MPU_EXPERT_TENSOR_PARALLEL_WORLD_SIZE = 1
        super().__init__(num_local_experts, config)
        parallel_state._MPU_EXPERT_TENSOR_PARALLEL_WORLD_SIZE = tp_size
        if self.config.gated_linear_unit:
            assert (self.config.activation_func == F.silu
                    ), 'Activation function must be silu when using fused_swiglu.'
            self.activation_func = fused_swiglu
        self.layer_number = None
        self.set_recompute_activation_func = False
        self.activation_checkpoint_manager = CheckpointWithoutOutput()

    def forward(self, permuted_local_hidden_states, tokens_per_expert, permuted_probs):
        is_recompute_activation = should_recompute_activation(
            self.layer_number) and not self.config.moe_alltoall_overlap_comm and not \
            self.config.moe_allgather_overlap_comm

        gemm_fusion = self.config.gemm_gradient_accumulation_fusion

        if permuted_local_hidden_states.nelement() != 0:
            w1 = self.weight1.view(self.num_local_experts, self.config.hidden_size, -1)
            w2 = self.weight2.view(self.num_local_experts, -1, self.config.hidden_size)

            fc1_output = gg.ops.gmm(
                permuted_local_hidden_states, w1, tokens_per_expert, trans_b=False, gemm_fusion=gemm_fusion,
                original_weight=self.weight1
            )
            if not is_recompute_activation:
                intermediate_parallel = self.activation_func(fc1_output)
            else:
                intermediate_parallel = self.activation_checkpoint_manager.checkpoint(self.activation_func,
                                                                                      False,
                                                                                      fc1_output)
            fc2_output = gg.ops.gmm(intermediate_parallel, w2, tokens_per_expert, trans_b=False,
                                    gemm_fusion=gemm_fusion, original_weight=self.weight2)
        else:
            assert torch.count_nonzero(tokens_per_expert) == 0
            w1 = self.weight1.view(self.config.hidden_size, -1)
            w2 = self.weight2.view(-1, self.config.hidden_size)
            h = torch.matmul(permuted_local_hidden_states, w1)
            if not is_recompute_activation:
                intermediate_parallel = self.activation_func(h)
            else:
                intermediate_parallel = self.activation_checkpoint_manager.checkpoint(self.activation_func,
                                                                                      False,
                                                                                      h)
            h = torch.matmul(intermediate_parallel, w2)
            fc2_output = h

        if is_recompute_activation:
            # discard the output of the activation function,
            # which will be restored by recomputation during backward.
            self.activation_checkpoint_manager.discard_output()

            # when backward to output of dense_4h_to_h,
            # recompute and restore the output of activation function.
            if fc2_output.requires_grad:
                fc2_output.register_hook(self.activation_checkpoint_manager.recompute)

        return fc2_output, None
