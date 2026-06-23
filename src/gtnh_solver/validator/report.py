"""The validator's output: a list of violations, never an exception.

A layout that breaks a rule is *reported*, not raised - the solver and CLI decide what to
do (the worst failure class is a *silently*-invalid layout passed off as valid, so the
validator's job is to surface every problem it can prove). ``ValidationReport.ok`` is the
single independent verdict; it is computed from geometry/structure alone, regardless of the
``status`` the layout claims for itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ViolationCode(str, Enum):
    """Stable identifiers for the things a layout can get wrong (assert on these in tests)."""

    # completeness / referential integrity (layout vs problem)
    UNKNOWN_MACHINE = "unknown_machine"
    PLACEMENT_COUNT_MISMATCH = "placement_count_mismatch"
    BAD_ORIENTATION = "bad_orientation"
    UNKNOWN_NET = "unknown_net"
    DUPLICATE_ROUTE = "duplicate_route"
    ROUTE_COMMODITY_MISMATCH = "route_commodity_mismatch"
    MISSING_CONNECTION = "missing_connection"  # net has neither a route nor an auto-connection
    NET_DOUBLE_CONNECTED = "net_double_connected"  # both a route and an auto-connection
    UNEXPECTED_ME_ROUTE = "unexpected_me_route"
    # geometry
    MACHINE_OUT_OF_BOUNDS = "machine_out_of_bounds"
    MACHINE_OVERLAP = "machine_overlap"
    MACHINE_ON_RESERVED = "machine_on_reserved"
    ROUTE_OUT_OF_BOUNDS = "route_out_of_bounds"
    ROUTE_DISCONTINUOUS = "route_discontinuous"
    PINNED_IO_NOT_ON_ROUTE = "pinned_io_not_on_route"
    # terminals / required-I/O-face reachability
    MISSING_TERMINAL = "missing_terminal"
    TERMINAL_ON_FRONT_FACE = "terminal_on_front_face"
    TERMINAL_NOT_ADJACENT = "terminal_not_adjacent"
    TERMINAL_NOT_ON_ROUTE = "terminal_not_on_route"
    # auto-output connections (adjacent machines feeding each other, no pipe)
    AUTO_OUTPUT_ON_FRONT_FACE = "auto_output_on_front_face"
    AUTO_OUTPUT_NOT_ADJACENT = "auto_output_not_adjacent"
    DUPLICATE_AUTO_OUTPUT = "duplicate_auto_output"
    # power (independent re-check of the shared-amperage primitive's output shape)
    POWER_THICKNESS_INVALID = "power_thickness_invalid"


@dataclass(frozen=True)
class Violation:
    """One proven defect: a stable ``code`` plus a human-readable ``message``."""

    code: ViolationCode
    message: str


@dataclass(frozen=True)
class ValidationReport:
    """The result of validating one layout against its problem."""

    violations: tuple[Violation, ...] = ()

    @property
    def ok(self) -> bool:
        """True iff the layout is geometrically and structurally valid."""
        return not self.violations

    def codes(self) -> tuple[ViolationCode, ...]:
        """The violation codes in order (handy for tests / summaries)."""
        return tuple(v.code for v in self.violations)

    def __str__(self) -> str:
        if self.ok:
            return "ValidationReport(ok)"
        lines = "\n".join(f"  - {v.code.value}: {v.message}" for v in self.violations)
        return f"ValidationReport({len(self.violations)} violation(s)):\n{lines}"
