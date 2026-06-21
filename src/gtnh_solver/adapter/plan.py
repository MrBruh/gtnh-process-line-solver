"""Typed view of a gtnh-factory-flow exported plan (only the fields the adapter consumes).

The export carries far more than the solver needs (positions, icons, colors, NEI hints, ...);
``extra="ignore"`` keeps us tolerant of all of it while validating the consumed path. Field
names are snake_case in Python and map to the export's camelCase keys via an alias generator,
so the rest of the codebase stays idiomatic. Pin point: this models schema as seen in the
committed fixtures (`examples/`), not a guessed shape.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

_CFG = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class Resource(BaseModel):
    """One item/fluid quantity on a recipe input or output."""

    model_config = _CFG

    kind: str  # "item" | "fluid"
    id: str
    amount: float = 0.0


class Recipe(BaseModel):
    """A placed recipe: its machine type, power/time, and item/fluid I/O."""

    model_config = _CFG

    id: str
    machine_type: str  # machineType, e.g. "Forge Hammer", "Large Chemical Reactor"
    eut: float = 0.0
    duration_ticks: float = 0.0
    inputs: list[Resource] = Field(default_factory=list)
    outputs: list[Resource] = Field(default_factory=list)


class Node(BaseModel):
    """A machine instance in the plan graph (references a recipe by id)."""

    model_config = _CFG

    id: str
    recipe_id: str
    machine_count: int = 1
    parallel: int = 1
    overclock_tier: str  # LV/MV/HV/... -> IR voltage_tier


class Storage(BaseModel):
    """A boundary source/sink (feed or drain) for one resource."""

    model_config = _CFG

    id: str
    kind: str
    resource_id: str


class Edge(BaseModel):
    """A directed material flow of one resource from a source to a target node/storage."""

    model_config = _CFG

    id: str
    source: str
    target: str
    resource_kind: str  # "item" | "fluid"
    resource_id: str


class Plan(BaseModel):
    """A whole exported plan."""

    model_config = _CFG

    schema_version: int
    name: str = ""
    recipes: list[Recipe] = Field(default_factory=list)
    nodes: list[Node] = Field(default_factory=list)
    storages: list[Storage] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
