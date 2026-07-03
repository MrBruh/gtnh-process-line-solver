"""The adapter's error type, in a leaf module so ``core`` and ``power`` can both import it at the
top level. ``power.synthesize_power`` needs to raise it and ``core`` drives the rest of the mapping;
keeping ``AdapterError`` here breaks the ``core`` <-> ``power`` import cycle that previously forced
``power`` to reach back into ``core`` with a function-local import.
"""

from __future__ import annotations


class AdapterError(ValueError):
    """An exported plan could not be mapped to the IR (dangling reference, bad kind, ...)."""
