"""Tests for a crop card as one Crop Manager on the edge of the build (#282).

An arodoid crop card's ``machineCount`` is the number of crop sticks planted, and the field is not
part of the build: the line receives what it yields, the way it receives power from a source it
does not build. So the adapter makes such a card the one block that yield comes out of, a Crop
Manager at the card's tier, flagged to stand on the region boundary facing outside
(``Machine.outside_front``). What these pin: the one machine and its type, tier, ports and rate;
the tier control as the fork reads it; what stays as it was; and that it lays out.
"""

from __future__ import annotations

import warnings

import pytest

from gtnh_solver.adapter import (
    AdapterWarning,
    Edge,
    MachineHandler,
    Node,
    Plan,
    Recipe,
    Resource,
    Storage,
    to_input_ir,
)
from gtnh_solver.adapter.core import _CROP_MANAGERS, _crop_cards_as_managers
from gtnh_solver.ir import InputIR, LayoutStatus, Machine
from gtnh_solver.ir.geometry import front_on_boundary
from gtnh_solver.previewer.textures import TextureManifest
from gtnh_solver.solver import solve
from gtnh_solver.system_io import system_io
from gtnh_solver.validator import validate

_LEAF = "cropsnh:materialleaf@1"
_CROPS = 359  # the community Bio Diesel plan's Canola card
_PER_CROP = 2.688  # leaves per crop per 1024-tick harvest
_RATE = _CROPS * _PER_CROP / 1024  # leaves/t the whole card yields


def _crop_plan(
    *,
    crops: int = _CROPS,
    tier_key: str | None = "1",
    handler_id: str = "",
    seed_feed: bool = False,
) -> Plan:
    """A Canola crop card feeding a Fluid Extractor, as the arodoid export spells it."""
    crop = Recipe(
        id="crop",
        machine_type="Crop Farm",
        duration_ticks=1024.0,
        inputs=[Resource(kind="item", id="seed", amount=1.0)],
        outputs=[Resource(kind="item", id=_LEAF, amount=_PER_CROP)],
        machine_handlers=[
            MachineHandler(
                id="crop-manager", kind="single", label="Crop Manager", machine_type="Crop Manager"
            ),
            MachineHandler(
                id="crop-industrial-farm",
                kind="multiblock",
                label="Industrial Farm",
                machine_type="Industrial Farm",
            ),
        ],
    )
    extractor = Recipe(
        id="extract",
        machine_type="Fluid Extractor",
        eut=8.0,
        duration_ticks=100.0,
        inputs=[Resource(kind="item", id=_LEAF, amount=1.0)],
        outputs=[Resource(kind="fluid", id="seedoil", amount=10.0)],
    )
    tiers = {"cropManagerTier": tier_key} if tier_key is not None else {}
    storages = [Storage(id="oil", kind="fluid")]
    edges = [
        Edge(id="e-leaf", source="c", target="x", resource_kind="item", resource_id=_LEAF),
        Edge(id="e-oil", source="x", target="oil", resource_kind="fluid", resource_id="seedoil"),
    ]
    if seed_feed:
        storages.append(Storage(id="seeds", kind="item"))
        edges.append(
            Edge(id="e-seed", source="seeds", target="c", resource_kind="item", resource_id="seed")
        )
    return Plan(
        schema_version=1,
        recipes=[crop, extractor],
        nodes=[
            Node(
                id="c",
                recipe_id="crop",
                overclock_tier="NONE",
                machine_count=crops,
                machine_handler_id=handler_id,
                machine_config_tiers=tiers,
            ),
            Node(id="x", recipe_id="extract", overclock_tier="LV"),
        ],
        storages=storages,
        edges=edges,
    )


def _adapt(plan: Plan) -> InputIR:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", AdapterWarning)  # notes this file is not about
        return to_input_ir(plan)


def _machine(ir: InputIR, machine_id: str) -> Machine:
    return next(m for m in ir.machines if m.id == machine_id)


# ------------------------------------------------------------------ the one block


def test_a_crop_card_is_one_crop_manager_facing_outside() -> None:
    ir = _adapt(_crop_plan())
    manager = _machine(ir, "c")
    assert manager.type == "Basic Crop Manager"
    assert manager.voltage_tier == "LV"
    assert manager.outside_front
    assert manager.fronts_outside
    assert not [m for m in ir.machines if m.id.startswith("c#")]  # not one machine per crop


def test_the_manager_puts_out_the_whole_cards_yield_and_takes_nothing() -> None:
    ir = _adapt(_crop_plan())
    manager = _machine(ir, "c")
    assert [(p.id, p.direction.value) for p in manager.faces.ports] == [
        (f"output:{_LEAF}", "output")
    ]
    assert manager.faces.ports[0].rate == pytest.approx(_RATE)
    assert next(n for n in ir.nets if n.id == "e-leaf").throughput == pytest.approx(_RATE)


