"""
    Utility functions
"""

from .anydict import AnyDict
from .error import ClippyInvalidSelectorError


def flat_dict_to_nested(input_dict: AnyDict) -> AnyDict:
    """input dictionary has dot-delineated keys which are then parsed as subkeys.
    That is: {'a.b.c': 5} becomes {'a': {'b': {'c': 5}}}
    """

    output_dict: AnyDict = {}
    for k, v in input_dict.items():
        # k is dotted
        if '.' not in k:
            raise ClippyInvalidSelectorError("cannot set top-level selectors")

        *path, last = k.split('.')
        curr_nest = output_dict
        for p in path:
            curr_nest = output_dict.setdefault(p, {})

        curr_nest[last] = v
    return output_dict
