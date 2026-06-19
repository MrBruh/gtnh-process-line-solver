"""Output layout schema - the *solution* the solver produces.

A first-class versioned contract (not a previewer-internal format), consumed by the
previewer, the build guide, and later the .schematic exporter. Spec: docs/IR.md.

Routes are cell-paths; they are lowered to concrete blocks only at export, never here.
Power routes additionally carry a per-segment cable thickness sized to the summed
amperage of the shared-amperage net (docs/DOMAIN.md).
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from ._base import StrictModel
from .enums import Commodity, Facing, LayoutStatus
from .geometry import CellCoord

#: Bump on any breaking change to the output contract; record it in ``ir/__init__.py``.
LAYOUT_RESULT_VERSION = 0

#: Allowed GT cable thicknesses (1x/2x/4x/8x/16x, 16x max - docs/DOMAIN.md).
_THICKNESSES = frozenset({1, 2, 4, 8, 16})


class Placement(StrictModel):
    """Where one machine instance landed and which way its front face points."""

    machine_id: str = Field(min_length=1)
    cell: CellCoord
    orientation: Facing


class Segment(StrictModel):
    """One cell-to-cell hop of a route on a given channel (< the per-edge channel cap).
    Named ``start``/``end`` (docs/IR.md uses ``from``/``to``; ``from`` is a Python
    keyword)."""

    start: CellCoord
    end: CellCoord
    channel: int = Field(ge=0)


class Route(StrictModel):
    """The path taken by one net. For power, ``thickness_per_segment`` is required and
    aligns 1:1 with ``segments``; for items/fluids it must be omitted."""

    net_id: str = Field(min_length=1)
    commodity: Commodity
    segments: list[Segment] = Field(default_factory=list)
    thickness_per_segment: list[int] | None = None  # power only; 1/2/4/8/16 per segment

    @model_validator(mode="after")
    def _check(self) -> Route:
        if self.commodity is Commodity.POWER:
            if self.thickness_per_segment is None:
                raise ValueError("power route requires thickness_per_segment")
            if len(self.thickness_per_segment) != len(self.segments):
                raise ValueError("thickness_per_segment must align 1:1 with segments")
            bad = [t for t in self.thickness_per_segment if t not in _THICKNESSES]
            if bad:
                raise ValueError(f"cable thickness must be one of 1/2/4/8/16, got {bad}")
        elif self.thickness_per_segment is not None:
            raise ValueError("thickness_per_segment is only valid on power routes")
        return self


class LayoutMetrics(StrictModel):
    """Advisory metrics about a layout (footprint, layer count, buildability/congestion
    scores, ...). ``extra="allow"`` on purpose: metrics are informational and additive,
    so new ones never break the contract."""

    model_config = ConfigDict(extra="allow")

    footprint: int | None = None
    layers: int | None = None
    buildability: float | None = None
    congestion: float | None = None


class Infeasibility(StrictModel):
    """Why a layout could not be produced: the tightest violated constraint, a human
    explanation, and (when known) a concrete relaxation that would admit a solution."""

    constraint: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    suggested_relaxation: str | None = None


class LayoutResult(StrictModel):
    """The whole solution. ``status`` and ``infeasibility`` are coupled: a ``valid``
    result has no infeasibility; ``infeasible``/``partial_invalid`` must carry one."""

    version: int = LAYOUT_RESULT_VERSION
    status: LayoutStatus
    infeasibility: Infeasibility | None = None
    placements: list[Placement] = Field(default_factory=list)
    routes: list[Route] = Field(default_factory=list)
    metrics: LayoutMetrics = Field(default_factory=LayoutMetrics)
    seed: int  # the RNG seed that produced this layout (for the seed-compare workflow)

    @model_validator(mode="after")
    def _check(self) -> LayoutResult:
        if self.status is LayoutStatus.VALID:
            if self.infeasibility is not None:
                raise ValueError("a valid layout must not carry an infeasibility")
        elif self.infeasibility is None:
            raise ValueError(f"status={self.status.value} requires an infeasibility")
        return self
