"""Local-only acceptance: a real GTNH 2.9 plan previews and exports as the machines it names (#208).

``examples/ev-nitrobenzene.json`` is the one real 2.9 line in the repo (#204). Before #205 the
adapter resolved each machine's structure by name and then dropped the identity, so the previewer and
the exporter re-guessed it from the recipe-map ``type``: most multiblocks drew nothing, and the
Dangote Distillus drew as a plain Distillation Tower. This is the end-to-end check that every
machine now draws, and exports, as the controller it was sized from::

    plan --adapter--> 14 multiblocks, each carrying the controller it resolved
         --solve(fast)--> placed layout
         --build_scene + machine_cubes--> each machine expands to the doc keyed by ITS block_key
         --build_schematic--> the controller mIDs the plan names, and nothing unresolved

**It needs the real 2.9 dump, so it skips without one.** The committed ``data/multiblocks/`` is a
two-controller sample and the committed manifest is example-scoped; neither holds these machines.
The suite pins the dataset every other test resolves to the committed copy (``tests/conftest.py``),
so this module reads the repo's own ``data/2.9.0-beta-2/`` by explicit path instead of through
``resolve_dataset_path``, and a missing half skips with the extractor run that makes it. For the
same reason it hands the exporter its manifest and docs directly; that the CLI hands ``--schematic``
this same pack is #206's test, in ``test_cli.py``.

Counted **per machine and per block key, never per type**: ``TextureSummary`` groups by ``type``,
and the Dangote Distillus shares ``type`` "Distillation Tower" with the three plain towers, so a
per-type count would pass with the Dangote drawn as a plain tower - the exact bug #205 fixed.
"""

from __future__ import annotations

import warnings
from collections import Counter
from pathlib import Path

import pytest

from gtnh_solver.adapter import load_plan, to_input_ir
from gtnh_solver.dataset import MultiblockDoc, extractor_hint, load_physical_dataset
from gtnh_solver.ir import InputIR, LayoutResult
from gtnh_solver.previewer.scene import build_scene
from gtnh_solver.previewer.textures import TextureManifest, load_multiblock_docs, machine_cubes
from gtnh_solver.schematic import build_schematic, nbt, read_schematic
from gtnh_solver.solver import solve

_ROOT = Path(__file__).resolve().parents[1]
_PACK = "2.9.0-beta-2"
_DUMP = _ROOT / "data" / _PACK
_MULTIBLOCKS = _DUMP / "multiblocks"
_MANIFEST = _DUMP / "textures" / "manifest.json"
_PLAN = _ROOT / "examples" / "ev-nitrobenzene.json"
_GT_MACHINES = "gregtech:gt.blockmachines"

#: The controller every multiblock of the plan resolves to, by ``mID``, with how many machines.
#: 31021 is the Dangote Distillus; before #205 it drew and exported as 1126, a plain tower.
_CONTROLLERS = Counter({31021: 1, 1126: 3, 15512: 3, 998: 2, 1169: 2, 15543: 2, 2730: 1})

pytestmark = [
    pytest.mark.skipif(
        not _MULTIBLOCKS.is_dir(),
        reason=f"no local {_PACK} structure dump at {_MULTIBLOCKS}; "
        f"{extractor_hint('multiblocks', _PACK)}",
    ),
    pytest.mark.skipif(
        not _MANIFEST.is_file(),
        reason=f"no local {_PACK} texture manifest at {_MANIFEST}; "
        f"{extractor_hint('textures/manifest.json', _PACK)}",
    ),
]


@pytest.fixture(scope="module")
def solved() -> tuple[InputIR, LayoutResult]:
    """The plan adapted against the real 2.9 dump and placed by the fast solver (a few seconds).

    Fast, because what is under test is what gets *drawn* for each placed machine, not the quality
    of the placement; the layout is ``partial_invalid`` until Track 2 routes every net, and that
    does not change which controller each machine is.
    """
    with warnings.catch_warnings():
        # The Dangote's 12x parallel is reported, not modelled; that warning is #204's to pin.
        warnings.simplefilter("ignore")
        problem = to_input_ir(load_plan(_PLAN), physical=load_physical_dataset(_MULTIBLOCKS))
    return problem, solve(problem, seed=0, optimize=False)


@pytest.fixture(scope="module")
def docs() -> dict[str, MultiblockDoc]:
    return load_multiblock_docs(_MULTIBLOCKS)


@pytest.fixture(scope="module")
def manifest() -> TextureManifest:
    return TextureManifest.load(_MANIFEST)


def _mid(block_key: str) -> int:
    registry, _, meta = block_key.rpartition("@")
    assert registry == _GT_MACHINES, block_key
    return int(meta)


def test_every_multiblock_carries_the_controller_it_resolved(
    solved: tuple[InputIR, LayoutResult],
) -> None:
    problem, _ = solved
    multiblocks = [m for m in problem.machines if m.footprint.volume > 1]
    assert all(m.block_key is not None for m in multiblocks), "a resolved machine lost its key"
    assert Counter(_mid(m.block_key) for m in multiblocks if m.block_key) == _CONTROLLERS


def test_every_machine_expands_to_the_doc_its_own_key_names(
    solved: tuple[InputIR, LayoutResult],
    docs: dict[str, MultiblockDoc],
    manifest: TextureManifest,
) -> None:
    """Each multiblock draws its own controller, and only the synthesized power sources do not draw.

    The machine's ``block_key`` must name a doc, that doc's controller must be the same key, and the
    controller block itself must be among the cubes drawn: an expansion that took some other doc
    would place a different machine's controller there.
    """
    scene = build_scene(*solved)
    unexpanded: list[str] = []
    drawn: Counter[int] = Counter()
    for machine in scene["machines"]:
        cubes = machine_cubes(machine, docs, manifest)
        if not cubes:
            unexpanded.append(str(machine.get("role")))
            continue
        key = machine.get("block_key")
        if key is None:
            continue  # a storage block: drawn from the manifest, and it names no multiblock
        doc = docs[key]
        controller = (doc.controller.registry_name, doc.controller.meta)
        assert f"{controller[0]}@{controller[1]}" == key
        assert controller in {(c.block, c.meta) for c in cubes}, (
            f"{machine['id']} lost its controller"
        )
        drawn[_mid(key)] += 1

    assert drawn == _CONTROLLERS
    assert len(scene["machines"]) - len(unexpanded) == 31
    assert unexpanded == ["source"] * 3, "only the synthesized power sources stay placeholders"


@pytest.mark.xfail(
    strict=True,
    raises=ValueError,
    reason="#212: a 2.9 frame box's meta is its material id (Steel 305, Black Steel 334), which "
    "the .schematic Data byte cannot hold, so the export crashes in to_nbt",
)
def test_the_schematic_holds_the_controllers_the_plan_names(
    solved: tuple[InputIR, LayoutResult],
    docs: dict[str, MultiblockDoc],
    manifest: TextureManifest,
) -> None:
    root = build_schematic(*solved, manifest=manifest, docs=docs)
    schematic = read_schematic(nbt.dumps("Schematic", root))
    machines = Counter(tile.mid for tile in schematic.tile_entities if tile.mid is not None)
    named = {mid: manifest.display_name(_GT_MACHINES, mid) for mid in machines}

    assert not [mid for mid, name in named.items() if name is None], "an mID the manifest lacks"
    by_name = Counter({named[mid]: count for mid, count in machines.items()})
    assert all(machines[mid] == count for mid, count in _CONTROLLERS.items())
    assert by_name["Super Tank I"] == 15
    assert by_name["Super Chest I"] == 2
    assert by_name["Debug Power Generator"] == 3
