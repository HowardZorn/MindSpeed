import copy
from mindspeed.auto_settings.module.operator.operator_elemental import ProfileList


class ConfigInfo(object):
    def __init__(self, config):
        self.tp = config.tensor_model_parallel_size
        self.dp = config.data_parallel_size
        self.pp = config.pipeline_model_parallel_size
        self.vp = config.num_layers_per_virtual_pipeline_stage if config.num_layers_per_virtual_pipeline_stage else 1
        self.cp = config.context_parallel_size
        self.ep = config.expert_model_parallel_size or 1
        self.jit = 1 if config.jit_compile else 0
        self.seq_length = config.seq_length
        self.num_experts = config.num_experts

    def __str__(self):
        return (f"tp:{self.tp}, dp:{self.dp}, pp:{self.pp}, vp:{self.vp}, cp:{self.cp}, ep:{self.ep}, jit:{self.jit}, "
                f"seq_length:{self.seq_length}, num_experts:{self.num_experts}")


class OriginalProfileData(object):
    def __init__(self, config):
        self.config_info = ConfigInfo(config)
        self.profile_list = ProfileList()


class OriginalProfileDataList(object):
    def __init__(self):
        self.data_list = []
        self.layer_time = []

    def get_origin_profile_data(self, profiling_results):
        for config, model in profiling_results:
            origin_profile_data = OriginalProfileData(config)

            profile_list_fw = self.get_profinfo_list_from_profiling(model.forward.operator_info[-1],
                                                                    forwardflag=1)
            profile_list_bw = self.get_profinfo_list_from_profiling(model.backward.operator_info[-1],
                                                                    forwardflag=0)
            origin_profile_data.profile_list.fw = copy.deepcopy(profile_list_fw)
            origin_profile_data.profile_list.bw = copy.deepcopy(profile_list_bw)

            self.data_list.append(origin_profile_data)

            forward_time, backward_time = self.get_forward_backward_time(profile_list_fw, profile_list_bw)
            self.layer_time.append([forward_time, backward_time])

    @staticmethod
    def get_forward_backward_time(profile_fw, profile_bw):
        total_time_fw = 0
        total_time_bw = 0
        for operator in profile_fw:
            total_time_fw += float(operator.duration_us)
        for operator in profile_bw:
            total_time_bw += float(operator.duration_us)
        return total_time_fw, total_time_bw

    @staticmethod
    def get_profinfo_list_from_profiling(items, forwardflag):
        operator_info_list = []
        alltoall_flag = 0
        cp_flag1 = 0
        cp_flag = 0
        for (index, item) in enumerate(items):
            # CP 正向FN部分打标记
            if forwardflag == 1:
                if "ConcatD" in item.name and index < (len(items) - 2):
                    if "hcom_send" in items[index + 1].name or "hcom_send" in items[index + 2].name:
                        cp_flag1 = 1
                if cp_flag1 == 1:
                    if "MatMul" in item.name:
                        cp_flag1 = 0
                        continue
                    item.name = "cp_for_flag_" + item.name
            # CP 反向部分打标记
            if forwardflag == 0:
                # CP 反向FN重计算部分
                if cp_flag == 0 and "ConcatD" in item.name and index < (len(items) - 2):
                    if "hcom_send" in items[index + 1].name or "hcom_send" in items[index + 2].name:
                        cp_flag1 = 2
                if cp_flag1 == 2:
                    if "MatMul" in item.name:
                        cp_flag1 = 0
                        continue
                    item.name = "cp_re_flag_" + item.name
                # CP 反向FN部分
                if cp_flag == 0 and "Concat" in item.name and index < (len(items) - 2):
                    if "ZerosLike" in items[index + 1].name:
                        cp_flag = 1
                if cp_flag == 1:
                    if "Mul" in item.name:
                        cp_flag = 0
                if cp_flag == 1:
                    item.name = "cp_back_flag_" + item.name

            # EP部分打标记
            if "alltoall" in item.name:
                alltoall_flag = alltoall_flag + 1
            if alltoall_flag % 2 == 1:
                item.name = "ep_flag_" + item.name

            if (
                not ("hcom" in item.name) and item.input_shapes != 'N/A'
                and item.input_shapes.replace('"', '').replace(';', '') != ''
            ):
                operator_info_list.append(item)
            setattr(item, "index_name", '')

        return operator_info_list
