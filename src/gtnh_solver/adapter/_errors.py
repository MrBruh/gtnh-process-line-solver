"""The adapter's error type, in a leaf module so ``core`` and ``power`` can both import it at the
top level. ``power.synthesize_power`` needs to raise it and ``core`` drives the rest of the mapping;
keeping ``AdapterError`` here breaks the ``core`` <-> ``power`` import cycle that previously forced
``power`` to reach back into ``core`` with a function-local import.
"""

from __future__ import annotations


class AdapterError(ValueError):
    """An exported plan could not be mapped to the IR (dangling reference, bad kind, ...)."""


class AdapterWarning(UserWarning):
    """A recoverable adapter finding, e.g. a v2 export's ``resolved`` figures disagreeing with
    the recipe-derived synthesis beyond float tolerance (the resolved figures still win - #2)."""
