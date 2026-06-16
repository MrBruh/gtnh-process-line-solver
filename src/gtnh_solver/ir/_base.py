"""Shared Pydantic base classes for the IR contracts.

Two bases, both reject unknown fields (``extra="forbid"``) so the adapter can never
*silently* drop or misspell a field — a contract must fail loud (see docs/TESTING.md).

- ``StrictModel``  — mutable aggregate models (machines, nets, the IR roots).
- ``FrozenModel``  — immutable, hashable value types (coordinates, boxes) so they can
  live in sets / dict keys during the solve.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Aggregate contract model: unknown fields are an error; assignment is validated."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class FrozenModel(BaseModel):
    """Immutable, hashable value type: unknown fields are an error."""

    model_config = ConfigDict(extra="forbid", frozen=True)
