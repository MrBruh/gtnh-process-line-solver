"""ir — the two versioned data contracts everything couples to.

InputIR (the problem) and LayoutResult (the solution, consumed by previewer, build guide,
and later export). Spec: docs/IR.md. Implement as Pydantic v2 models.

Contract changelog (bump `version` on any breaking change):
- v0: initial draft (see docs/IR.md).

TODO(ir): define InputIR, Machine, Net, PinnedIO and LayoutResult, Placement, Route,
Segment as typed, versioned models. This unblocks every other lane — do it first.
"""
