"""The GT rule data for a multiblock's tiered channels (``dataset/structure_blocks.py``, #312).

The tables are checked against the two independent sources that state them: gtnh-factory-flow's
own controls (the resource each ``pipeCasing`` and ``heatingCoil`` tier names, in the arodoid
example) and the committed Chemical Plant dump (the order its ``casing`` and ``coil`` channels list
their blocks in). Then the gap rule: the two tiers GT accepts but the dump never records are
added for the plant only, and a dump that contradicts the rule wins with a warning.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gtnh_solver.dataset import (
    CHEMICAL_PLANT,
    COIL,
    HEATING_COILS,
    MACHINE_CASING,
    PIPE,
    PIPE_CASING_ALIASES,
    PIPE_CASINGS,
    SOLID_CASING,
    SOLID_CASINGS,
    BlockId,
    DatasetWarning,
    channel_blocks,
    load_physical_dataset,
    machine_casing_for,
    tier_block,
)

_REPO = Path(__file__).resolve().parents[1]
_PLANT_FIXTURE = _REPO / "data" / "multiblocks" / "gregtech_gt_blockmachines_998.json"
_AUTOCLAVE = "gregtech:gt.blockmachines@687"


def _plant_dump() -> dict[str, list[BlockId]]:
    """The committed plant fixture's substitution channels, as the dump lists them."""
    doc = json.loads(_PLANT_FIXTURE.read_text(encoding="utf-8"))
    return {
        channel: [(sub["block"], sub["meta"]) for sub in subs]
        for channel, subs in doc["substitutions"].items()
    }


def _control(control_id: str) -> dict[str, Any]:
    """The arodoid example's Chemical Plant control ``control_id``, as gtnh-factory-flow exports it."""
    plan = json.loads((_REPO / "examples" / "ev-nitrobenzene.json").read_text(encoding="utf-8"))
    for recipe in plan["recipes"]:
        if recipe["machineType"] == "Chemical Plant":
            for control in recipe["machineConfigControls"]:
                if control["id"] == control_id:
                    return dict(control)
    raise AssertionError(f"no Chemical Plant control {control_id!r} in ev-nitrobenzene.json")


def _resource_block(resource_id: str) -> BlockId:
    """A factory-flow resource id (``"gregtech:gt.blockcasings2@12"``) as a block; no ``@`` is meta 0."""
    block, _, meta = resource_id.partition("@")
    return (block, int(meta or 0))


# --------------------------------------------------------------------------- the tables


def test_the_coil_table_is_the_planners_heating_coil_control() -> None:
    tiers = _control("heatingCoil")["tiers"]
    assert [(t.key, t.block_id) for t in HEATING_COILS] == [
        (tier["key"], _resource_block(tier["resource"]["id"])) for tier in tiers
    ]


def test_the_pipe_table_is_the_planners_pipe_control_less_the_tiers_the_plant_refuses() -> None:
    """PTFE and PBI are offered by the planner but are other blocks, which the plant's ``check()``
    refuses; they are exactly the aliases, so every key the planner can write resolves."""
    tiers = _control("pipeCasing")["tiers"]
    accepted = [t for t in tiers if t["key"] not in PIPE_CASING_ALIASES]
    assert [(t.key, t.block_id) for t in PIPE_CASINGS] == [
        (tier["key"], _resource_block(tier["resource"]["id"])) for tier in accepted
    ]
    assert {t["key"] for t in tiers} - {t["key"] for t in accepted} == set(PIPE_CASING_ALIASES)


def test_the_solid_casing_table_is_the_dumped_casing_channel_in_tier_order() -> None:
    assert [t.block_id for t in SOLID_CASINGS] == _plant_dump()[SOLID_CASING]


def test_the_coil_table_is_the_dumped_coil_channel_in_tier_order() -> None:
    assert [t.block_id for t in HEATING_COILS] == _plant_dump()[COIL]


def test_a_key_resolves_to_its_rung() -> None:
    assert tier_block(SOLID_CASINGS, "titanium") == (SOLID_CASINGS[4], False)
    assert tier_block(HEATING_COILS, "hss_g")[0] == HEATING_COILS[4]


@pytest.mark.parametrize("alias", sorted(PIPE_CASING_ALIASES))
def test_a_pipe_the_plant_refuses_builds_as_tungstensteel(alias: str) -> None:
    rung, aliased = tier_block(PIPE_CASINGS, alias, PIPE_CASING_ALIASES)
    assert rung == PIPE_CASINGS[-1]
    assert aliased


def test_an_alias_reaches_nothing_without_the_alias_table() -> None:
    assert tier_block(PIPE_CASINGS, "ptfe") == (None, False)


def test_an_unknown_key_resolves_to_nothing() -> None:
    assert tier_block(SOLID_CASINGS, "unobtainium") == (None, False)


# --------------------------------------------------------------------------- machine casing


@pytest.mark.parametrize(
    ("tier", "meta"),
    [("ULV", 0), ("LV", 1), ("HV", 3), ("EV", 4), ("UV", 8), ("UHV", 9), ("UEV", 9), ("MAX", 9)],
)
def test_the_machine_casing_is_the_supplied_tier_capped_at_uhv(tier: str, meta: int) -> None:
    assert machine_casing_for(tier) == ("gregtech:gt.blockcasings", meta)


def test_a_tier_off_the_ladder_has_no_machine_casing() -> None:
    assert machine_casing_for("XV") is None


# --------------------------------------------------------------------------- the gap rule


def test_the_plants_channels_take_the_tiers_gt_accepts_but_never_places() -> None:
    accepted = channel_blocks(CHEMICAL_PLANT, _plant_dump())

    assert accepted[PIPE] == tuple(("gregtech:gt.blockcasings2", meta) for meta in range(12, 16))
    assert accepted[MACHINE_CASING] == tuple(
        ("gregtech:gt.blockcasings", meta) for meta in range(10)
    )
    assert accepted[SOLID_CASING] == tuple(t.block_id for t in SOLID_CASINGS)
    assert accepted[COIL] == tuple(_plant_dump()[COIL]), "a channel the rule skips is as dumped"


def test_the_committed_record_carries_its_channels() -> None:
    record = load_physical_dataset(_REPO / "data" / "multiblocks").by_block_key[CHEMICAL_PLANT]

    assert dict(record.substitutions) == {
        channel: tuple(blocks) for channel, blocks in _plant_dump().items()
    }
    assert record.channel_blocks == channel_blocks(CHEMICAL_PLANT, _plant_dump())


def test_another_controllers_pipe_channel_is_left_as_dumped() -> None:
    """The Industrial Autoclave also has a ``pipe`` channel, built by another element: the rule is
    read off the plant's structure definition and says nothing about it."""
    dumped = {PIPE: [("gregtech:gt.blockcasings2", 13), ("gregtech:gt.blockcasings2", 14)]}
    assert channel_blocks(_AUTOCLAVE, dumped) == {PIPE: tuple(dumped[PIPE])}


def test_a_channel_the_dump_does_not_record_is_not_invented() -> None:
    assert channel_blocks(CHEMICAL_PLANT, {COIL: _plant_dump()[COIL]}).keys() == {COIL}


def test_a_dump_that_contradicts_the_rule_wins_with_a_warning() -> None:
    dumped = {PIPE: [("gregtech:gt.blockcasings2", 13), ("gregtech:gt.blockcasings8", 1)]}
    with pytest.warns(DatasetWarning, match=r"'pipe' channel lists .*gt\.blockcasings8"):
        accepted = channel_blocks(CHEMICAL_PLANT, dumped)
    assert accepted == {PIPE: tuple(dumped[PIPE])}
