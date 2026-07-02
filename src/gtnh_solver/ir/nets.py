"""Net-topology helpers shared across the solver lanes.

Small, pure lookups over the input IR that were hand-rolled identically in the placement, router,
buildguide, solver, and system_io lanes: the port-direction map, a net's source/sink split, and
the one-placement-per-machine index. Kept here (not re-exported from ``ir``) so the lanes
deep-import them the way they deep-import ``ir.geometry`` - helpers stay off the contract's public
surface (see ``ir/__init__``).

Reading input port directions is *data plumbing*, not rule computation, so the validator may share
these too without giving up its independence (docs/ARCHITECTURE.md #4): it re-derives the RULES on
its own arithmetic, it does not re-invent how to read the same input data.
"""

from __future__ import annotations

from collections.abc import Sequence

from .enums import IODirection
from .input_ir import InputIR, MachineFaceRef, Net
from .output import Placement


def port_direction_map(problem: InputIR) -> dict[tuple[str, str], IODirection]:
    """``(machine_id, port_id) -> IODirection`` for every port in ``problem``."""
    return {(m.id, p.id): p.direction for m in problem.machines for p in m.faces.ports}


def net_sources_sinks(
    net: Net, port_dir: dict[tuple[str, str], IODirection]
) -> tuple[list[MachineFaceRef], list[MachineFaceRef]]:
    """Split ``net``'s endpoints into ``(sources, sinks)`` by port direction (OUTPUT, INPUT).

    ``port_dir`` is a :func:`port_direction_map`, passed in so a caller in a loop builds it once.
    Endpoint order within each list is preserved, and an endpoint whose direction is unknown
    (absent from ``port_dir``) falls into neither - exactly as the hand-rolled comprehensions did.
    Returns the endpoints themselves; a caller wanting machine ids maps ``e.machine_id`` over them.
    """
    sources: list[MachineFaceRef] = []
    sinks: list[MachineFaceRef] = []
    for endpoint in net.endpoints:
        direction = port_dir.get((endpoint.machine_id, endpoint.port_id))
        if direction is IODirection.OUTPUT:
            sources.append(endpoint)
        elif direction is IODirection.INPUT:
            sinks.append(endpoint)
    return sources, sinks


def placement_index(placements: Sequence[Placement]) -> dict[str, Placement]:
    """``machine_id -> its placement``, keeping the first when a machine id repeats (the
    one-placement-per-machine convention the routers and validator share)."""
    by_machine: dict[str, Placement] = {}
    for placement in placements:
        by_machine.setdefault(placement.machine_id, placement)
    return by_machine
