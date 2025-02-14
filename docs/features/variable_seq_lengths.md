## PP支持动态形状

## 背景与挑战

在深度学习模型的训练过程中，特别是在多模态任务中，输入序列的长度往往是变化的。传统的做法是将所有序列填充或截断到相同的长度，以便能够以固定大小的张量进行批量处理。这种做法虽然简化了数据处理和模型设计，但会导致计算资源的浪费，特别是在处理较短序列时，因为需要大量的填充。

对于使用流水线并行（Pipeline Parallelism, PP）的模型来说，如果每个微批次（micro-batch）中的序列长度不同，这将导致额外的通信开销，因为在不同的阶段之间需要同步和传递不同大小的数据。因此，当序列长度恒定时，通常不推荐使用此特性。

**主要挑战：**
- **内存效率低下**：大量填充导致内存利用率低。
- **计算效率低下**：对填充部分进行不必要的计算。
- **额外的通信开销**：在流水线并行中处理不同长度的序列会导致额外的通信成本。

## 解决方案

- 引入 `--variable-seq-lengths` 选项，允许数据加载器生成具有不同序列长度的批次/微批次。
- 在模型内部动态调整张量尺寸，以适应不同长度的输入序列。
- 优化通信协议，减少因序列长度不一致而产生的额外开销。

**具体实现：**
- **跨阶段通信张量形状**：在实际的张量通信之前，首先在各个阶段之间通信张量的形状信息。这是为了确保在传输实际张量数据之前，各个阶段已经知道了即将接收的张量的具体形状，从而可以正确地分配内存并进行必要的预处理。
- **数据加载器支持可变序列长度**：数据加载器能够生成具有不同序列长度的批次/微批次，并提供每个样本的实际序列长度信息。
- **模型架构支持动态输入尺寸**：模型前向传播逻辑能够处理动态输入尺寸，并更新任何依赖于固定序列长度的层或操作。
- **通信机制优化**：通过提前沟通张量形状，最小化由于序列长度变化带来的额外通信开销。

#### 使用场景

- **多变长度文本处理**：如文档分类、机器翻译等任务，其中文本长度差异很大。
- **提高内存利用率**：在内存受限的情况下，避免不必要的填充可以显著提升内存使用效率。
- **增强模型泛化能力**：让模型更好地适应各种长度的输入，从而提高其在实际应用中的表现。

#### 使用方法

**注意事项：**
- 监控训练过程中的内存使用情况，以防因序列长度变化导致的内存溢出。


**设置训练脚本参数**
- 在训练脚本中添加 `--variable-seq-lengths` 参数。


#### 使用效果

- **内存效率提升**：通过避免不必要的填充，减少了内存占用，提高了内存使用效率。
- **灵活性增加**：模型能够更好地处理不同长度的输入，提高了模型的泛化能力。
- **潜在性能影响**：由于增加了序列长度变化带来的复杂性，可能会导致训练速度减慢，特别是在使用流水线并行的情况下。
- **更真实的数据表示**：保留了原始文本的真实长度，有助于模型更准确地捕捉文本特征。

启用 `--variable-seq-lengths` 特性可以带来更高效的内存使用和更强的模型泛化能力，但同时也带来了实现上的复杂性和可能的性能损失。用户应该根据自己的具体需求和资源状况来决定是否启用这一特性。