"""Tests for the cover tier table (:mod:`gtnh_solver.dataset.covers`).

The figures are GT's (``MetaGeneratedItem01.registerCovers`` at 5.09.54.133); these pin the meta
numbers a written cover names and the rule that picks a tier, because a wrong meta is a different
item in game and a tier below the face's rate is a line that backs up.
"""

from __future__ import annotations

import pytest

from gtnh_solver.dataset import COVER_ITEM, COVER_TIERS, cover_for


def test_the_metas_are_gts() -> None:
    """``gt.metaitem.01`` metas are 32000 + the ``IDMetaItem01`` id: conveyors 630 up, pumps 610 up."""
    assert COVER_ITEM == "gregtech:gt.metaitem.01"
    assert [meta for meta, _ in COVER_TIERS["conveyor"].values()] == list(range(32630, 32635))
    assert [meta for meta, _ in COVER_TIERS["pump"].values()] == list(range(32610, 32615))
    assert COVER_TIERS["conveyor"]["HV"] == (32632, 3.2)  # the one on the maintainer's 2.9 chest


@pytest.mark.parametrize(
    ("kind", "rate", "tier"),
    [
        ("conveyor", 0.16, "LV"),  # exactly an LV conveyor's 16 items per 100 ticks
        ("conveyor", 0.17, "MV"),
        ("conveyor", 3.2, "HV"),
        ("conveyor", 64.0, "IV"),
        ("conveyor", 500.0, "IV"),  # past the table: the most it has
        ("pump", 32.0, "LV"),
        ("pump", 100.0, "MV"),
        ("pump", 2048.0, "EV"),
    ],
)
def test_the_lowest_tier_that_keeps_up_is_chosen(kind: str, rate: float, tier: str) -> None:
    choice = cover_for(kind, rate, "LV")
    assert choice.tier == tier
    assert choice.kind == kind
    assert (choice.meta, choice.per_tick) == COVER_TIERS[kind][tier]
    assert choice.label == f"{tier} {kind}"


@pytest.mark.parametrize(
    ("machine_tier", "tier"),
    [("MV", "MV"), ("ULV", "LV"), ("UV", "IV"), ("MAX", "IV"), ("not-a-tier", "LV"), (None, "LV")],
)
def test_an_unknown_rate_takes_the_machines_tier_clamped_onto_the_table(
    machine_tier: str | None, tier: str
) -> None:
    assert cover_for("conveyor", None, machine_tier).tier == tier


def test_a_kind_the_table_lacks_is_an_error() -> None:
    with pytest.raises(KeyError):
        cover_for("robot arm", 1.0, "LV")
