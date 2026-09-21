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

#: ``allow_inf_nan=False`` because this is the **untrusted** boundary: the export is a file the
#: solver did not write, and JSON has no literal for a non-finite number but ``1e400`` parses to
#: ``inf`` all the same. Unbounded, ``inf`` flows through the whole mapping (it satisfies every
#: ``ge=0`` the IR states) and surfaces as an ``OverflowError`` deep inside power synthesis, which
#: is an ``ArithmeticError`` rather than a ``ValueError`` and so escaped the CLI as a traceback.
#: Refused here instead, where the message can still name the field that carried it (#115).
_CFG = ConfigDict(
    alias_generator=to_camel, populate_by_name=True, extra="ignore", allow_inf_nan=False
)

#: Ceiling on a node's ``parallel`` and on the ``parallel`` of one runtime variant. A parallel
#: count is a throughput multiplier that ends up in ``amount * parallel * machineCount /
#: duration``, and Python ints are unbounded, so a 310-digit one (an export bug, or a hostile
#: file) raises ``OverflowError`` the moment that product is converted to a float. This is an
#: arithmetic sanity bound, not a claim about what GT can run: the largest parallel counts in the
#: pack are in the low thousands, so anything this far out is a broken export either way.
MAX_PARALLEL = 1_000_000

#: Ceiling on a node's ``machineCount``. Tighter than :data:`MAX_PARALLEL` because every instance
#: becomes a real ``Machine`` to place and route (``core._instance_ids``): where a huge
#: ``parallel`` is one bad multiplication, a huge ``machineCount`` is an allocation the adapter
#: would sit in until the box ran out of memory. The solver targets tens of machines
#: (docs/ARCHITECTURE.md decision #6), so this is already far past anything it can lay out.
MAX_MACHINE_COUNT = 1_000


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
    """Provenance for a recipe: which controller block, and which recipe dataset it came from."""

    model_config = _CFG

    machine_block: MachineBlock | None = None
    #: The exporter's recipe-dataset id, channel-prefixed: ``"stable-2.8.4"``,
    #: ``"local-2.9.0-beta-2"``. Both forks emit it, which makes it the one place a plan states the
    #: GTNH pack it was balanced against, so the physical dataset can be matched to it rather than
    #: guessed (:func:`producer.plan_pack_version`). A top-level ``datasetVersionId`` exists only on
    #: the MrBruh fork, so the per-recipe field is the portable one.
    dataset_version_id: str = ""


class MachineConfigTier(BaseModel):
    """One setting of a machine-configuration control, e.g. one parallel step of a GT++ multiblock."""

    model_config = _CFG

    key: str = ""
    #: Fractional in practice (1.5, 2.5, 3.5 all occur), so this is a throughput multiplier rather
    #: than a batch count - one more reason the adapter reports it instead of composing it.
    parallel_multiplier: float = 1.0


class MachineConfigControl(BaseModel):
    """A configurable dimension of a machine: its parallel step, coil, pipe casing, solenoid.

    Only ``machineParallel`` is read, and only to *report* that the adapter is not modelling it
    (``core._handler_parallel``). The node names its chosen setting in
    ``Node.machine_config_tiers``; absent, ``default_key`` is what the machine is running.
    """

    model_config = _CFG

    id: str = ""  # e.g. "machineParallel"
    default_key: str = ""
    tiers: list[MachineConfigTier] = Field(default_factory=list)


class MachineHandler(BaseModel):
    """One machine a recipe can run in: the **controller**, as distinct from the recipe map.

    arodoid's export lists every machine that can run a recipe and lets the node pick one with
    ``machineHandlerId``; with no id the node uses the FIRST entry, the default (which holds for
    every node in ``examples/gtnh-parallel-sand.json``). MrBruh's fork does not emit this list at
    all, which is what identifies the producer (:func:`producer.detect_producer`).

    Two fields are load-bearing rather than descriptive:

    ``label``
        The controller's own display name ("Dangote Distillus", "Electric Blast Furnace"), which is
        what the structure dump is keyed by. ``machine_type`` is the localized *recipe-map* name
        ("Distillation Tower", "Blast Furnace") and for a GT++ machine the two differ, so the dump
        never joins on ``machine_type`` alone.
    ``kind``
        ``"single"`` or ``"multiblock"``. This decides what a **census miss means**: absence of a
        ``single`` handler is positive evidence the machine is a single block (GT's
        ``MTEBasicMachine`` intake rule applies), while absence of a ``multiblock`` one is an
        extraction gap worth reporting. Treating both the same way is how a name-alias table
        silently swallows real misses.
    """

    model_config = _CFG

    id: str = ""
    kind: str = ""  # "single" | "multiblock"
    label: str = ""
    machine_type: str = ""
    minimum_tier: str = ""
    #: Handler-level overrides the recipe's own ``runtime_calculation`` does not account for, most
    #: importantly a parallel multiplier. See :class:`MachineConfigControl`.
    machine_config_controls: list[MachineConfigControl] = Field(default_factory=list)


