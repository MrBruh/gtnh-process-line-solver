"""Local-only acceptance: each real dump marks a machine type by the face that shows its art (#322).

The unit tests hold ``textures._legend_side`` to the rule on hand-written stacks. This holds it to
every MTE of each local dump, judged with the sprites of that pack's own GT jar, as a preview
judges it::

    data/<pack>/textures/manifest.json --every MTE--> _legend_side(..., the jar's sprites)
        --> which classes leave their front, for which side, and how many blocks
    three single blocks --texturize_scene(the jar)--> each legend entry's tile

**It needs a real dump and that pack's jar already cached, so it skips without either**, as it does
in CI and on a fresh clone. It never downloads one (``jar.cached_jar``); previewing any line against
the pack caches it. The suite pins the dataset every other test resolves to the committed copy
(``tests/conftest.py``), so each manifest is read from the repo's own ``data/`` by explicit path.

The map of classes that leave their front is exact. A class that starts or stops moving after a pack
bump fails here, so a change in what GT draws is looked at rather than silently re-marked.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gtnh_solver.dataset import extractor_hint
from gtnh_solver.previewer.jar import cached_jar, extract_icons
from gtnh_solver.previewer.textures import (
    _LEGEND_SIDES,
    TextureManifest,
    _legend_side,
    texturize_scene,
)

pytest.importorskip("PIL")

_ROOT = Path(__file__).resolve().parents[1]
_PACKS = ("2.8.4", "2.9.0-beta-2", "2.9.0-beta-3")
_MACHINES = "gregtech:gt.blockmachines"

#: The classes whose blocks leave their front in every local dump, and the side each goes to.
_LEAVE_FRONT_EVERYWHERE = {
    "MTEBoilerSolar": "UP",
    "MTEBoilerSolarSteel": "UP",
    "MTECharcoalPit": "UP",
    "MTECleanroom": "UP",
    "MTEDieselGenerator": "UP",  # the Combustion Generator
    "MTEEnergyBuffer": "UP",
    "MTEGasTurbine": "EAST",
    "MTEGeothermalGenerator": "UP",
    "MTELightningRod": "UP",
    "MTEMonsterRepellent": "UP",
    "MTEQuantumTank": "UP",
    "MTESemiFluidGenerator": "EAST",
    "MTESolarGenerator": "UP",
    "MTESolarTower": "UP",
    "MTESteamTurbine": "EAST",
    "MTESuperTank": "UP",
    "MTETeslaCoil": "UP",  # the Tesla transceivers
}
_LEAVE_FRONT_29 = {
    **_LEAVE_FRONT_EVERYWHERE,
    # 2.8.4 drew a Solid Steel casing layer over the Acid Generator's front, which counts as art
    # (textures._PORT_MARK says why); 2.9 draws only its energy plug there.
    "MTEAcidGenerator": "UP",
    "MTEDebugTank": "UP",  # new in 2.9
}
_LEAVE_FRONT = {
    "2.8.4": _LEAVE_FRONT_EVERYWHERE,
    "2.9.0-beta-2": _LEAVE_FRONT_29,
    "2.9.0-beta-3": _LEAVE_FRONT_29,
}

#: How many of each pack's MTEs leave their front, per side (textures._LEGEND_SIDES cites these).
_MOVED = {
    "2.8.4": {"UP": 72, "EAST": 13},
    "2.9.0-beta-2": {"UP": 79, "EAST": 13},
    "2.9.0-beta-3": {"UP": 79, "EAST": 13},
}

#: Classes that keep their front, each one the rule could plausibly have moved: a basic machine,
#: blocks with art on several faces (the Naquadah Reactor, the Large Turbines), a multiblock
#: controller, a power block with a plain front, the tanks' sibling chests, and hatches.
_KEEP_FRONT = (
    "MTEBasicMachineWithRecipe",
    "MTENaquadahReactor",
    "MTELargeTurbineSteam",
    "MTELargeTurbineGas",
    "MTELargeChemicalReactor",
    "MTEWetTransformer",
    "MTESuperChest",
    "MTEQuantumChest",
    "MTEHatchInputBus",
    "MTEHatchOutput",
    "MTEHatchEnergy",
)

#: The 2.8.4 classes whose front names sprites GT shipped empty, so only the pixels move them.
_BLANK_FRONTS_284 = {
    "MTEDieselGenerator",
    "MTEGasTurbine",
    "MTEGeothermalGenerator",
    "MTESemiFluidGenerator",
    "MTESteamTurbine",
}


@dataclass(frozen=True)
class _Pack:
    """One local dump, read once per module: its manifest, its jar and that jar's sprites, and each
    class's MTEs counted by the side their legend mark is read from, judged with the sprites and by
    icon names alone."""

    name: str
    path: Path
    manifest: TextureManifest
    jar: Path
    sprites: dict[str, bytes]
    sides: dict[str, Counter[str]]
    sides_by_name: dict[str, Counter[str]]


def _short(source_class: str) -> str:
    return source_class.rsplit(".", 1)[-1]


def _sides_by_class(
    manifest: TextureManifest, mtes: list[tuple[str, int]], sprites: dict[str, bytes]
) -> dict[str, Counter[str]]:
    sides: dict[str, Counter[str]] = defaultdict(Counter)
    for block, meta in mtes:
        side = _legend_side(manifest, block, meta, sprites)
        sides[_short(manifest.source_class(block, meta))][side] += 1
    return dict(sides)


@pytest.fixture(scope="module", params=_PACKS)
def pack(request: pytest.FixtureRequest) -> _Pack:
    name = str(request.param)
    path = _ROOT / "data" / name / "textures" / "manifest.json"
    if not path.is_file():
        pytest.skip(
            f"no local {name} texture manifest at {path}; "
            f"{extractor_hint('textures/manifest.json', name)}"
        )
    jar = cached_jar(path)
    if jar is None:
        pytest.skip(f"no cached GT jar for {name}; previewing any line against {name} caches it")
    raw = json.loads(path.read_text(encoding="utf-8"))
    manifest = TextureManifest(raw, source=path)
    mtes = [
        (block, int(meta))
        for block, meta in (
            key.rsplit("|", 1) for key, entry in raw["blocks"].items() if entry.get("kind") == "mte"
        )
    ]
    icons = {
        layer["icon"]
        for block, meta in mtes
        for side in _LEGEND_SIDES
        for layer in manifest.layers(block, meta, side)[1:]
    }
    paths = {icon: asset for icon in icons if (asset := manifest.icon_path(icon)) is not None}
    sprites = extract_icons(jar, paths)
    return _Pack(
        name,
        path,
        manifest,
        jar,
        sprites,
        _sides_by_class(manifest, mtes, sprites),
        _sides_by_class(manifest, mtes, {}),
    )


def test_exactly_these_classes_leave_their_front_each_for_one_side(pack: _Pack) -> None:
    split = {cls: dict(counts) for cls, counts in pack.sides.items() if len(counts) > 1}
    assert not split, "every tier of a class is marked by the same side"
    moved = {cls: next(iter(counts)) for cls, counts in pack.sides.items() if "NORTH" not in counts}
    assert moved == _LEAVE_FRONT[pack.name]
    total: Counter[str] = Counter()
    for counts in pack.sides.values():
        total.update(counts)
    del total["NORTH"]
    assert dict(total) == _MOVED[pack.name]


def test_the_controls_keep_their_front(pack: _Pack) -> None:
    for cls in _KEEP_FRONT:
        counts = pack.sides.get(cls)
        assert counts, f"no {cls} in the {pack.name} dump, so it controls nothing"
        assert set(counts) == {"NORTH"}, cls


def test_only_2_8_4_needs_the_sprites_to_tell_art_apart(pack: _Pack) -> None:
    """2.8.4 names empty sprites on these classes' fronts; 2.9 records them invisible."""
    differ = {cls for cls, counts in pack.sides.items() if counts != pack.sides_by_name[cls]}
    assert differ == (_BLANK_FRONTS_284 if pack.name == "2.8.4" else set())


