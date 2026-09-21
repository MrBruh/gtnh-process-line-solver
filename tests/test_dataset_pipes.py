"""The cable/pipe stand-in policy, and the guard that it names real blocks.

The policy is a *choice* (docs/DOMAIN.md, "Cables and pipes"): GT gives every tier several cable
materials and this project picks one to draw. That makes two things worth testing and they are
different in kind:

1. **The choice is coherent** - every tier on the ladder is a real tier, the gauges are exactly the
   ones the contract allows, and a tier nobody has a cable for says so rather than inventing one.
2. **The choice names blocks that exist.** A table of material names is exactly the sort of thing
   that rots silently against a regenerated dataset, and a stand-in that resolves to nothing renders
   as a flat bar with no error - so the committed manifest is asked directly, tier by tier, and a
   real 2.9 dump's cable/pipe namespace too. The committed manifest alone cannot catch this: it is
   frozen at the pack that generated it, so it kept agreeing with the policy through GT's 2.9
   rename while every consumer of a 2.9 dump lost its cables (#176).

The 2.9 names are read from ``fixtures/local/manifest_pipe_names_2.9.json`` rather than from
whatever dump sits in ``data/`` on this machine, because the suite pins the dataset it resolves
(``conftest._pinned_dataset_root``, #182). That list is dumper output, so it is never committed:
it is generated locally from a staged 2.9 dump by ``tools/derive_pipe_names.py``, and the check
that reads it skips where nobody has. CI is one of those places, so CI does not run the 2.9 check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gtnh_solver.dataset import (
    CABLE_DISPLAY_MATERIAL,
    CABLE_MATERIAL_BY_TIER,
    CABLE_THICKNESS_BLOCKS,
    CABLE_THICKNESSES,
    DEFAULT_PIPE_SIZE,
    DEFAULT_PIPE_THICKNESS_BLOCKS,
    PIPE_DISPLAY_STEM,
    PIPE_MATERIAL,
    PIPE_THICKNESS_BLOCKS,
    ROUTED_PIPE_SIZES,
    VOLTAGE_BY_TIER,
    UnknownTierError,
    cable_display_name,
    manifest_names,
    pipe_display_name,
    route_material,
    tier_voltage,
)
from gtnh_solver.ir import Commodity, PipeFamily, PipeSize

_COMMITTED_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "textures" / "manifest.json"


def _pipes_by_name() -> dict[str, dict[str, object]]:
    """Every ``kind: "pipe"`` entry in the committed manifest, keyed by its dataset name."""
    raw = json.loads(_COMMITTED_MANIFEST.read_text(encoding="utf-8"))
    return {
        str(entry["display_name"]): entry
        for entry in raw["blocks"].values()
        if entry.get("kind") == "pipe" and entry.get("display_name")
    }


#: Every ``kind: "pipe"`` display name a real GTNH 2.9 dump carries: GT's whole cable, wire and
#: pipe namespace at that pack. It is a filtered slice of extractor output, and nothing a dumper
#: produced is committed, so it lives in the gitignored ``tests/fixtures/local/`` and exists only
#: where someone generated it with ``tools/derive_pipe_names.py``. That tool filters the manifest on
#: ``kind`` and takes ``display_name`` with no solver code involved, so the list cannot agree with
#: ``dataset/pipes.py`` by construction, and it refuses a pruned manifest, which could.
_PIPE_NAMES_2_9 = (
    Path(__file__).resolve().parent / "fixtures" / "local" / "manifest_pipe_names_2.9.json"
)


def _dump_pipe_names() -> set[str]:
    """The local 2.9 name list, or a skip that says how to generate it."""
    if not _PIPE_NAMES_2_9.is_file():
        pytest.skip(
            "no local 2.9 cable/pipe name list at tests/fixtures/local/manifest_pipe_names_2.9.json"
            " (dumper output, never committed); generate it from a staged 2.9 dump with "
            "`python tools/derive_pipe_names.py data/<2.9 version>/textures/manifest.json`"
        )
    raw: dict[str, Any] = json.loads(_PIPE_NAMES_2_9.read_text(encoding="utf-8"))
    return {str(name) for name in raw["display_names"]}


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


def test_item_and_fluid_routes_get_their_pipe_at_the_size_asked_and_no_tier() -> None:
    for commodity, family in (
        (Commodity.ITEM, PipeFamily.ITEM_PIPE),
        (Commodity.FLUID, PipeFamily.FLUID_PIPE),
    ):
        material = route_material(commodity, size=PipeSize.LARGE)
        assert material is not None
        assert material.family is family
        assert material.material == PIPE_MATERIAL[commodity]
        assert material.size is PipeSize.LARGE
        assert material.tier is None
        assert material.stand_in


def test_a_pipe_with_no_size_raises_and_a_cable_with_one_does_too() -> None:
    """Neither mistake may default. A pipe that fell back to the normal size is what starved the
    parallel sand line (#165); a size on a cable is a gauge the contract keeps elsewhere."""
    with pytest.raises(ValueError, match="needs a size"):
        route_material(Commodity.ITEM)
    with pytest.raises(ValueError, match="cable has no size"):
        route_material(Commodity.POWER, "LV", size=PipeSize.NORMAL)


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
    assert pipe_display_name("tin", PipeSize.HUGE) == "gt_pipe_tin_huge"  # the value, not the repr
    assert pipe_display_name("tin", PipeSize.NORMAL) == "gt_pipe_tin"
    with pytest.raises(ValueError, match="quadruple"):  # a size GT does not build names no block
        pipe_display_name("tin", "quadruple")


def test_every_material_the_policy_draws_has_a_localized_spelling() -> None:
    """A material on the ladder but missing from the display table would resolve against a 2.8.4
    dump and silently lose its cable against a 2.9 one, which is exactly the bug (#176). The tables
    are pinned against each other because the shipped examples exercise only two tiers of either.
    """
    assert set(CABLE_DISPLAY_MATERIAL) == set(CABLE_MATERIAL_BY_TIER.values())
    assert set(PIPE_DISPLAY_STEM) == set(PIPE_MATERIAL.values())


def test_a_block_offers_both_of_gts_spellings_newest_first() -> None:
    """The join key survives the rename. A 2.9 dump carries the localized name only and a 2.8.4
    dump the unlocalized one only, so a lookup has to be handed both, newest first."""
    assert manifest_names("cable.tin.02") == ("2x Tin Cable", "cable.tin.02")
    assert manifest_names("cable.redalloy.01") == ("1x Red Alloy Cable", "cable.redalloy.01")
    assert manifest_names("cable.niobiumtitanium.16") == (
        "16x Niobium-Titanium Cable",
        "cable.niobiumtitanium.16",
    )
    assert manifest_names("gt_pipe_bronze") == ("Bronze Fluid Pipe", "gt_pipe_bronze")
    assert manifest_names("gt_pipe_tin") == ("Tin Item Pipe", "gt_pipe_tin")
    assert manifest_names("gt_pipe_bronze_large") == (
        "Large Bronze Fluid Pipe",
        "gt_pipe_bronze_large",
    )


def test_the_item_pipe_sizes_the_router_lays_carry_the_names_a_real_dump_uses() -> None:
    """Both spellings of every size the router can lay an item pipe at (#165), each copied from a
    real dump rather than derived from the other: the localized names from the 2.9.0-beta-2
    extraction (GT5U 5.09.54.20, which also renders them from ``en_US.lang`` lines 178-182) and the
    unlocalized ones from the 2.8.4 extraction. A plausible name built from the 2.8.4 id is the
    trap #186 fixed, so the expected strings here are literals, never formatted."""
    assert manifest_names("gt_pipe_tin") == ("Tin Item Pipe", "gt_pipe_tin")
    assert manifest_names("gt_pipe_tin_large") == ("Large Tin Item Pipe", "gt_pipe_tin_large")
    assert manifest_names("gt_pipe_tin_huge") == ("Huge Tin Item Pipe", "gt_pipe_tin_huge")
    routed = [pipe_display_name("tin", size) for size in ROUTED_PIPE_SIZES[Commodity.ITEM]]
    assert routed == ["gt_pipe_tin", "gt_pipe_tin_large", "gt_pipe_tin_huge"]


def test_a_name_the_policy_never_generated_gets_no_invented_alias() -> None:
    """This maps between two spellings of a block the policy knows; it must not guess at one it has
    never heard of. Deriving "1x Lead Cable" from an id is the plausible-confident-wrong failure the
    whole stand-in lane guards against, so an unknown name comes back alone and the lookup misses.
    """
    assert manifest_names("cable.lead.01") == ("cable.lead.01",)
    assert manifest_names("gt_pipe_steel") == ("gt_pipe_steel",)
    assert manifest_names("Basic Forge Hammer") == ("Basic Forge Hammer",)


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


def test_both_pipe_stand_ins_exist_at_every_size_the_router_lays() -> None:
    """Every size, not only today's: the exporter refuses a pipe the manifest lacks and the
    previewer draws it as a flat bar, and the item sizes a line needs move with its endpoints."""
    pipes = _pipes_by_name()
    if not any(name.startswith("gt_pipe_") for name in pipes):
        pytest.skip("no pipes in the committed manifest (fixture-only checkout)")

    assert PIPE_THICKNESS_BLOCKS[PipeFamily.ITEM_PIPE][DEFAULT_PIPE_SIZE] == pytest.approx(
        DEFAULT_PIPE_THICKNESS_BLOCKS
    )
    for commodity, family in (
        (Commodity.ITEM, PipeFamily.ITEM_PIPE),
        (Commodity.FLUID, PipeFamily.FLUID_PIPE),
    ):
        for size in ROUTED_PIPE_SIZES[commodity]:
            name = pipe_display_name(PIPE_MATERIAL[commodity], size)
            entry = pipes.get(name)
            assert entry is not None, f"{commodity.value} lays {name}, which is not present"
            pipe = entry["pipe"]
            assert isinstance(pipe, dict)
            assert pipe["thickness"] == pytest.approx(PIPE_THICKNESS_BLOCKS[family][size])


def test_a_real_29_dump_names_every_cable_and_pipe_the_policy_draws() -> None:
    """The same guard against the pack that did the renaming, which the committed one cannot give.

    The committed manifest is example-scoped and frozen at the pack that generated it, so it can
    only ever prove that pack's spelling; it shipped three tiers at 2.8.4 and would keep passing
    through any number of renames after. GT's 2.9 rename is where the join actually broke (#176),
    and a full 2.9 dump holds every cable and pipe GT has, so every name on the ladder must resolve
    under one spelling or the other.

    This used to read the newest dump staged in ``data/``, which made it run differently per
    checkout (#182). It now reads a name list generated from such a dump on purpose, so the
    dataset it checks is a stated input rather than whatever the machine holds. It still skips
    where that list is absent, and CI has no dump to generate it from, so CI never runs it.
    """
    names = _dump_pipe_names()

    wanted = [
        cable_display_name(material, gauge)
        for material in CABLE_MATERIAL_BY_TIER.values()
        for gauge in CABLE_THICKNESS_BLOCKS
    ]
    wanted += [
        pipe_display_name(material, size)
        for commodity, material in PIPE_MATERIAL.items()
        for size in ROUTED_PIPE_SIZES[commodity]
    ]
    for canonical in wanted:
        tried = manifest_names(canonical)
        assert any(name in names for name in tried), (
            f"the dump names no {canonical}; looked for {' or '.join(tried)}"
        )
