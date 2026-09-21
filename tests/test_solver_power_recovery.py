"""The power-first recovery: a power dock the pipes walled in gets its cable laid first (#226).

Power routes last, against every pipe cell as a wall. ``reserve_power_docks`` keeps one dock cell
per power endpoint free of pipes, but a free cell is not a reachable one: a pipe can detour around
it and seal it into a pocket of one. ``solver.core._assemble`` answers that by laying the failed
power nets alone, holding their trunk from the pipes, and laying the attempt again.

The repro was reduced by delta-debugging from ``examples/ev-nitrobenzene.json`` against the 2.9
dump. There, every ``solve`` seed from 0 to 7 returned the same placement (the only attempt the
crowding gate lets through), and on it ``power:MV`` could not reach the second of three Distillation
Towers. The geometry below keeps the five machines that failure needs and pulls them together into
a 7x4x8 region. The towers are built by hand from ``MTEDistillationTower``'s structure (``b~b``
base layer, ``lll`` layers above), with the hatch kinds the dump records for those cells, so no
dumper output is involved.

What goes wrong without the recovery, in order:

1. ``reserve_power_docks`` holds one dock cell per power endpoint. For tower #2 that is the cell
   south of its bottom ring, since its energy-capable cells are all on the bottom layer, whose
   down faces point out of the region.
2. The water net docks every tower's input hatch on that same bottom ring, and its pipe runs
   around the ring at ``y=0``. It routes *around* the reserved cell, so the cell stays free, but
   the pipe encloses it on every side the region and the tower leave open.
3. Power routes last with every pipe cell as a hard obstacle, and finds the free dock cell
   unreachable: ``no free cell path to a dock face``.

The layer cells are deliberately left as the dump records them. In game they also take an energy
hatch (the ``l`` element chains ``addEnergyInputToMachineList`` as a bare adder, which the
extractor cannot see), and allowing that would make this layout route even without the recovery.
That is a separate data fix (#227) and would not touch this test, which is about routing order.
"""

from __future__ import annotations

import dataclasses

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


def test_without_the_recovery_the_water_pipe_walls_the_dock_in() -> None:
    # Pins the failure the recovery exists for, so the test below provably goes through it: a
    # single pass routes every pipe and then cannot reach tower #2's reserved dock.
    problem, placements = _problem()
    first = solver_core._lay(problem, placements, 0, "footprint", repair=True)
    assert first.layout.status is LayoutStatus.PARTIAL_INVALID
    assert first.power_failed == ("power:MV",)
    assert first.layout.infeasibility is not None
    assert "no free cell path to a dock face of 'dt#2'" in first.layout.infeasibility.detail


@pytest.mark.parametrize("repair", [True, False], ids=["optimize", "fast"])
def test_the_recovery_lays_a_walled_in_power_dock(repair: bool) -> None:
    problem, placements = _problem()
    layout, failed = solver_core._assemble(problem, placements, 0, repair=repair)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert failed == ()
    assert validate(problem, layout).ok  # the independent gate agrees, not just the routers


def test_a_layout_whose_power_routes_never_takes_the_recovery_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The recovery costs a second full routing, so a layout that does not need it must not pay:
    # the same towers and pipes, powered from a source whose trunk the pipes do not cut off.
    problem, placements = _problem()
    unpowered = problem.model_copy(
        update={"nets": [n for n in problem.nets if n.commodity is not Commodity.POWER]}
    )
    calls = {"route": 0}
    real_route = route

    def counting_route(*args: object, **kwargs: object) -> object:
        calls["route"] += 1
        return real_route(*args, **kwargs)  # type: ignore[arg-type]

    def no_recovery(*args: object, **kwargs: object) -> None:
        raise AssertionError("the recovery ran on a layout whose power routed")

    monkeypatch.setattr(solver_core, "route", counting_route)
    monkeypatch.setattr(solver_core, "_power_corridor", no_recovery)
    layout, failed = solver_core._assemble(unpowered, placements, 0)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert failed == ()
    assert calls["route"] == 1  # one pass, no second routing


def test_a_recovery_that_cannot_help_returns_the_first_pass_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A sink boxed in by machines has no dock with or without pipes, so laying its net first finds
    # nothing to hold, and the attempt comes back exactly as the single pass left it.
    problem, placements = _walled_in()
    tried: list[object] = []
    real_corridor = solver_core._power_corridor

    def spy(*args: object, **kwargs: object) -> object:
        tried.append(result := real_corridor(*args, **kwargs))  # type: ignore[arg-type]
        return result

    monkeypatch.setattr(solver_core, "_power_corridor", spy)
    layout, failed = solver_core._assemble(problem, placements, 0)
    first = solver_core._lay(problem, placements, 0, "footprint", repair=True)
    assert tried == [None]  # the recovery was asked, and had nothing to hold
    assert first.power_failed == ("power:LV",)
    assert (layout, failed) == (first.layout, first.failed)


@pytest.mark.parametrize(
    "second_failed",
    [("power:MV", "water"), ("water",)],
    ids=["more-failures", "as-many-failures"],
)
def test_the_second_pass_is_kept_only_when_it_is_better(
    monkeypatch: pytest.MonkeyPatch, second_failed: tuple[str, ...]
) -> None:
    # The invariant that makes the recovery safe to run: an attempt can never come out worse. A
    # second pass that fails more nets, or as many, is discarded for the first, whatever it did.
    problem, placements = _problem()
    real_lay = solver_core._lay

    def rigged(*args: object, held: object = (), **kwargs: object) -> object:
        done = real_lay(*args, **kwargs)  # type: ignore[arg-type]
        if not held:
            return done
        marked = done.layout.model_copy(update={"seed": 99})  # tells the two passes apart
        return dataclasses.replace(done, layout=marked, failed=second_failed)

    monkeypatch.setattr(solver_core, "_lay", rigged)
    layout, failed = solver_core._assemble(problem, placements, 0)
    assert layout.seed == 0  # the first pass, not the rigged second one
    assert failed == ("power:MV",)
