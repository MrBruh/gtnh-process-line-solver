"""previewer - interactive 3D preview of a candidate layout.

A single self-contained ``.html`` page (three.js from a CDN, no npm build): orbit/pan/zoom
camera and a layer-by-layer slider. The pipeline is two pure steps - ``build_scene`` flattens a
(problem, layout) pair into a render-ready dict, ``render_html`` inlines it into a static viewer
template - so the mapping is fully unit-tested while the un-CI-testable WebGL stays a thin
template (validated by eye). ``write_preview`` composes both to a file (what the CLI calls).

Stated v1 scope is "build-assist": boxes coloured + labelled by type, region wireframe, pipes
coloured by commodity, power cables sized by thickness, source markers, a legend. The congestion
heatmap + multi-seed compare and offline (vendored three.js) are follow-ups (docs/ROADMAP.md).
"""

from __future__ import annotations

from pathlib import Path

from gtnh_solver.ir import InputIR, LayoutResult

from .html import render_html
from .scene import SCENE_VERSION, build_scene

__all__ = ["SCENE_VERSION", "build_scene", "render_html", "write_preview"]


def write_preview(problem: InputIR, layout: LayoutResult, path: str | Path) -> Path:
    """Render the preview for ``layout`` and write the self-contained HTML to ``path``."""
    out = Path(path)
    out.write_text(render_html(build_scene(problem, layout)), encoding="utf-8")
    return out
