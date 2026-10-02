"""The output layers the extractor recorded on the committed tower fixtures (#299).

GT fills a Distillation Tower's outputs by layer: recipe fluid output ``i`` goes only to the output
hatches on the tower's ``i``-th layer above its base, and the tower does not form while any layer
has none. The extractor reads which layer each output cell feeds from the machine's own structure
check (``HatchProbe.outputLayers``) and records it as ``HatchSlot.output_layer``. These pin what it
found on the two towers the shipped examples use, against GT's own bookkeeping
(``mOutputHatchesByLayer.get(mHeight - 1)``, where the first layer above the base is height 1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gtnh_solver.dataset import MultiblockDoc, load_multiblock_doc
from gtnh_solver.dataset.schema import HatchSlot

_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "multiblocks"
#: The two committed towers: GT's Distillation Tower and GT++'s Dangote Distillus. Both are a 3x3 ring
#: per layer around a hollow core, with the controller at the front of the base.
_TOWERS = ["gregtech_gt_blockmachines_1126.json", "gregtech_gt_blockmachines_31021.json"]


def _tower(name: str) -> MultiblockDoc:
    return load_multiblock_doc(_DATA_DIR / name)


def _is_top_centre(slot: HatchSlot, height: int) -> bool:
    """The cell that closes a 3x3 tower: the middle of its top layer, above the hollow core."""
    dx, dy, dz = slot.d
    return dy == height - 1 and (dx, dz) == (0, 1)


@pytest.mark.parametrize("name", _TOWERS)
def test_every_ring_cell_feeds_the_layer_its_height_names(name: str) -> None:
    for variant in _tower(name).variants:
        height = variant.bbox[1]
        ring = [
            s
            for s in variant.hatch_slots
            if "OutputHatch" in s.kinds and s.d[1] >= 1 and not _is_top_centre(s, height)
        ]
        assert ring, f"{name} form {variant.trigger_stack_size} records no output ring"
        for slot in ring:
            assert slot.output_layer == slot.d[1] - 1, (variant.trigger_stack_size, slot)


@pytest.mark.parametrize("name", _TOWERS)
def test_each_form_has_one_layer_per_story_above_the_base(name: str) -> None:
    # A form h blocks tall has h - 1 output layers, numbered from 0, every one holding cells: the
    # tower forms only with an output hatch on each.
    for variant in _tower(name).variants:
        layers = {s.output_layer for s in variant.hatch_slots if s.output_layer is not None}
        assert layers == set(range(variant.bbox[1] - 1)), variant.trigger_stack_size


@pytest.mark.parametrize("name", _TOWERS)
def test_the_top_centre_and_the_base_feed_no_layer(name: str) -> None:
    # GT files a hatch in the top centre with the plain outputs (addOutputToMachineList), not by
    # layer, and the base takes no layered output hatch at all.
    for variant in _tower(name).variants:
        height = variant.bbox[1]
        for slot in variant.hatch_slots:
            if slot.d[1] == 0 or _is_top_centre(slot, height):
                assert slot.output_layer is None, (variant.trigger_stack_size, slot)


def test_the_top_centre_still_takes_an_output_hatch() -> None:
    # It is a slot, just not a layered one: a hatch there ends the tower as the casing does.
    for variant in _tower(_TOWERS[0]).variants:
        top = [s for s in variant.hatch_slots if _is_top_centre(s, variant.bbox[1])]
        assert len(top) == 1
        assert "OutputHatch" in top[0].kinds


def _committed_docs() -> list[Path]:
    return sorted(p for p in _DATA_DIR.glob("*.json") if p.name != "_meta.json")


@pytest.mark.parametrize("path", _committed_docs(), ids=lambda p: p.name)
def test_only_output_slots_carry_a_layer(path: Path) -> None:
    for variant in load_multiblock_doc(path).variants:
        for slot in variant.hatch_slots:
            if slot.output_layer is not None:
                assert "OutputHatch" in slot.kinds, slot


@pytest.mark.parametrize("path", _committed_docs(), ids=lambda p: p.name)
def test_no_fixture_notes_a_failed_layer_probe(path: Path) -> None:
    # The layer probe drops its answer, with a note in the doc's failures, when it cannot be whole.
    # A committed fixture carrying one would ship a tower with no layers, which docks anywhere.
    failures = load_multiblock_doc(path).failures
    assert not [f for f in failures if "output layer" in f], failures


def test_only_the_towers_record_layers() -> None:
    # Every other committed machine fills its outputs first fit, so none of its slots has a layer.
    layered = {
        path.name
        for path in _committed_docs()
        if any(
            s.output_layer is not None
            for v in load_multiblock_doc(path).variants
            for s in v.hatch_slots
        )
    }
    assert layered == set(_TOWERS)
