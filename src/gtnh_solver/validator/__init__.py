"""validator — the only automated correctness gate (no headless GT simulator).

Independent CHECKING LOGIC over SHARED rule DATA (docs/ARCHITECTURE.md #4), so it can catch
router/placement bugs. The public entry point is :func:`validate`, which checks a
``LayoutResult`` against the ``InputIR`` it claims to solve and returns a
:class:`ValidationReport` of every proven violation — it never raises and never passes a
silently-invalid layout (``report.ok`` is the independent verdict).

Checks live in :mod:`.core`; geometry helpers in ``_geometry``; the report types in
:mod:`.report`. Rule-data-dependent checks (throughput/tier caps, summed amperage vs cable
rating, required-face reachability) are wired in once the dataset lands — see the TODO in
``core``.
"""

from __future__ import annotations

from .core import validate
from .report import ValidationReport, Violation, ViolationCode

__all__ = ["validate", "ValidationReport", "Violation", "ViolationCode"]
