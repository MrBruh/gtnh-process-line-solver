"""What a plan says a multiblock's tiered parts must be built from.

A recipe's **special value** (GT's ``mSpecialValue``) is a requirement each machine reads its own
way; the Chemical Plant reads it as the lowest solid casing tier the recipe runs on. Exporters
state it in up to three places, none of them on every plan::

    recipe.specialValue                arodoid
    recipe.metadata.specialValue       arodoid
    recipe.nei.additionalInfo          both forks: "Special value: 4" (the only place MrBruh's does)

:func:`recipe_special_value` reads all three. The rule data it feeds lives in
``dataset/structure_blocks.py``.
"""

from __future__ import annotations

import re
import warnings

from ._errors import AdapterWarning
from .plan import Recipe

#: How NEI states a recipe's special value, the pattern gtnh-factory-flow's own reader matches
#: (``getRecipeSpecialValue``).
_SPECIAL_VALUE_LINE = re.compile(r"special\s+value\s*:\s*(-?\d+)", re.IGNORECASE)


def recipe_special_value(recipe: Recipe) -> int | None:
    """The special value ``recipe`` states, or ``None`` when it states none.

    Every place it is stated counts, so two that disagree are not settled by which is read first:
    that warns and takes the highest, since a machine built for the higher requirement also runs
    the lower. A negative value is read as 0, the lowest requirement GT's comparison can mean.
    """
    stated: list[int] = []
    if recipe.special_value is not None:
        stated.append(recipe.special_value)
    if recipe.metadata is not None and recipe.metadata.special_value is not None:
        stated.append(recipe.metadata.special_value)
    if recipe.nei is not None:
        for line in recipe.nei.additional_info or ():
            match = _SPECIAL_VALUE_LINE.search(line)
            if match is not None:
                stated.append(int(match.group(1)))
    if not stated:
        return None
    if len(set(stated)) > 1:
        warnings.warn(
            f"recipe {recipe.id!r} states special values {sorted(set(stated))}; building for the "
            f"highest, {max(stated)}",
            AdapterWarning,
            stacklevel=2,
        )
    return max(0, max(stated))
