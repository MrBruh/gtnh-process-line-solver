"""Docking is slot-driven: a terminal may only sit outside a cell that can host its hatch.

Before this, ``_dock_faces`` walked every cell of the bounding box, so a route could dock against
a casing block GT would never let a hatch replace. These cover the three-level kind fallback
(``Machine.hatch_slots_for``), the exposure rule that keeps a hatch off an interior slot, and the
per-machine claim - the pool an input bus and an energy hatch compete for, which is a casing cell
for a multiblock and a face for a single block (``_grid.claim_key``).
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    HatchSlot,
    InputIR,
    IODirection,
    Machine,
    MachineFaceRef,
    Net,
    Port,
)
from gtnh_solver.ir.geometry import Cell
from gtnh_solver.ir.output import (
    LayoutResult,
    LayoutStatus,
    Route,
    Segment,
    Terminal,
)
from gtnh_solver.router import claims_by_machine, route, route_power
from gtnh_solver.router._grid import claim_key, dock_candidates
from gtnh_solver.validator import validate
from gtnh_solver.validator.report import ViolationCode
from tests._helpers import at, power_source

_REGION = CellBox(sx=12, sy=6, sz=12)


def _slot(x: int, y: int, z: int, *kinds: str) -> HatchSlot:
    return HatchSlot(offset=CellCoord(x=x, y=y, z=z), kinds=tuple(kinds))


def _multiblock(
    mid: str,
    ports: list[Port],
    slots: list[HatchSlot],
    *,
    size: int = 3,
    orientation: Facing = Facing.NORTH,
    eut: float = 0.0,
) -> Machine:
    """A ``size``-cubed multiblock whose casing accepts hatches exactly at ``slots``."""
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=[orientation],
        footprint=CellBox(sx=size, sy=size, sz=size),
        faces=FaceSpec(ports=ports),
        hatch_slots=tuple(slots),
        hatch_cells=len(slots),
        eut=eut,
    )


#: Where the synthetic 3x3x3 machines sit: clear of the region walls, so a face is refused for
#: being walled in by the machine's own body and never merely for falling out of bounds.
_ORIGIN = (2, 0, 2)


def _dock(machine: Machine, port_id: str, claimed: set[Cell] = frozenset()) -> list[Terminal]:  # type: ignore[assignment]
    placement = at(machine.id, *_ORIGIN, orientation=machine.orientation_options[0])
    return dock_candidates(port_id, placement, machine, set(), set(), _REGION, claimed)


# ------------------------------------------------------- Machine.hatch_slots_for, the three levels


def test_a_machine_with_no_recorded_slots_constrains_nothing() -> None:
    # 23 of 208 controllers, plus every single-block machine and every plan adapted without the
    # dataset. None means "unknown", and the caller must keep treating any body cell as dockable.
    m = _multiblock("m", [Port(id="p", commodity=Commodity.ITEM, direction=IODirection.INPUT)], [])
    assert m.hatch_slots_for("p") is None


def test_slots_naming_the_kind_are_the_only_candidates() -> None:
    # A Distillation Tower in miniature: its upper cells take an output hatch and nothing else, so
    # an INPUT bus may not go there. This is the constraint the whole lane exists to enforce.
    m = _multiblock(
        "m",
        [
            Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT),
            Port(id="out", commodity=Commodity.FLUID, direction=IODirection.OUTPUT),
        ],
        [_slot(0, 0, 1, "InputBus", "OutputHatch"), _slot(0, 2, 1, "OutputHatch")],
    )
    assert [s.offset.as_tuple() for s in m.hatch_slots_for("in") or ()] == [(0, 0, 1)]
    assert [s.offset.as_tuple() for s in m.hatch_slots_for("out") or ()] == [(0, 0, 1), (0, 2, 1)]


def test_a_kind_no_slot_names_falls_back_to_every_slot() -> None:
    # The load-bearing fallback. 61 of 185 controllers record no Energy-capable cell - the Chemical
    # Plant among them, which nitrobenzene must power - because a hatch adder built from a bare
    # method reference exposes no filter. Reading that silence as a prohibition would refuse to
    # power a machine that certainly takes power.
    m = _multiblock(
        "m",
        [Port(id="pwr", commodity=Commodity.POWER, direction=IODirection.INPUT)],
        [_slot(0, 0, 1, "InputBus"), _slot(0, 1, 1, "OutputHatch")],
    )
    assert m.hatch_slots_for("pwr") == m.hatch_slots


def test_an_unknown_port_is_permissive_too() -> None:
    m = _multiblock("m", [], [_slot(0, 0, 1, "InputBus")])
    assert m.hatch_kinds_for("nope") == ()
    assert m.hatch_slots_for("nope") == m.hatch_slots


@pytest.mark.parametrize(
    ("commodity", "direction", "kinds"),
    [
        (Commodity.ITEM, IODirection.INPUT, ("InputBus",)),
        (Commodity.ITEM, IODirection.OUTPUT, ("OutputBus",)),
        (Commodity.FLUID, IODirection.INPUT, ("InputHatch",)),
        (Commodity.FLUID, IODirection.OUTPUT, ("OutputHatch",)),
        (Commodity.POWER, IODirection.INPUT, ("Energy", "ExoticEnergy", "MultiAmpEnergy")),
        (Commodity.POWER, IODirection.OUTPUT, ("Dynamo",)),
    ],
)
def test_every_port_shape_maps_to_its_gt_hatch_element(
    commodity: Commodity, direction: IODirection, kinds: tuple[str, ...]
) -> None:
    # The bus/hatch split is lexical in GT and means items/fluids; power input has three spellings
    # because 34 controllers record only TecTech's ExoticEnergy.
    m = _multiblock("m", [Port(id="p", commodity=commodity, direction=direction)], [])
    assert m.hatch_kinds_for("p") == kinds


# --------------------------------------------------------------------- docking against the slots


def test_docking_only_offers_cells_outside_a_hatch_capable_slot() -> None:
    # One slot, mid-west-face. Its five other neighbours are all cells of the machine's own body,
    # so exactly one candidate survives - and none of the other 26 cells of the cube offers a face,
    # which is the whole change: docking used to walk every one of them.
    m = _multiblock(
        "m",
        [Port(id="in", commodity=Commodity.FLUID, direction=IODirection.INPUT)],
        [_slot(0, 1, 1, "InputHatch")],
    )
    assert [(t.face, t.cell.as_tuple()) for t in _dock(m, "in")] == [(Facing.WEST, (1, 1, 3))]


def test_an_interior_slot_offers_no_face_at_all() -> None:
    # 29% of dumped slots touch no bbox face. A hatch there would be walled inside the structure
    # and could reach nothing, so it must never become a dock candidate.
    m = _multiblock(
        "m",
        [Port(id="in", commodity=Commodity.FLUID, direction=IODirection.INPUT)],
        [_slot(1, 1, 1, "InputHatch")],
    )
    assert _dock(m, "in") == []


def test_a_claimed_casing_cell_is_not_offered_twice() -> None:
    # One casing cell is one block: a cell already holding a hatch cannot host a second one, not
    # even by facing the other way. A claim on the dock cell alone would miss that, since one
    # casing cell has up to five free faces.
    m = _multiblock(
        "m",
        [
            Port(id="a", commodity=Commodity.FLUID, direction=IODirection.INPUT),
            Port(id="b", commodity=Commodity.FLUID, direction=IODirection.INPUT),
        ],
        [_slot(0, 1, 1, "InputHatch"), _slot(2, 1, 1, "InputHatch")],
    )
    assert {t.cell.as_tuple() for t in _dock(m, "b")} == {(1, 1, 3), (5, 1, 3)}
    # ...and with the west casing cell spent, only the east one is left to offer a face.
    assert [t.cell.as_tuple() for t in _dock(m, "b", claimed={(2, 1, 3)})] == [(5, 1, 3)]


def test_claim_key_is_the_casing_cell_for_a_multiblock_and_the_face_for_a_single_block() -> None:
    multi = _multiblock(
        "m",
        [Port(id="in", commodity=Commodity.FLUID, direction=IODirection.INPUT)],
        [_slot(0, 1, 1, "InputHatch")],
    )
    (terminal,) = _dock(multi, "in")
    assert claim_key(terminal, multi) == (2, 1, 3)  # the casing block the hatch replaces

    single = Machine(
        id="s",
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)]
        ),
    )
    (west,) = [t for t in _dock(single, "in") if t.face is Facing.WEST]
    assert claim_key(west, single) == west.cell.as_tuple()  # the face, not its one body cell


# --------------------------------------------------- the pool items and power actually share


def test_two_energy_hatches_of_a_multiblock_take_two_casing_cells() -> None:
    # Two hatches on one casing cell would be one hatch charged twice, under a cable sized for a
    # draw no single hatch can take. Before the claim moved to the casing cell, two terminals on
    # two faces of one block looked distinct and passed.
    m = _multiblock(
        "m",
        [
            Port(
                id="power:in#1", commodity=Commodity.POWER, direction=IODirection.INPUT, rate=16.0
            ),
            Port(
                id="power:in#2", commodity=Commodity.POWER, direction=IODirection.INPUT, rate=16.0
            ),
        ],
        [_slot(0, 1, 1, "Energy"), _slot(2, 1, 1, "Energy")],
        eut=32.0,
    )
    problem = InputIR(
        bounding_region=_REGION,
        machines=[power_source(), m],
        nets=[
            Net(
                id="power:LV",
                commodity=Commodity.POWER,
                throughput=32.0,
                endpoints=[
                    MachineFaceRef(machine_id="src", port_id="power:out"),
                    MachineFaceRef(machine_id="m", port_id="power:in#1"),
                    MachineFaceRef(machine_id="m", port_id="power:in#2"),
                ],
            )
        ],
    )
    result = route_power(problem, [at("src", 8, 1, 3), at("m", *_ORIGIN)])

    assert result.ok, result.infeasibility
    (trunk,) = result.routes
    hatch_cells = {claim_key(t, m) for t in trunk.terminals if t.machine_id == "m"}
    assert hatch_cells == {(2, 1, 3), (4, 1, 3)}  # both recorded slots, one hatch each


def test_a_pipe_and_a_cable_do_not_share_one_casing_cell() -> None:
    # The pool is shared across commodities: a cell an input bus stands on cannot also hold an
    # energy hatch. The item/fluid router docks first and hands its claims to the power router.
    # A corner-ish cell with TWO exposed faces (west and down), so the control below is real:
    # without the claim, power simply takes the other face of the block the bus is standing on.
    only_cell = [_slot(0, 0, 1, "InputBus", "Energy")]
    m = _multiblock(
        "m",
        [
            Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT),
            Port(id="power:in", commodity=Commodity.POWER, direction=IODirection.INPUT),
        ],
        only_cell,
        eut=32.0,
    )
    feeder = Machine(
        id="feeder",
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)]
        ),
    )
    problem = InputIR(
        bounding_region=_REGION,
        machines=[power_source(), feeder, m],
        nets=[
            Net(
                id="items",
                commodity=Commodity.ITEM,
                fluid_or_item="x",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="feeder", port_id="out"),
                    MachineFaceRef(machine_id="m", port_id="in"),
                ],
            ),
            Net(
                id="power:LV",
                commodity=Commodity.POWER,
                throughput=32.0,
                endpoints=[
                    MachineFaceRef(machine_id="src", port_id="power:out"),
                    MachineFaceRef(machine_id="m", port_id="power:in"),
                ],
            ),
        ],
    )
    placements = [at("src", 8, 1, 3), at("feeder", 0, 1, 3), at("m", 2, 1, 2)]

    items = route(problem, placements)
    assert items.ok, items.infeasibility
    claims = claims_by_machine(items.routes, {mm.id: mm for mm in problem.machines})
    assert claims["m"] == {(2, 1, 3)}  # the input bus took the machine's one hatch cell

    # That cell is now spent, so power has nowhere left to enter. Reported as an infeasibility,
    # not quietly stacked a second hatch onto the block the bus is standing on.
    power = route_power(
        problem,
        placements,
        extra_obstacles={c for r in items.routes for c in r.cells()},
        claimed_cells=claims,
    )
    assert not power.ok
    assert power.infeasibility is not None
    assert power.infeasibility.constraint == "face_reachability"

    # Without the claim it would have routed, which is exactly the bug: two hatches, one block.
    assert route_power(
        problem, placements, extra_obstacles={c for r in items.routes for c in r.cells()}
    ).ok


# ---------------------------------------------- the validator proves it, from its own geometry


def _layout(
    machine: Machine, terminals: list[Terminal], origin: tuple[int, int, int]
) -> tuple[InputIR, LayoutResult]:
    """A one-net layout whose route just links the given terminals along the z axis."""
    problem = InputIR(
        bounding_region=_REGION,
        machines=[machine],
        nets=[
            Net(
                id="n",
                commodity=Commodity.FLUID,
                fluid_or_item="x",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id=machine.id, port_id=t.port_id) for t in terminals
                ],
            )
        ],
    )
    cells = [t.cell for t in terminals]
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[at(machine.id, *origin)],
        routes=[
            Route(
                net_id="n",
                commodity=Commodity.FLUID,
                terminals=terminals,
                segments=[Segment(start=a, end=b, channel=0) for a, b in pairwise(cells)],
            )
        ],
    )
    return problem, layout


def _terminal(port_id: str, face: Facing, cell: tuple[int, int, int]) -> Terminal:
    return Terminal(
        machine_id="m", port_id=port_id, face=face, cell=CellCoord(x=cell[0], y=cell[1], z=cell[2])
    )


def _codes(problem: InputIR, layout: LayoutResult) -> set[ViolationCode]:
    return {v.code for v in validate(problem, layout).violations}


def test_validator_rejects_a_terminal_docked_against_a_cell_that_hosts_no_hatch() -> None:
    # The casing cell behind the terminal is a plain casing block: GT will not let a hatch replace
    # it, so the layout describes a structure that cannot be built. The router cannot dock here any
    # more, which is precisely why the gate must be able to say so on its own.
    m = _multiblock(
        "m",
        [Port(id="in", commodity=Commodity.FLUID, direction=IODirection.INPUT)],
        [_slot(0, 1, 1, "InputHatch")],
    )
    good = _terminal("in", Facing.WEST, (1, 1, 3))  # behind it: (2, 1, 3), the recorded slot
    bad = _terminal("in", Facing.WEST, (1, 2, 3))  # behind it: (2, 2, 3), plain casing
    assert ViolationCode.TERMINAL_NOT_ON_HATCH_CELL not in _codes(*_layout(m, [good], _ORIGIN))
    assert ViolationCode.TERMINAL_NOT_ON_HATCH_CELL in _codes(*_layout(m, [bad], _ORIGIN))


def test_validator_rejects_a_terminal_on_a_slot_that_refuses_its_kind() -> None:
    # A Distillation Tower in miniature: the upper cell takes an output hatch and nothing else. The
    # machine DOES record an InputHatch cell elsewhere, so the silence-is-permission fallback does
    # not apply and this is a real refusal.
    m = _multiblock(
        "m",
        [Port(id="in", commodity=Commodity.FLUID, direction=IODirection.INPUT)],
        [_slot(0, 1, 1, "InputHatch"), _slot(0, 2, 1, "OutputHatch")],
    )
    on_output_only = _terminal("in", Facing.WEST, (1, 2, 3))
    assert ViolationCode.TERMINAL_NOT_ON_HATCH_CELL in _codes(
        *_layout(m, [on_output_only], _ORIGIN)
    )


def test_validator_stays_permissive_where_no_slot_names_the_kind() -> None:
    # The Chemical Plant case: 61 of 185 controllers record no Energy-capable cell because the
    # adder exposes no filter. Enforcing the absence would refuse to power a machine that plainly
    # takes power, so an unnamed kind is accepted on any recorded slot.
    m = _multiblock(
        "m",
        [Port(id="in", commodity=Commodity.FLUID, direction=IODirection.INPUT)],
        [_slot(0, 1, 1, "OutputHatch"), _slot(0, 2, 1, "OutputHatch")],
    )
    anywhere = _terminal("in", Facing.WEST, (1, 2, 3))
    assert ViolationCode.TERMINAL_NOT_ON_HATCH_CELL not in _codes(*_layout(m, [anywhere], _ORIGIN))


def test_validator_ignores_a_machine_that_records_no_slots() -> None:
    # A single block, or one the dump knows nothing about. It has no hatches: its faces are its
    # own, and input on one face with output on another is exactly how GT works.
    single = Machine(
        id="m",
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="in", commodity=Commodity.FLUID, direction=IODirection.INPUT)]
        ),
    )
    anywhere = _terminal("in", Facing.WEST, (1, 0, 2))
    codes = _codes(*_layout(single, [anywhere], (2, 0, 2)))
    assert ViolationCode.TERMINAL_NOT_ON_HATCH_CELL not in codes
    assert ViolationCode.TERMINAL_HATCH_CONTENTION not in codes


def test_validator_rejects_two_connections_wanting_the_same_casing_cell() -> None:
    # The collision a claim on the DOCK cell cannot see: two terminals one cell apart, on two
    # different faces of the same block. Both would have to be that one hatch.
    m = _multiblock(
        "m",
        [
            Port(id="a", commodity=Commodity.FLUID, direction=IODirection.INPUT),
            Port(id="b", commodity=Commodity.FLUID, direction=IODirection.INPUT),
        ],
        [_slot(0, 0, 1, "InputHatch")],
    )
    west = _terminal("a", Facing.WEST, (1, 1, 3))  # behind it: (2, 1, 3)
    down = _terminal("b", Facing.DOWN, (2, 0, 3))  # behind it: (2, 1, 3) - the same block
    problem, layout = _layout(m, [west, down], (2, 1, 2))
    assert ViolationCode.TERMINAL_HATCH_CONTENTION in _codes(problem, layout)
