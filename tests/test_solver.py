"""Tests for the Phase 1 solver (place + auto-output + item/fluid + power route).

Headline: solving the real sand line yields a fully valid layout whose item chain **auto-feeds
with zero pipes** and whose synthesized power net is cabled as a shared-amperage trunk. Plus the
invariants: the result is always either VALID-and-validator-clean or
non-VALID-with-an-explicit-infeasibility.
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


def test_solve_sand_items_auto_feed_and_power_is_cabled() -> None:
    ir = adapt_file(_SAND)
    layout = solve(ir)
    assert layout.status is LayoutStatus.VALID
    assert validate(ir, layout).ok
    item_nets = [n for n in ir.nets if n.commodity is Commodity.ITEM]
    assert len(layout.auto_connections) == len(item_nets)  # every item net auto-feeds: zero pipes
    assert [r.commodity for r in layout.routes] == [
        Commodity.POWER
    ]  # only the power trunk is cabled


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


def _output_machine(mid: str) -> Machine:
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="o", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)]
        ),
    )


def test_solve_downgrades_when_assembled_layout_fails_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # place.ok && route.ok alone never proved the layout sound. With a buggy router that emits
    # a geometrically invalid route, solve() must run its own output through the independent
    # validator and downgrade VALID -> partial_invalid instead of passing it off as valid. (The
    # two output ports keep auto-output out of it, so the injected route is what gets validated.)
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_output_machine("a"), _output_machine("b")],
        nets=[
            Net(
                id="n",
                commodity=Commodity.ITEM,
                fluid_or_item="x",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="a", port_id="o"),
                    MachineFaceRef(machine_id="b", port_id="o"),
                ],
            )
        ],
    )
    teleport = Route(  # a single segment that jumps two cells - the validator must reject it
        net_id="n",
        commodity=Commodity.ITEM,
        segments=[Segment(start=CellCoord(x=0, y=0, z=0), end=CellCoord(x=0, y=0, z=2), channel=0)],
    )
    monkeypatch.setattr(solver_core, "route", lambda *a, **k: RouteResult(routes=(teleport,)))

    layout = solve(problem)
    assert layout.status is LayoutStatus.PARTIAL_INVALID
    assert layout.infeasibility is not None
    assert layout.infeasibility.constraint == "validation"
    assert validate(problem, layout).ok is False  # the bad route is preserved, not silently dropped
