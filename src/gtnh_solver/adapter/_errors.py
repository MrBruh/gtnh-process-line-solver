"""The adapter's error type, in a leaf module so ``core`` and ``power`` can both import it at the
top level. ``power.synthesize_power`` needs to raise it and ``core`` drives the rest of the mapping;
keeping ``AdapterError`` here breaks the ``core`` <-> ``power`` import cycle that previously forced
``power`` to reach back into ``core`` with a function-local import.
"""

from __future__ import annotations

from gtnh_solver.ir import Infeasibility


class AdapterError(ValueError):
    """An exported plan could not be mapped to the IR (dangling reference, bad kind, ...)."""


class InfeasiblePlanError(AdapterError):
    """The plan mapped cleanly, but describes a line this build cannot lay out as stated.

    The distinction the CLI keys on: "could not load the export" (exit 2) is a *plan* problem, a
    dangling reference, an unreadable file, a shape the mapping does not speak. This is the other
    kind: a plan that says exactly what it means, whose answer is that no layout exists (exit 1,
    the contract ``LayoutResult`` already carries). Both used to arrive as a bare
    ``AdapterError``, which reported an infeasible line as an unloadable file (#112).

    Carries the :class:`~gtnh_solver.ir.Infeasibility` the CLI prints, so an adapter-stage reason
    reaches the user in the same shape the solver's own reasons do. It subclasses
    ``AdapterError`` (hence ``ValueError``), so a caller that only knows the older contract still
    catches it.
    """

    def __init__(self, infeasibility: Infeasibility) -> None:
        super().__init__(infeasibility.detail)
        self.infeasibility = infeasibility


class AdapterWarning(UserWarning):
    """A recoverable adapter finding, e.g. a v2 export's ``resolved`` figures disagreeing with
    the recipe-derived synthesis beyond float tolerance (the resolved figures still win - #2)."""
