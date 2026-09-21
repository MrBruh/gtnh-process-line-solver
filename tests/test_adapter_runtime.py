"""Tests for the runtime figures a node actually runs at: post-overclock EU/t and duration.

``recipe.eut`` and ``recipe.durationTicks`` are the **base** values at the recipe's minimum tier. A
machine run above that tier draws 4x and runs 2x faster per step, so reading the base values
understates a whole plan's draw by ~6x and a single machine by up to 256x, and understates every
pipe's flow by the matching factor. Both forks ship the real per-tier figures in
``recipes[].runtimeCalculation``, which is why consuming them needs no producer branch.

The tests that matter most here are the negative ones: that selection is by field rather than by id
string, that an ambiguous match refuses to guess, and that the committed fixtures do not move.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from gtnh_solver.adapter import (
    AdapterWarning,
    Edge,
    MachineConfigControl,
    MachineConfigTier,
    MachineHandler,
    Node,
    Plan,
    Recipe,
    ResolvedMachine,
    Resource,
    RuntimeCalculation,
    RuntimeVariant,
    Storage,
    load_plan,
    to_input_ir,
)
from gtnh_solver.adapter.core import (
    _check_unmodelled_parallel,
    _effective_duration,
    _handler_parallel,
    _matched_variant,
    _node_eut,
    _synthesized_eut,
)

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
#: The fixtures that run at their recipe's own tier. ``ev-nitrobenzene.json`` is deliberately not
#: one: most of its nodes run overclocked, so its figures are *meant* to move (#204).
_FIXTURES = [
    _EXAMPLES / "gtnh-sand.json",
    _EXAMPLES / "gtnh-nitrobenzene.json",
    _EXAMPLES / "gtnh-parallel-sand.json",
]


def _variant(
    variant_id: str,
    tier: str,
    eut: float,
    duration: float = 10.0,
    coil: str | None = None,
    parallel: int = 1,
) -> RuntimeVariant:
    return RuntimeVariant(
        id=variant_id,
        overclock_tier=tier,
        coil_tier=coil,
        eut=eut,
        duration_ticks=duration,
        parallel=parallel,
    )


def _recipe(*variants: RuntimeVariant, eut: float = 30.0, duration: float = 40.0) -> Recipe:
    return Recipe(
        id="r",
        machine_type="M",
        eut=eut,
        duration_ticks=duration,
        inputs=[Resource(kind="item", id="feed", amount=8.0)],
        outputs=[Resource(kind="item", id="product", amount=1.0)],
        runtime_calculation=RuntimeCalculation(variants=list(variants)) if variants else None,
    )


def _node(tier: str = "EV", coil: str = "", parallel: int = 1) -> Node:
    return Node(id="n", recipe_id="r", overclock_tier=tier, coil_tier=coil, parallel=parallel)


# ------------------------------------------------------------------ variant selection


def test_a_variant_is_selected_by_overclock_tier() -> None:
    recipe = _recipe(_variant("tier-lv", "LV", 30.0), _variant("tier-ev", "EV", 1920.0))
    variant = _matched_variant(recipe, _node("EV"))
    assert variant is not None
    assert variant.eut == 1920.0


def test_selection_is_by_field_not_by_id_string() -> None:
    # Real ids carry suffixes beyond the tier ("tier-ev-perfect-oc"). Composing "tier-ev" and
    # matching on it misses roughly half the nodes of a real plan while appearing to work.
    recipe = _recipe(_variant("tier-ev-perfect-oc", "EV", 1920.0))
    variant = _matched_variant(recipe, _node("EV"))
    assert variant is not None
    assert variant.eut == 1920.0


def test_a_coil_narrows_among_coil_keyed_variants() -> None:
    recipe = _recipe(
        _variant("tier-ev-coil-cupronickel", "EV", 1000.0, coil="cupronickel"),
        _variant("tier-ev-coil-hss_g", "EV", 1486.0, coil="hss_g"),
    )
    variant = _matched_variant(recipe, _node("EV", coil="hss_g"))
    assert variant is not None
    assert variant.eut == 1486.0


def test_coil_keyed_variants_without_a_stated_coil_stay_ambiguous() -> None:
    # Every coil is a different heat bonus and so a different EU/t; guessing one would be inventing
    # a machine the plan never described.
    recipe = _recipe(
        _variant("tier-ev-coil-cupronickel", "EV", 1000.0, coil="cupronickel"),
        _variant("tier-ev-coil-hss_g", "EV", 1486.0, coil="hss_g"),
    )
    assert _matched_variant(recipe, _node("EV")) is None


def test_no_variant_for_the_nodes_tier_is_no_match() -> None:
    assert _matched_variant(_recipe(_variant("tier-lv", "LV", 30.0)), _node("EV")) is None


def test_a_recipe_without_runtime_figures_has_no_match() -> None:
    assert _matched_variant(_recipe(), _node("EV")) is None


# ------------------------------------------------------------------ the EU/t ladder


def test_the_variant_beats_the_base_recipe_figure() -> None:
    # An EV machine running an LV recipe: 4^3 = 64x the base value.
    recipe = _recipe(_variant("tier-ev", "EV", 1920.0), eut=30.0)
    assert _synthesized_eut(recipe, _node("EV")) == 1920.0


def test_an_unmatched_node_falls_back_to_the_base_figure() -> None:
    recipe = _recipe(_variant("tier-lv", "LV", 30.0), eut=30.0)
    assert _synthesized_eut(recipe, _node("EV")) == 30.0


def test_both_parallel_factors_multiply_through() -> None:
    recipe = _recipe(_variant("tier-ev", "EV", 100.0, parallel=3))
    assert _synthesized_eut(recipe, _node("EV", parallel=2)) == 600.0


def test_a_resolved_figure_still_wins_over_the_variant() -> None:
    # resolved is the exporter's own balancer output and accounts for machine count and parallelism
    # the variant cannot see, so it stays the top of the ladder.
    recipe = _recipe(_variant("tier-mv", "MV", 96.0), eut=96.0)
    node = _node("MV")
    resolved = {"n": ResolvedMachine(node_id="n", total_eut=2355.0)}
    with pytest.warns(AdapterWarning, match="trusting the resolved figure"):
        assert _node_eut(recipe, node, resolved) == 2355.0


# ------------------------------------------------------------------ duration and throughput


def test_the_variant_duration_wins() -> None:
    recipe = _recipe(_variant("tier-ev", "EV", 1920.0, duration=5.0), duration=40.0)
    assert _effective_duration(recipe, _node("EV")) == 5.0


def test_duration_falls_back_when_unmatched() -> None:
    recipe = _recipe(_variant("tier-lv", "LV", 30.0, duration=5.0), duration=40.0)
    assert _effective_duration(recipe, _node("EV")) == 40.0


def test_throughput_uses_the_overclocked_duration() -> None:
    # Fixing EU/t without this leaves every pipe on an overclocked machine sized for a fraction of
    # the flow it carries: 8 items per 5 ticks, not per 40.
    recipe = _recipe(_variant("tier-ev", "EV", 1920.0, duration=5.0), duration=40.0)
    plan = Plan(
        schema_version=1,
        recipes=[recipe],
        nodes=[_node("EV")],
        storages=[Storage(id="s", kind="item")],
        edges=[Edge(id="e", source="s", target="n", resource_kind="item", resource_id="feed")],
    )
    ir = to_input_ir(plan)
    assert next(n for n in ir.nets if n.id == "e").throughput == pytest.approx(1.6)


# ------------------------------------------------------------------ unmodelled parallelism


def _handler_with_parallel(*tiers: MachineConfigTier, default: str = "") -> MachineHandler:
    return MachineHandler(
        id="h",
        kind="multiblock",
        label="Dangote Distillus",
        machine_config_controls=[
            MachineConfigControl(id="machineParallel", default_key=default, tiers=list(tiers))
        ],
    )


def test_the_default_configuration_supplies_the_multiplier() -> None:
    recipe = _recipe()
    recipe.machine_handlers = [
        _handler_with_parallel(
            MachineConfigTier(key="fixed-12", parallel_multiplier=12), default="fixed-12"
        )
    ]
    assert _handler_parallel(recipe, _node()) == 12


def test_the_node_can_choose_a_different_configuration() -> None:
    recipe = _recipe()
    recipe.machine_handlers = [
        _handler_with_parallel(
            MachineConfigTier(key="fixed-12", parallel_multiplier=12),
            MachineConfigTier(key="fixed-4", parallel_multiplier=4),
            default="fixed-12",
        )
    ]
    node = _node()
    node.machine_config_tiers = {"machineParallel": "fixed-4"}
    assert _handler_parallel(recipe, node) == 4


def test_no_parallel_control_means_one() -> None:
    assert _handler_parallel(_recipe(), _node()) == 1.0


def test_unmodelled_parallelism_warns_and_names_the_machine() -> None:
    recipe = _recipe()
    recipe.machine_handlers = [
        _handler_with_parallel(
            MachineConfigTier(key="fixed-12", parallel_multiplier=12), default="fixed-12"
        )
    ]
    with pytest.warns(AdapterWarning, match="under-provisions") as caught:
        _check_unmodelled_parallel(recipe, _node())
    message = str(caught[0].message)
    assert "Dangote Distillus" in message
    assert "12x parallel" in message


def test_a_fractional_multiplier_reads_cleanly() -> None:
    # parallelMultiplier is fractional in practice (1.5, 2.5, 3.5), which is also why it is
    # reported rather than composed: it is a throughput multiplier, not a batch count.
    recipe = _recipe()
    recipe.machine_handlers = [
        _handler_with_parallel(MachineConfigTier(key="t", parallel_multiplier=1.5), default="t")
    ]
    with pytest.warns(AdapterWarning, match=r"1\.5x parallel"):
        _check_unmodelled_parallel(recipe, _node())


def test_a_machine_without_extra_parallelism_is_quiet() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _check_unmodelled_parallel(_recipe(), _node())


# ------------------------------------------------------------------ the re-tier workaround gate


def _heavy_plan(producer_markers: bool) -> Plan:
    """One machine drawing far more than its tier can plausibly deliver, so _supply_tier applies."""
    recipe = Recipe(
        id="r",
        machine_type="M",
        eut=200.0,
        duration_ticks=10.0,
        outputs=[Resource(kind="item", id="x", amount=1.0)],
        # A realistic arodoid plan carries both markers together: the handler that identifies
        # the fork, and the runtime figures that make its draw trustworthy.
        machine_handlers=[MachineHandler(id="h", kind="multiblock", label="M")]
        if producer_markers
        else [],
        runtime_calculation=RuntimeCalculation(
            variants=[_variant("tier-lv", "LV", 200.0, duration=10.0)]
        )
        if producer_markers
        else None,
    )
    return Plan(
        schema_version=1, recipes=[recipe], nodes=[Node(id="n", recipe_id="r", overclock_tier="LV")]
    )


def test_a_arodoid_plan_is_not_re_tiered() -> None:
    # Its EU/t comes from GT's own overclock calculator, so the workaround would move a machine that
    # was already right.
    from tests._helpers import hatched_dataset

    ir = to_input_ir(_heavy_plan(True), physical=hatched_dataset())
    assert ir.machines[0].voltage_tier == "LV"


def test_an_undetermined_plan_keeps_the_defensive_re_tier() -> None:
    # The workaround changes only the voltage supplied, never the stated draw, so applying it when
    # unsure is the safe direction and is what every hand-built plan has always done.
    from tests._helpers import hatched_dataset

    ir = to_input_ir(_heavy_plan(False), physical=hatched_dataset())
    assert ir.machines[0].voltage_tier == "MV"


# ------------------------------------------------------------------ the committed fixtures


@pytest.mark.parametrize("path", _FIXTURES, ids=lambda p: p.name)
def test_the_committed_fixtures_do_not_move(path: Path) -> None:
    """Every committed fixture runs at its recipe's own tier, so the ladder must be a no-op there.

    This is the regression guard for the whole change: if consuming runtime figures ever shifts one
    of these, the golden layouts and every solver test built on them shift with it.
    """
    plan = load_plan(path)
    recipes = {r.id: r for r in plan.recipes}
    resolved = {m.node_id: m for m in plan.resolved.machines} if plan.resolved else {}
    for node in plan.nodes:
        recipe = recipes[node.recipe_id]
        if node.id in resolved:
            continue  # the resolved figure wins regardless; covered above
        assert _synthesized_eut(recipe, node) == recipe.eut * node.parallel
        assert _effective_duration(recipe, node) == recipe.duration_ticks
