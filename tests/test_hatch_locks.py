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
from gtnh_solver.hatch_locks import LOCK_SLOT, fills_by_layer, hatch_locks
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
from tests._helpers import at

_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_DATASET = _ROOT / "data" / "multiblocks"
_NITROBENZENE = _ROOT / "examples" / "gtnh-nitrobenzene.json"

_FLUID, _ITEM = Commodity.FLUID, Commodity.ITEM
_OUT, _IN = IODirection.OUTPUT, IODirection.INPUT


def _port(direction: IODirection, resource: str, commodity: Commodity = _FLUID) -> Port:
    """A port the way the adapter names one: ``{direction}:{resource}``."""
    return Port(id=f"{direction.value}:{resource}", commodity=commodity, direction=direction)


def _multi(ports: list[Port], *, recipe_map: str | None = None) -> Machine:
    return Machine(
        id="m",
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        footprint=CellBox(sx=3, sy=3, sz=3),
        faces=FaceSpec(ports=ports),
        recipe_map=recipe_map,
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


def test_a_tower_that_fills_by_layer_is_never_locked() -> None:
    """A Distillation Tower sends output ``i`` to layer ``i`` whatever is locked, and a lock that
    disagrees voids the product, so it gets none however many products it has. So does every
    machine on the distillation tower map (the Dangote Distillus and the Mega towers) and the Sparge
    Tower."""
    ports = [_port(_OUT, "benzene"), _port(_OUT, "phenol"), _port(_OUT, "creosote")]
    hatches = [("OutputHatch", p.id) for p in ports]
    for recipe_map in ("gt.recipe.distillationtower", "gtpp.recipe.lftr.sparging"):
        tower = _multi(ports, recipe_map=recipe_map)
        assert fills_by_layer(tower)
        assert _locks(tower, *hatches) == {}
    # The same machine on any other map fills first fit, and needs all three.
    chemical_plant = _multi(ports, recipe_map="gtpp.recipe.chemicalplant")
    assert not fills_by_layer(chemical_plant)
    assert set(_locks(chemical_plant, *hatches).values()) == {"benzene", "phenol", "creosote"}


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
    Tower's five products fill by layer, and every other machine makes one product of each kind.

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
    towers = [m for m in multiblocks if fills_by_layer(m)]
    # The tower making five products, and the one making distilled water: neither is locked.
    assert sorted(len([p for p in m.faces.ports if p.direction is _OUT]) for m in towers) == [1, 5]
