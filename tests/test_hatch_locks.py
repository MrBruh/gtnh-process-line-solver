"""Tests for ``hatch_locks``: the product each multiblock output hatch or bus must be locked to (#120).

GT fills a multiblock's output hatches first fit, so on a machine with two products of a kind the
pipe from one hatch carries whichever product reached it first. These pin which hatches the build
has to lock, on hand-built layouts for each rule and once on the real nitrobenzene line, whose Large
Chemical Reactor makes nitric acid and water through two output hatches.
"""

from __future__ import annotations

from pathlib import Path

from gtnh_solver.adapter import adapt_file
from gtnh_solver.dataset import load_physical_dataset
from gtnh_solver.hatch_locks import LOCK_SLOT, hatch_layers, hatch_locks
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
    PlacedHatch,
    Port,
)
from tests._helpers import at, layered_tower

_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_DATASET = _ROOT / "data" / "multiblocks"
_NITROBENZENE = _ROOT / "examples" / "gtnh-nitrobenzene.json"

_FLUID, _ITEM = Commodity.FLUID, Commodity.ITEM
_OUT, _IN = IODirection.OUTPUT, IODirection.INPUT


def _port(direction: IODirection, resource: str, commodity: Commodity = _FLUID) -> Port:
    """A port the way the adapter names one: ``{direction}:{resource}``."""
    return Port(id=f"{direction.value}:{resource}", commodity=commodity, direction=direction)


def _multi(ports: list[Port], *, recipe_map: str | None = None) -> Machine:
    """A multiblock with ``ports``; one whose ports name output layers is a tower with that many."""
    layers = {p.output_layer for p in ports if p.output_layer is not None}
    tower = layered_tower(outputs=(), layers=max(layers) + 1) if layers else None
    return Machine(
        id="m",
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        footprint=tower.footprint if tower is not None else CellBox(sx=3, sy=3, sz=3),
        faces=FaceSpec(ports=ports),
        recipe_map=recipe_map,
        hatch_slots=tower.hatch_slots if tower is not None else (),
    )


def _locks(machine: Machine, *hatches: tuple[str, str | None]) -> dict[int, str]:
    """``hatch_locks`` over ``machine`` with ``(kind, port id)`` hatches on cells x = 0, 1, 2, ...,
    keyed back by that x."""
    problem = InputIR(bounding_region=CellBox(sx=8, sy=4, sz=8), machines=[machine])
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[at(machine.id, 0, 0, 0)],
        hatches=[
            PlacedHatch(
                machine_id=machine.id,
                kind=kind,
                cell=CellCoord(x=x, y=0, z=0),
                facing=Facing.NORTH,
                port_id=port_id,
            )
            for x, (kind, port_id) in enumerate(hatches)
        ],
    )
    locks = hatch_locks(problem, layout)
    assert all(machine_id == machine.id for machine_id, _ in locks)
    return {cell[0]: product for (_, cell), product in locks.items()}


def test_two_fluid_products_lock_every_output_hatch_to_its_own() -> None:
    # The Large Chemical Reactor on the nitrobenzene line: both of its output hatches, not all but
    # one, since a locked hatch that fills spills into an unlocked one.
    lcr = _multi([_port(_OUT, "nitricacid"), _port(_OUT, "water")])
    locks = _locks(lcr, ("OutputHatch", "output:nitricacid"), ("OutputHatch", "output:water"))
    assert locks == {0: "nitricacid", 1: "water"}


def test_two_item_products_lock_every_output_bus_to_its_own() -> None:
    machine = _multi([_port(_OUT, "a", _ITEM), _port(_OUT, "b", _ITEM)])
    assert _locks(machine, ("OutputBus", "output:a"), ("OutputBus", "output:b")) == {0: "a", 1: "b"}


def test_one_product_of_a_kind_needs_no_lock() -> None:
    # A Coke Oven: one fluid through its one output hatch, one item through its one bus. Hatches and
    # buses never take each other's products, so each kind is counted on its own.
    coke_oven = _multi([_port(_OUT, "woodtar"), _port(_OUT, "coal", _ITEM)])
    assert _locks(coke_oven, ("OutputHatch", "output:woodtar"), ("OutputBus", "output:coal")) == {}


def test_a_product_with_two_hatches_is_still_one_product() -> None:
    # What decides it is how many products compete for the hatches, not how many hatches there are.
    machine = _multi([_port(_OUT, "water")])
    assert _locks(machine, ("OutputHatch", "output:water"), ("OutputHatch", "output:water")) == {}


def _layered(*outputs: tuple[str, int | None], commodity: Commodity = _FLUID) -> list[Port]:
    """Output ports the way the adapter names a tower's: each fluid with the layer it fills."""
    return [
        Port(
            id=f"output:{name}",
            commodity=commodity,
            direction=_OUT,
            output_layer=layer if commodity is _FLUID else None,
        )
        for name, layer in outputs
    ]


def test_a_tower_layer_with_one_product_is_never_locked() -> None:
    """A Distillation Tower sends output ``i`` to layer ``i`` whatever is locked (#299), and a lock
    that disagrees voids the product, so a layer carrying one product needs none, however many
    layers the tower has. The rule follows the port's layer, not the machine's recipe map."""
    tower = _multi(_layered(("benzene", 0), ("phenol", 1), ("creosote", 2)))
    hatches = [("OutputHatch", p.id) for p in tower.faces.ports]
    assert _locks(tower, *hatches) == {}
    # The same three products filled first fit (no layers) compete for every hatch.
    chemical_plant = _multi(
        [_port(_OUT, "benzene"), _port(_OUT, "phenol"), _port(_OUT, "creosote")]
    )
    assert set(_locks(chemical_plant, *hatches).values()) == {"benzene", "phenol", "creosote"}


