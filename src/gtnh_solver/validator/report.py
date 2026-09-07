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
    ROUTE_NET_NO_CONSUMER = "route_net_no_consumer"  # routed net has no INPUT endpoint (consumer)
    ROUTE_NET_MIXED_COMMODITY = "route_net_mixed_commodity"  # endpoints mix commodities on one net
    # geometry
    MACHINE_OUT_OF_BOUNDS = "machine_out_of_bounds"
    MACHINE_OVERLAP = "machine_overlap"
    MACHINE_ON_RESERVED = "machine_on_reserved"
    ROUTE_OUT_OF_BOUNDS = "route_out_of_bounds"
    ROUTE_DISCONTINUOUS = "route_discontinuous"
    ROUTE_SEGMENT_NOT_UNIT = "route_segment_not_unit"  # a segment is not a single unit hop
    ROUTE_THROUGH_MACHINE = "route_through_machine"  # a route cell sits inside a machine body
    ROUTE_ON_RESERVED = "route_on_reserved"  # a route cell sits on a reserved cell
    ROUTE_CELL_COLLISION = "route_cell_collision"  # one cell carries >1 route (single-channel cap)
    PINNED_IO_NOT_ON_ROUTE = "pinned_io_not_on_route"
    # terminals / required-I/O-face reachability
    MISSING_TERMINAL = "missing_terminal"
    TERMINAL_NOT_AN_ENDPOINT = "terminal_not_an_endpoint"  # terminal's (machine,port) not in net
    DUPLICATE_TERMINAL = "duplicate_terminal"  # >1 terminal for the same net endpoint
    TERMINAL_ON_FRONT_FACE = "terminal_on_front_face"
    TERMINAL_NOT_ADJACENT = "terminal_not_adjacent"
    TERMINAL_NOT_ON_ROUTE = "terminal_not_on_route"
    # auto-output connections (adjacent machines feeding each other, no pipe)
    AUTO_OUTPUT_ON_FRONT_FACE = "auto_output_on_front_face"
    AUTO_OUTPUT_NOT_ADJACENT = "auto_output_not_adjacent"
    DUPLICATE_AUTO_OUTPUT = "duplicate_auto_output"
    AUTO_OUTPUT_WRONG_ENDPOINTS = "auto_output_wrong_endpoints"  # not the net's out->in machines
    AUTO_OUTPUT_ILLEGAL_COMMODITY = "auto_output_illegal_commodity"  # power/ME can't auto-output
    # power (independent re-check of the shared-amperage primitive)
    POWER_THICKNESS_INVALID = (
        "power_thickness_invalid"  # missing/misaligned/illegal thickness value
    )
    POWER_THICKNESS_INSUFFICIENT = "power_thickness_insufficient"  # cable thinner than summed amps
    POWER_NET_NO_SINGLE_SOURCE = "power_net_no_single_source"  # zero or >1 source terminals
    POWER_ROUTE_NOT_A_TREE = "power_route_not_a_tree"  # cable graph has a cycle/disconnect
    POWER_VOLTAGE_DROP_EXCESSIVE = (
        "power_voltage_drop_excessive"  # cable loss leaves a machine <= 0 V: unpowerable at tier
    )
    POWER_TIER_UNKNOWN = (
        "power_tier_unknown"  # a machine's voltage tier is off the ladder: amperage unverifiable
    )
    POWER_FEED_NOT_ON_BOUNDARY = (
        "power_feed_not_on_boundary"  # a source's front (feed) face is not on the region boundary
    )
    HATCH_CELLS_EXCEEDED = (
        "hatch_cells_exceeded"  # more connections wired to a machine than its structure can host
    )
    POWER_SUPPLY_INSUFFICIENT = (
        "power_supply_insufficient"  # after cable loss, a machine's hatches cannot deliver its EU/t
    )
    # route material (the stand-in a route is drawn and costed as; docs/DOMAIN.md)
    ROUTE_MATERIAL_TIER_MISMATCH = (
        "route_material_tier_mismatch"  # published tier is not the tier its machines run at
    )
    ROUTE_MATERIAL_UNKNOWN = (
        "route_material_unknown"  # a material outside the sanctioned stand-in policy - invented
    )
    # placed hatches (LayoutResult v1): the record must describe a hatch that could actually exist
    HATCH_NOT_ON_MACHINE = "hatch_not_on_machine"  # its cell is not a body cell of its machine
    HATCH_FACES_INWARD = "hatch_faces_inward"  # it faces its own structure, so it moves nothing
    HATCH_CELL_COLLISION = "hatch_cell_collision"  # two hatches claim one casing cell
    HATCH_TERMINAL_MISMATCH = "hatch_terminal_mismatch"  # its port's terminal says somewhere else
    HATCH_UNKNOWN_PORT = "hatch_unknown_port"  # port_id names no port on that machine
    MUFFLER_BLOCKED = "muffler_blocked"  # no empty air in front of it: the machine shuts down
    MUFFLER_MISSING = "muffler_missing"  # a structure that accepts a muffler was given none
    # where a routed connection may attach: the casing cell behind its terminal must be able to
    # host that connection's hatch, and no two connections may want the same block
    TERMINAL_NOT_ON_HATCH_CELL = (
        "terminal_not_on_hatch_cell"  # the casing cell behind it hosts no hatch of that kind
    )
    TERMINAL_HATCH_CONTENTION = (
        "terminal_hatch_contention"  # two connections on one machine need the same block
    )


@dataclass(frozen=True)
class Violation:
    """One proven defect: a stable ``code`` plus a human-readable ``message``.

    ``machine_id`` names the machine the violation is about, when it is about one. The message
    already says so in prose, but prose is for a human; a consumer that has to *act* on a
    violation needs it structurally. Today that consumer is the solver's feedback loop, which
    maps a starved machine back to its power net and penalizes it (``solver/core``). Optional,
    so a violation that names no single machine (or whose consumers do not need one) leaves it
    ``None``.
    """

    code: ViolationCode
    message: str
    machine_id: str | None = None


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
