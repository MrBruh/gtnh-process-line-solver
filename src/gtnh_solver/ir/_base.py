"""Shared Pydantic base classes for the IR contracts.

Two bases, both reject unknown fields (``extra="forbid"``) so the adapter can never
*silently* drop or misspell a field - a contract must fail loud (see docs/TESTING.md).

- ``StrictModel``  - mutable aggregate models (machines, nets, the IR roots).
- ``FrozenModel``  - immutable, hashable value types (coordinates, boxes) so they can
  live in sets / dict keys during the solve.

Plus ``check_contract_version``, the guard both IR roots put on their ``version`` field.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Aggregate contract model: unknown fields are an error; assignment is validated."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class FrozenModel(BaseModel):
    """Immutable, hashable value type: unknown fields are an error."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def check_contract_version(value: int, current: int, contract: str) -> int:
    """Return ``value`` if it is this build's contract version, else raise a pointed error.

    ``extra="forbid"`` already catches a payload carrying *fields* this build does not know. It
    cannot catch the other half, which is why this exists: a bump can change what an existing
    field **means** while leaving the shape alone, and such a payload validates clean and is then
    read as if it agreed. ``InputIR`` v3 is exactly that - a power port's ``rate`` became its own
    connection's share of the draw rather than nothing at all, so a v2 payload and a v3 consumer
    disagree by a multiple with every field present and well-typed.

    Rejects a *newer* version as firmly as an older one. Being unable to name the difference is
    the reason to refuse, not a reason to hope: this build cannot know what a later contract
    changed the meaning of.
    """
    if value != current:
        raise ValueError(
            f"{contract} payload declares contract version {value}, but this build speaks "
            f"version {current}. Re-generate it with this build rather than editing the field: a "
            f"contract bump can change what an existing field MEANS, so a mismatched payload is "
            f"read as if it agreed."
        )
    return value
