"""Tests for the Phase 1 solver (place + auto-output + route).

Headline: solving the real sand line yields a fully valid layout that uses **auto-output and
zero pipes** - the row of adjacent machines just feed each other. Plus the invariants: the
result is always either VALID-and-validator-clean or non-VALID-with-an-explicit-infeasibility.
"""

from __future__ import annotations

from pathlib import Path

from gtnh_solver.adapter import adapt_file
from gtnh_solver.ir import (
    CellBox,
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
)
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
