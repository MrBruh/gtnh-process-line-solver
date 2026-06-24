"""Tests for the Phase 1 solver (place + auto-output + route).

Headline: solving the real sand line yields a fully valid layout that uses **auto-output and
zero pipes** - the row of adjacent machines just feed each other. Plus the invariants: the
result is always either VALID-and-validator-clean or non-VALID-with-an-explicit-infeasibility.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gtnh_solver.adapter import adapt_file
from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    InputIR,
    IODirection,
    LayoutStatus,
    Machine,
    MachineFaceRef,
    Net,
    Port,
    Route,
    Segment,
)
from gtnh_solver.router import RouteResult
from gtnh_solver.solver import core as solver_core
from gtnh_solver.solver import solve
from gtnh_solver.validator import validate

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_SAND = _EXAMPLES / "gtnh-sand.json"
_NITROBENZENE = _EXAMPLES / "gtnh-nitrobenzene.json"


def test_solve_sand_is_valid_and_all_auto_output() -> None:
    ir = adapt_file(_SAND)
    layout = solve(ir)
    assert layout.status is LayoutStatus.VALID
    assert validate(ir, layout).ok
    assert len(layout.auto_connections) == len(ir.nets)  # every net auto-feeds
    assert layout.routes == []  # no pipes needed for a straight chain


def test_solve_is_deterministic() -> None:
    ir = adapt_file(_SAND)
    assert solve(ir) == solve(ir)


def test_solve_returns_valid_or_explicit_infeasibility() -> None:
    ir = adapt_file(_NITROBENZENE)
    layout = solve(ir)
    if layout.status is LayoutStatus.VALID:
        assert validate(ir, layout).ok
    else:
        assert layout.infeasibility is not None  # incompleteness is never silent


def test_solve_infeasible_when_machines_do_not_fit() -> None:
    a = Machine(id="a", type="t", voltage_tier="LV", orientation_options=[Facing.NORTH])
    b = Machine(id="b", type="t", voltage_tier="LV", orientation_options=[Facing.NORTH])
    problem = InputIR(bounding_region=CellBox(sx=1, sy=1, sz=1), machines=[a, b], nets=[])
    layout = solve(problem)
    assert layout.status is LayoutStatus.INFEASIBLE
    assert layout.infeasibility is not None


def _producer(mid: str) -> Machine:
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)]
        ),
    )


def _consumer(mid: str) -> Machine:
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)]
        ),
    )


def _net(nid: str, src: str, dst: str) -> Net:
    return Net(
        id=nid,
        commodity=Commodity.ITEM,
        fluid_or_item="x",
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id=src, port_id="out"),
            MachineFaceRef(machine_id=dst, port_id="in"),
        ],
    )


def test_solve_fork_auto_outputs_one_and_pipes_the_other() -> None:
    # m1 feeds both m2 and m3; its single auto-output covers one, the other is piped.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_producer("m1"), _consumer("m2"), _consumer("m3")],
        nets=[_net("n1", "m1", "m2"), _net("n2", "m1", "m3")],
    )
    layout = solve(problem)
    assert layout.status is LayoutStatus.VALID
    assert validate(problem, layout).ok
    assert len(layout.auto_connections) == 1  # one auto-output...
    assert len(layout.routes) == 1  # ...and the rest piped


def test_solve_downgrades_when_assembled_layout_fails_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # place.ok && route.ok alone never proved the layout sound. With a buggy router that emits
    # a geometrically invalid route, solve() must run its own output through the independent
    # validator and downgrade VALID -> partial_invalid instead of passing it off as valid.
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=4, sz=4),
        machines=[
            Machine(
                id="mp",
                type="t",
                voltage_tier="LV",
                orientation_options=[Facing.NORTH],
                faces=FaceSpec(
                    ports=[Port(id="pwr", commodity=Commodity.POWER, direction=IODirection.INPUT)]
                ),
            )
        ],
        nets=[
            Net(
                id="np",
                commodity=Commodity.POWER,
                throughput=32.0,
                endpoints=[MachineFaceRef(machine_id="mp", port_id="pwr")],
            )
        ],
    )
    teleport = Route(  # a single segment that jumps two cells - the validator must reject it
        net_id="np",
        commodity=Commodity.POWER,
        segments=[Segment(start=CellCoord(x=0, y=0, z=1), end=CellCoord(x=0, y=0, z=3), channel=0)],
        thickness_per_segment=[8],
    )
    monkeypatch.setattr(solver_core, "route", lambda *a, **k: RouteResult(routes=(teleport,)))

    layout = solve(problem)
    assert layout.status is LayoutStatus.PARTIAL_INVALID
    assert layout.infeasibility is not None
    assert layout.infeasibility.constraint == "validation"
    assert validate(problem, layout).ok is False  # the bad route is preserved, not silently dropped
