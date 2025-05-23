# Copyright (c) 2025, Huawei Technologies Co., Ltd. All rights reserved.
from argparse import ArgumentParser

from mindspeed.features_manager.feature import MindSpeedFeature


class BuildTokenizerFeature(MindSpeedFeature):

    def __init__(self):
        super().__init__('tokenizer-type', optimization_level=2)

    def register_args(self, parser: ArgumentParser):
        self.add_parser_argument_choices_value(
            parser,
            "--tokenizer-type",
            'PretrainedFromHF'
        )

        group = parser.add_argument_group(title=self.feature_name)
        group.add_argument("--tokenizer-name-or-path", type=str, default=None,
                           help="Name or path of the huggingface tokenizer.")
        group.add_argument("--tokenizer-not-use-fast", action='store_false',
                           help="HuggingFace tokenizer not use the fast version.")


    def register_patches(self, patch_manager, args):
        if args.tokenizer_type == "PretrainedFromHF":
            from mindspeed.tokenizer.build_tokenizer.adaptor import build_tokenizer_HF
            patch_manager.register_patch('megatron.training.tokenizer.tokenizer.build_tokenizer', build_tokenizer_HF)
