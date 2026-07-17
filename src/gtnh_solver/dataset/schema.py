"""Schema v1 for the extracted multiblock dataset (``data/multiblocks/``).

A typed loader for the raw JSON the extractor emits (the Java tool of issue #45, lane 2 of
the dataset-extraction plan). One file per controller plus a ``_meta.json`` run summary. The
shape mirrors ``docs/dataset-extraction/plan.md`` section 4.2 exactly.

This is the **cross-language contract** between the (future) Java extractor and the Python
solver, so it is validated the way the rest of the repo validates data: Pydantic models with
``extra="forbid"``, which makes a mis-spelled or dropped field fail loud rather than get
silently ignored. A schema bump adds a field here in the same PR that bumps ``SCHEMA_VERSION``.

This module holds **no interpretation** - it only re-states the extractor's raw facts (blocks,
hints, variants, substitutions). Footprint bounding boxes, hint-derived face constraints, and
tier semantics are the adapter's job (``multiblocks.py``), per design principle 3 of the plan
("extractor emits facts, Python interprets"). A JSON Schema for non-Python consumers is
available from :func:`multiblock_json_schema` (derived from these models, so it never drifts).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: The dataset schema version these models implement. Every ``data/multiblocks`` file (and the
#: run's ``_meta.json``) carries a matching top-level ``schema`` field; a breaking change bumps
#: this and the files in the same PR (mirrors the IR ``*_VERSION`` discipline, docs/IR.md).
SCHEMA_VERSION = 1

# Unknown fields are an error: the extractor contract must fail loud, never silently drop a key
# (mirrors the IR bases in ``ir/_base.py`` and the plan adapter's ``_CFG`` in ``adapter/plan.py``).
# ``populate_by_name`` lets Python build a model by field name while JSON still loads via alias.
_STRICT = ConfigDict(extra="forbid", populate_by_name=True)

#: A raw ``[dx, dy, dz]`` offset from the controller origin (Minecraft axes: x/z horizontal, y up).
Offset = tuple[int, int, int]


class Controller(BaseModel):
    """Identity of the multiblock controller the file describes (plan section 4.2)."""

    model_config = _STRICT

    registry_name: str = Field(min_length=1)  # e.g. "gregtech:gt.blockmachines"
    meta: int  # block metadata that selects this machine within the registry entry
    display_name: str = Field(min_length=1)  # e.g. "Electric Blast Furnace" (keys the solver)
    source_class: str = Field(min_length=1)  # the GT5U class, for provenance in a diff
    facing_convention: str = ""  # how the raw offsets relate to the controller's front face


class Block(BaseModel):
    """One placed block in a variant: its offset and identity. No interpretation."""

    model_config = _STRICT

    d: Offset  # [dx, dy, dz] from the controller
    block: str = Field(min_length=1)  # registry name
    meta: int = 0


class Hint(BaseModel):
    """A hint-dot position: a legal hatch/degree-of-freedom slot the projector shows.

    The extractor captures these (via the ``gt_no_hatch`` channel) as the raw positions where
    the player may place an I/O hatch; the adapter turns them into face constraints.
    """

    model_config = _STRICT

    d: Offset
    hint: int  # StructureLib hint colour/index; opaque to the solver, kept for fidelity


class Variant(BaseModel):
    """One distinct built form of the controller (a trigger-stack / channel selection)."""

    model_config = _STRICT

    trigger_stack_size: int = Field(ge=1)  # controller item stack size that selected this variant
    channels: dict[str, int] = Field(default_factory=dict)  # StructureLib channels applied
    blocks: list[Block] = Field(min_length=1)
    hints: list[Hint] = Field(default_factory=list)
    bbox: Offset  # [sx, sy, sz]; extractor-derived convenience, re-derived + checked by the adapter


class Substitution(BaseModel):
    """One identity-only channel alternative (a tiered coil/glass swap that keeps the shape)."""

    model_config = _STRICT

    channel_value: int  # the channel setting that selects this block
    block: str = Field(min_length=1)
    meta: int = 0


class MultiblockDoc(BaseModel):
    """A whole ``data/multiblocks/<name>.json`` file: one controller and its variants."""

    model_config = _STRICT

    schema_version: int = Field(alias="schema")
    controller: Controller
    variants: list[Variant] = Field(min_length=1)
    #: Identity-only channel effects, keyed by channel name (e.g. ``"coil"``): the tiered blocks
    #: that swap in without changing the shape (plan section 4.1, "block-substitution table").
    substitutions: dict[str, list[Substitution]] = Field(default_factory=dict)
    #: Per-controller notes the extractor could not fully resolve (usually empty). Distinct from
    #: the run-wide ``_meta.json`` failure list, which records controllers that failed outright.
    failures: list[str] = Field(default_factory=list)


class ControllerFailure(BaseModel):
    """One controller the extractor could not dump, for the run summary (plan section 4.1)."""

    model_config = _STRICT

    registry_name: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class DatasetMeta(BaseModel):
    """``data/multiblocks/_meta.json``: the run summary that makes a dataset diff reviewable."""

    model_config = _STRICT

    schema_version: int = Field(alias="schema")
    pack_version: str = Field(min_length=1)  # the GTNH pack release the dump tracks
    mod_versions: dict[str, str] = Field(default_factory=dict)  # {mod: version} it was built from
    generated_at: str = Field(min_length=1)  # ISO-8601 generation timestamp
    extractor_sha: str = Field(min_length=1)  # git SHA of the extractor tool that produced it
    controller_count: int = Field(ge=0)  # controllers successfully dumped
    failures: list[ControllerFailure] = Field(default_factory=list)  # controllers that did not


def load_multiblock_doc(path: str | Path) -> MultiblockDoc:
    """Parse and validate one ``data/multiblocks/<name>.json`` file into a :class:`MultiblockDoc`."""
    return MultiblockDoc.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_meta(path: str | Path) -> DatasetMeta:
    """Parse and validate a ``data/multiblocks/_meta.json`` run summary."""
    return DatasetMeta.model_validate_json(Path(path).read_text(encoding="utf-8"))


def multiblock_json_schema() -> dict[str, Any]:
    """The JSON Schema for a multiblock file, derived from :class:`MultiblockDoc`.

    Handed to non-Python consumers (notably the Java extractor's tests) so they can validate
    their output against the same contract without re-stating it - it is generated from the
    Pydantic model, so it cannot drift from what the loader actually accepts.
    """
    return MultiblockDoc.model_json_schema(by_alias=True)
