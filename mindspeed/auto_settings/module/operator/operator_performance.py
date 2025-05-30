import json
import time

from mindspeed.auto_settings.config.system_config import get_system_config
from mindspeed.auto_settings.utils.logger import get_logger
from mindspeed.auto_settings.config.model_config import ModelConfig
from mindspeed.auto_settings.config.search_config import SearchConfig
from mindspeed.auto_settings.module.operator.operator_profile_get import OriginalProfileDataList
from mindspeed.auto_settings.module.operator.operator_note_cal import OperatorNoteList
from mindspeed.auto_settings.module.operator.operator_base_block import BaseBlock
from mindspeed.auto_settings.module.operator.operator_change_block_cp import CpBlock
from mindspeed.auto_settings.module.operator.operator_change_block_ep import EpBlock
from mindspeed.auto_settings.module.operator.operator_elemental import DictCalShape, OperatorLayerTime
from mindspeed.auto_settings.module.operator.operator_database import DataBase, Operator, OperatorHistory
from mindspeed.auto_settings.module.operator.operator_shape_analysis import separate_ep_tp_cp
from mindspeed.auto_settings.module.operator.operator_shape_cal import (model_operator_with_tp,
                                                                      model_operator_with_shape,
                                                                      cal_new_shape_tce,
                                                                      cal_operator_flops,
                                                                      cal_operator_duration_with_shape)


