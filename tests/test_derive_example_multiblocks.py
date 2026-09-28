"""The committed multiblocks the example lines resolve to, and the tool that derives them.

``tools/derive_example_multiblocks.py`` trims each controller the shipped examples need out of a full
local dump and commits it to ``data/multiblocks/``, so a fresh clone (and CI) reserves each machine's
real form instead of a 1x1x1 box. What that buys is the headline here: both nitrobenzene lines
resolve every multiblock from the committed data alone, and solve. The rest pins the two rules the
trim leans on, and, where a local dump is staged, that the trim changed nothing the solver is handed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import warnings
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

from gtnh_solver.adapter import adapt_file, load_plan
from gtnh_solver.dataset import PhysicalDataset, load_physical_dataset
from gtnh_solver.ir import InputIR, LayoutStatus
from gtnh_solver.solver import solve

_REPO = Path(__file__).resolve().parents[1]
_TOOL = _REPO / "tools" / "derive_example_multiblocks.py"
_COMMITTED = _REPO / "data" / "multiblocks"
_NITROBENZENE = _REPO / "examples" / "gtnh-nitrobenzene.json"
_EV_NITROBENZENE = _REPO / "examples" / "ev-nitrobenzene.json"
#: The pack the committed trim was cut from (``data/multiblocks/README.md``), read by explicit path:
#: the suite pins dataset resolution to the committed copy (``conftest._pinned_dataset_root``).
_SOURCE_DUMP = _REPO / "data" / "2.9.0-beta-2" / "multiblocks"

#: The controller each line's machines resolve to, by ``mID``, with how many machines. The ev line's
#: is the same census ``test_acceptance_2_9`` takes against the full dump: 31021 is the Dangote.
_EV_CONTROLLERS = Counter({31021: 1, 1126: 3, 15512: 3, 998: 2, 1169: 2, 15543: 2, 2730: 1})
_NITROBENZENE_CONTROLLERS = Counter({998: 1, 1126: 2, 791: 1, 1169: 3})


def _tool() -> ModuleType:
    """``tools/derive_example_multiblocks.py`` imported by path (``tools/`` is not a package)."""
    spec = importlib.util.spec_from_file_location("derive_example_multiblocks", _TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def committed() -> PhysicalDataset:
    return load_physical_dataset(_COMMITTED)


def _adapted(plan: Path, dataset: PhysicalDataset) -> InputIR:
    with warnings.catch_warnings():
        # The plans' own caveats (the Dangote's 12x parallel, a resolved EU/t mismatch) are pinned
        # by the adapter's tests; here they are noise.
        warnings.simplefilter("ignore")
        return adapt_file(plan, physical=dataset)


def _controllers(plan: Path, dataset: PhysicalDataset) -> Counter[int]:
    """How many of the line's machines resolved to each controller ``mID``, via a real record."""
    records = dataset.by_block_key
    return Counter(
        records[m.block_key].meta
        for m in _adapted(plan, dataset).machines
        if m.block_key is not None and m.block_key in records
    )


# ------------------------------------------------------------------ what the committed data buys


@pytest.mark.parametrize(
    ("plan", "expected"),
    [(_NITROBENZENE, _NITROBENZENE_CONTROLLERS), (_EV_NITROBENZENE, _EV_CONTROLLERS)],
    ids=["nitrobenzene", "ev-nitrobenzene"],
)
def test_every_multiblock_of_the_example_lines_resolves_from_the_committed_data(
    plan: Path, expected: Counter[int], committed: PhysicalDataset
) -> None:
    # Every machine standing for a plan node finds its controller, so none falls to 1x1x1.
    nodes = {node.id for node in load_plan(plan).nodes}
    machines = [m for m in _adapted(plan, committed).machines if m.id.split("#")[0] in nodes]
    assert all(m.footprint.volume > 1 for m in machines)
    assert _controllers(plan, committed) == expected
    assert sum(expected.values()) == len(machines)


def test_ev_nitrobenzene_solves_from_the_committed_data(committed: PhysicalDataset) -> None:
    # The acceptance line (#204) on a fresh clone: fourteen multiblocks, a Dangote Distillus and
    # shared multiblock ports, VALID from the committed data and the suite's one short attempt.
    layout = solve(_adapted(_EV_NITROBENZENE, committed), seed=0)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility


# ------------------------------------------------------------------------ the tool's own rules


def test_rendering_round_trips_every_derived_fixture() -> None:
    # The renderer joins short arrays and flat objects onto one line with regular expressions, so it
    # must never change what a document says. Every derived file is also exactly what the tool
    # writes, so none was edited by hand after the tool wrote it.
    tool = _tool()
    derived = [
        path
        for path in sorted(_COMMITTED.glob("*.json"))
        if path.name not in {"_meta.json", *tool._HAND_AUTHORED}
    ]
    assert derived, "the committed data carries the example lines' controllers"
    for path in derived:
        text = path.read_text(encoding="utf-8")
        doc = json.loads(text)
        assert json.loads(tool._render(doc)) == doc, path.name
        assert tool._render(doc) == text, f"{path.name} is not what the tool writes"


def test_a_tower_keeps_every_form_up_to_the_tallest_it_reserves(committed: PhysicalDataset) -> None:
    # A layer-indexed machine picks the shortest form with room for its outputs, and reads its forms
    # as a height ladder only while they climb one layer at a time. Keeping just the reserved rung
    # would break the ladder, and every tower would fall back to its largest form.
    tool = _tool()
    tower = committed.by_block_key["gregtech:gt.blockmachines@1126"]
    assert tower.is_layer_indexed
    assert tool._kept_stacks(tower, {3}) == {1, 2, 3}
    assert tool._kept_stacks(tower, {1}) == {1}


def test_a_fixed_machine_keeps_only_the_forms_it_reserves(committed: PhysicalDataset) -> None:
    oven = committed.by_block_key["gregtech:gt.blockmachines@15543"]
    assert not oven.is_layer_indexed
    assert _tool()._kept_stacks(oven, {1}) == {1}


@pytest.mark.skipif(
    not _SOURCE_DUMP.is_dir(), reason=f"no local dump at {_SOURCE_DUMP} to compare the trim against"
)
def test_the_trim_adapts_every_example_exactly_as_the_full_dump_does(
    monkeypatch: pytest.MonkeyPatch, committed: PhysicalDataset
) -> None:
    # The tool checks this on every run; this keeps the committed files honest between runs, on a
    # machine that has the dump they were cut from.
    tool = _tool()
    monkeypatch.setattr(tool, "REPO", _REPO)
    dump = load_physical_dataset(_SOURCE_DUMP)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tool._check_unchanged(dump, tool._adapted(dump), tool._adapted(committed))
