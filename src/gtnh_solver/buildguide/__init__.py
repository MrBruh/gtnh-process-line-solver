"""buildguide - render a LayoutResult into a player-buildable text guide.

Phase 1 ships a text guide (:func:`build_guide`, in ``core``): header, bill of materials,
per-net connections, and a per-layer ASCII map with a key. The richer HTML/markdown guide
(placement order, per-cover callouts) and the three.js previewer come later (docs/ROADMAP.md).
"""

from __future__ import annotations

from .core import build_guide

__all__ = ["build_guide"]
