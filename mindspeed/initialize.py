import time
from functools import wraps
import torch
import torch_npu
from megatron.training import get_args
from megatron.training.utils import print_rank_0
from megatron.training.initialize import _warmup_jit_function
from mindspeed.core.tensor_parallel.ascend_turbo.initialize import initialize_cfg_from_args
from .utils import extend_seed_all


def _compile_dependencies():
    if torch.distributed.get_rank() == 0:
        start_time = time.time()
        print('> compiling dataset index builder ...')
        from megatron.core.datasets.utils import compile_helpers
        compile_helpers()
        print('>>> done with dataset index builder. Compilation time: {:.3f} '
              'seconds'.format(time.time() - start_time), flush=True)


def set_jit_fusion_options_wrapper(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        def _jit_set_nvfuser_enabled(option):
            pass
        torch._C._jit_set_nvfuser_enabled = _jit_set_nvfuser_enabled
        fn(*args, **kwargs)
        args = get_args()
        if args.jit_compile:
            torch_npu.npu.set_compile_mode(jit_compile=True)

    return wrapper


def coc_registration_wrapper(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        res = fn(*args, **kwargs)
        from mindspeed.core.tensor_parallel.lcal_coc.user_config import initialize_coc_from_cfg
        args = get_args()
        initialize_coc_from_cfg(args)
        return res

    return wrapper


def mc2_wrapper(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        res = fn(*args, **kwargs)
        args = get_args()
        initialize_cfg_from_args(args)
        return res

    return wrapper


def deter_comp_wrapper(fn):
    @wraps(fn)
    def wrapper(seed_, data_parallel_random_init=False):
        fn(seed_, data_parallel_random_init=False)
        extend_seed_all(seed_)
        print_rank_0("deterministic computing is applied for npu.")
    return wrapper


def _initialize_distributed_wrapper(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        device_count = torch.cuda.device_count()
        device = get_args().rank % device_count
        torch.cuda.set_device(device)
        from mindio_ttp.adaptor import tft_init_controller_processor
        tft_init_controller_processor(enable_tls=False, tls_option_top_path='')
        fn(*args, **kwargs)

    return wrapper


def reboot_skip_wrapper(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        from mindio_ttp.adaptor import tft_is_arf_reboot_node
        if tft_is_arf_reboot_node():
            return None
        res = fn(*args, **kwargs)
        return res

    return wrapper


def new_group_wrapper(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        backend = kwargs.get('backend', None)
        from mindio_ttp.adaptor import tft_is_arf_reboot_node
        if tft_is_arf_reboot_node() and isinstance(backend, str) and 'gloo' in backend:
            return None

        if torch.distributed.distributed_c10d._is_barrier_after_init():
            kwargs['use_local_synchronization'] = True

        res = fn(*args, **kwargs)
        return res

    return wrapper