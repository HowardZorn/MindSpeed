
/*
 * Copyright (c) Huawei Technologies Co., Ltd. 2023. All rights reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
#include "../inc/atb_adapter.h"
#include <torch_npu/csrc/core/npu/NPUStream.h>
#include <torch_npu/csrc/core/npu/DeviceUtils.h>
#include <torch_npu/csrc/core/npu/NPUFormat.h>

using namespace std;

static atb::Context* msContext = nullptr;

at::Tensor FormatTrans(const at::Tensor &at_tensor)
{
    if (at_tensor.defined()) {
        TORCH_CHECK(torch_npu::utils::is_npu(at_tensor), "only npu tensor is supported");
        return at_npu::native::npu_format_cast(at_tensor, ACL_FORMAT_ND);
    }
    return at_tensor;
}

atb::Tensor AtTensor2Tensor(const at::Tensor atTensor)
{
    static std::map<at::ScalarType, aclDataType> dtypeMap = {
        {at::ScalarType::Bool, ACL_BOOL},   {at::ScalarType::Byte, ACL_UINT8},
        {at::ScalarType::Char, ACL_INT8},   {at::ScalarType::Half, ACL_FLOAT16},
        {at::ScalarType::Float, ACL_FLOAT}, {at::ScalarType::Int, ACL_INT32},
        {at::ScalarType::Long, ACL_INT64}, {at::ScalarType::BFloat16, ACL_BF16},
    };

    TORCH_CHECK(atTensor.is_contiguous(), "atTensor is not contiguous");
    atb::Tensor tensor;
    tensor.desc.format = ACL_FORMAT_ND;
    tensor.deviceData = atTensor.data_ptr();

    tensor.desc.shape.dimNum = atTensor.sizes().size();
    for (uint64_t i = 0; i < atTensor.sizes().size(); i++) {
        tensor.desc.shape.dims[i] = atTensor.sizes()[i];
    }

    auto it = dtypeMap.find(atTensor.scalar_type());
    TORCH_CHECK(it != dtypeMap.end(), "not support dtype:");
    tensor.desc.dtype = it->second;

    tensor.dataSize = atb::Utils::GetTensorSize(tensor);

    return tensor;
}

void RunAtbCmd(atb::Operation *op, const ParamSetter &paramsetter, const std::string &name)
{
    auto contextPtr = GetContext();
    uint64_t workspaceSize = OperationSetup(paramsetter.variantPack, op, contextPtr);
    auto workspaceTensor = GetWorkspaceTensor(workspaceSize, op);
    const void *workspacePtr = nullptr;
    workspacePtr = workspaceTensor.storage().data();
    auto acl_call = [op, contextPtr, paramsetter, workspacePtr, workspaceSize]() -> int {
        auto st = op->Execute(paramsetter.variantPack, (uint8_t *)workspacePtr, workspaceSize, contextPtr);
        DestroyOperation(op);
        return 0;
    };
    at_npu::native::OpCommand cmd;
    cmd.Name(name);
    cmd.SetCustomHandler(acl_call);
    cmd.Run();
}

ParamSetter& ParamSetter::Input(const at::Tensor &tensor)
{
    if (!tensor.defined()) {
        variantPack.inTensors.push_back(atb::Tensor());
        return *this;
    }
    at::Tensor newTensor = FormatTrans(tensor);
    if(!newTensor.is_contiguous()) {
        newTensor = newTensor.contiguous();
    }
    auto AtTensor = AtTensor2Tensor(newTensor);

    variantPack.inTensors.push_back(AtTensor);
    return *this;
}

ParamSetter& ParamSetter::Input(const c10::optional<at::Tensor> &tensor)
{
    if (!tensor.has_value()) {
        variantPack.inTensors.push_back(atb::Tensor());
        return *this;
    }
    return Input(tensor.value());
}

ParamSetter& ParamSetter::Output(at::Tensor &output)
{
    auto AtTensor = AtTensor2Tensor(output);
    variantPack.outTensors.push_back(AtTensor);
    return *this;
}

uint64_t OperationSetup(atb::VariantPack variantPack, atb::Operation *operation, atb::Context* contextPtr)
{
    uint64_t workspaceSize = 0;
    atb::Status status = operation->Setup(variantPack, workspaceSize, contextPtr);
    TORCH_CHECK(status == 0, "setup failed!");
    return workspaceSize;
}

at::Tensor GetWorkspaceTensor(uint64_t workspaceSize, atb::Operation *operation)
{
    at::TensorOptions options = at::TensorOptions(torch_npu::utils::get_npu_device_type());
    at::Tensor workspaceTensor = at::empty(at::IntArrayRef(workspaceSize), options.dtype(at::kByte));
    return workspaceTensor;
}

atb::Context* GetContext()
{        
    if (msContext == nullptr) {
        auto status = atb::CreateContext(&msContext);
        TORCH_CHECK(status == 0, "create context failed!");
        int32_t devId = 0;
        aclrtGetDevice(&devId);
        aclrtStream stream = c10_npu::getCurrentNPUStream(devId).stream(false);
        TORCH_CHECK(stream != nullptr, "get current stream failed");
        msContext->SetExecuteStream(stream);
    }
    return msContext;
}
