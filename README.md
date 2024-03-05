<p align="center"> <img src="sources/images/logo.png" height="90px" width="400px"></p>

AscendSpeed 是针对华为昇腾设备的大模型加速库。

大模型训练是一种非常复杂的过程，涉及到许多技术和挑战，其中大模型训练需要大量的显存资源是一个难题，对计算卡提出了不小的挑战。为了在单个计算卡显存资源不足时，可以通过多张计算卡进行计算，业界出现了类似 Megatron、DeepSpeed 等第三方大模型加速库，对模型、输入数据等进行切分并分配到不同的计算卡上，最后在通过集合通信对结果进行汇总。 

昇腾提供 AscendSpeed 加速库，使能客户大模型业务快速迁移至昇腾设备，并且支持昇腾专有算法，确保开箱可用。

## 模型训练软件配套

| 软件      | [版本](https://www.hiascend.com/zh/) |
| --------- |------------------------------------|
| Python    | 推荐 3.8                             |
| driver    | Ascend HDK 23.0.0                  |
| firmware  | Ascend HDK 23.0.0                  |
| CANN      | CANN 7.0.0                         |
| kernel    | CANN 7.0.0                         |
| torch     | 2.1.0                              |
| torch_npu | release v5.0.0                     |
| apex      | v5.0.rc3                           |

## 安装

1. 安装 AscendSpeed

    方式一、从 git 直接安装
    
    ```shell
    pip install git+https://gitee.com/ascend/AscendSpeed.git
    ```
    
    方式二、下载源码安装
    
    ```shell
    git clone https://gitee.com/ascend/AscendSpeed.git
    cd AscendSpeed
    pip install -e .
    ```

2. 获取 Megatron-LM 并指定 commit id

    ```shell
    git clone https://github.com/NVIDIA/Megatron-LM.git
    cd Megatron-LM
    git checkout bcce6f54e075e3c3374ea67adefe54f3f2da2b07
    ```
## AscendSpeed 特性

### Megatron-LM NPU 适配
AscendSpeed 对 Megatron-LM 的基本功能进行了适配，已适配如下特性：
- 张量并行
- 流水线并行
- 序列并行
- 重计算
- 分布式优化器
- 异步分布式数据并行

使用方式：
1. 在 Megatron-LM 目录下修改`pretrain_gpt.py`文件，在`import torch`下新增一行`import ascendspeed.megatron_adaptor`

    ```diff
     import os
     import torch
    +import ascendspeed.megatron_adaptor
     from torch import Tensor
     from functools import partial
     from typing import Union
    ```

2. 在 Megatron-LM 目录下，准备好训练数据，并在示例脚本中填写对应路径，然后执行。
    ```shell
    bash examples/pretrain_gpt_distributed.sh
    ```

### TP重计算通信优化

优化重计算中通信算子，提升模型性能。  
具体信息请查看：[link](./docs/recomputation-communication.md)

### 内存碎片优化

通过对不同生命周期的 tensor 进行分别管理，以减少显存碎片。  
具体信息请查看：[link](./docs/memory-fragmentation.md)


### 自适应选择重计算

自动调整训练显存大小，选择重计算策略，提高模型训练的性能。  
具体信息请查看：[link](./docs/adaptive-recompute.md)

## 说明

### 安全加固方案
#### 关于文件的权限控制

- 建议您参考[附录A 文件权限清单](#A-文件权限清单)对各类文件权限进行设计与控制。
- linux系统的umask值建议不低于`027`。
- linux系统的ASLR值建议为2级（默认为2级）。
- 建议您务必对模型训练相关文件（如数据集、配置文件、源代码、checkpoint等）做好权限管理，避免文件被恶意篡改、破坏业务进行等风险，比如可以控制为同组/其他用户仅有只读权限。
- 原生megatron以及torch框架执行中所生成的文件权限受到linux系统umask参数影响，如umask设置为`027`，其目录/文件权限默认为`750`/`640`，您可进一步管理权限。
#### 关于命令执行
基于安全性考虑，建议您在执行任何命令时，都尽量使用非root账户执行，遵循权限最小化原则。

#### 关于资源使用

建议您根据自身运行环境资源状况，进行训练配置的设定与数据集的准备，若与资源状况不匹配，比如数据集的size超出内存容量/NPU存储容量等，那么原生的Megatron或Pytorch库的组件会直接退出，并自动释放占用的资源。

#### 关于数据集与index map

第一次执行训练，原生megatron会打印`WARNING: could not find index map files`，并尝试**在数据集目录下帮您创建index map files**，从而能够继续训练。为兼容多用户共享数据集文件以及`index map files`的业务场景，生成的`index map files`权限默认为`644`，存在被其他用户访问的风险，您可以参考[附录A 文件权限清单](#c-文件权限清单)对其进行加固。

#### 关于通信

您作为计算集群的完全控制者，务必注意集群节点间的通信安全，比如做好组网设计并采取相关安全措施。建议在内部网络下部署计算集群，从而避免公网环境下的诸多安全风险。

#### 关于网络端口
AscendSpeed不主动开放端口，对于原生Pytorch开放的相关端口，您可以参考其官方文档进行设置。在单机训练的情况下，不建议开放全局端口。具体的通信矩阵可以参考[附录B 通信矩阵](#B-通信矩阵)。

运行时底层的CANN会缓存算子编译文件，存储在运行目录下的`kernel_meta_*`文件夹内，加快后续训练的运行速度。


## 附录

### A-文件权限清单

您可以根据自身需要，参考此清单对各类文件进行加固:

|           类型            |  linux权限参考值  |                             备注                             |
| :-----------------------: | :---------------: | :----------------------------------------------------------: |
|       文件夹 / 目录       | `750` (rwxr-x---) |      包括checkpoint保存目录、数据集存放目录，安装目录等      |
|        数据集文件         | `640` (rw-r-----) | 这里的数据集为公开数据集，不涉及隐私数据、商业资产等。另外，若需要共享数据集目录/文件，您可酌情调整为`755`/`644`，并注意调整后存在被其他用户（Others）读取的风险 |
|       运行生成文件        | `640` (rw-r-----) |      如checkpoint、数据集预处理npy文件等就属于生成文件       |
|     不可执行程序文件      | `440` (r--r-----) | 一般程序文件不应修改，如果需要进行开发，您可酌情调整为`640`  |
| 程序目录 / 可执行程序文件 | `550` (r-xr-x---) | 一般程序目录/可执行程序不应修改，如果需要进行开发，您可酌情调整为`750` |
|    日志文件（已归档）     | `440` (r--r-----) |                                                              |
|   日志文件（正在记录）    | `640`(rw-r-----)  |                                                              |
### B-通信矩阵

|           源设备            |    源IP    |                            源端口                            |          目的设备           |   目的IP   |                       目的端口（侦听）                       | 协议 |                           端口说明                           |                             备注                             |
| :-------------------------: | :--------: | :----------------------------------------------------------: | :-------------------------: | :--------: | :----------------------------------------------------------: | :--: | :----------------------------------------------------------: | :----------------------------------------------------------: |
| 运行torch_npu进程的计算设备 | 设备地址IP | 操作系统自动分配，分配范围由操作系统决定，如ubuntu是采用`/proc/sys/net/ipv4_local_port_range`文件指定 | 运行torch_npu进程的计算设备 | 设备地址IP | 当用户不使用**测试示例脚本**，则默认29500/29400。用户可调用`torch.distributed.launch`函数，通过传入的`--master_port`自由指定1024-65535之间未被占用的端口 | TCP  | 源端口与目的端口均用于收发数据。对于静态分布式场景（backend=static）默认端口为29400；对于动态分布式场景（backend=c10d）中默认端口29500 | megatron_npu本身不开启端口，该通信过程由开源软件Pytorch控制，配置方式可参考其官方文档：https://pytorch.org/docs/stable/distributed.html#launch-utility |
| 运行torch_npu进程的计算设备 | 设备地址IP | 操作系统自动分配，分配范围由操作系统决定，如ubuntu是采用`/proc/sys/net/ipv4_local_port_range`文件指定 | 运行torch_npu进程的计算设备 | 设备地址IP | 当使用`pretrain_gpt_distributed*`系列测试示例脚本，脚本对`torch.distributed.launch`传入的`--master_port`为**6000**，用户可以自由指定1024-65535之间未被占用的端口 | TCP  | 原生Pytorch（调用`torchrun`、`torch.distributed.launch`）通信需要，用于收发数据 | 和第一条记录所述为同一端口，这里特别说明**测试示例脚本**对Pytorch开启的master_port默认配置为6000 |
| 运行torch_npu进程的计算设备 | 设备地址IP | 操作系统自动分配，分配范围由操作系统决定，如ubuntu是采用`/proc/sys/net/ipv4_local_port_range`文件指定 | 运行torch_npu进程的计算设备 | 设备地址IP | 当使用`test_gpt_distributed*`系列测试示例脚本，脚本对`torch.distributed.launch`传入的`--master_port`为**60035**，用户可以自由指定1024-65535之间未被占用的端口 | TCP  | 原生Pytorch（调用`torchrun`、`torch.distributed.launch`）通信需要，用于收发数据 | 和第一条记录所述为同一端口，这里特别说明**测试示例脚本**对Pytorch开启的master_port默认配置为60035 |
| 运行torch_npu进程的计算设备 | 设备地址IP |                  请参见备注中的CANN官方文档                  | 运行torch_npu进程的计算设备 | 设备地址IP |                  请参见备注中的CANN官方文档                  | TCP  |                  请参见备注中的CANN官方文档                  | 该通信过程完全由HCCL组件控制，端口范围可参考文档：https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/700alpha001/ref/envref/envref_07_0065.html CANN通信文档：https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/700alpha001/ref/hcclapiref/hcclapi_07_0001.html |

