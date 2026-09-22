"""A power dock the pipes used to wall in routes in a single pass (#226, #164).

Power used to route after the pipes with one dock cell held per energy port
(``reserve_power_docks``), and a held cell is not a held path: a pipe could detour around it and seal
it into a pocket of one. #226 answered that with a power-first recovery pass. The router now
negotiates each power net as a tree alongside the pipes, so the pipes leave its cable room in the
first place, and the recovery (which never rescued an attempt once that landed) is gone. These
tests keep the placement that recovery was built for, and pin that it now routes VALID in one pass.

The repro was reduced by delta-debugging from ``examples/ev-nitrobenzene.json`` against the 2.9
dump. There, every ``solve`` seed from 0 to 7 returned the same placement (the only attempt the
crowding gate lets through), and on it ``power:MV`` could not reach the second of three Distillation
Towers. The geometry below keeps the five machines that failure needs and pulls them together into
a 7x4x8 region. The towers are built by hand from ``MTEDistillationTower``'s structure (``b~b``
base layer, ``lll`` layers above), with the hatch kinds the dump records for those cells, so no
dumper output is involved.

What went wrong under the old router, in order:

1. ``reserve_power_docks`` held one dock cell per power endpoint. For tower #2 that was the cell
   south of its bottom ring, since its energy-capable cells are all on the bottom layer, whose
   down faces point out of the region.
2. The water net docks every tower's input hatch on that same bottom ring, and its pipe ran
   around the ring at ``y=0``. It routed *around* the reserved cell, so the cell stayed free, but
   the pipe enclosed it on every side the region and the tower leave open.
3. Power routed last with every pipe cell as a hard obstacle, and found the free dock cell
   unreachable: ``no free cell path to a dock face``.

The layer cells are deliberately left as the dump records them. In game they also take an energy
hatch (the ``l`` element chains ``addEnergyInputToMachineList`` as a bare adder, which the
extractor cannot see), and allowing that would make this layout easier to route. That is a separate
data fix (#227) and would not touch this test, which is about routing order.
"""

from __future__ import annotations

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
    LayoutStatus,
    Machine,
    MachineFaceRef,
    Net,
    Placement,
    Port,
)
from gtnh_solver.router import route, route_power
from gtnh_solver.solver import core as solver_core
from gtnh_solver.validator import validate
from tests._helpers import at, power_source

_FACINGS = [Facing.NORTH, Facing.SOUTH, Facing.EAST, Facing.WEST]

#: The bottom layer's hatch cells: every base cell but the controller at (1, 0, 0).
_BASE_KINDS = ("Energy", "InputBus", "InputHatch", "Maintenance", "OutputBus")
#: The layer rings above it, as the dump records them (output hatches only).
_LAYER_KINDS = ("OutputHatch",)


def _tower_slots() -> tuple[HatchSlot, ...]:
    """A 3x3x3 Distillation Tower's 24 hatch cells: 8 on the base, a ring of 8 on each layer."""
    slots = [
        HatchSlot(offset=CellCoord(x=x, y=0, z=z), kinds=_BASE_KINDS)
        for x in range(3)
        for z in range(3)
        if (x, z) != (1, 0)
    ]
    slots += [
        HatchSlot(offset=CellCoord(x=x, y=y, z=z), kinds=_LAYER_KINDS)
        for y in (1, 2)
        for x in range(3)
        for z in range(3)
        if (x, z) != (1, 1)
    ]
    return tuple(slots)


def _tower(mid: str, *, powered: bool) -> Machine:
    ports = [
        Port(id="input:water", commodity=Commodity.FLUID, direction=IODirection.INPUT, rate=31.25)
    ]
    if powered:
        ports.append(
            Port(
                id="power:in",
                commodity=Commodity.POWER,
                direction=IODirection.INPUT,
                rate=120.0,
                max_amps=2.0,
            )
        )
    return Machine(
        id=mid,
        type="Distillation Tower",
        footprint=CellBox(sx=3, sy=3, sz=3),
        faces=FaceSpec(ports=ports),
        voltage_tier="MV",
        orientation_options=_FACINGS,
        eut=120.0,
        hatch_cells=24,
        hatch_slots=_tower_slots(),
    )


