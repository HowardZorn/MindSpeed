// Copyright (c) 2024 Huawei Technologies Co., Ltd
// All rights reserved.
//
// Licensed under the BSD 3-Clause License (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
// https://opensource.org/licenses/BSD-3-Clause
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
#pragma once

#include <torch_npu/csrc/core/npu/NPUEvent.h>
#include <torch_npu/csrc/core/npu/NPUFunctions.h>

class EventPool {
public:
    using Event = std::unique_ptr<c10_npu::NPUEvent, std::function<void(c10_npu::NPUEvent *)>>;
    // Explicit device count
    EventPool() : pools_(c10_npu::device_count()) {}

    Event get(int device);

    void empty_cache();

private:
    struct PerDevicePool {
        alignas(64) std::mutex mutex_;
        std::vector<std::unique_ptr<c10_npu::NPUEvent>> event_pool_;
    };
    std::vector<PerDevicePool> pools_;
};