def test_a_dumped_controller_keeps_its_front_unless_its_art_is_on_top(pack: _Pack) -> None:
    docs = _ROOT / "data" / pack.name / "multiblocks"
    if not docs.is_dir():
        pytest.skip(f"no local {pack.name} structure dump at {docs}")
    moved: dict[str, str] = {}
    for path in sorted(docs.glob("*.json")):
        controller: dict[str, Any] | None = json.loads(path.read_text(encoding="utf-8")).get(
            "controller"
        )
        if controller is None:
            continue  # _meta.json
        side = _legend_side(
            pack.manifest, controller["registry_name"], int(controller["meta"]), pack.sprites
        )
        if side != "NORTH":
            moved[_short(str(controller.get("source_class", "")))] = side
    assert moved == {"MTECleanroom": "UP", "MTESolarTower": "UP"}


#: Each block's legend tile in a preview, the same on every pack: the Super Tank's display on its
#: top, the Semifluid Generator's art on its right-hand side, the Combustion Generator's on its top.
_TILES = {
    "Super Tank I": f"{_MACHINES}|130|UP|inactive",
    "Basic Semifluid Generator": f"{_MACHINES}|837|EAST|inactive",
    "Basic Combustion Generator": f"{_MACHINES}|1110|UP|inactive",
}


def test_a_preview_marks_each_block_by_the_face_that_shows_its_art(
    pack: _Pack, tmp_path: Path
) -> None:
    machines = [
        {
            "id": f"m{i}",
            "type": name,
            "cell": [3 * i, 0, 0],
            "size": [1, 1, 1],
            "front": "north",
            "voltage_tier": "LV",
            "role": "machine",
            "color": "#6ca0dc",
            "hatches": [],
        }
        for i, name in enumerate(_TILES)
    ]
    scene: dict[str, Any] = {
        "version": 1,
        "machines": machines,
        "legend": [{"label": name, "color": "#6ca0dc"} for name in _TILES],
    }
    texturize_scene(
        scene,
        multiblocks_dir=tmp_path,  # single blocks: no structure doc is read
        manifest_path=pack.path,
        png_provider=lambda paths: extract_icons(pack.jar, paths),
    )
    assert {entry["label"]: entry.get("tile") for entry in scene["legend"]} == _TILES
