#!/bin/bash

export CUDA_DEVICE_MAX_CONNECTIONS=1
source "tests_extend/system_tests/env_npu.sh"

NPUS_PER_NODE=8
MASTER_ADDR=localhost
MASTER_PORT=6001
NNODES=1
NODE_RANK=0
WORLD_SIZE=$(($NPUS_PER_NODE*$NNODES))

CKPT_DIR=./ckpt_llama
DATA_PATH="/home/dataset/llama2/alpaca_text_document"
TOKENIZER_MODEL="/home/dataset/model/llama-2-7b-hf/tokenizer.model"

TP=1     # MLA only support TP1
PP=2
CP=1
EP=2

DISTRIBUTED_ARGS="
    --nproc_per_node $NPUS_PER_NODE \
    --nnodes $NNODES \
    --node_rank $NODE_RANK \
    --master_addr $MASTER_ADDR \
    --master_port $MASTER_PORT
"

RECOMPUTE_ARGS="
    --recompute-activation-function \
    --swap-attention \
"

MOE_ARGS="
    --expert-model-parallel-size ${EP} \
    --moe-model-type megatron_moe \
    --moe-token-dispatcher-type alltoall_seq \
    --moe-permutation-async-comm \
    --moe-pad-expert-input-to-capacity \
    --moe-expert-capacity-factor 1.5 \
    --n-shared-experts 1 \
    --num-experts 16 \
    --moe-router-topk 4 \
    --moe-aux-loss-coeff 0.02 \
"

MLA_ARGS="
    --multi-head-latent-attention \
    --qk-rope-head-dim 64 \
    --qk-nope-head-dim 128 \
    --q-lora-rank 1536 \
    --kv-lora-rank 512 \
    --v-head-dim 128 \
    --qk-layernorm \
    --rotary-scaling-factor 40 \
"

ROPE_ARGS="
    --rope-scaling-beta-fast 32 \
    --rope-scaling-beta-slow 1 \
    --rope-scaling-factor  40 \
    --rope-scaling-mscale 0.707 \
    --rope-scaling-mscale-all-dim  0.707 \
    --rope-scaling-original-max-position-embeddings 4096 \
    --rope-scaling-type yarn
"

GPT_ARGS="
    --tensor-model-parallel-size ${TP} \
    --pipeline-model-parallel-size ${PP} \
    --num-layers-per-virtual-pipeline-stage 1 \
    --context-parallel-size ${CP} \
    --context-parallel-algo megatron_cp_algo \
    --use-flash-attn \
    --use-fused-rotary-pos-emb \
    --use-fused-swiglu \
    --use-fused-rmsnorm \
    --reuse-fp32-param \
    --sequence-parallel \
    --use-distributed-optimizer \
    --overlap-grad-reduce \
    --num-layers 4 \
    --seq-length 8192 \
    --max-position-embeddings 8192 \
    --train-iters 10000 \
    --hidden-size 8192 \
    --num-attention-heads 128 \
    --ffn-hidden-size 4352 \
    --make-vocab-size-divisible-by 128 \
    --vocab-size 126464 \
    --micro-batch-size 1 \
    --global-batch-size 8 \
    --tokenizer-type Llama2Tokenizer \
    --tokenizer-model ${TOKENIZER_MODEL} \
    --disable-bias-linear \
    --lr-decay-style linear \
    --lr-warmup-iters 1500 \
    --short-seq-prob 0.0 \
    --attention-dropout 0.0 \
    --hidden-dropout 0.0 \
    --untie-embeddings-and-output-weights \
    --init-method-std 0.006 \
    --position-embedding-type rope \
    --normalization RMSNorm \
    --swiglu \
    --no-masked-softmax-fusion \
    --attention-softmax-in-fp32 \
    --no-gradient-accumulation-fusion \
    --bf16 \
    --lr 2.0e-4 \
    --min-lr 2.0e-4 \
    --weight-decay 0.1 \
    --clip-grad 1.0 \
    --adam-beta1 0.9 \
    --adam-beta2 0.95 \
    --rotary-base 100000 \
    --norm-epsilon 1.0e-5 \
    --npu-deterministic \
"

DATA_ARGS="
    --data-path $DATA_PATH \
    --split 995,5,0
"

OUTPUT_ARGS="
    --log-throughput \
    --log-interval 1 \
    --save-interval 10000 \
    --eval-interval 10000 \
    --eval-iters 10 \
"

torchrun $DISTRIBUTED_ARGS pretrain_gpt.py \
    $GPT_ARGS \
    $RECOMPUTE_ARGS \
    $MOE_ARGS \
    $MLA_ARGS \
    $ROPE_ARGS \
    $DATA_ARGS \
    $OUTPUT_ARGS \

set +x
