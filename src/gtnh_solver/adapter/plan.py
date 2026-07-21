"""Typed view of a gtnh-factory-flow exported plan (only the fields the adapter consumes).

The export carries far more than the solver needs (positions, icons, colors, NEI hints, ...);
``extra="ignore"`` keeps us tolerant of all of it while validating the consumed path. Field
names are snake_case in Python and map to the export's camelCase keys via an alias generator,
so the rest of the codebase stays idiomatic. Pin point: this models schema as seen in the
committed fixtures (`examples/`, `tests/fixtures/`), not a guessed shape.

Schema v2 is **additive**: the export gains ``app`` (exporter identity), ``datasetVersionId``
(the recipe dataset the plan was balanced against), and a ``resolved`` throughput block - the
exporter's own balancer output (per-machine EU/t, per-edge rates, external I/O, a power
total). All three are optional here so v1 plans keep parsing unchanged, and the same
tolerance policy applies inside ``resolved`` (unknown subfields are ignored).
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


class MachineBlock(BaseModel):
    """The GT controller *block* a machine is, as distinct from its recipe-map name.

    gtnh-factory-flow names a machine by its localized ``RecipeMap`` ("Chemical Plant"), which for
    GT++ machines is NOT the controller block's own name ("ExxonMobil Chemical Plant") - and the
    block name is what the structure dataset is keyed by, so the two never join on name alone.
    This carries the block's canonical ``registry@meta`` id ("gregtech:gt.blockmachines@998"),
    which the dump records as ``controller.registry_name`` + ``controller.meta``, making the join
    exact. Optional: only plans exported after gtnh-factory-flow #25 carry it, and a plan without
    it falls back to the machine-type name (see ``dataset.multiblocks.PhysicalDataset.get``).
    """

    model_config = _CFG

    id: str = ""  # "<registry_name>@<meta>", e.g. "gregtech:gt.blockmachines@998"
    display_name: str = ""


class RecipeSource(BaseModel):
    """Provenance for a recipe. Only ``machine_block`` is consumed; the rest is ignored."""

    model_config = _CFG

    machine_block: MachineBlock | None = None


class Recipe(BaseModel):
    """A placed recipe: its machine type, power/time, and item/fluid I/O."""

    model_config = _CFG

    id: str
    machine_type: str  # machineType, e.g. "Forge Hammer", "Large Chemical Reactor"
    eut: float = 0.0
    duration_ticks: float = 0.0
    inputs: list[Resource] = Field(default_factory=list)
    outputs: list[Resource] = Field(default_factory=list)
    source: RecipeSource | None = None


class Node(BaseModel):
    """A machine instance in the plan graph (references a recipe by id)."""

    model_config = _CFG

    id: str
    recipe_id: str
    machine_count: int = 1
    parallel: int = 1
    overclock_tier: str  # LV/MV/HV/... -> IR voltage_tier


class Storage(BaseModel):
    """A boundary source/sink (feed or drain). The resource it carries is taken from the edges
    touching it (``adapter.core._storage_ports``), so the export's per-storage ``resourceId`` is
    redundant here and not modelled - the edge is the single source of truth for what flows."""

    model_config = _CFG

    id: str
    kind: str


class Edge(BaseModel):
    """A directed material flow of one resource from a source to a target node/storage."""

    model_config = _CFG

    id: str
    source: str
    target: str
    resource_kind: str  # "item" | "fluid"
    resource_id: str


class AppInfo(BaseModel):
    """v2: which exporter produced the plan (provenance only, not consumed by the mapping)."""

    model_config = _CFG

    name: str = ""
    version: str = ""
    exported_at: str = ""


class ResolvedFlow(BaseModel):
    """v2: one resolved resource rate (an input, output, or external boundary flow)."""

    model_config = _CFG

    kind: str = ""  # "item" | "fluid"
    id: str = ""
    per_second: float = 0.0


class ResolvedMachine(BaseModel):
    """v2: the balancer's per-node throughput result - notably the real EU/t draw.

    ``totalEut`` (= ``eutPerMachine`` x machineCount x parallel) is what the adapter consumes:
    the exporter models overclocking, so it can exceed the raw ``recipe.eut`` the v1 synthesis
    multiplies up (see ``core._node_eut``).
    """

    model_config = _CFG

    node_id: str
    machine_key: str = ""
    machine_type: str = ""
    # The same controller-block id the recipe's ``source`` carries, mirrored here by the exporter.
    # Consumed as a fallback when a plan's ``resolved`` block is richer than its recipes.
    machine_block: MachineBlock | None = None
    tier: str = ""
    machine_count: int = 1
    parallel: int = 1
    eut_per_machine: float = 0.0
    total_eut: float = 0.0
    inputs: list[ResolvedFlow] = Field(default_factory=list)
    outputs: list[ResolvedFlow] = Field(default_factory=list)


class ResolvedNet(BaseModel):
    """v2: a per-edge resolved rate (mirrors an :class:`Edge` with its computed flow)."""

    model_config = _CFG

    edge_id: str
    source: str = Field("", alias="from")  # "from" is a Python keyword; explicit alias
    target: str = Field("", alias="to")  # named to match Edge.source/Edge.target
    kind: str = ""
    id: str = ""
    per_second: float = 0.0


class ResolvedPower(BaseModel):
    """v2: the plan-wide power summary (total EU/t across every powered machine)."""

    model_config = _CFG

    total_eut: float = 0.0
    total_eu_per_second: float = 0.0
    fuel: str = ""
    fuel_per_second: float = 0.0
    fuel_unit: str = ""


class ResolvedExternalIO(BaseModel):
    """v2: the plan's boundary flows (what the line consumes from / emits to the outside)."""

    model_config = _CFG

    inputs: list[ResolvedFlow] = Field(default_factory=list)
    outputs: list[ResolvedFlow] = Field(default_factory=list)


class ResolvedBlock(BaseModel):
    """v2: the exporter's balanced-throughput results for the whole plan."""

    model_config = _CFG

    generated_at: str = ""
    power: ResolvedPower | None = None
    machines: list[ResolvedMachine] = Field(default_factory=list)
    nets: list[ResolvedNet] = Field(default_factory=list)
    external_io: ResolvedExternalIO | None = Field(None, alias="externalIO")  # not "externalIo"


class Plan(BaseModel):
    """A whole exported plan."""

    model_config = _CFG

    schema_version: int
    name: str = ""
    recipes: list[Recipe] = Field(default_factory=list)
    nodes: list[Node] = Field(default_factory=list)
    storages: list[Storage] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    # v2 additive fields; None/absent on v1 plans.
    app: AppInfo | None = None
    dataset_version_id: str | None = None
    resolved: ResolvedBlock | None = None
