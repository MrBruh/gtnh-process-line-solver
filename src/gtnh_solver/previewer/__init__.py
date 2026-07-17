"""previewer - interactive 3D preview of a candidate layout.

A single self-contained ``.html`` page (three.js from a CDN, no npm build): orbit/pan/zoom
camera and a layer-by-layer slider. The pipeline is two pure steps - ``build_scene`` flattens a
(problem, layout) pair into a render-ready dict, ``render_html`` inlines it into a static viewer
template - so the mapping is fully unit-tested while the un-CI-testable WebGL stays a thin
template (validated by eye). ``write_preview`` composes both to a file (what the CLI calls), with
a texture pass in between that skins each machine box with its real GT casing texture where the
committed dataset + manifest resolve one (``textures.py``), degrading to the flat colour box
otherwise.

Stated v1 scope is "build-assist": boxes coloured + labelled by type, region wireframe, pipes
coloured by commodity, power cables sized by thickness, source markers, a legend. The congestion
heatmap + multi-seed compare and offline (vendored three.js) are follow-ups (docs/ROADMAP.md).
"""

from __future__ import annotations

import logging
from pathlib import Path

from gtnh_solver.dataset.roots import resolve_dataset_path
from gtnh_solver.ir import InputIR, LayoutResult

from .html import render_html
from .jar import JAR_VERSION, gt5u_version_from_manifest, jar_png_provider
from .scene import SCENE_VERSION, build_scene
from .textures import TextureSummary, texturize_scene

__all__ = [
    "SCENE_VERSION",
    "TextureSummary",
    "build_scene",
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
    """
    scene = build_scene(problem, layout)
    if textures:
        try:
            manifest_path = resolve_dataset_path("textures/manifest.json", version=version)
            gt5u = gt5u_version_from_manifest(manifest_path) or JAR_VERSION
            texturize_scene(
                scene, version=version, png_provider=jar_png_provider(gt5u_version=gt5u)
            )
        except Exception as exc:  # never let a texture fetch/parse issue block a preview
            _log.warning("texture pass skipped, using placeholder boxes: %s", exc)
            scene.setdefault("textures", {})
    out = Path(path)
    out.write_text(render_html(scene), encoding="utf-8")
    return out
