"""Define base class for mind speed features."""

import argparse
from argparse import ArgumentParser, Namespace

from mindspeed.patch_utils import MindSpeedPatchesManager


class MindSpeedFeature:
    """Base class for mindspeed features."""

    def __init__(self, feature_name: str, optimization_level: int = 2):
        self.feature_name = feature_name.lower().strip().replace('-', '_')
        self.optimization_level = optimization_level
        self.default_patches = self.optimization_level == 0

    def is_need_apply(self, args):
        """Check the feature is need to apply."""
        return (self.optimization_level <= args.optimization_level and getattr(args, self.feature_name, None)) \
            or self.default_patches

    def register_args(self, parser: ArgumentParser):
        """Register cli arguments to enable the feature."""
        pass

    def pre_validate_args(self, args: Namespace):
        """Validate the arguments of mindspeed before megatron args validation
        and store some arguments of the mindspeed temporarily,
        incase that megatron validate faile.
        for example:
            ```python
            origin_context_parallel_size = args.context_parallel_size
            args.context_parallel_size = 1
            ```
        """
        pass

    def validate_args(self, args: Namespace):
        """Restore the arguments of the mindspeed.

        for example:
        ```python
        args.context_parallel_size = origin_context_parallel_size
        ```
        """
        pass

    def post_validate_args(self, args: Namespace):
        """validate mindspeed arguments after megatron arguments validation."""
        pass

    def pre_register_patches(self, patch_manager: MindSpeedPatchesManager, args: Namespace):
        """Register all patch functions before import megatron"""
        pass

    def register_patches(self, patch_manager: MindSpeedPatchesManager, args: Namespace):
        """Register all patch functions the feature is related."""
        pass

    def incompatible_check(self, global_args, check_args):
        """Register all incompatible functions the feature is related."""
        if getattr(global_args, self.feature_name, None) and getattr(global_args, check_args, None):
            raise AssertionError('{} and {} are incompatible.'.format(self.feature_name, check_args))

    def dependency_check(self, global_args, check_args):
        """Register all dependency functions the feature is related."""
        if getattr(global_args, self.feature_name, None) and not getattr(global_args, check_args, None):
            raise AssertionError('{} requires {}.'.format(self.feature_name, check_args))

    @staticmethod
    def add_parser_argument_choices_value(parser, argument_name, new_choice):
        """Add a new choice value to the existing choices of a parser argument."""
        for action in parser._actions:
            exist_arg = isinstance(action, argparse.Action) and argument_name in action.option_strings
            if exist_arg and action.choices is not None and new_choice not in action.choices:
                action.choices.append(new_choice)