def test_the_manager_draws_nothing_and_gets_no_cable() -> None:
    # Its upkeep is not simulated, so the power synthesis owes it nothing.
    ir = _adapt(_crop_plan())
    assert _machine(ir, "c").eut == 0
    assert not [
        n
        for n in ir.nets
        if n.fluid_or_item is None and any(e.machine_id == "c" for e in n.endpoints)
    ]


@pytest.mark.parametrize(
    ("key", "tier", "block"),
    [
        ("1", "LV", "Basic Crop Manager"),
        ("2", "MV", "Advanced Crop Manager"),
        ("3", "HV", "Advanced Crop Manager II"),
        ("8", "UV", "Ultimate Crop Manager"),
        ("12", "UV", "Ultimate Crop Manager"),  # CropsNH registers more; the fork clamps at UV
        ("0", "LV", "Basic Crop Manager"),
        ("none", "LV", "Basic Crop Manager"),  # a legacy key the fork reads as LV
        (None, "LV", "Basic Crop Manager"),  # unset: the control's default, LV
    ],
)
def test_the_tier_control_picks_the_block_as_the_fork_reads_it(
    key: str | None, tier: str, block: str
) -> None:
    manager = _machine(_adapt(_crop_plan(tier_key=key)), "c")
    assert (manager.voltage_tier, manager.type) == (tier, block)


def test_a_seed_feed_has_no_port_to_land_on_and_is_dropped() -> None:
    ir = _adapt(_crop_plan(seed_feed=True))
    assert not [n for n in ir.nets if n.fluid_or_item == "seed"]
    assert [p.id for p in _machine(ir, "c").faces.ports] == [f"output:{_LEAF}"]


def test_every_tiers_manager_draws_and_exports_as_its_own_block() -> None:
    # CropsNH registers its managers as GT machines 28001 (LV) up, named as _CROP_MANAGERS names
    # them (checked against a 2.9 dump). The type is the exact name, so each tier resolves to its
    # own block; the generic tier-prefix ladder would draw every HV+ manager as the LV one.
    blocks = {
        f"gregtech:gt.blockmachines|{28000 + tier}": {
            "kind": "mte",
            "display_name": name,
            "tier": tier,
            "sides": {},
        }
        for tier, (_, name) in enumerate(_CROP_MANAGERS, start=1)
    }
    manifest = TextureManifest({"schema": 2, "blocks": blocks})
    for key in range(1, len(_CROP_MANAGERS) + 1):
        manager = _machine(_adapt(_crop_plan(tier_key=str(key))), "c")
        assert manifest.mte_block(manager.type, manager.voltage_tier) == (
            "gregtech:gt.blockmachines",
            28000 + key,
        ), manager.type


# ------------------------------------------------------------------ what stays as it was


def test_an_industrial_farm_card_is_left_as_it_was() -> None:
    ir = _adapt(_crop_plan(crops=3, handler_id="crop-industrial-farm"))
    farms = [m for m in ir.machines if m.id.startswith("c#")]
    assert [m.type for m in farms] == ["Crop Farm"] * 3
    assert not any(m.outside_front for m in farms)


def test_a_plan_with_no_crop_card_comes_back_as_it_went_in() -> None:
    plan = _crop_plan(handler_id="crop-industrial-farm")
    assert _crop_cards_as_managers(plan) == (plan, frozenset())


def test_the_callers_plan_and_its_shared_recipe_stay_as_parsed() -> None:
    plan = _crop_plan(seed_feed=True)
    converted, managers = _crop_cards_as_managers(plan)
    assert managers == frozenset({"c"})
    assert plan.nodes[0].machine_count == _CROPS
    assert plan.recipes[0].machine_type == "Crop Farm"
    assert [e.id for e in plan.edges] == ["e-leaf", "e-oil", "e-seed"]
    own = next(r for r in converted.recipes if r.id == "crop@c")
    assert own.outputs[0].amount == pytest.approx(_CROPS * _PER_CROP)
    assert own.inputs == []


# ------------------------------------------------------------------ it lays out


def test_a_crop_line_solves_with_its_manager_on_the_edge() -> None:
    ir = _adapt(_crop_plan())
    layout = solve(ir)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert validate(ir, layout).ok
    placed = next(p for p in layout.placements if p.machine_id == "c")
    manager = _machine(ir, "c")
    assert front_on_boundary(placed.cell, manager.footprint, placed.orientation, ir.bounding_region)


def test_the_managers_yield_is_an_input_to_the_line_not_a_product() -> None:
    # Like a storage the builder fills, it only sources the line, so its yield crosses the edge in.
    ir = _adapt(_crop_plan())
    boundary = system_io(ir, solve(ir))
    (crops,) = [flow for flow in boundary.inputs if flow.machine_id == "c"]
    assert (crops.machine_type, crops.resource) == ("Basic Crop Manager", _LEAF)
    assert crops.rate == pytest.approx(_RATE)
    assert not [flow for flow in boundary.outputs if flow.machine_id == "c"]
