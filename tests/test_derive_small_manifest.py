"""The one rule in ``tools/derive_small_manifest.py`` that must never degrade quietly.

The tool writes a **committed** artifact, which makes its failure mode unlike the rest of the
codebase: a manifest that silently lost its cables is byte-for-byte a plausible manifest, it gets
committed, and every consumer downstream then reports a *different* symptom (the exporter refuses,
the previewer draws flat bars) with no trace of the one cause. So the
route rule fails the run instead of keeping what it happens to find - that is the regression GT's
2.9 rename produced and this pins (#176).

The rest of the tool is exercised by running it; this covers only the guard, because the guard is
the part that has to hold when a *new* dataset arrives and nobody is watching - plus the one example
the tool must skip, which is the same kind of quiet failure pointed the other way: a committed
manifest that grew to cover a line it was never meant to (#204) - and the tiers a fixture's channels
accept but its blocks never show, which a manifest pruned to the dump alone would drop (#312).

What the tool wrote is checked end to end as well: every example it prunes for must export a
``.schematic`` from the committed data alone, as on a fresh clone (#319).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import warnings
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from gtnh_solver.adapter import adapt_file
from gtnh_solver.dataset import CHEMICAL_PLANT, load_physical_dataset
from gtnh_solver.previewer.textures import TextureManifest, load_multiblock_docs
from gtnh_solver.schematic import build_schematic
from gtnh_solver.solver import solve

_REPO = Path(__file__).resolve().parents[1]
_TOOL = _REPO / "tools" / "derive_small_manifest.py"
_COMMITTED_MANIFEST = _REPO / "data" / "textures" / "manifest.json"
_COMMITTED_MULTIBLOCKS = _REPO / "data" / "multiblocks"


def _tool() -> ModuleType:
    """``tools/derive_small_manifest.py`` imported as a module.

    Loaded by path rather than imported: ``tools/`` is a scripts folder, deliberately not a
    package, so there is nothing on ``sys.path`` to import it from.
    """
    spec = importlib.util.spec_from_file_location("derive_small_manifest", _TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pipe(display_name: str) -> dict[str, Any]:
    """One ``kind: "pipe"`` manifest entry, carrying only what the route lookup reads.

    Which is the name and the kind: the rule under test decides *which blocks to keep*, and the
    sprite stacks a preview would then bake are the previewer's problem, tested there.
    """
    return {"kind": "pipe", "display_name": display_name}


#: The six cable gauges plus every pipe size the router lays, as a dump spells them at each pack.
#: An LV line wants every one of these, so either list is a *complete* answer and neither is a
#: partial one. The large and huge tin pipes joined when the router began sizing item runs (#165);
#: both spellings are copied from real extractions, the 2.9 ones from the 2.9.0-beta-2 dump.
_GAUGES = (1, 2, 4, 8, 12, 16)
_AS_284 = [f"cable.tin.{gauge:02d}" for gauge in _GAUGES] + [
    "gt_pipe_bronze",
    "gt_pipe_tin",
    "gt_pipe_tin_large",
    "gt_pipe_tin_huge",
]
_AS_29 = [f"{gauge}x Tin Cable" for gauge in _GAUGES] + [
    "Bronze Fluid Pipe",
    "Tin Item Pipe",
    "Large Tin Item Pipe",
    "Huge Tin Item Pipe",
]


def _dump(names: list[str]) -> dict[str, Any]:
    return {
        "blocks": {
            f"gregtech:gt.blockmachines|{1240 + i}": _pipe(name) for i, name in enumerate(names)
        }
    }


def test_a_cable_the_dump_does_not_name_stops_the_run() -> None:
    """The silent empty set this replaced. Run against a dump whose spelling had moved on, the old
    rule kept nothing and said nothing, and the committed manifest went out with no cables in it.
    """
    tool = _tool()

    with pytest.raises(SystemExit) as raised:
        tool._route_keys(_dump(_AS_284[1:]), {"LV"})  # every wanted block but the 1x cable

    message = str(raised.value)
    assert "cable.tin.01" in message, "the run must name what it could not find"
    assert "1x Tin Cable" in message, "and both spellings it looked for"
    assert "cable.tin.02" not in message, "only the missing one; the rest resolved"


def test_a_pipe_size_the_router_lays_but_the_dump_lacks_stops_the_run() -> None:
    """The same guard for a size: a committed manifest without the huge pipe would pass every
    eyeball check and then refuse to export the parallel sand line (#165)."""
    tool = _tool()

    with pytest.raises(SystemExit) as raised:
        tool._route_keys(_dump([n for n in _AS_284 if n != "gt_pipe_tin_huge"]), {"LV"})

    message = str(raised.value)
    assert "gt_pipe_tin_huge" in message
    assert "Huge Tin Item Pipe" in message


def test_both_of_gts_spellings_are_accepted_for_the_same_block() -> None:
    """A dump carries one spelling or the other, never both, and either is a complete answer. The
    2.9 half is the fix: those names joined against nothing before, for every cable at once.
    """
    tool = _tool()

    kept = tool._route_keys(_dump(_AS_29), {"LV"})
    assert kept == tool._route_keys(_dump(_AS_284), {"LV"})
    assert len(kept) == len(_AS_29), "every gauge is kept, not only the ones a line routes today"


def test_the_2_9_acceptance_fixture_stays_out_of_the_committed_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ev-nitrobenzene.json`` is committed to test 2.9 work against, not to be skinned out of the
    box (#204). Were the tool to prune for it, the next regeneration would quietly pull its EV tier
    and every machine only it uses into the committed manifest.

    Run against an ``examples/`` holding only the excluded plans, so the answer cannot lean on what
    the other examples happen to use: skipped by name, they contribute nothing at all. A name that
    no longer matches a committed file would make the exclusion a silent no-op, hence the check.
    """
    tool = _tool()
    examples = tmp_path / "examples"
    examples.mkdir()
    for name in sorted(tool._NOT_MANIFEST_SCOPED):
        source = _REPO / "examples" / name
        assert source.is_file(), f"{name} is excluded by name but is not in examples/"
        shutil.copyfile(source, examples / name)
    monkeypatch.setattr(tool, "REPO", tmp_path)

    assert tool._example_machines() == []


def test_a_fixtures_channel_tiers_the_dump_never_places_are_kept() -> None:
    """Bronze pipe casing and the ULV machine casing are valid in the Chemical Plant but absent from
    its dump (``dataset.channel_blocks``); a plant built from either must still skin and export."""
    kept = _tool()._fixture_block_keys({CHEMICAL_PLANT})
    assert {"gregtech:gt.blockcasings2|12", "gregtech:gt.blockcasings|0"} <= kept


def test_the_committed_manifest_skins_every_block_the_plants_channels_accept() -> None:
    manifest = json.loads(_COMMITTED_MANIFEST.read_text(encoding="utf-8"))
    record = load_physical_dataset(_COMMITTED_MULTIBLOCKS).by_block_key[CHEMICAL_PLANT]
    wanted = {
        f"{block}|{meta}" for blocks in record.channel_blocks.values() for block, meta in blocks
    }
    assert wanted - set(manifest["blocks"]) == set()


@pytest.mark.parametrize("example", _tool()._scoped_examples(), ids=lambda path: path.name)
def test_every_scoped_example_exports_against_the_committed_data(example: Path) -> None:
    """What a fresh clone does with a shipped example: solve it and export a ``.schematic``, from
    the committed manifest and fixtures alone. The manifest is cut for exactly these lines, so a
    block one of them needs and it lacks is a manifest cut wrong, and the exporter refuses that
    block by raising, uncaught here (#319: 2.9's Industrial Coke Oven, absent from a manifest cut
    from a 2.8.4 dump).

    Both files are read by path rather than resolved, since resolution prefers a local dump and
    would test that instead on any machine that has one. Every machine must be placed first: a
    partial layout exports only what was placed, and would pass with the missing block left out.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # pack and cover notes; an untypeable block is an error
        problem = adapt_file(str(example), physical=load_physical_dataset(_COMMITTED_MULTIBLOCKS))
        layout = solve(problem, optimize=False)
        assert {p.machine_id for p in layout.placements} == {m.id for m in problem.machines}
        build_schematic(
            problem,
            layout,
            manifest=TextureManifest.load(_COMMITTED_MANIFEST),
            docs=load_multiblock_docs(_COMMITTED_MULTIBLOCKS),
        )


def test_the_shadow_line_is_in_the_manifests_scope() -> None:
    """The converted ShadowTheAge line is the one #319 broke, so the export test above has to run
    it. Its parameters come from the tool, and an empty or shrunken set would skip quietly."""
    assert "shadow-nitrobenzene.json" in {path.name for path in _tool()._scoped_examples()}
