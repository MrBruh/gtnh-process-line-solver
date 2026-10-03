"""Tests for reading a ShadowTheAge calculator plan (``.gtnh``) through gtnh-shadow-convert (#293).

Two layers. Most of this file needs no Shadow data and no converter: ``fixtures/shadow-minimal.json``
is a hand-made plan in the shape the converter writes, and the converter call is stubbed. That pins
what this repo relies on: the ``converter`` block identifies the plan ahead of its
``machineHandlers``, its tiers are not re-tiered, and its one runtime variant per recipe is read so
that the node's parallels count exactly once.

The contract test at the end runs the real converter (the ``shadow`` extra, pinned in pyproject) on
a tiny synthetic ``data.bin`` built with its own ``testing.SyntheticData``, so CI checks that what
the pinned converter writes is still a plan this adapter loads.
"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest

from gtnh_solver.adapter import (
    SHADOW_EXTRA_HINT,
    AdapterError,
    ConverterInfo,
    MachineHandler,
    Node,
    Plan,
    PlanProducer,
    Recipe,
    Resource,
    RuntimeCalculation,
    RuntimeVariant,
    describe_markers,
    detect_producer,
    load_plan,
    load_shadow_plan,
    plan_pack_version,
    to_input_ir,
)
from gtnh_solver.adapter import shadow as shadow_module
from gtnh_solver.adapter.core import _matched_variant, _node_eut, _rate, _trigger_stack
from gtnh_solver.ir import IODirection

_ROOT = Path(__file__).resolve().parents[1]
_MINIMAL = _ROOT / "tests" / "fixtures" / "shadow-minimal.json"
_EXAMPLE = _ROOT / "examples" / "shadow-nitrobenzene.json"
_EXAMPLE_GTNH = _ROOT / "examples" / "Shadow-NB.gtnh"


def _raw() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_MINIMAL.read_text(encoding="utf-8"))
    return data


def _plan(raw: dict[str, Any] | None = None) -> Plan:
    return Plan.model_validate(_raw() if raw is None else raw)


# ------------------------------------------------------------------ provenance


@pytest.mark.parametrize("path", [_MINIMAL, _EXAMPLE], ids=lambda p: p.name)
def test_a_converted_plan_detects_as_shadow(path: Path) -> None:
    plan = load_plan(path)
    assert plan.converter is not None
    assert plan.converter.name == "gtnh-shadow-convert"
    assert plan.converter.pack_version == "2.9.0-beta-2"
    # It carries the arodoid fork's machineHandlers too; the converter block wins.
    assert any(recipe.machine_handlers for recipe in plan.recipes)
    assert detect_producer(plan) is PlanProducer.SHADOW_V1


def test_another_converter_is_undetermined() -> None:
    raw = _raw()
    raw["converter"]["name"] = "some-other-converter"
    assert detect_producer(_plan(raw)) is None


def test_describe_markers_names_the_converter() -> None:
    assert "converter=gtnh-shadow-convert" in describe_markers(_plan())
    raw = _raw()
    raw["converter"]["name"] = ""
    assert "converter=unnamed" in describe_markers(_plan(raw))
    del raw["converter"]
    assert "converter=absent" in describe_markers(_plan(raw))


def test_the_pack_comes_from_the_recipes() -> None:
    # "shadow-2.9.0-beta-2" is a channel-prefixed dataset id, like the forks' "local-2.9.0-beta-2".
    assert plan_pack_version(_plan()) == "2.9.0-beta-2"
    assert to_input_ir(_plan()).pack_version == "2.9.0-beta-2"


# ------------------------------------------------------------------ mapping


def test_the_node_parallels_count_once() -> None:
    plan = _plan()
    recipes = {r.id: r for r in plan.recipes}
    tower = next(n for n in plan.nodes if n.id == "node-1")
    recipe = recipes[tower.recipe_id]
    variant = _matched_variant(recipe, tower)
    assert variant is not None
    assert (variant.eut, variant.parallel, tower.parallel) == (240, 1, 4)
    # Per machine: one parallel's EU/t times the node's four, never times four twice.
    assert _node_eut(recipe, tower, {}) == 960
    assert _rate(recipe, "benzene", tower, outputs=True) == 400 * 4 / 11
    machine = next(m for m in to_input_ir(plan).machines if m.id == "node-1")
    assert machine.eut == 960
    benzene = next(p for p in machine.faces.ports if p.id == "output:benzene")
    assert benzene.rate == pytest.approx(400 * 4 / 11)


def test_the_coil_selects_the_variant() -> None:
    plan = _plan()
    recipes = {r.id: r for r in plan.recipes}
    oven = next(n for n in plan.nodes if n.id == "node-0")
    variant = _matched_variant(recipes[oven.recipe_id], oven)
    assert variant is not None
    assert variant.coil_tier == oven.coil_tier == "cupronickel"


def test_coke_oven_slices_choose_the_form() -> None:
    plan = _plan()
    recipes = {r.id: r for r in plan.recipes}
    oven = next(n for n in plan.nodes if n.id == "node-0")
    assert _trigger_stack(recipes[oven.recipe_id], oven) == 2


def test_a_circuit_is_a_port_nothing_feeds() -> None:
    ir = to_input_ir(_plan())
    oven = next(m for m in ir.machines if m.id == "node-0#1")
    circuit = "input:gregtech:gt.integrated_circuit@10"
    assert circuit in {p.id for p in oven.faces.ports}
    fed = {(ep.machine_id, ep.port_id) for net in ir.nets for ep in net.endpoints}
    assert ("node-0#1", circuit) not in fed
    # Every consumed input is fed.
    for machine in ir.machines:
        for port in machine.faces.ports:
            if port.direction is IODirection.INPUT and "integrated_circuit" not in port.id:
                if port.commodity.value == "power":
                    continue
                assert (machine.id, port.id) in fed, (machine.id, port.id)


def _heavy_plan(converter: bool) -> Plan:
    """One machine drawing far more than its tier can plausibly deliver, so _supply_tier applies
    (the same line as ``test_adapter_runtime._heavy_plan``)."""
    recipe = Recipe(
        id="r",
        machine_type="M",
        eut=200.0,
        duration_ticks=10.0,
        outputs=[Resource(kind="item", id="x", amount=1.0)],
        machine_handlers=[MachineHandler(id="shadow", kind="multiblock", label="M")],
        runtime_calculation=RuntimeCalculation(
            variants=[
                RuntimeVariant(id="shadow", overclock_tier="LV", eut=200.0, duration_ticks=10.0)
            ]
        ),
    )
    return Plan(
        schema_version=1,
        recipes=[recipe],
        nodes=[Node(id="n", recipe_id="r", overclock_tier="LV")],
        converter=ConverterInfo(name="gtnh-shadow-convert") if converter else None,
    )


def test_the_tiers_are_not_re_tiered() -> None:
    # The player chose them and the draw comes from the calculator's own rules, so the MrBruh
    # fork's workaround (_supply_tier) must not move them, as for an arodoid plan.
    from tests._helpers import hatched_dataset

    plan = _heavy_plan(True)
    assert detect_producer(plan) is PlanProducer.SHADOW_V1
    assert to_input_ir(plan, physical=hatched_dataset()).machines[0].voltage_tier == "LV"
    # The gate is the producer's: the same line, said to be a MrBruh-fork plan, is re-tiered.
    mrbruh = to_input_ir(plan, physical=hatched_dataset(), producer=PlanProducer.MRBRUH_V2)
    assert mrbruh.machines[0].voltage_tier == "MV"


def test_the_example_maps_cleanly() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ir = to_input_ir(load_plan(_EXAMPLE))
    assert len([m for m in ir.machines if m.id.startswith("node-")]) == 10


# ------------------------------------------------------------------ loading a .gtnh


def test_load_shadow_plan_validates_what_the_converter_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[object, object]] = []

    def convert(gtnh: object, data: object) -> dict[str, Any]:
        seen.append((gtnh, data))
        return _raw()

    monkeypatch.setattr(shadow_module, "_require_converter", lambda: convert)
    plan = load_shadow_plan("plan.gtnh", "data.bin")
    assert seen == [("plan.gtnh", "data.bin")]
    assert detect_producer(plan) is PlanProducer.SHADOW_V1


def test_a_converter_error_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    def convert(gtnh: object, data: object) -> dict[str, Any]:
        raise ValueError("no rule for the machine 'Nano Forge'")

    monkeypatch.setattr(shadow_module, "_require_converter", lambda: convert)
    with pytest.raises(ValueError, match="Nano Forge"):
        load_shadow_plan("plan.gtnh", "data.bin")


def test_a_missing_extra_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "gtnh_shadow_convert", None)
    with pytest.raises(AdapterError, match="shadow") as caught:
        load_shadow_plan("plan.gtnh", "data.bin")
    assert SHADOW_EXTRA_HINT in str(caught.value)


# ------------------------------------------------------------------ the pinned converter


def test_the_pinned_converter_writes_a_plan_this_adapter_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end on synthetic data: what the pinned converter writes validates as a Plan, is
    detected as a Shadow plan, and maps to the IR with whole machines and every input fed."""
    pytest.importorskip("gtnh_shadow_convert")
    from gtnh_shadow_convert import fetch
    from gtnh_shadow_convert.testing import Gt, SyntheticData, voltage_tooltip

    data = SyntheticData()
    water = data.fluid("minecraft", "water", "Water")
    steam = data.fluid("IC2", "ic2steam", "Steam")
    acid = data.fluid("gregtech", "sulfuricacid", "Sulfuric Acid")
    circuit = data.item("gregtech", "gt.integrated_circuit", 1, "Programmed Circuit")
    heater = data.item(
        "gregtech", "gt.blockmachines", 621, "Basic Fluid Heater", tooltip=[voltage_tooltip("LV")]
    )
    lcr = data.item("gregtech", "gt.blockmachines", 1169, "Large Chemical Reactor")
    data.recipe_type("Fluid Heater", multiblocks=[heater])
    data.recipe_type("Large Chemical Reactor", multiblocks=[lcr])
    data.recipe(
        "r~heat",
        "Fluid Heater",
        inputs=[(water, 100), (circuit, 0)],
        outputs=[(steam, 100)],
        gt=Gt(voltage=30, duration_ticks=20),
    )
    data.recipe(
        "r~acid",
        "Large Chemical Reactor",
        inputs=[(steam, 1000)],
        outputs=[(acid, 500)],
        gt=Gt(voltage=120, duration_ticks=100, voltage_tier=1),
    )
    data_bin = data.write(tmp_path / "data.bin")
    gtnh = tmp_path / "plan.gtnh"
    gtnh.write_text(
        json.dumps(
            {
                "name": "Synthetic",
                "products": [{"goodsId": acid, "amount": 3000}],
                "rootGroup": {
                    "type": "recipe_group",
                    "links": {},
                    "elements": [
                        {"type": "recipe", "recipeId": "r~heat", "voltageTier": 0, "choices": {}},
                        {"type": "recipe", "recipeId": "r~acid", "voltageTier": 2, "choices": {}},
                    ],
                },
                "settings": {"minVoltage": 0, "timeUnit": "min"},
            }
        ),
        encoding="utf-8",
    )
    sha = fetch.file_sha256(data_bin)
    monkeypatch.setitem(fetch.KNOWN_DATA, sha, fetch.KnownData("2.9.0-beta-2", 7, "", "", "test"))
    plan = load_shadow_plan(gtnh, data_bin)
    assert detect_producer(plan) is PlanProducer.SHADOW_V1
    assert plan.converter is not None
    assert plan.converter.data_sha256 == sha
    nodes = {n.id: n for n in plan.nodes}
    # 3000 acid a minute: 6 LCR runs (perfect OC at HV: 4x speed), 6000 steam, 60 heater runs.
    assert nodes["node-1"].overclock_tier == "HV"
    assert nodes["node-0"].machine_count == 1
    ir = to_input_ir(plan)
    assert ir.pack_version == "2.9.0-beta-2"
    fed = {(ep.machine_id, ep.port_id) for net in ir.nets for ep in net.endpoints}
    assert ("node-1", "input:ic2steam") in fed
    assert ("node-0", "input:water") in fed