def _problem() -> tuple[InputIR, tuple[Placement, ...]]:
    tank = Machine(
        id="tank",
        type="Super Tank",
        faces=FaceSpec(
            ports=[
                Port(
                    id="output:water",
                    commodity=Commodity.FLUID,
                    direction=IODirection.OUTPUT,
                    rate=93.75,
                )
            ]
        ),
        voltage_tier="LV",
        orientation_options=_FACINGS,
    )
    source = Machine(
        id="power-source:MV",
        type="Power Source (MV)",
        faces=FaceSpec(
            ports=[Port(id="power:out", commodity=Commodity.POWER, direction=IODirection.OUTPUT)]
        ),
        voltage_tier="MV",
        orientation_options=_FACINGS,
    )
    towers = [
        _tower("dt#1", powered=True),
        _tower("dt#2", powered=True),
        _tower("dt#3", powered=False),
    ]
    water = Net(
        id="water",
        commodity=Commodity.FLUID,
        fluid_or_item="water",
        throughput=93.75,
        endpoints=[
            MachineFaceRef(machine_id="tank", port_id="output:water"),
            *(MachineFaceRef(machine_id=t.id, port_id="input:water") for t in towers),
        ],
    )
    power = Net(
        id="power:MV",
        commodity=Commodity.POWER,
        throughput=240.0,
        endpoints=[
            MachineFaceRef(machine_id="power-source:MV", port_id="power:out"),
            MachineFaceRef(machine_id="dt#1", port_id="power:in"),
            MachineFaceRef(machine_id="dt#2", port_id="power:in"),
        ],
    )
    problem = InputIR(
        bounding_region=CellBox(sx=7, sy=4, sz=8),
        machines=[tank, *towers, source],
        nets=[water, power],
    )

    placements = (
        at("tank", 4, 1, 5),
        at("dt#1", 1, 0, 1),
        at("dt#2", 1, 0, 4),
        at("dt#3", 4, 0, 2),
        at("power-source:MV", 0, 1, 3, orientation=Facing.WEST),
    )
    return problem, placements


def _walled_in() -> tuple[InputIR, tuple[Placement, ...]]:
    """A powered block boxed in by three other machines, the floor and the ceiling.

    Its front faces the north wall and carries no I/O, and its other five faces all touch a
    machine body or leave the region, so no cable can dock on it at all, pipes or no pipes.
    """
    blocks = [
        Machine(id=mid, type="t", voltage_tier="LV", orientation_options=[Facing.NORTH])
        for mid in ("west", "east", "south")
    ]
    sink = Machine(
        id="m",
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        eut=16.0,
        faces=FaceSpec(
            ports=[Port(id="power:in", commodity=Commodity.POWER, direction=IODirection.INPUT)]
        ),
    )
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=1, sz=3),
        machines=[*blocks, sink, power_source("src", orientations=[Facing.WEST])],
        nets=[
            Net(
                id="power:LV",
                commodity=Commodity.POWER,
                throughput=16.0,
                endpoints=[
                    MachineFaceRef(machine_id="src", port_id="power:out"),
                    MachineFaceRef(machine_id="m", port_id="power:in"),
                ],
            )
        ],
    )
    placements = (
        at("west", 0, 0, 0),
        at("m", 1, 0, 0),
        at("east", 2, 0, 0),
        at("south", 1, 0, 1),
        at("src", 0, 0, 2, orientation=Facing.WEST),
    )
    return problem, placements


def test_power_alone_routes_on_the_repro_placement() -> None:
    # The placement is not the problem: with no pipes laid, the trunk reaches both towers.
    problem, placements = _problem()
    assert route_power(problem, placements).ok


def test_the_pipes_leave_the_walled_in_dock_its_cable_in_one_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The failure #226 was filed for, now absent: the water pipe routes around the room the cable
    # needs because the power net is negotiated with it, so one routing lays everything.
    problem, placements = _problem()
    calls = {"route": 0}
    real_route = route

    def counting_route(*args: object, **kwargs: object) -> object:
        calls["route"] += 1
        return real_route(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(solver_core, "route", counting_route)
    layout, failed = solver_core._assemble(problem, placements, 0)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert failed == ()
    assert calls["route"] == 1


@pytest.mark.parametrize("repair", [True, False], ids=["optimize", "fast"])
def test_the_walled_in_dock_routes_valid(repair: bool) -> None:
    problem, placements = _problem()
    layout, failed = solver_core._assemble(problem, placements, 0, repair=repair)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert failed == ()
    assert validate(problem, layout).ok  # the independent gate agrees, not just the routers


def test_a_sink_walled_in_by_machines_is_reported_not_routed() -> None:
    # A sink boxed in by machines has no dock with or without pipes: an explicit infeasibility
    # naming its power net, never a cable laid somewhere it cannot connect.
    problem, placements = _walled_in()
    layout, failed = solver_core._assemble(problem, placements, 0)
    assert layout.status is LayoutStatus.PARTIAL_INVALID
    assert failed == ("power:LV",)
    assert layout.infeasibility is not None
    assert layout.infeasibility.constraint == "face_reachability"