class OperatorPerformance(object):
    """
    Operator Performance modeling
        1. 试跑
        2. profiler 解析
        3. 建模【我理解就是试跑结果 放入modeling内所有模块数学建模估算，然后动态调整试跑配置再进行数学建模估算【循环】】
        4. 返回推荐配置
    """

    def __init__(self, model_config: ModelConfig, working_dir: str):
        self.db = DataBase(working_dir=working_dir)
        self.waas = None
        self.origin_profile_data_list = OriginalProfileDataList()
        self.model_config = model_config
        self._logger = get_logger('operator')

        self.base_block = BaseBlock()
        self.cp_block = CpBlock()
        self.ep_block = EpBlock()

        self.dict_model = dict()

    def model_operator_timer(self, profiling_results):
        """
        对外接口一，根据profiler结果进行shape 和 duration的建模，当前算子都只取一个microbitch，无论PP是否开启
        """
        self.dict_model = dict()
        # 第 0 轮取到原始数据
        self.origin_profile_data_list.get_origin_profile_data(profiling_results)
        # 第 1 轮取到base_block
        self.base_block.get_block(self.origin_profile_data_list.data_list)
        # 第 2 轮取到change_block
        self.cp_block.get_block(self.origin_profile_data_list, self.base_block)
        if self.origin_profile_data_list.data_list[0].config_info.num_experts and not self.model_config.moe_grouped_gemm:
            self.ep_block.get_block(self.origin_profile_data_list, self.base_block)

        st_time = time.time()
        # 第 3 轮, Note数据表重新排序，按照新生成的index_name分类
        operator_note_list = OperatorNoteList()
        operator_note_list.get_operator_note(self)

        self.get_history_db(operator_note_list.operator_note_list)
        self._logger.info(f'-----------------------------------')
        # 第 4 轮，基于operator_note_model建shape计算operator_model_dao
        self.get_operator_model(operator_note_list.operator_note_dict)

        self._logger.info("get operator_base_dao successful")
        self._logger.info("total number of operator_note_dict: {}, dict_model {}, base_block {}, cp_block {}, "
                          "ep_block {}".format(len(operator_note_list.operator_note_dict), len(self.dict_model),
                                               len(self.base_block.fw) + len(self.base_block.bw),
                                               len(self.cp_block.fw) + len(self.cp_block.bw) + len(self.cp_block.re),
                                               len(self.ep_block.fw) + len(self.ep_block.bw) + len(self.ep_block.re)))
        if self.model_config.moe_grouped_gemm:
            self._logger.info("ep_block will not include when using moe_grouped_gemm")
        self._logger.info(f'total time: {time.time() - st_time}')
        self._logger.info(f'---------------------------【Add operator to db】---------------------------')

    def get_history_db(self, operator_note_list):
        self._logger.info("******************   duration_sum(ms)  ***********************")
        tplt = "{0:<2}\t{1:<2}\t{2:<2}\t{3:<2}\t{4:<2}\t{5:<8}\t{6:<8}\t{7:<8}\t{8:<8}\t{9:<8}"
        self._logger.info(tplt.format('tp', 'dp', 'pp', 'cp', 'ep', 'duration_sum', 'operator_num_fw',
                                      'operator_num_bw', 'layer_time_fw', 'layer_time_bw'))
        self._logger.info(f'--------------------------------------------------------------------------')
        for (index, operator_note) in enumerate(operator_note_list):
            operator_history_list = []
            operator_waas_list = []
            duration_sum = 0
            operator_list = operator_note.fw + operator_note.bw
            for operator in operator_list:
                duration_sum += float(operator.duration)
                operator_history = OperatorHistory(types=operator.type,
                                                   accelerator_core=operator.accelerator_core,
                                                   input_shape=operator.input_shape,
                                                   output_shape=operator.output_shape,
                                                   duration=operator.duration,
                                                   device=get_system_config().device_type,
                                                   jit=operator.jit,
                                                   cann=get_system_config().cann_version,
                                                   driver=get_system_config().driver_version,
                                                   dtype=self.model_config.dtype.value[0])
                operator_waas_list.append(operator_history)
                operator_history_list.append(operator_history.convert_to_dict())
            # 历史数据
            if get_system_config().waas_enabled and get_system_config().node_rank == 0 and self.waas and self.waas.connection:
                self.waas.attribute_separator(operator_waas_list[0])
                waas_data = self.waas.convert_level_db_format(operator_waas_list)
                insert_key = waas_data['key']
                insert_value = waas_data['value']
                self.waas.insert_data(insert_key, insert_value)
            else:
                self.db.operator_history_dao.insert_history(operator_history_list)
            self._logger.info(tplt.format(
                self.origin_profile_data_list.data_list[index].config_info.tp,
                self.origin_profile_data_list.data_list[index].config_info.dp,
                self.origin_profile_data_list.data_list[index].config_info.pp,
                self.origin_profile_data_list.data_list[index].config_info.cp,
                self.origin_profile_data_list.data_list[index].config_info.ep,
                int(duration_sum), len(operator_note.fw), len(operator_note.bw),
                int(self.origin_profile_data_list.layer_time[index][0]),
                int(self.origin_profile_data_list.layer_time[index][1]), chr(12288)))

    def get_operator_model(self, operator_note_dict):
        operator_list = self.base_block.fw + self.base_block.bw
        self.get_operator_model_dao(operator_list, operator_note_dict)
        self.base_block.exist_cal_list = self.get_dict_base_shape(operator_list, operator_note_dict)

        operator_list = self.cp_block.fw + self.cp_block.bw + self.cp_block.re
        self.get_operator_model_dao(operator_list, operator_note_dict)
        self.cp_block.exist_cal_list = self.get_dict_base_shape(operator_list, operator_note_dict)

        operator_list = self.cp_block.diff_list.fw + self.cp_block.diff_list.bw + self.cp_block.diff_list.re
        self.get_operator_model_dao(operator_list, operator_note_dict)
        self.cp_block.diff_cal_list = self.get_dict_base_shape(operator_list, operator_note_dict)

        if not self.model_config.moe_grouped_gemm:
            operator_list = self.ep_block.fw + self.ep_block.bw + self.ep_block.re
            self.get_operator_model_dao(operator_list, operator_note_dict)
            self.ep_block.exist_cal_list = self.get_dict_base_shape(operator_list, operator_note_dict)

            operator_list = self.ep_block.diff_list.fw + self.ep_block.diff_list.bw + self.ep_block.diff_list.re
            self.get_operator_model_dao(operator_list, operator_note_dict)
            self.ep_block.diff_cal_list = self.get_dict_base_shape(operator_list, operator_note_dict)
        else:
            self.ep_block.exist_cal_list = []
            self.ep_block.diff_cal_list = []
        # 基于 operator_model_dao 给base添加上shape

    def get_dict_base_shape(self, operator_list, operator_note_dict):
        re_list = []
        for operator in operator_list:
            index_name = operator.index_name
            # cp 1  tp 1 2 4 8  -> shape_tp
            # cp 2  tp 1 2 4 8  -> shape_tp
            # shape_cp
            # shape 建模, 通过收集不同tp 之间profiler的算子的变化规律，推理出算子shape每个位置的变化公式
            results = operator_note_dict[index_name]

            input_shape_cal, output_shape_cal = separate_ep_tp_cp(results)

            dict_shape = DictCalShape()
            dict_shape.name = operator.name
            dict_shape.index_name = index_name
            dict_shape.accelerator_core = operator.accelerator_core
            dict_shape.types = operator.type
            dict_shape.input_cal = json.dumps(input_shape_cal)
            dict_shape.output_cal = json.dumps(output_shape_cal)
            re_list.append(dict_shape)
        return re_list

    def get_operator_model_dao(self, operator_list, operator_note_dict):
        for operator in operator_list:
            index_name = operator.index_name
            # cp 1  tp 1 2 4 8  -> shape_tp
            # cp 2  tp 1 2 4 8  -> shape_tp
            # shape_cp
            # shape 建模, 通过收集不同tp 之间profiler的算子的变化规律，推理出算子shape每个位置的变化公式
            results = operator_note_dict[index_name]
            # input_shape_cal ，格式和shape数组一致，有变化位置为正数，代表不变位，负数代表变化位，假设该数为num，变化规律为 -num/tp

            # duration 基于相同位置算子 和 tp之间的建模，对于shape变化的算子，初步观察到 随着tp增大[2,4,8], duration 以约2倍的规律下降
            # tp_model_w 为下降时计算的数，理论上上是 tp=1 时算子的duration, 所以tp = 2时， duration(2) = tp_model_w/2; tp_model_b为冗余系数
            tp_model_w, tp_model_b = model_operator_with_tp(results)

            # duration 基于shape计算的Flops 之间的建模i，对于所有算子，F(duration) = shape_model_w * Flops + shape_model_b
            if get_system_config().waas_enabled and get_system_config().node_rank == 0 and self.waas and self.waas.connection:
                history_results = self.waas.restore_all_data(operator)
            else:
                history_results = self.db.operator_history_dao.get_by_types_and_accelerator_core(
                    operator.accelerator_core, operator.type)
            shape_model_w, shape_model_b = model_operator_with_shape(history_results)
            dict_shape = {
                'index_name': index_name,
                'accelerator_core': operator.accelerator_core,
                'model_w': float(tp_model_w),
                'model_b': float(tp_model_b),
                'shape_model_w': shape_model_w,
                'shape_model_b': shape_model_b,
            }
            accelerator_core_exist = False
            if dict_shape["index_name"] in self.dict_model.keys():
                for dict_temp in self.dict_model[dict_shape["index_name"]]:
                    if dict_temp['accelerator_core'] == dict_shape['accelerator_core']:
                        accelerator_core_exist = True
                        break
                if not accelerator_core_exist:
                    self.dict_model[dict_shape["index_name"]].append(dict_shape)
            else:
                self.dict_model[dict_shape["index_name"]] = [dict_shape]

    def getmodel_by_accelerator_core_and_index_name(self, accelerator_core, index_name):
        for dict_shape in self.dict_model.get(index_name):
            if dict_shape['accelerator_core'] == accelerator_core:
                return dict_shape
        self._logger.info("can not find the accelerator_core!")
        return self.dict_model.get(index_name)[0]

    def cal_operator_timer_bymodel(self, operator_list, search_cfg: SearchConfig, ratio=0.3,
                                   re_profiling_flag=False):
        operator_list_re = []

        operator_total_num = len(operator_list)
        operator_not_found = []
        if search_cfg.seq_length > 32 * 1024:
            seq_ratio = search_cfg.seq_length / (32 * 1024)
        else:
            seq_ratio = 1
        for operator_base in operator_list:
            # 根据tp、cp、ep计算 input_shape 和 output_shape
            input_shape = cal_new_shape_tce(operator_base.input_cal, search_cfg, seq_ratio)
            output_shape = cal_new_shape_tce(operator_base.output_cal, search_cfg, seq_ratio)
            # 情况二， 根据 input_shape 和 types 在 operator_history 搜索 duration， 并且可以获得搜索结果，直接返回
            if get_system_config().waas_enabled and get_system_config().node_rank == 0 and self.waas and self.waas.connection:
                key = self.waas.merge_operator_cal(operator_base, input_shape)
                value = self.waas.get_data(key)
                if value:
                    dict_operator = self.waas.unmerge_get_attributes(key, value)
                    operators = self.waas.restore_attributes_to_operator(OperatorHistory(types='', accelerator_core='',
                                                                                         input_shape='',
                                                                                         output_shape='',
                                                                                         duration=0, device='', jit='',
                                                                                         cann='', driver='', dtype=''),
                                                                         dict_operator)
                    operators = [operators]
                else:
                    operators = []
            else:
                operators = self.db.operator_history_dao.get_by_types_and_input_shape(operator_base.types, input_shape)
            if len(operators) > 0:
                operator_list_re.append(Operator(name=operator_base.index_name, types=operator_base.types,
                                                 accelerator_core=operator_base.accelerator_core,
                                                 input_shape=input_shape,
                                                 output_shape=output_shape,
                                                 duration=operators[0].duration))

            # 情况三， 根据tp --- duration 建模结果进行预测结果
            else:
                operator_not_found.append([OperatorHistory(types=operator_base.types,
                                                           accelerator_core=operator_base.accelerator_core,
                                                           input_shape=input_shape,
                                                           output_shape=output_shape,
                                                           duration=0,
                                                           device=get_system_config().device_type,
                                                           jit=int(self.model_config.jit_compile),
                                                           cann=get_system_config().cann_version,
                                                           driver=get_system_config().driver_version,
                                                           dtype=self.model_config.dtype.value[0]),
                                           operator_base.index_name])
                operator_model = self.getmodel_by_accelerator_core_and_index_name(
                    operator_base.accelerator_core, operator_base.index_name
                )
                flops = cal_operator_flops(input_shape, output_shape,
                                           operator_base.types)

                duration = cal_operator_duration_with_shape(operator_model["shape_model_w"],
                                                            operator_model["shape_model_b"],
                                                            flops)
                operator_list_re.append(Operator(name=operator_base.index_name, types=operator_base.types,
                                                 accelerator_core=operator_base.accelerator_core,
                                                 input_shape=input_shape,
                                                 output_shape=output_shape,
                                                 duration=duration))

        operator_not_found_total_num = len(operator_not_found)
        if operator_not_found_total_num / operator_total_num > ratio and re_profiling_flag:
            return operator_list_re, operator_not_found

        else:
            # 如果算子数量缺少比例较低，默认通过线性拟合方式补充算子
            if re_profiling_flag:
                self._logger.info(
                    f'The total operator not found proportion is {operator_not_found_total_num / operator_total_num},'
                    f' there is no need for re profiling.')
            operator_linear = []
            for operator_cal_base in operator_not_found:
                operator_base, operator_index_name = operator_cal_base
                operator_model = self.getmodel_by_accelerator_core_and_index_name(
                    operator_base.accelerator_core, operator_index_name
                )
                flops = cal_operator_flops(operator_base.input_shape, operator_base.output_shape,
                                           operator_base.types)

                duration = cal_operator_duration_with_shape(operator_model["shape_model_w"],
                                                            operator_model["shape_model_b"],
                                                            flops)
                operator_linear.append(Operator(name=operator_index_name, types=operator_base.types,
                                                accelerator_core=operator_base.accelerator_core,
                                                input_shape=operator_base.input_shape,
                                                output_shape=operator_base.output_shape,
                                                duration=duration))
        return operator_list_re, operator_not_found

    def cal_operator_timer_layer(self, operator_list, block_list, search_cfg: SearchConfig):
        operator_fw_list = block_list.fw
        operator_bw_list = block_list.bw
        operator_re_list = block_list.re
        nums_fw_list = len(operator_fw_list)
        nums_bw_list = len(operator_bw_list)
        nums_re_list = len(operator_re_list)
        operator_fw_time = 0
        operator_bw_time = 0
        operator_re_time = 0
        if search_cfg.seq_length > 32 * 1024:
            seq_ratio = search_cfg.seq_length / (32 * 1024)
        else:
            seq_ratio = 1
        for operator in operator_list[:nums_fw_list]:
            input_shape = cal_new_shape_tce(operator.input_cal, search_cfg, seq_ratio)
            output_shape = cal_new_shape_tce(operator.output_cal, search_cfg, seq_ratio)

            operators = self.db.operator_history_dao.get_by_types_and_input_shape(operator.types, input_shape)
            if len(operators) > 0:
                operator_fw_time += operators[0].duration
            else:
                duration = self.cal_duration_by_model(operator, input_shape, output_shape)
                operator_fw_time += duration
        for operator in operator_list[nums_fw_list:nums_bw_list]:
            input_shape = cal_new_shape_tce(operator.input_cal, search_cfg, seq_ratio)
            output_shape = cal_new_shape_tce(operator.output_cal, search_cfg, seq_ratio)

            operators = self.db.operator_history_dao.get_by_types_and_input_shape(operator.types, input_shape)
            if len(operators) > 0:
                operator_bw_time += operators[0].duration
            else:
                duration = self.cal_duration_by_model(operator, input_shape, output_shape)
                operator_bw_time += duration
        if nums_re_list != 0:
            for operator in operator_list[nums_bw_list:nums_re_list]:
                input_shape = cal_new_shape_tce(operator.input_cal, search_cfg, seq_ratio)
                output_shape = cal_new_shape_tce(operator.output_cal, search_cfg, seq_ratio)

                operators = self.db.operator_history_dao.get_by_types_and_input_shape(operator.types, input_shape)
                if len(operators) > 0:
                    operator_re_time += operators[0].duration
                else:
                    duration = self.cal_duration_by_model(operator, input_shape, output_shape)
                    operator_re_time += duration
        return operator_fw_time, operator_bw_time

    def cal_duration_by_model(self, operator, input_shape, output_shape):
        operator_model = self.getmodel_by_accelerator_core_and_index_name(
            operator.accelerator_core, operator.index_name
        )
        flops = cal_operator_flops(input_shape, output_shape, operator.types)

        duration = cal_operator_duration_with_shape(operator_model["shape_model_w"],
                                                    operator_model["shape_model_b"],
                                                    flops)
        return duration

    def cal_operator_timer(self, search_cfg: SearchConfig) -> tuple:
        """
            对外接口二，根据tp 变化返回duration
        """
        # 获得模型一层所有的算子
        operator_not_found = []
        operator_layer_time = OperatorLayerTime()
        if len(self.base_block.fw) == 0:
            return [], [], [], 1, 1, 1
        operator_base_list = self.base_block.exist_cal_list
        operator_list, operator_not_found_list = self.cal_operator_timer_bymodel(operator_base_list,
                                                                                 search_cfg)
        operator_fw, operator_bw = self.cal_operator_timer_layer(operator_base_list,
                                                                 self.base_block,
                                                                 search_cfg)

        operator_not_found.extend(operator_not_found_list)
        operator_layer_time.base_operator.fw, operator_layer_time.base_operator.bw = operator_fw, operator_bw
        cp_operator_exist_list = self.cp_block.exist_cal_list
        cp_operator_diff_list = self.cp_block.diff_cal_list
        ep_operator_exist_list = self.ep_block.exist_cal_list
        ep_operator_diff_list = self.ep_block.diff_cal_list
        cp_exist_list, cp_exist_not_found_list = [], []
        if len(cp_operator_exist_list) > 0:
            cp_exist_list, cp_exist_not_found_list = self.cal_operator_timer_bymodel(
                cp_operator_exist_list,
                search_cfg)
            cp_operator_exist_fw, cp_operator_exist_bw = self.cal_operator_timer_layer(
                cp_operator_exist_list, self.cp_block, search_cfg
            )
            operator_layer_time.cp_exist.fw, operator_layer_time.cp_exist.bw = cp_operator_exist_fw, cp_operator_exist_bw
            if search_cfg.cp > 1:
                operator_not_found.extend(cp_exist_not_found_list)
        cp_diff_list, cp_diff_not_found_list = [], []
        if len(cp_operator_diff_list) > 0:
            cp_diff_list, cp_diff_not_found_list = self.cal_operator_timer_bymodel(cp_operator_diff_list,
                                                                                   search_cfg)
            cp_operator_diff_fw, cp_operator_diff_bw = self.cal_operator_timer_layer(
                cp_operator_diff_list, self.cp_block.diff_list, search_cfg
            )
            operator_layer_time.cp_diff.fw, operator_layer_time.cp_diff.bw = cp_operator_diff_fw, cp_operator_diff_bw
            if search_cfg.cp > 1:
                operator_not_found.extend(cp_diff_not_found_list)
        ep_exist_list, ep_exist_not_found_list = [], []
        if len(ep_operator_exist_list) > 0:
            ep_exist_list, ep_exist_not_found_list = self.cal_operator_timer_bymodel(
                ep_operator_exist_list, search_cfg
            )
            ep_operator_exist_fw, ep_operator_exist_bw = self.cal_operator_timer_layer(
                ep_operator_exist_list, self.ep_block, search_cfg
            )
            operator_layer_time.ep_exist.fw, operator_layer_time.ep_exist.bw = ep_operator_exist_fw, ep_operator_exist_bw
            if search_cfg.ep and search_cfg.ep > 1:
                operator_not_found.extend(ep_exist_not_found_list)
        ep_diff_list, ep_diff_not_found_list = [], []
        if len(ep_operator_diff_list) > 0:
            ep_diff_list, ep_diff_not_found_list = self.cal_operator_timer_bymodel(ep_operator_exist_list,
                                                                                   search_cfg)
            ep_operator_diff_fw, ep_operator_diff_bw = 0, 0
            operator_layer_time.ep_diff.fw, operator_layer_time.ep_diff.bw = ep_operator_diff_fw, ep_operator_diff_bw
            if search_cfg.ep and search_cfg.ep > 1:
                operator_not_found.extend(ep_diff_not_found_list)
        self.db.insert_not_found_list(operator_not_found)
        return (operator_list, cp_exist_list, cp_diff_list, ep_exist_list, ep_diff_list, operator_not_found,
                operator_layer_time)
