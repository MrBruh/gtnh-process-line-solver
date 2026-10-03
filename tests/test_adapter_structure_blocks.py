"""What a plan says a multiblock's tiered parts are built from (``adapter/structure_blocks.py``, #312).

First the recipe's special value, which exporters state in up to three places.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gtnh_solver.adapter import AdapterWarning, Recipe, load_plan
from gtnh_solver.adapter.structure_blocks import recipe_special_value

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _recipe(**fields: Any) -> Recipe:
    """A Chemical Plant recipe carrying only ``fields`` beyond its identity, as an export spells them."""
    return Recipe.model_validate({"id": "r", "machineType": "Chemical Plant", **fields})


def _plant_recipe(example: str) -> Recipe:
    plan = load_plan(str(_EXAMPLES / example))
    return next(r for r in plan.recipes if "Chemical Plant" in r.machine_type)


# --------------------------------------------------------------------------- special value


@pytest.mark.parametrize(
    "fields",
    [
        {"specialValue": 4},
        {"metadata": {"recipeMapId": "gtpp.recipe.fluidchemicaleactor", "specialValue": 4}},
        {"nei": {"additionalInfo": ["Total: 9,600 EU", "Special value: 4"]}},
        {"nei": {"additionalInfo": ["special  VALUE:4"]}},
    ],
    ids=["top-level", "metadata", "nei", "nei-spacing-and-case"],
)
def test_each_place_a_special_value_is_stated_is_read(fields: dict[str, Any]) -> None:
    assert recipe_special_value(_recipe(**fields)) == 4


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"nei": None},
        {"nei": {"additionalInfo": None}},
        {"nei": {"additionalInfo": ["Total: 9,600 EU"]}},
        {"metadata": {"specialValue": None}},
    ],
    ids=["nothing", "nei-null", "lines-null", "no-matching-line", "metadata-null"],
)
def test_a_recipe_stating_none_has_none(fields: dict[str, Any]) -> None:
    assert recipe_special_value(_recipe(**fields)) is None


def test_agreeing_places_are_read_once_without_a_warning(recwarn: pytest.WarningsRecorder) -> None:
    recipe = _recipe(
        specialValue=4,
        metadata={"specialValue": 4},
        nei={"additionalInfo": ["Special value: 4"]},
    )
    assert recipe_special_value(recipe) == 4
    assert not [w for w in recwarn if issubclass(w.category, AdapterWarning)]


def test_disagreeing_places_warn_and_take_the_highest() -> None:
    recipe = _recipe(specialValue=2, nei={"additionalInfo": ["Special value: 5"]})
    with pytest.warns(AdapterWarning, match=r"states special values \[2, 5\]"):
        assert recipe_special_value(recipe) == 5


def test_a_negative_special_value_reads_as_the_lowest_requirement() -> None:
    assert recipe_special_value(_recipe(nei={"additionalInfo": ["Special value: -1"]})) == 0


@pytest.mark.parametrize(
    ("example", "expected"),
    [
        ("ev-nitrobenzene.json", 4),
        ("gtnh-nitrobenzene.json", 4),
        ("shadow-nitrobenzene.json", None),
    ],
)
def test_the_shipped_plants_state_their_special_value(example: str, expected: int | None) -> None:
    """arodoid states it three times, MrBruh's fork only in NEI, and a converted ShadowTheAge plan
    not at all (it names the casing itself, ``machineConfigTiers.solidCasing``)."""
    assert recipe_special_value(_plant_recipe(example)) == expected