def test_two_time_shared_products_on_one_layer_are_both_locked() -> None:
    # Two recipes the tower time-shares put different fluids at index 0, so both fill layer 0,
    # first fit among its hatches: each hatch must be locked to its own product.
    tower = _multi(_layered(("ethane", 0), ("butane", 0), ("propane", 1)))
    hatches = [("OutputHatch", p.id) for p in tower.faces.ports]
    assert _locks(tower, *hatches) == {0: "ethane", 1: "butane"}


def test_a_towers_output_buses_fill_first_fit_and_are_locked() -> None:
    # A tower fills only its fluids by layer; two item products share its buses first fit.
    tower = _multi([*_layered(("tar", 0)), _port(_OUT, "ash", _ITEM), _port(_OUT, "coke", _ITEM)])
    locks = _locks(
        tower,
        ("OutputHatch", "output:tar"),
        ("OutputBus", "output:ash"),
        ("OutputBus", "output:coke"),
    )
    assert locks == {1: "ash", 2: "coke"}


def test_a_spare_output_hatch_is_never_locked() -> None:
    # It stands on a layer no product uses so the tower forms, and serves no port.
    tower = _multi(_layered(("water", 0)))
    assert _locks(tower, ("OutputHatch", "output:water"), ("OutputHatch", None)) == {}


def test_each_output_hatch_on_a_tower_reports_the_layer_it_stands_on() -> None:
    # ``hatch_layers`` reads the slot under each hatch, a spare's included; a hatch off the
    # layers (the top centre here) and every hatch of a first-fit machine has none.
    tower = layered_tower(outputs=["water"], layers=2)
    problem = InputIR(bounding_region=CellBox(sx=8, sy=4, sz=8), machines=[tower])

    def out(x: int, y: int, z: int, port: str | None) -> PlacedHatch:
        return PlacedHatch(
            machine_id=tower.id,
            kind="OutputHatch",
            cell=CellCoord(x=x, y=y, z=z),
            facing=Facing.WEST,
            port_id=port,
        )

    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[at(tower.id, 2, 0, 2)],
        hatches=[out(2, 1, 3, "output:water"), out(2, 2, 3, None), out(3, 2, 3, None)],
    )
    assert hatch_layers(problem, layout) == {
        (tower.id, (2, 1, 3)): 0,
        (tower.id, (2, 2, 3)): 1,
    }


def test_input_and_upkeep_hatches_are_never_locked() -> None:
    machine = _multi(
        [
            _port(_IN, "water"),
            _port(_IN, "oxygen"),
            Port(id="power:in", commodity=Commodity.POWER, direction=_IN),
        ]
    )
    hatches = (
        ("InputHatch", "input:water"),
        ("InputHatch", "input:oxygen"),
        ("Energy", "power:in"),
        ("Maintenance", None),
    )
    assert _locks(machine, *hatches) == {}


def test_a_hatch_whose_port_the_problem_lacks_is_left_out() -> None:
    # The validator reports a hatch naming a port that does not exist; this only reads what is there.
    machine = _multi([_port(_OUT, "nitricacid")])
    hatches = (("OutputHatch", "output:nitricacid"), ("OutputHatch", "output:gone"))
    assert _locks(machine, *hatches) == {}


def test_each_kind_names_the_slot_gt_sets_its_lock_in() -> None:
    assert LOCK_SLOT == {"OutputHatch": "Locked Fluid slot", "OutputBus": "output filter slot"}


def test_the_nitrobenzene_line_locks_only_the_reactor_making_two_fluids() -> None:
    """The real line's machines, with their structures from the committed fixtures: the reactor
    making nitric acid and water is the only one with two products of a kind. The Distillation
    Tower's five products each fill a layer of their own, and every other machine makes one product
    of each kind.

    Every output port gets its hatch (one port is one hatch, docs/DOMAIN.md), which is what any
    VALID layout of the line holds; which casing cell each lands on does not change the answer, so
    no solve is needed to ask it."""
    problem = adapt_file(_NITROBENZENE, physical=load_physical_dataset(_FIXTURE_DATASET))
    multiblocks = [m for m in problem.machines if m.hatch_slots]
    hatches = [
        PlacedHatch(
            machine_id=m.id,
            kind="OutputHatch" if p.commodity is _FLUID else "OutputBus",
            cell=CellCoord(x=x, y=0, z=0),
            facing=Facing.NORTH,
            port_id=p.id,
        )
        for m in multiblocks
        for x, p in enumerate(p for p in m.faces.ports if p.direction is _OUT)
    ]
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, hatches=hatches)
    locks = hatch_locks(problem, layout)
    assert sorted(locks.values()) == ["nitricacid", "water"]
    machines = {m.id: m for m in problem.machines}
    assert {machines[machine_id].type for machine_id, _ in locks} == {"Large Chemical Reactor"}
    towers = [m for m in multiblocks if m.output_layers]
    # The tower making five products, and the one making distilled water: neither is locked.
    assert sorted(len([p for p in m.faces.ports if p.direction is _OUT]) for m in towers) == [1, 5]
