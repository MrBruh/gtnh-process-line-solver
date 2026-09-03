"""The cable/pipe stand-in policy, and the guard that it names real blocks.

The policy is a *choice* (docs/DOMAIN.md, "Cables and pipes"): GT gives every tier several cable
materials and this project picks one to draw. That makes two things worth testing and they are
different in kind:

1. **The choice is coherent** - every tier on the ladder is a real tier, the gauges are exactly the
   ones the contract allows, and a tier nobody has a cable for says so rather than inventing one.
2. **The choice names blocks that exist.** A table of material names is exactly the sort of thing
   that rots silently against a regenerated dataset, and a stand-in that resolves to nothing renders
   as a flat bar with no error - so the committed manifest is asked directly, tier by tier.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gtnh_solver.dataset import (
    CABLE_MATERIAL_BY_TIER,
    CABLE_THICKNESS_BLOCKS,
    CABLE_THICKNESSES,
    DEFAULT_PIPE_SIZE,
    DEFAULT_PIPE_THICKNESS_BLOCKS,
    PIPE_MATERIAL,
    VOLTAGE_BY_TIER,
    UnknownTierError,
    cable_display_name,
    pipe_display_name,
    route_material,
    tier_voltage,
)
from gtnh_solver.ir import Commodity, PipeFamily

_COMMITTED_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "textures" / "manifest.json"


def _pipes_by_name() -> dict[str, dict[str, object]]:
    """Every ``kind: "pipe"`` entry in the committed manifest, keyed by its dataset name."""
    raw = json.loads(_COMMITTED_MANIFEST.read_text(encoding="utf-8"))
    return {
        str(entry["display_name"]): entry
        for entry in raw["blocks"].values()
        if entry.get("kind") == "pipe" and entry.get("display_name")
    }


# --------------------------------------------------------------------------------------------- 1


def test_every_tier_on_the_ladder_is_a_real_tier() -> None:
    assert set(CABLE_MATERIAL_BY_TIER) <= set(VOLTAGE_BY_TIER)


def test_the_gauge_ladder_is_exactly_the_contract_thicknesses() -> None:
    """A gauge the router can size to but the policy cannot draw would degrade silently."""
    assert set(CABLE_THICKNESS_BLOCKS) == set(CABLE_THICKNESSES)


def test_power_route_gets_its_tier_cable() -> None:
    material = route_material(Commodity.POWER, "LV")
    assert material is not None
    assert material.family is PipeFamily.CABLE
    assert material.material == "tin"
    assert material.tier == "LV"
    assert material.stand_in


def test_item_and_fluid_routes_get_their_pipe_and_no_tier() -> None:
    for commodity, family in (
        (Commodity.ITEM, PipeFamily.ITEM_PIPE),
        (Commodity.FLUID, PipeFamily.FLUID_PIPE),
    ):
        material = route_material(commodity)
        assert material is not None
        assert material.family is family
        assert material.material == PIPE_MATERIAL[commodity]
        assert material.tier is None
        assert material.stand_in


def test_above_uv_there_is_no_cable_to_stand_in_for() -> None:
    """UHV and beyond are carried by superconductor bare wire, so GT ships no insulated cable at
    all. ``None`` is the honest answer and the route keeps its flat bar."""
    assert "UHV" in VOLTAGE_BY_TIER
    assert route_material(Commodity.POWER, "UHV") is None
    assert route_material(Commodity.POWER, "MAX") is None


def test_an_unknown_tier_raises_rather_than_going_quiet() -> None:
    """The one case that must NOT degrade to None: a typo has to be loud, or a mis-spelled tier
    silently renders every cable on the line as an unlabelled bar."""
    with pytest.raises(UnknownTierError):
        route_material(Commodity.POWER, "LVV")


def test_a_trunk_with_no_agreed_tier_says_nothing() -> None:
    assert route_material(Commodity.POWER, None) is None


def test_dataset_names_match_gts_own_spelling() -> None:
    assert cable_display_name("tin", 1) == "cable.tin.01"
    assert cable_display_name("niobiumtitanium", 16) == "cable.niobiumtitanium.16"
    # The normal size is the bare name; only the others take a suffix.
    assert pipe_display_name("bronze") == "gt_pipe_bronze"
    assert pipe_display_name("bronze", "large") == "gt_pipe_bronze_large"


# --------------------------------------------------------------------------------------------- 2


@pytest.mark.parametrize("tier", sorted(CABLE_MATERIAL_BY_TIER))
def test_every_shipped_cable_is_complete_and_correctly_rated(tier: str) -> None:
    """The ladder is asserted against extracted data, not against the comment that wrote it.

    The committed manifest is deliberately example-scoped, so most tiers are absent and that is not
    a defect - ``derive_small_manifest`` keeps only what a preview of the shipped lines can draw.
    What must hold is that a tier which IS shipped is shipped *whole*: all six gauges, rated at or
    above the tier it stands for, at the thickness the previewer will draw. A half-shipped ladder
    would lose a cable at one gauge and render it as a bare flat bar with no error anywhere.
    """
    pipes = _pipes_by_name()
    material = CABLE_MATERIAL_BY_TIER[tier]
    shipped = [g for g in CABLE_THICKNESS_BLOCKS if cable_display_name(material, g) in pipes]
    if not shipped:
        pytest.skip(f"no {tier} cable in the example-scoped manifest")

    for gauge, thickness in CABLE_THICKNESS_BLOCKS.items():
        name = cable_display_name(material, gauge)
        entry = pipes.get(name)
        assert entry is not None, f"{tier} ships {shipped} but not gauge {gauge} ({name})"
        pipe = entry["pipe"]
        assert isinstance(pipe, dict)
        assert pipe["insulated"] is True, f"{name} is bare wire, not an insulated cable"
        assert pipe["voltage"] >= tier_voltage(tier), f"{name} is underrated for {tier}"
        assert pipe["thickness"] == pytest.approx(thickness), f"{name} thickness moved"


def test_the_committed_manifest_ships_the_examples_own_cables() -> None:
    """The pruning rule actually reaches cables - the hole this closed.

    A cable's name carries no machine type, so the name rule in ``derive_small_manifest`` can never
    keep one; without the dedicated rule the committed manifest would ship zero. The sand line is
    LV and nitrobenzene reaches HV, so those two tiers are the floor.
    """
    pipes = _pipes_by_name()
    if not any(name.startswith("cable.") for name in pipes):
        pytest.skip("no cables in the committed manifest (fixture-only checkout)")

    for tier in ("LV", "HV"):
        name = cable_display_name(CABLE_MATERIAL_BY_TIER[tier], 1)
        assert name in pipes, f"the examples use {tier}; {name} must ship"


def test_both_pipe_stand_ins_exist_at_the_size_v1_draws() -> None:
    pipes = _pipes_by_name()
    if not any(name.startswith("gt_pipe_") for name in pipes):
        pytest.skip("no pipes in the committed manifest (fixture-only checkout)")

    for commodity in (Commodity.ITEM, Commodity.FLUID):
        name = pipe_display_name(PIPE_MATERIAL[commodity], DEFAULT_PIPE_SIZE)
        entry = pipes.get(name)
        assert entry is not None, f"{commodity.value} stands in for {name}, which is not present"
        pipe = entry["pipe"]
        assert isinstance(pipe, dict)
        assert pipe["thickness"] == pytest.approx(DEFAULT_PIPE_THICKNESS_BLOCKS)
