"""Tests for the Phase 1 crude router.

Headline: the real sand line now goes export -> place -> route -> validator.ok, the whole
thin slice end to end. The rest are synthetic cases for the routing/docking branches and the
never-silently-invalid promise (incompleteness is always an explicit infeasibility).
"""

from __future__ import annotations

from pathlib import Path

from gtnh_solver.adapter import adapt_file
from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    InputIR,
    IODirection,
    LayoutResult,
    LayoutStatus,
    Machine,
    MachineFaceRef,
    METoggles,
    Net,
    Placement,
    Port,
)
from gtnh_solver.placement import place
from gtnh_solver.router import route, route_power
from gtnh_solver.validator import validate
from gtnh_solver.validator.report import ViolationCode

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_SAND = _EXAMPLES / "gtnh-sand.json"
_NITROBENZENE = _EXAMPLES / "gtnh-nitrobenzene.json"

_MALFORMED_ROUTE_CODES = {
    ViolationCode.ROUTE_OUT_OF_BOUNDS,
    ViolationCode.ROUTE_DISCONTINUOUS,
    ViolationCode.ROUTE_COMMODITY_MISMATCH,
    ViolationCode.MISSING_TERMINAL,
    ViolationCode.TERMINAL_ON_FRONT_FACE,
    ViolationCode.TERMINAL_NOT_ADJACENT,
    ViolationCode.TERMINAL_NOT_ON_ROUTE,
}


def _machine(mid: str, ports: list[Port], *, orientation: Facing = Facing.NORTH) -> Machine:
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=[orientation],
        faces=FaceSpec(ports=ports),
    )


def _item_pair(region: CellBox) -> InputIR:
    a = _machine("a", [Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)])
    b = _machine("b", [Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)])
    net = Net(
        id="n",
        commodity=Commodity.ITEM,
        fluid_or_item="x",
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id="a", port_id="out"),
            MachineFaceRef(machine_id="b", port_id="in"),
        ],
    )
    return InputIR(bounding_region=region, machines=[a, b], nets=[net])


def _at(mid: str, x: int, y: int, z: int) -> Placement:
    return Placement(machine_id=mid, cell=CellCoord(x=x, y=y, z=z), orientation=Facing.NORTH)


# --------------------------------------------------------------- real fixtures


def test_route_sand_full_slice_validates() -> None:
    # The generic router handles item/fluid; the power router handles power. Together they
    # cover every net of the sand line, and the combined layout validates.
    ir = adapt_file(_SAND)
    pr = place(ir)
    rr = route(ir, pr.placements)
    pwr = route_power(ir, pr.placements)
    assert rr.ok
    assert pwr.ok
    assert all(r.commodity is not Commodity.POWER for r in rr.routes)  # power is not its job
    assert all(r.commodity is Commodity.POWER for r in pwr.routes)
    assert len(rr.routes) + len(pwr.routes) == len(ir.nets)
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=list(pr.placements),
        routes=[*rr.routes, *pwr.routes],
    )
    assert validate(ir, layout).ok, str(validate(ir, layout))


def test_route_emits_only_valid_routes_even_when_incomplete() -> None:
    # nitrobenzene's many-port multiblocks overflow crude 1x1x1 faces, so routing is
    # incomplete - but every route it DOES emit is sound, and incompleteness is explicit.
    ir = adapt_file(_NITROBENZENE)
    pr = place(ir)
    rr = route(ir, pr.placements)
    layout = LayoutResult(
        status=LayoutStatus.VALID, seed=0, placements=list(pr.placements), routes=list(rr.routes)
    )
    assert _MALFORMED_ROUTE_CODES.isdisjoint(validate(ir, layout).codes())
    if not rr.ok:
        assert rr.infeasibility is not None


# --------------------------------------------------------------- synthetic


def test_route_two_machines_ok_and_validates() -> None:
    problem = _item_pair(CellBox(sx=8, sy=4, sz=8))
    placements = [_at("a", 1, 0, 1), _at("b", 3, 0, 1)]
    result = route(problem, placements)
    assert result.ok
    assert len(result.routes) == 1
    layout = LayoutResult(
        status=LayoutStatus.VALID, seed=0, placements=placements, routes=list(result.routes)
    )
    assert validate(problem, layout).ok


def test_route_terminals_avoid_the_front_face() -> None:
    problem = _item_pair(CellBox(sx=8, sy=4, sz=8))
    result = route(problem, [_at("a", 1, 0, 1), _at("b", 3, 0, 1)])
    faces = [t.face for r in result.routes for t in r.terminals]
    assert faces  # there are terminals
    assert all(face is not Facing.NORTH for face in faces)  # north is the front (orientation)


def test_route_skips_me_toggled_commodity() -> None:
    problem = _item_pair(CellBox(sx=8, sy=4, sz=8)).model_copy(
        update={"me_toggles": METoggles(items=True)}
    )
    result = route(problem, [_at("a", 1, 0, 1), _at("b", 3, 0, 1)])
    assert result.ok
    assert result.routes == ()  # the item net is ME-toggled, not physically routed


def test_route_infeasible_when_a_machine_cannot_dock() -> None:
    # 2x1x1: the two machines fill the region, leaving no free non-front face to dock.
    problem = _item_pair(CellBox(sx=2, sy=1, sz=1))
    result = route(problem, [_at("a", 0, 0, 0), _at("b", 1, 0, 0)])
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "face_reachability"


def test_route_infeasible_when_no_path_between_terminals() -> None:
    # A reserved wall at x=1 splits the single-layer region; terminals can't connect.
    problem = _item_pair(CellBox(sx=3, sy=1, sz=3)).model_copy(
        update={
            "reserved_cells": [
                CellCoord(x=1, y=0, z=0),
                CellCoord(x=1, y=0, z=1),
                CellCoord(x=1, y=0, z=2),
            ]
        }
    )
    result = route(problem, [_at("a", 0, 0, 0), _at("b", 2, 0, 0)])
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "routing"


def test_route_infeasible_when_endpoint_has_no_placement() -> None:
    problem = _item_pair(CellBox(sx=8, sy=4, sz=8))
    result = route(problem, [])  # nothing placed
    assert not result.ok
    assert result.infeasibility is not None


def test_route_skips_power_commodity() -> None:
    # The generic router no longer routes power - that is router.power's job (router.power).
    a = _machine("a", [Port(id="pa", commodity=Commodity.POWER, direction=IODirection.OUTPUT)])
    b = _machine("b", [Port(id="pb", commodity=Commodity.POWER, direction=IODirection.INPUT)])
    net = Net(
        id="p",
        commodity=Commodity.POWER,
        throughput=32.0,
        endpoints=[
            MachineFaceRef(machine_id="a", port_id="pa"),
            MachineFaceRef(machine_id="b", port_id="pb"),
        ],
    )
    problem = InputIR(bounding_region=CellBox(sx=8, sy=4, sz=8), machines=[a, b], nets=[net])
    result = route(problem, [_at("a", 1, 0, 1), _at("b", 3, 0, 1)])
    assert result.ok
    assert result.routes == ()  # the power net is left for the power router