class RuntimeVariant(BaseModel):
    """The recipe as GT would actually run it at one tier (and coil, casing, ...) of a machine.

    ``eut`` and ``duration_ticks`` here are **post-overclock**: the recipe's own figures are the base
    values at its minimum tier, and a machine one tier up draws 4x and runs 2x faster. Reading the
    base values instead understates a whole plan's draw by 6.1x and a single machine by up to 256x.

    ``inputs``/``outputs`` are deliberately **not modelled**. Some variants carry an empty ``inputs``
    list even for a recipe that plainly has inputs, so the array cannot be read as the recipe's I/O;
    amounts keep coming from the recipe (with the node's overrides applied).
    """

    model_config = _CFG

    id: str = ""  # "tier-ev", "tier-ev-coil-hss_g", "tier-ev-perfect-oc": NOT parseable, see below
    overclock_tier: str = ""
    #: Present only on variants of a coil-bearing machine (an EBF), where the coil sets the heat
    #: bonus. Absence means the machine has no coil dimension, not that the coil is unknown.
    coil_tier: str | None = None
    eut: float = 0.0
    duration_ticks: float = 0.0
    parallel: int = Field(default=1, ge=1, le=MAX_PARALLEL)


class RuntimeCalculation(BaseModel):
    """Per-tier runtime figures, computed by the exporter against GT's own overclock calculator.

    Both forks emit this (``sourceClass: gregtech.api.util.OverclockCalculator``), which is why
    reading it needs no producer branch. Selection must match on the variant's **fields**
    (:func:`core._matched_variant`), never by building an id string: ids carry suffixes beyond the
    tier and coil (``tier-ev-perfect-oc``), so string matching silently misses about half the nodes
    of a real plan.
    """

    model_config = _CFG

    variants: list[RuntimeVariant] = Field(default_factory=list)


class Recipe(BaseModel):
    """A placed recipe: its machine type, power/time, and item/fluid I/O."""

    model_config = _CFG

    id: str
    machine_type: str  # machineType, e.g. "Forge Hammer", "Large Chemical Reactor"
    #: Base figures at the recipe's minimum tier. Prefer the matched :class:`RuntimeVariant`.
    eut: float = 0.0
    duration_ticks: float = 0.0
    runtime_calculation: RuntimeCalculation | None = None
    inputs: list[Resource] = Field(default_factory=list)
    outputs: list[Resource] = Field(default_factory=list)
    source: RecipeSource | None = None
    #: Empty on a MrBruh-fork plan, which never emits it; see :class:`MachineHandler`.
    machine_handlers: list[MachineHandler] = Field(default_factory=list)


class Node(BaseModel):
    """A machine instance in the plan graph (references a recipe by id)."""

    model_config = _CFG

    id: str
    recipe_id: str
    #: Both are bounded here rather than checked downstream: they are multipliers straight out of
    #: an untrusted file, and unbounded they reach ``core._rate`` as an int too large to convert
    #: to a float (:data:`MAX_PARALLEL`, :data:`MAX_MACHINE_COUNT`).
    machine_count: int = Field(default=1, ge=1, le=MAX_MACHINE_COUNT)
    parallel: int = Field(default=1, ge=1, le=MAX_PARALLEL)
    overclock_tier: str  # LV/MV/HV/... -> IR voltage_tier
    #: Which of the recipe's :class:`MachineHandler` entries this node runs in. Empty means the
    #: default, the first entry; empty also on every MrBruh-fork plan, which emits no handlers.
    machine_handler_id: str = ""
    #: The heating coil this machine is built with, which selects among coil-keyed
    #: :class:`RuntimeVariant`s. Empty for a machine with no coil dimension.
    coil_tier: str = ""
    #: Chosen settings of the machine's :class:`MachineConfigControl`s, keyed by control id
    #: (``{"machineParallel": "fixed-12"}``). A control the node does not name runs its default.
    machine_config_tiers: dict[str, str] = Field(default_factory=dict)
    #: Per-node replacements for ``recipe.inputs``, keyed by index into that list. A recipe is
    #: shared between nodes while an override belongs to one node, so these are resolved per node
    #: and never written back onto the recipe (``core._effective_inputs``). Keys arrive as JSON
    #: strings and coerce to ``int``; a non-numeric key is malformed and fails validation here
    #: rather than silently dropping an input the edges then reference.
    recipe_input_overrides: dict[int, Resource] = Field(default_factory=dict)


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
    # Descriptive only: the adapter multiplies by the NODE's counts, never these, so they carry no
    # bound. Bounding a field nothing reads could only refuse a plan, never protect a calculation.
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
