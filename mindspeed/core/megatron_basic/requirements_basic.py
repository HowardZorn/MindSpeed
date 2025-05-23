from functools import wraps
import math
import torch


def version_wrapper(fn):
    @wraps(fn)
    def wrapper(name, *args, **kwargs):
        return '0.0' if name == 'transformer-engine' else fn(name, *args, **kwargs)

    return wrapper


def multi_tensor_applier(op, noop_flag_buffer, tensor_lists, *args):
    return op(noop_flag_buffer, tensor_lists, *args)


def multi_tensor_l2norm(overflow_buf, tensor_lists, per_parameter):
    total_norm = 0.0
    norm_type = 2.0
    ret_per_tensor = [] if per_parameter else None
    for grads_for_norm in tensor_lists:
        for grad in grads_for_norm:
            grad_norm = torch.norm(grad, norm_type)
            total_norm += grad_norm ** norm_type
        if per_parameter:
            ret_per_tensor.append(total_norm.clone())
    if not tensor_lists:
        grad_norm = torch.cuda.FloatTensor([0])
        total_norm = grad_norm ** norm_type
    return total_norm ** (1 / norm_type), ret_per_tensor


def multi_tensor_scale(overflow_buf, tensor_lists, scale):
    if len(tensor_lists) != 2:
        raise AssertionError('The size of tensor list must be 2, but got {}'.format(len(tensor_lists)))
    if len(tensor_lists[0]) != len(tensor_lists[1]):
        raise AssertionError('The size of tensor list must be same, but got {} and {}'
                             .format(len(tensor_lists[0]), len(tensor_lists[1])))
    with torch.no_grad():
        for i in range(len(tensor_lists[0])):
            tensor_lists[1][i].copy_(tensor_lists[0][i] * scale)


def type_wrapper(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        res = fn(*args, **kwargs)
        if isinstance(res, str):
            res = res.replace('npu', 'cuda')
        return res

    return wrapper


def ensure_contiguous_wrapper(fn):
    @wraps(fn)
    def wrapper(tensor, *args, **kwargs):
        tensor = tensor.contiguous() if not tensor.is_contiguous() else tensor
        return fn(tensor, *args, **kwargs)

    return wrapper


def lcm(a, b):
    return (a * b) // math.gcd(a, b)


def dummy_function(*args, **kwargs):
    pass


def torch_all_reduce_double_dtype_bypass_wrapper(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if torch.is_tensor(args[0]) and args[0].dtype == torch.double:
            args = list(args)
            args[0] = args[0].float()
            handle = fn(*args, **kwargs)
            if handle is not None:
                handle.wait()
            args[0] = args[0].double()
            return None

        return fn(*args, **kwargs)

    return wrapper
