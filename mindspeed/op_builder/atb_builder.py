import os
import site
from torch.utils.cpp_extension import load
from mindspeed.op_builder.builder import MindSpeedOpBuilder


class AtbOpBuilder(MindSpeedOpBuilder):
    def __init__(self, name):
        super(AtbOpBuilder, self).__init__(name)

    def get_atb_path(self):
        if os.getenv('ATB_HOME_PATH') is not None:
            return os.environ['ATB_HOME_PATH']  # 'ATB_HOME_PATH' refers to home path of Ascend Transformer Boost lib, which is used for ops loading.
        else:
            raise RuntimeError("please install cann-nnal package first")

    def include_paths(self):
        paths = super().include_paths()
        paths.append(os.path.join(self._torch_npu_path, 'include', 'third_party', 'acl', 'inc'))
        paths.append('ops/csrc/atb/extensions/inc')
        paths.append(os.path.join(self.get_atb_path(), 'include'))
        return paths
    
    def cxx_args(self):
        args = super().cxx_args()
        args += [
            '-Wno-sign-compare',
            '-Wno-deprecated-declarations',
            '-Wno-return-type',
            "-D__FILENAME__='\"$$(notdir $$(abspath $$<))\"'",
            '-D ENABLE_ATB',
            '-fstack-protector-all', 
            '-Wl,-z,relro,-z,now,-z,noexecstack',
            '-fPIC', '-pie', '-s'
        ]
        return args
    
    def extra_ldflags(self):
        flags = ['-L' + os.path.join(self._torch_npu_path, 'lib'), '-ltorch_npu',
                 '-L' + os.path.join(self.get_atb_path(), 'lib'), '-lasdops',
                 '-L' + os.path.join(self.get_atb_path(), 'lib'), '-llcal',
                 '-L' + os.path.join(self.get_atb_path(), 'lib'), '-latb']
        return flags

    def load(self, verbose=True):
        if os.getenv('ASDOPS_HOME_PATH') is None or os.getenv('ASDOPS_OPS_PATH') is None:
            os.environ['ASDOPS_HOME_PATH'] = self.get_atb_path()  # 'ASDOPS_HOME_PATH' refers to home path of Ascend ops, which is used for ops loading.
            os.environ['ASDOPS_OPS_PATH'] = os.path.join(self.get_atb_path(), 'ops')  # 'ASDOPS_OPS_PATH' refers to ops path of Ascend ops, which is used for ops loading.
        return super().load()