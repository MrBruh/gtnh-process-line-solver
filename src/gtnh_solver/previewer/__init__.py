"""previewer - interactive 3D preview of a candidate layout.

A single self-contained ``.html`` page (three.js from a CDN, no npm build): orbit/pan/zoom
camera and a layer-by-layer slider. The pipeline is two pure steps - ``build_scene`` flattens a
(problem, layout) pair into a render-ready dict, ``render_html`` inlines it into a static viewer
template - so the mapping is fully unit-tested while the un-CI-testable WebGL stays a thin
template (validated by eye). ``write_preview`` composes both to a file (what the CLI calls), with
a texture pass in between that skins each machine box with its real GT casing texture where the
committed dataset + manifest resolve one (``textures.py``), degrading to the flat colour box
otherwise, and packs every baked face into the one image the viewer draws from (``atlas.py``).
Beside it an icon pass embeds a picture of each fluid and item the line moves, from a local icon
index where one exists (``icons.py``, #297); without one the page draws the plan's own colours.

Stated v1 scope is "build-assist": boxes coloured + labelled by type, region wireframe, pipes
coloured by commodity, power cables sized by thickness, source markers, a legend. The congestion
heatmap + multi-seed compare and offline (vendored three.js) are follow-ups (docs/ROADMAP.md).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from gtnh_solver.dataset.icons import IconPack, resolve_icon_index
from gtnh_solver.dataset.roots import resolve_dataset_path
from gtnh_solver.ir import InputIR, LayoutResult

from .atlas import pack_atlas
from .html import render_html
from .icons import resource_art
from .jar import JAR_VERSION, gt5u_version_from_manifest, jar_png_provider
from .scene import SCENE_VERSION, build_scene
from .textures import TextureSummary, texturize_scene

__all__ = [
    "SCENE_VERSION",
    "TextureSummary",
    "build_scene",
    "pack_atlas",
    "render_html",
    "texturize_scene",
    "write_preview",
]

_log = logging.getLogger(__name__)


def write_preview(
    problem: InputIR,
    layout: LayoutResult,
    path: str | Path,
    *,
    textures: bool = True,
    version: str | None = None,
) -> Path:
    """Render the preview for ``layout`` and write the self-contained HTML to ``path``.

    With ``textures`` on (the default) each machine box is skinned with its real GT casing texture
    where the resolved dataset + manifest supply one; the jar fetch is best-effort and any failure
    (offline, missing jar) is logged and degrades to placeholder boxes, never blocking the preview.
    Machines with no doc simply stay placeholders and trigger no jar fetch. ``version`` pins a
    generated ``data/<version>/`` dataset; the default resolves the newest local one, else the
    committed fixtures. The jar is fetched at the version the resolved manifest was extracted
    against, so its icons match.

    The directory ``path`` sits in is created if it is missing, as
    :func:`~gtnh_solver.schematic.write_schematic` already did. The write is the last step, after
    the scene and every texture are built, so a missing ``out/`` used to throw that work away
    with a raw ``FileNotFoundError`` (#150).

    The icon pass is local only and as best-effort as the texture pass: it draws from the icon index
    of ``version`` (else of the plan's own pack, else of the newest pack that has one, since icons
    are display only), and a missing or unusable index leaves the page with the plan's colours.
    """
    names, icons = _resource_art(problem, version or problem.pack_version)
    scene = build_scene(problem, layout, extra_names=names)
    scene["icons"] = icons
    if textures:
        try:
            manifest_path = resolve_dataset_path("textures/manifest.json", version=version)
            gt5u = gt5u_version_from_manifest(manifest_path) or JAR_VERSION
            texturize_scene(
                scene, version=version, png_provider=jar_png_provider(gt5u_version=gt5u)
            )
            pack_atlas(scene)  # the viewer draws every face from one image (previewer.atlas)
        except Exception as exc:  # never let a texture fetch/parse issue block a preview
            _log.warning("texture pass skipped, using placeholder boxes: %s", exc)
            _untextured(scene)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(scene), encoding="utf-8")
    return out


#: Where a missing icon index sends the reader: how to export one and derive the index from it.
ICON_RUNBOOK = "docs/dataset-extraction/icons.md"


def _resource_art(problem: InputIR, pack: str | None) -> tuple[dict[str, str], dict[str, str]]:
    """``icons.resource_art`` from ``pack``'s icon index, or nothing, logged, when there is none
    or it cannot be read: the page is drawn either way."""
    index = resolve_icon_index(pack)
    if index is None:
        _log.info(
            "no icon index under data/<version>/icons/, so the preview draws each resource in the "
            "plan's colour; %s says how to make one",
            ICON_RUNBOOK,
        )
        return {}, {}
    try:
        with IconPack.load(index) as icon_pack:
            return resource_art(problem, icon_pack)
    except Exception as exc:  # never let an icon index block a preview
        _log.warning("icon pass skipped, drawing the plan's colours: %s", exc)
        return {}, {}


def _untextured(scene: dict[str, Any]) -> None:
    """Put ``scene`` back to plain placeholder boxes after a texture pass that failed part way.

    The pass writes as it goes (a machine is flagged ``expanded`` before its blocks are stored), so a
    failure in the middle could leave a machine that skips its box and has no blocks to draw in its
    place: an invisible machine. Every trace of the pass goes, so each machine is a box again.
    """
    for machine in scene["machines"]:
        machine.pop("expanded", None)
    for route in scene.get("routes", []):
        for cell in route.get("cells", []):
            cell["tex"] = None
    scene["blocks"] = []
    scene.pop("textures", None)
    scene.pop("texturesActive", None)
    scene["atlas"] = None
