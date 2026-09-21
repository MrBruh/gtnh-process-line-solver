"""Map a gtnh-factory-flow exported plan to the solver's ``InputIR``.

Mapping (see docs/ARCHITECTURE.md, docs/IR.md):
- ``node``    -> ``Machine`` (recipe.machineType -> type, overclockTier -> voltage_tier,
                recipe.eut * parallel -> eut); recipe inputs/outputs -> item/fluid ``Port``s.
                ``machineCount`` must be 1 - multi-instance nodes are rejected (see below).
- ``storage`` -> a boundary ``Machine`` typed **Super Chest** (items) or **Super Tank**
                (fluids) - blocks that take I/O covers on their faces, so every cover rides a
                machine/storage face and never a pipe (a deliberate Phase 1 simplification).
                Its ports come from the edges touching it (source edge -> output, target ->
                input), so a feed/drain is just another placeable node; the IR needs no
                separate boundary concept.
- ``edge``    -> ``Net`` (resourceKind -> commodity, resourceId -> fluid_or_item); endpoints
                reference the matching out/in ports; typed throughput is computed from the
                recipe rate. **Edges sharing a multiblock port are one net** (``_edge_groups``):
                in game one port is one hatch feeding one pipe network (#213).
- ``power``   -> a source machine + shared-amperage net per voltage tier feed the powered
                machines (``power`` submodule, docs/DOMAIN.md); the export carries no source.

**A node's EU/t and duration come from the best source the plan offers**, because the recipe's own
``eut``/``durationTicks`` are the values at its MINIMUM tier and a machine run above that draws 4x
and runs 2x faster per step::

    resolved.totalEut          the exporter's own balancer; knows machine count and parallelism
      |                        (cross-checked against the synthesis: mismatch warns, resolved wins)
      v
    matched runtimeCalculation variant    GT's own OverclockCalculator, per tier and coil
      |                                  (matched on FIELDS - variant ids are not parseable)
      v
    recipe.eut * parallel      the base value; the floor, and a reported fallback

**A machine is joined to the structure dump by its controller**, not by the recipe-map name the
export leads with (``_physical_record``): controller-block id, then the handler's ``label``, then
the recipe-map name, each through a small alias table. The record the join lands on also names the
machine: its ``block_key`` is stamped on the ``Machine``, so the previewer and ``.schematic`` export
draw the controller the footprint came from instead of re-guessing it from ``type`` (#205).
Footprints are single-block 1x1x1 until that join lands a record, so the solver runs with or without
a ``data/multiblocks/`` dump, and the bounding region is sized to fit whatever footprints result
(``_bounding_region``). What a *census* miss means depends on ``handler.kind`` and the two readings
are opposites - see ``_classify_census_miss``.

**A node standing for several machines expands** into one ``Machine`` per physical machine
(``_instance_ids``), all sharing the node's nets - which needed no IR concept, because
``Net.endpoints`` is already unbounded. The distinction that matters is per machine vs per group:
``Port.rate`` and ``Machine.eut`` are one machine's (the power synthesis sums them), while
``Net.throughput`` is the group's (:func:`_group_rate`), because one bus carries what all of them
move. A single-machine node keeps its bare id, so nothing that predates this moves.

**Edges that meet at one multiblock port become one net** (#213). A plan draws an edge per
consumer, so an output feeding two machines is two edges from one port; built as two nets they dock
two terminals on the port, while the build has one hatch there, and the router and the validator
each paired that hatch with a different one of the two. The edges are grouped by transitive closure
over the hatch-bearing ``(machine, port)`` pairs they touch::

    plan edges (plan order)        e1: LCR -> Chem Plant   e2: LCR -> Super Tank   e3: Oven -> DT
      |  _edge_groups: union edges touching one multiblock (machine, port), transitively
      v
    groups                         {e1, e2} (share LCR output:x)   {e3}
      |  _net_for_edges: one net per group; id "e1+e2", endpoints producers then consumers,
      v                            throughput = each distinct producer's rate once
    nets                           "e1+e2"                          "e3"   (a lone edge: as before)

Only a multiblock's port merges (:func:`_is_multiblock` says which machines are). A boundary storage
has no hatch, and its covers can push through several faces, so storage-shared nets stay apart and
a line that shares only storages lays out exactly as before. A single-block machine sharing a port
is left unmerged too: it has no hatch to disagree about, and whether it should merge is a separate
question this does not settle.

Still crude-on-purpose for Phase 1 (docs/ROADMAP.md): all four horizontal orientations for every
machine, non-square bases included (``occupied_cells`` rotates the reserved box); and hint-derived
face constraints stay on the dataset record. The InputIR's own referential-integrity check is the
validation gate: a dangling edge or commodity mismatch fails loud here, which is the adapter
contract (docs/TESTING.md).
"""

from __future__ import annotations

import json
import math
import warnings
from pathlib import Path

from gtnh_solver.dataset import MachinePhysical, PhysicalDataset
from gtnh_solver.ir import (
    CellBox,
    Commodity,
    FaceSpec,
    InputIR,
    IODirection,
    Machine,
    MachineFaceRef,
    METoggles,
    Net,
    Port,
)
from gtnh_solver.ir.enums import HORIZONTAL_FACINGS_ORDERED

from ._errors import AdapterError, AdapterWarning
from .plan import (
    Edge,
    MachineHandler,
    Node,
    Plan,
    Recipe,
    ResolvedMachine,
    Resource,
    RuntimeVariant,
)
from .power import synthesize_power
from .producer import PlanProducer, plan_pack_version, resolve_producer

# Crude single-block physical defaults until the dataset lane provides real footprints/faces.
_DEFAULT_FOOTPRINT = CellBox()  # 1x1x1
_DEFAULT_ORIENTATIONS = list(HORIZONTAL_FACINGS_ORDERED)  # front defaults to the first (NORTH)
_STORAGE_TIER = "LV"  # storages are unpowered; placeholder tier to satisfy the contract

_COMMODITY = {"item": Commodity.ITEM, "fluid": Commodity.FLUID}
# Boundary I/O blocks that accept I/O covers on their faces (keeps covers off pipes).
_STORAGE_TYPE = {"item": "Super Chest", "fluid": "Super Tank"}
# Cross-check tolerance (relative AND absolute) between a v2 export's resolved EU/t figures and
# the recipe-derived synthesis: wide enough for float noise, tight enough that any modelling
# difference (overclocking, duty cycling) trips the warning.
_RESOLVED_EUT_TOLERANCE = 1e-6
#: The machine-configuration control that sets a GT++ multiblock's parallel batch count.
_PARALLEL_CONTROL = "machineParallel"


def load_plan(path: str | Path) -> Plan:
    """Parse a gtnh-factory-flow exported plan JSON file into a typed :class:`Plan`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Plan.model_validate(data)


def adapt_file(
    path: str | Path,
    *,
    physical: PhysicalDataset | None = None,
    producer: PlanProducer | None = None,
    me_toggles: METoggles | None = None,
) -> InputIR:
    """Load an exported plan file and map it to the solver's ``InputIR``.

    ``physical`` is an optional multiblock dataset (``dataset.load_physical_dataset``); when given,
    a node whose machine type it knows gets that machine's real footprint (see :func:`to_input_ir`).
    ``producer`` pins which gtnh-factory-flow fork exported the plan; ``None`` detects it.
    ``me_toggles`` says which commodities ride ME instead of pipes and cables (see
    :func:`to_input_ir`).
    """
    return to_input_ir(load_plan(path), physical=physical, producer=producer, me_toggles=me_toggles)


def _block_key_for(recipe: Recipe, resolved: ResolvedMachine | None) -> str | None:
    """The recipe's GT controller-block id (``"<registry_name>@<meta>"``), if the export carries it.

    Prefers the recipe's own ``source.machineBlock`` and falls back to the ``resolved`` block's
    mirror of it, since gtnh-factory-flow #25 emits it in both places. None for any plan exported
    before that landed, and for an arodoid plan, which does not emit it: such a plan joins the
    dataset by name.

    This is only the export's claim, used to look the record up. The key a ``Machine`` carries is
    the resolved record's own (see :func:`to_input_ir`), which is what makes a name-resolved machine
    draw as the controller its footprint came from.
    """
    for block in (
        recipe.source.machine_block if recipe.source is not None else None,
        resolved.machine_block if resolved is not None else None,
    ):
        if block is not None and block.id:
            return block.id
    return None


def _fluid_output_count(recipe: Recipe) -> int:
    """How many distinct fluids the recipe outputs.

    Sizes a layer-indexed multiblock: a Distillation Tower needs one structure layer per fluid
    output because output ``i`` routes to layer ``i`` (see ``dataset.multiblocks.VariantShape``).
    """
    return sum(1 for out in recipe.outputs if out.kind == "fluid")


#: Exporter recipe-map names whose machine our structure dump keys under a different name, where the
#: export gives us no handler to bridge the two (its ``machineHandlers`` list is empty for these).
#: Deliberately tiny and hand-checked. It is a **stopgap**, not a mapping layer: the durable fix is
#: the controller-block id (``registry@meta``), which joins exactly and cannot rot on a rename, and
#: every entry here is a silent wrong answer waiting for a pack release to move a display name.
_MACHINE_NAME_ALIASES = {
    "Chemical Plant": "ExxonMobil Chemical Plant",
    "Multiblock Electrolyzer": "Industrial Electrolyzer",
    "Large Sifter": "Large Sifter Control Block",
}


def _candidate_names(recipe: Recipe, node: Node) -> list[str]:
    """The names to try against the structure dump, most authoritative first.

    The exporter names a machine by its localized **recipe map** ("Distillation Tower", "Blast
    Furnace"), while the dump is keyed by the **controller's** own display name ("Dangote Distillus",
    "Electric Blast Furnace"). For a GT++ machine the two differ, so joining on the recipe-map name
    alone resolves only a fraction of a real plan and drops the rest to a 1x1x1 footprint.

    The node's effective handler carries the controller name, so it goes first; the recipe-map name
    follows for a plan that has no handlers at all. Each is also tried through
    :data:`_MACHINE_NAME_ALIASES`.
    """
    names: list[str] = []
    handler = _effective_handler(recipe, node)
    if handler is not None and handler.label:
        names.append(handler.label)
    names.append(recipe.machine_type)
    ordered: list[str] = []
    for name in names:
        for candidate in (name, _MACHINE_NAME_ALIASES.get(name, "")):
            if candidate and candidate not in ordered:
                ordered.append(candidate)
    return ordered


def _physical_record(
    recipe: Recipe,
    node: Node,
    physical: PhysicalDataset | None,
    block_key: str | None,
) -> MachinePhysical | None:
    """This node's machine in the structure dump, or ``None`` if the dump does not have it.

    ``block_key`` is an exact ``registry@meta`` identity and wins whenever the export carries it;
    the name ladder (:func:`_candidate_names`) is the fallback for the plans that do not.
    """
    if physical is None:
        return None
    names = _candidate_names(recipe, node)
    record = physical.get(names[0], block_key=block_key)
    if record is not None:
        return record
    for name in names[1:]:
        record = physical.get(name)
        if record is not None:
            return record
    return None


def to_input_ir(
    plan: Plan,
    *,
    physical: PhysicalDataset | None = None,
    producer: PlanProducer | None = None,
    me_toggles: METoggles | None = None,
) -> InputIR:
    """Map a typed :class:`Plan` to an ``InputIR`` (referential integrity enforced on build).

    When ``physical`` is supplied, each node's machine footprint, hatch slots and ``block_key`` come
    from the record that dataset resolves for it (:func:`_physical_record`), so the machine is drawn
    as the same controller it was sized from. Otherwise the crude 1x1x1 default stands and the
    machine keeps whatever block key the export supplied, if any; boundary storages/buffers are
    never in the dataset and carry none.

    ``producer`` pins which gtnh-factory-flow fork exported the plan. ``None`` (the default) detects
    it from the plan's structural markers (``producer.resolve_producer``), which is itself allowed to
    come back undetermined - the two meanings never collide, because a *parameter* of ``None`` asks
    for detection while a *detected* ``None`` disables producer-specific handling.

    ``me_toggles`` names the commodities the line moves over ME (AE2) rather than over pipes and
    cables. It is not something a plan states, so it comes from the caller (the CLI's ``--me``) and
    is stamped on the ``InputIR`` unchanged; ``None`` keeps the default, every commodity routed
    physically. The mapping itself ignores it: the nets, storages and power synthesis are the same
    either way, and each downstream stage skips a toggled commodity itself (docs/DOMAIN.md).

    Raises :class:`~gtnh_solver.adapter.AdapterError` for a plan that does not map (a dangling
    reference, an unsupported kind), and :class:`~gtnh_solver.adapter.InfeasiblePlanError` for one
    that maps but states a line no layout can satisfy - the CLI keeps those apart, reporting the
    first as an unloadable export and the second as an infeasibility (#112).
    """
    resolved_producer = resolve_producer(plan, producer)
    _check_power_provenance(plan, resolved_producer)
    _check_dataset_version(plan, physical)
    recipes = {r.id: r for r in plan.recipes}
    nodes_by_id = {n.id: n for n in plan.nodes}
    storage_ids = {s.id for s in plan.storages}
    resolved_machines = (
        {rm.node_id: rm for rm in plan.resolved.machines} if plan.resolved is not None else {}
    )

    machines: list[Machine] = []
    # Machines a CENSUS dataset positively failed to find, so absence proves they are not
    # multiblocks: the only population whose intake ceiling GT's single-block rule may be stated
    # for (adapter.power). Empty without a dataset, and empty under the committed fixtures - a
    # two-machine sample, where a miss proves nothing (dataset.PhysicalDataset).
    single_block_ids: set[str] = set()
    identifies_single_blocks = physical is not None and physical.identifies_single_blocks
    # Machines whose I/O rides hatches, so a port of theirs is one hatch and one net (#213).
    multiblock_ids: set[str] = set()
    for node in plan.nodes:
        recipe = recipes.get(node.recipe_id)
        if recipe is None:
            raise AdapterError(f"node {node.id!r} references unknown recipe {node.recipe_id!r}")
        _check_input_overrides(recipe, node)
        _check_unmodelled_parallel(recipe, node)
        exported_key = _block_key_for(recipe, resolved_machines.get(node.id))
        record = _physical_record(recipe, node, physical, exported_key)
        # The machine carries the controller that was RESOLVED, however the join found it (block id,
        # handler label, recipe-map name or alias), not merely the one the export named. Its
        # footprint and hatch slots below come from `record`, and the previewer and `.schematic`
        # exporter draw whatever this key names, so the two must be one controller. Stamping only
        # the export's key left every name-resolved machine keyless (every 2.9 plan), and those
        # consumers fell back to `type`, a recipe-map name that can belong to a different machine:
        # a Dangote Distillus drew and exported as a plain Distillation Tower (#205). With no
        # record, the export's key (or None) passes through as before.
        block_key = record.block_key if record is not None else exported_key
        # One fluid-output count drives both the reserved shape and its hatch ceiling, so the two
        # cannot describe different built forms of the same machine. Both now come from the single
        # record resolved above, so the footprint cannot be looked up differently from the ceiling.
        fluid_outputs = _fluid_output_count(recipe)
        footprint = (
            record.footprint_for(fluid_outputs) if record is not None else _DEFAULT_FOOTPRINT
        )
        if record is None and identifies_single_blocks:
            _classify_census_miss(recipe, node, single_block_ids)
        if _is_multiblock(recipe, node, record):
            multiblock_ids.update(_instance_ids(node))
        # Every machine of a parallel node is the same build with the same ports and draw; they
        # differ only in id and, later, in where the placer puts them.
        machines.extend(
            Machine(
                id=instance_id,
                type=recipe.machine_type,
                block_key=block_key,
                footprint=footprint,
                # None without a dataset record: no structural ceiling is known, so the power
                # synthesis keeps such a machine on one connection (see adapter.power).
                hatch_cells=(
                    record.variant_for(fluid_outputs).hatch_cells or None
                    if record is not None
                    else None
                ),
                # From the same variant as the footprint and the ceiling, so all three describe one
                # built form. Empty when the dump recorded no slots, which reads as "unknown".
                hatch_slots=(record.variant_for(fluid_outputs).slots if record is not None else ()),
                faces=FaceSpec(ports=_recipe_ports(recipe, node)),
                voltage_tier=node.overclock_tier,
                # Every machine keeps all four horizontal facings: occupied_cells rotates a
                # non-cubic footprint now, so there is nothing left to pin against.
                orientation_options=list(_DEFAULT_ORIENTATIONS),
                # Per-machine EU/t the power synthesis sizes amperage from (see _node_eut).
                eut=_node_eut(recipe, node, resolved_machines),
            )
            for instance_id in _instance_ids(node)
        )

    instances_by_node = {node.id: _instance_ids(node) for node in plan.nodes}
    storage_ports = _storage_ports(plan, storage_ids, nodes_by_id, recipes)
    for storage in plan.storages:
        machines.append(
            Machine(
                id=storage.id,
                type=_storage_type(storage.kind),
                footprint=_DEFAULT_FOOTPRINT,
                faces=FaceSpec(ports=storage_ports.get(storage.id, [])),
                voltage_tier=_STORAGE_TIER,
                orientation_options=_DEFAULT_ORIENTATIONS,
            )
        )

    nets = [
        _net_for_edges(group, nodes_by_id, recipes, instances_by_node)
        for group in _edge_groups(plan.edges, instances_by_node, multiblock_ids)
    ]
    # Close the line: collect each unconsumed output (#16). Storages are named by id rather than
    # by their type string - a real GT machine called "Super ..." would have been skipped silently.
    # A merged net lists every endpoint its edges did, so the merge changes nothing consumed here.
    group_of = {
        instance_id: node_id
        for node_id, instance_ids in instances_by_node.items()
        for instance_id in instance_ids
    }
    machines, nets = _add_output_buffers(machines, nets, storage_ids, group_of)
    # The export has no power source; invent it. ``single_block_ids`` is what lets the synthesis
    # state a basic machine's own intake ceiling without guessing at a multiblock's.
    # _supply_tier absorbs an implausible draw from the MrBruh fork's recipe model. An arodoid
    # plan does not have that defect - its figures come from GT's own overclock calculator - so there
    # the workaround would re-tier machines that were already right. Disabled only for the producer
    # positively known not to need it: an undetermined plan keeps the defensive behaviour, since the
    # workaround changes only the voltage supplied and never the stated draw.
    machines, nets = synthesize_power(
        machines,
        nets,
        single_block_ids=frozenset(single_block_ids),
        allow_retier=resolved_producer is not PlanProducer.ARODOID_V1,
    )
    _check_resolved_power(plan, nets)
    region = _bounding_region([m.footprint for m in machines])
    return InputIR(
        bounding_region=region,
        machines=machines,
        nets=nets,
        me_toggles=me_toggles if me_toggles is not None else METoggles(),
    )


def _synthesized_eut(recipe: Recipe, node: Node) -> float:
    """The node's draw from the plan's own runtime figures, best available source first.

    The matched :class:`RuntimeVariant` wins, because its ``eut`` is post-overclock and the recipe's
    is the base value at the recipe's minimum tier; on a machine run a few tiers up those differ by
    4x per step. ``recipe.eut * parallel`` remains the floor for a node whose variant cannot be
    identified, and for a plan carrying no runtime figures at all.

    Producer-independent on purpose: both forks emit ``runtimeCalculation``, so this needs no branch
    on which one exported the plan.
    """
    variant = _matched_variant(recipe, node)
    if variant is not None:
        return variant.eut * variant.parallel * node.parallel
    return recipe.eut * node.parallel


def _node_eut(recipe: Recipe, node: Node, resolved: dict[str, ResolvedMachine]) -> float:
    """The EU/t **one machine** of this node draws, which the power synthesis sizes amperage from.

    Per instance, because the synthesis gives every machine its own energy hatches and sums their
    draw over the shared net. A group figure here would be counted ``machineCount`` times over.

    A v2 export's ``resolved.totalEut`` still wins where it covers the node: it is the exporter's own
    balancer output and models overclocking the recipe figure cannot. **It is a group total** though
    (``eutPerMachine x machineCount x parallel``), so it is divided back down before being compared
    with the per-machine synthesis or returned. That division is what makes the cross-check below an
    apples-to-apples comparison; it was only ever coherent because ``machineCount`` was forced to 1.

    **The two sources disagree by 24.5x on one real node** (an Industrial Coke Oven resolving to
    2355 EU/t where GT's own overclock calculator says 96) with nothing in the export to arbitrate,
    which is why the order is "the producer's balancer, then GT's calculator, then the base value"
    rather than a single source.
    """
    computed = _synthesized_eut(recipe, node)
    machine = resolved.get(node.id)
    if machine is None:
        return computed
    instances = max(1, node.machine_count)
    resolved_per_machine = machine.total_eut / instances
    if not math.isclose(
        resolved_per_machine,
        computed,
        rel_tol=_RESOLVED_EUT_TOLERANCE,
        abs_tol=_RESOLVED_EUT_TOLERANCE,
    ):
        warnings.warn(
            f"resolved EU/t for node {node.id!r} is {resolved_per_machine} per machine, but the "
            f"recipe synthesizes {computed} (eut {recipe.eut} x parallel {node.parallel}); "
            f"trusting the resolved figure",
            AdapterWarning,
            stacklevel=2,
        )
    return resolved_per_machine


def _instance_ids(node: Node) -> list[str]:
    """One machine id per physical machine this node stands for.

    A single-machine node keeps its **bare** node id, so every existing layout, golden file and
    preview is byte-identical to before parallel nodes were supported; only a node that really has
    several machines gains ``#1``-suffixed ids. That mirrors how the power synthesis already names
    things (``power:in`` alone, ``power:in#1`` when there are several).
    """
    if node.machine_count <= 1:
        return [node.id]
    return [f"{node.id}#{index + 1}" for index in range(node.machine_count)]


def _classify_census_miss(recipe: Recipe, node: Node, single_block_ids: set[str]) -> None:
    """Decide what a **census** dump's failure to find this machine means, and act on it.

    A census enumerates every multiblock controller in the pack, so a miss is a positive fact - but
    which fact depends on what the machine is, and the two readings are opposites:

    ``kind: "single"``, or no handler at all
        The machine is a single block. GT's ``MTEBasicMachine`` intake rule applies, so it joins
        ``single_block_ids`` and the under-supply check can run against a real ceiling.
    ``kind: "multiblock"``
        The export says this *is* a multiblock, so the dump is incomplete rather than the machine
        being basic. Claiming it as a single block would state the wrong intake formula AND reserve a
        1x1x1 box for a structure that does not fit it - the "coarse-cell abstraction can lie"
        failure, arriving quietly. Reported instead, and left unclaimed.

    Treating every miss alike is how a name-alias table swallows a real extraction gap.
    """
    handler = _effective_handler(recipe, node)
    if handler is None or handler.kind != "multiblock":
        single_block_ids.update(_instance_ids(node))
        return
    name = handler.label or recipe.machine_type
    warnings.warn(
        f"the structure dataset is a census but does not contain {name!r}, which node "
        f"{node.id!r} declares a multiblock; it keeps the 1x1x1 default footprint and its power "
        f"intake stays unmeasured. Re-run the extractor against this plan's pack version.",
        AdapterWarning,
        stacklevel=3,
    )


def _is_multiblock(recipe: Recipe, node: Node, record: MachinePhysical | None) -> bool:
    """Whether this node's machines do their I/O through hatches, so each port is one hatch.

    Either of two pieces of evidence settles it, and they are complementary rather than ranked:

    - **a structure record** (``record``), which the dump holds only for multiblock controllers, and
      which is what gives a machine the ``hatch_slots`` the router places hatches on;
    - **the export's own handler** declaring ``kind: "multiblock"``, which holds with no dataset at
      all, or with one that missed the machine.

    The physical fact is what matters: a multiblock's port is one hatch whether or not a local dump
    resolved its footprint, so a plan's nets do not change shape with the dataset it is adapted
    against. Neither piece of evidence proves the opposite, though. A machine with no record and no
    multiblock handler (a plan whose recipes list no handlers, adapted without a dump) reads as not
    known to be a multiblock and keeps its edges' nets apart. That costs nothing the router can see:
    it places hatches only where a record gave slots, so such a machine has no hatch to disagree
    about.
    """
    if record is not None:
        return True
    handler = _effective_handler(recipe, node)
    return handler is not None and handler.kind == "multiblock"


def _matched_variant(recipe: Recipe, node: Node) -> RuntimeVariant | None:
    """The runtime variant describing how this node runs the recipe, or ``None`` if not identifiable.

    Matching is on **fields, never on the variant id**: ids carry suffixes beyond the tier and coil
    (``tier-ev-perfect-oc``), so composing one from ``overclock_tier`` misses roughly half the nodes
    of a real plan while appearing to work.

    Narrowed in two steps. The node's ``overclock_tier`` selects candidates; then, if those
    candidates are coil-keyed, the node's ``coil_tier`` picks among them. A node that leaves
    ``coil_tier`` empty against coil-keyed variants stays ambiguous, and ambiguity returns ``None``
    rather than a guess, because every coil is a different heat bonus and so a different EU/t.
    """
    if recipe.runtime_calculation is None:
        return None
    candidates = [
        variant
        for variant in recipe.runtime_calculation.variants
        if variant.overclock_tier == node.overclock_tier
    ]
    if node.coil_tier and any(variant.coil_tier is not None for variant in candidates):
        narrowed = [variant for variant in candidates if variant.coil_tier == node.coil_tier]
        candidates = narrowed or candidates
    if len(candidates) != 1:
        return None
    return candidates[0]


def _handler_parallel(recipe: Recipe, node: Node) -> float:
    """The parallel multiplier this node's machine configuration applies, or 1.

    Read only to report it (:func:`_check_unmodelled_parallel`). The multiplier lives on the
    handler's ``machineParallel`` control and appears in neither ``node.parallel`` (always 1 on the
    plans seen) nor any runtime variant (all of which report ``parallel: 1``), so it is the one
    runtime figure the export does not compose for us.
    """
    handler = _effective_handler(recipe, node)
    if handler is None:
        return 1.0
    for control in handler.machine_config_controls:
        if control.id != _PARALLEL_CONTROL:
            continue
        chosen = node.machine_config_tiers.get(_PARALLEL_CONTROL) or control.default_key
        for tier in control.tiers:
            if tier.key == chosen:
                return tier.parallel_multiplier
    return 1.0


def _check_unmodelled_parallel(recipe: Recipe, node: Node) -> None:
    """Warn when a node's machine runs parallel batches the adapter does not account for.

    Deliberately not composed into the draw. Multiplying the variant through would be re-deriving
    the exporter's machine model in this codebase, and GT parallel multiblocks do not all scale EU/t
    linearly (several carry a parallel discount), so a composed figure would be confidently wrong
    with nothing to check it against. Reporting it keeps the error visible and one-directional: the
    layout is under-powered by a known factor rather than wrong by an unknown one.
    """
    multiplier = _handler_parallel(recipe, node)
    if multiplier <= 1.0:
        return
    handler = _effective_handler(recipe, node)
    name = (handler.label if handler is not None else "") or recipe.machine_type
    warnings.warn(
        f"node {node.id!r} runs {name} at {multiplier:g}x parallel, which the export states only as "
        f"a machine-configuration multiplier and the adapter does not model; its EU/t and throughput "
        f"are those of a single batch, so the layout under-provisions this machine by up to "
        f"{multiplier:g}x",
        AdapterWarning,
        stacklevel=3,
    )


def _effective_duration(recipe: Recipe, node: Node) -> float:
    """The recipe's duration as this node runs it: the matched variant's, else the base figure.

    Overclocking halves duration per tier step, so a throughput computed from the base figure is low
    by the same factor the draw would be. Fixing EU/t without this would leave every pipe on an
    overclocked machine sized for a fraction of the flow it carries.
    """
    variant = _matched_variant(recipe, node)
    if variant is not None and variant.duration_ticks > 0:
        return variant.duration_ticks
    return recipe.duration_ticks


def _effective_handler(recipe: Recipe, node: Node) -> MachineHandler | None:
    """The machine a node actually runs in: its ``machineHandlerId``, else the list's first entry.

    The first entry is the exporter's default, which is what a node with no explicit id is running
    (see :class:`plan.MachineHandler`). ``None`` when the recipe lists no handlers at all - common
    even on an arodoid plan, where several machine types carry an empty list - so callers must
    treat "no handler" as "no information", never as "single block".
    """
    if not recipe.machine_handlers:
        return None
    if node.machine_handler_id:
        for handler in recipe.machine_handlers:
            if handler.id == node.machine_handler_id:
                return handler
    return recipe.machine_handlers[0]


def _check_power_provenance(plan: Plan, producer: PlanProducer | None) -> None:
    """Warn when a plan's multiblocks will be powered from pre-overclock EU/t figures.

    A machine whose draw falls all the way to ``recipe.eut * parallel`` (:func:`_node_eut`) is being
    sized from the base value at its recipe's minimum tier. For a single-block machine running at
    that tier the figure is exact, which is why this stays quiet on such a plan. For a multiblock
    overclocked above it, the figure is low by 4x per tier step and the cable amperage is sized from
    it, so the layout comes out buildable and under-powered: the one failure a builder cannot see in
    the preview.

    **Both better sources silence it.** A ``resolved`` block covers the whole plan, and a matched
    runtime variant covers the node, so neither is a fallback. Warning merely because ``resolved`` is
    absent would fire on every arodoid plan forever, including the ones whose figures now come
    from GT's own overclock calculator and are right.

    Scoped to machines a handler declares ``multiblock``, since that is the only evidence available
    here of a machine being one at all; a plan whose handlers are empty says nothing and stays quiet.
    """
    if plan.resolved is not None:
        return
    recipes = {r.id: r for r in plan.recipes}
    affected = sorted(
        {
            handler.label or recipe.machine_type
            for node in plan.nodes
            if (recipe := recipes.get(node.recipe_id)) is not None
            and (handler := _effective_handler(recipe, node)) is not None
            and handler.kind == "multiblock"
            and _matched_variant(recipe, node) is None
        }
    )
    if not affected:
        return
    warnings.warn(
        f"plan carries no resolved throughput block, so EU/t for {len(affected)} multiblock "
        f"machine type(s) is synthesized from pre-overclock recipe figures and understates the "
        f"real draw ({', '.join(affected)}); power nets may be sized too thin"
        + (f" [producer: {producer.value}]" if producer is not None else ""),
        AdapterWarning,
        stacklevel=2,
    )


def _check_dataset_version(plan: Plan, physical: PhysicalDataset | None) -> None:
    """Warn when the plan was balanced against a different GTNH pack than the loaded dataset.

    Machine display names, recipe ids and item ids all move between pack releases, and the join from
    a plan's machine to its physical record is by name. So a mismatch does not fail: it *quietly
    degrades*, resolving some machines to the wrong footprint and missing others into the 1x1x1
    default, which is the shape of bug the whole dataset lane exists to remove.

    Silent when no dataset is loaded (every machine is 1x1x1 anyway, so there is nothing to
    mis-join) and when the plan does not state a single pack version (:func:`plan_pack_version`).

    Also silent for a **non-census** dump. The committed ``data/multiblocks/`` fixtures are a
    two-machine sample whose ``pack_version`` is nominal rather than surveyed, and they are what a
    fresh clone resolves to - so trusting it here would greet every new contributor with a mismatch
    against the shipped examples. This is the same reading ``identifies_single_blocks`` already
    applies to that dump: a sample is evidence of nothing beyond the machines in it.
    """
    if physical is None or not physical.meta.census:
        return
    stated = plan_pack_version(plan)
    if stated is None or stated == physical.meta.pack_version:
        return
    warnings.warn(
        f"plan was balanced against GTNH {stated}, but the loaded physical dataset is "
        f"{physical.meta.pack_version}; machine names move between packs, so footprints may be "
        f"resolved from the wrong machine or missed entirely. "
        f"Pass --dataset-version {stated} once a dump for it exists.",
        AdapterWarning,
        stacklevel=2,
    )


def _check_resolved_power(plan: Plan, nets: list[Net]) -> None:
    """Cross-check a v2 export's ``resolved.power`` total against the synthesized power nets.

    Each per-tier power net carries the summed EU/t draw of its machines
    (``power.synthesize_power``), so across tiers the nets must add up to
    ``resolved.power.totalEut``. A mismatch beyond float tolerance means the resolved block is
    internally inconsistent (its per-machine figures don't sum to its own total) or covers
    machines the plan graph doesn't; warn and continue - amperage stays sized from the per-net
    figures (#2). Silent for v1 plans and for a ``resolved`` block without ``power``.
    """
    if plan.resolved is None or plan.resolved.power is None:
        return
    synthesized = sum(net.throughput for net in nets if net.commodity is Commodity.POWER)
    resolved_total = plan.resolved.power.total_eut
    if not math.isclose(
        resolved_total,
        synthesized,
        rel_tol=_RESOLVED_EUT_TOLERANCE,
        abs_tol=_RESOLVED_EUT_TOLERANCE,
    ):
        warnings.warn(
            f"resolved power total is {resolved_total} EU/t, but the synthesized power nets "
            f"carry {synthesized} EU/t in total; layout keeps the per-net figures",
            AdapterWarning,
            stacklevel=2,
        )


def _commodity(kind: str) -> Commodity:
    try:
        return _COMMODITY[kind]
    except KeyError:
        raise AdapterError(f"unsupported resource kind {kind!r} (expected item or fluid)") from None


def _storage_type(kind: str) -> str:
    try:
        return _STORAGE_TYPE[kind]
    except KeyError:
        raise AdapterError(f"unsupported storage kind {kind!r} (expected item or fluid)") from None


def _port_id(direction: IODirection, resource_id: str) -> str:
    return f"{direction.value}:{resource_id}"  # e.g. "output:minecraft:sand"


def _port_resource(port_id: str) -> str:
    """The resource id back out of a ``_port_id`` (drop the ``{direction}:`` prefix) - the inverse
    of :func:`_port_id`, kept beside it so the encode/decode stay in sync. The resource keeps its
    own colons (``output:minecraft:sand`` -> ``minecraft:sand``)."""
    return port_id.split(":", 1)[1]


#: Forge's ``OreDictionary.WILDCARD_VALUE``. A recipe input spelled ``<registry>@32767`` accepts any
#: metadata of that block/item ("any log"), and the node's override is the exporter recording which
#: one the player actually feeds it ("oak log"). That narrowing is the only override this adapter
#: applies; see :func:`_refines`.
_WILDCARD_META = "32767"


def _refines(source: Resource, override: Resource) -> bool:
    """Whether ``override`` narrows ``source`` rather than replacing it.

    True only for an override that names the same resource more precisely: the same ``kind``, the
    same registry name, and a ``source`` whose metadata is the wildcard. An identical id is trivially
    true and costs nothing to apply.

    **Everything else is a substitution and must not be applied.** Real plans contain overrides that
    name an entirely different resource at that index (``oxygen -> water``,
    ``ammonia -> hydrochloricacid_gt5u``), always one the recipe already lists at the *next* index.
    Applying those would drop a required input and duplicate another, silently shrinking the port
    set; the adapter cannot tell a stale plan from a deliberate swap, so it keeps the recipe's own
    input and says so (:func:`_check_input_overrides`).
    """
    if source.kind != override.kind:
        return False
    if source.id == override.id:
        return True
    source_name, _, source_meta = source.id.partition("@")
    override_name, _, _ = override.id.partition("@")
    return source_name == override_name and source_meta == _WILDCARD_META


def _effective_inputs(recipe: Recipe, node: Node) -> list[Resource]:
    """``recipe.inputs`` with this node's refining overrides applied. Never mutates the recipe.

    One recipe is shared by every node that runs it, so resolving overrides in place would leak one
    node's ingredient choice into its siblings. The result is what both the ports and the throughput
    rates are built from, which is what keeps a concretised id from resolving to a rate of zero:
    ``_rate`` matches amounts by resource id, and the recipe's own list still says ``@32767``.
    """
    if not node.recipe_input_overrides:
        return recipe.inputs
    effective = list(recipe.inputs)
    for index, override in node.recipe_input_overrides.items():
        if 0 <= index < len(effective) and _refines(effective[index], override):
            effective[index] = override
    return effective


def _check_input_overrides(recipe: Recipe, node: Node) -> None:
    """Warn once per node for each override :func:`_effective_inputs` declined to apply.

    Separate from the resolution itself so the message lands once per node rather than once per
    port and per rate lookup, and so the resolution stays a pure function.
    """
    for index, override in sorted(node.recipe_input_overrides.items()):
        if not 0 <= index < len(recipe.inputs):
            warnings.warn(
                f"node {node.id!r} overrides input {index} of recipe {recipe.id!r}, which has "
                f"only {len(recipe.inputs)} input(s); ignoring the override",
                AdapterWarning,
                stacklevel=3,
            )
            continue
        source = recipe.inputs[index]
        if _refines(source, override):
            continue
        warnings.warn(
            f"node {node.id!r} overrides input {index} of recipe {recipe.id!r} from "
            f"{source.kind}:{source.id} to {override.kind}:{override.id}, which substitutes a "
            f"different resource rather than narrowing a wildcard; keeping the recipe's own input "
            f"(applying it would drop {source.id} from the machine's ports)",
            AdapterWarning,
            stacklevel=3,
        )


def _recipe_ports(recipe: Recipe, node: Node) -> list[Port]:
    """One input/output port per distinct recipe resource (deduped by id), each carrying the
    throughput it moves (items/t or mB/t) so boundary rates - notably a dangling output's product,
    which no net records - are reportable downstream."""
    ports: dict[str, Port] = {}
    for direction, pool, outputs in (
        (IODirection.INPUT, _effective_inputs(recipe, node), False),
        (IODirection.OUTPUT, recipe.outputs, True),
    ):
        for res in pool:
            pid = _port_id(direction, res.id)
            ports[pid] = Port(
                id=pid,
                commodity=_commodity(res.kind),
                direction=direction,
                rate=_rate(recipe, res.id, node, outputs=outputs),
            )
    return list(ports.values())


def _storage_ports(
    plan: Plan, storage_ids: set[str], nodes_by_id: dict[str, Node], recipes: dict[str, Recipe]
) -> dict[str, list[Port]]:
    """Ports a storage needs, inferred from the edges touching it (source->out, target->in).

    Each port carries the touching edge's own rate. That is the stated purpose of ``Port.rate`` -
    surfacing boundary I/O rates to ``system_io`` and the previewer - and a storage IS the boundary,
    so leaving it None was the one place the figure was both wanted and already computable.
    """
    by_storage: dict[str, dict[str, Port]] = {sid: {} for sid in storage_ids}
    for edge in plan.edges:
        commodity = _commodity(edge.resource_kind)
        rate = _throughput(edge, nodes_by_id, recipes)
        if edge.source in storage_ids:
            pid = _port_id(IODirection.OUTPUT, edge.resource_id)
            by_storage[edge.source][pid] = Port(
                id=pid, commodity=commodity, direction=IODirection.OUTPUT, rate=rate
            )
        if edge.target in storage_ids:
            pid = _port_id(IODirection.INPUT, edge.resource_id)
            by_storage[edge.target][pid] = Port(
                id=pid, commodity=commodity, direction=IODirection.INPUT, rate=rate
            )
    return {sid: list(ports.values()) for sid, ports in by_storage.items()}


def _add_output_buffers(
    machines: list[Machine],
    nets: list[Net],
    storage_ids: set[str],
    group_of: dict[str, str] | None = None,
) -> tuple[list[Machine], list[Net]]:
    """Close the line: synthesize a boundary Super Chest/Tank + net to collect each unconsumed
    system output - a machine OUTPUT port (item/fluid) that no net already sources (GitHub #16), so
    a final product is placed and wired to a buffer instead of exiting into thin air.

    **One buffer per node, not per machine.** ``group_of`` maps each machine id back to the node it
    is an instance of, so a parallel node's machines collect into a single chest on a shared net, the
    way a real line is built - rather than each getting a chest of its own. The net's rate is the
    summed ``Port.rate`` of the instances feeding it, which is the group rate again.
    """
    group_of = group_of or {}
    wired = {(ep.machine_id, ep.port_id) for net in nets for ep in net.endpoints}
    #: (group id, port id) -> the instance ports feeding one buffer, in machine order.
    pending: dict[tuple[str, str], list[tuple[Machine, Port]]] = {}
    for machine in machines:
        if machine.id in storage_ids:
            continue  # a storage's own output is already a boundary, not something to collect
        for port in machine.faces.ports:
            if port.direction is not IODirection.OUTPUT or port.commodity is Commodity.POWER:
                continue
            if (machine.id, port.id) in wired:
                continue  # already consumed by a net
            pending.setdefault((group_of.get(machine.id, machine.id), port.id), []).append(
                (machine, port)
            )

    buffers: list[Machine] = []
    buffer_nets: list[Net] = []
    for (group_id, port_id), feeds in pending.items():
        resource = _port_resource(port_id)  # strip the "output:" prefix -> the resource id
        commodity = feeds[0][1].commodity
        rate = sum(port.rate or 0.0 for _, port in feeds)
        buffer_id = f"output-buffer:{group_id}:{resource}"
        in_pid = _port_id(IODirection.INPUT, resource)
        buffers.append(
            Machine(
                id=buffer_id,
                type=_STORAGE_TYPE[commodity.value],
                footprint=_DEFAULT_FOOTPRINT,
                faces=FaceSpec(
                    ports=[
                        Port(
                            id=in_pid,
                            commodity=commodity,
                            direction=IODirection.INPUT,
                            rate=rate,
                        )
                    ]
                ),
                voltage_tier=_STORAGE_TIER,
                orientation_options=_DEFAULT_ORIENTATIONS,
            )
        )
        buffer_nets.append(
            Net(
                id=f"output-net:{group_id}:{resource}",
                commodity=commodity,
                fluid_or_item=resource,
                throughput=rate,
                endpoints=[
                    *(
                        MachineFaceRef(machine_id=machine.id, port_id=port.id)
                        for machine, port in feeds
                    ),
                    MachineFaceRef(machine_id=buffer_id, port_id=in_pid),
                ],
            )
        )
    return machines + buffers, nets + buffer_nets


def _edge_sides(
    edge: Edge, instances_by_node: dict[str, list[str]]
) -> tuple[list[MachineFaceRef], list[MachineFaceRef]]:
    """The ``(producers, consumers)`` an edge wires: **every machine** on each side of it.

    A parallel node's machines share one bus, which is both how GT lines are actually built and what
    keeps this from needing a new IR concept: ``Net.endpoints`` is already an unbounded list, so N
    producers and M consumers on one net is expressible today. The router chains the endpoints and
    the validator permits several producers, so nothing downstream has to learn about groups.

    An endpoint referring to a storage passes through unexpanded: a storage is one block.
    """
    out_pid = _port_id(IODirection.OUTPUT, edge.resource_id)
    in_pid = _port_id(IODirection.INPUT, edge.resource_id)
    return (
        [
            MachineFaceRef(machine_id=mid, port_id=out_pid)
            for mid in instances_by_node.get(edge.source, [edge.source])
        ],
        [
            MachineFaceRef(machine_id=mid, port_id=in_pid)
            for mid in instances_by_node.get(edge.target, [edge.target])
        ],
    )


def _edge_groups(
    edges: list[Edge], instances_by_node: dict[str, list[str]], multiblock_ids: set[str]
) -> list[list[Edge]]:
    """Partition ``edges`` into the sets that must be one net: those meeting at a multiblock port.

    Union by transitive closure over the hatch-bearing ``(machine, port)`` pairs each edge touches,
    so an output feeding two consumers is one group and so is an input fed by two producers, and two
    such ports chained through a shared edge join into one. A port of any other machine joins
    nothing (see the module docstring for why storages and single blocks stay apart).

    Deterministic by construction: each group lists its edges in plan order, and the groups come
    out in the plan order of their first edge. An edge that shares no multiblock port is a group of
    one, which is what keeps every plan without such a port byte-identical to before.
    """
    parent = list(range(len(edges)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]  # path halving
            index = parent[index]
        return index

    first_edge_at: dict[tuple[str, str], int] = {}
    for index, edge in enumerate(edges):
        producers, consumers = _edge_sides(edge, instances_by_node)
        for ref in (*producers, *consumers):
            if ref.machine_id not in multiblock_ids:
                continue
            here = root(index)
            there = root(first_edge_at.setdefault((ref.machine_id, ref.port_id), index))
            # The lower index roots the union, so a group's root is its first edge in plan order.
            parent[max(here, there)] = min(here, there)
    groups: dict[int, list[Edge]] = {}
    for index, edge in enumerate(edges):
        groups.setdefault(root(index), []).append(edge)
    return list(groups.values())


def _net_for_edges(
    group: list[Edge],
    nodes_by_id: dict[str, Node],
    recipes: dict[str, Recipe],
    instances_by_node: dict[str, list[str]],
) -> Net:
    """One net carrying every edge of ``group`` (:func:`_edge_groups`); a lone edge maps as ever.

    - **id**: the edge ids joined with ``+`` in plan order, so a merged net names every edge it
      stands for and a lone edge keeps its own id unchanged.
    - **endpoints**: every producer, then every consumer, each once and in plan order. A shared port
      is listed once however many of the group's edges touch it, which is the point.
    - **throughput**: :func:`_group_throughput`, which counts each producer once.

    Every edge must carry the same resource. A port is named for its resource, so sharing one
    already implies the same id; what can still disagree is the kind, and a port that is somehow
    both an item and a fluid is a malformed plan, refused here rather than routed as one or other.
    """
    first = group[0]
    for edge in group[1:]:
        if (edge.resource_kind, edge.resource_id) != (first.resource_kind, first.resource_id):
            raise AdapterError(
                f"edges {first.id!r} and {edge.id!r} meet at one multiblock port but carry "
                f"different resources ({first.resource_kind}:{first.resource_id} vs "
                f"{edge.resource_kind}:{edge.resource_id}); one port has one resource"
            )
    producers: list[MachineFaceRef] = []
    consumers: list[MachineFaceRef] = []
    for edge in group:
        edge_producers, edge_consumers = _edge_sides(edge, instances_by_node)
        producers.extend(ref for ref in edge_producers if ref not in producers)
        consumers.extend(ref for ref in edge_consumers if ref not in consumers)
    return Net(
        id="+".join(edge.id for edge in group),
        commodity=_commodity(first.resource_kind),
        fluid_or_item=first.resource_id,
        throughput=_group_throughput(group, nodes_by_id, recipes),
        endpoints=[*producers, *consumers],
    )


def _group_throughput(
    group: list[Edge], nodes_by_id: dict[str, Node], recipes: dict[str, Recipe]
) -> float:
    """What a net of these edges moves: each distinct flow its edges are rated by, counted once.

    Each edge is rated by one node's port (:func:`_throughput`): its producer's group output, or,
    for a storage-fed edge, its consumer's demand. Summing the edge ratings would double-count a
    shared port, because every edge off one output is rated at that output's **full** rate: an LCR
    whose acid feeds a Chemical Plant and an overflow tank moves its acid once, not twice. So a
    merged fan-out carries its producer's rate and a merged fan-in the sum of its producers', which
    is the figure a lone edge's net always meant.

    The one approximation: an input fed by both a storage and a machine sums the machine's output
    with the storage-fed demand, because the plan carries no per-edge rate saying how much of that
    demand the storage covers. It can only over-state the flow, never under-state it.
    """
    rated: dict[tuple[str, IODirection], float] = {}
    for edge in group:
        rated.setdefault(_rated_port(edge, nodes_by_id), _throughput(edge, nodes_by_id, recipes))
    return sum(rated.values())


def _rated_port(edge: Edge, nodes_by_id: dict[str, Node]) -> tuple[str, IODirection]:
    """Whose port :func:`_throughput` rates ``edge`` by, as ``(id, direction)``.

    The producer's output when the producer is a node, else the consumer's input. (A storage to
    storage edge, rated zero, touches no multiblock and so is only ever a group of one, where the
    key decides nothing.)
    """
    if edge.source in nodes_by_id:
        return edge.source, IODirection.OUTPUT
    return edge.target, IODirection.INPUT


def _throughput(edge: Edge, nodes_by_id: dict[str, Node], recipes: dict[str, Recipe]) -> float:
    """Typed rate for an edge: the producing node's group output rate, else the consumer's demand.

    A *group* rate, because the edge is one shared net across however many machines the node stands
    for (:func:`_group_rate`). Which port that reads is :func:`_rated_port`.
    """
    source = nodes_by_id.get(edge.source)
    if source is not None:
        return _group_rate(recipes[source.recipe_id], edge.resource_id, source, outputs=True)
    target = nodes_by_id.get(edge.target)
    if target is not None:
        return _group_rate(recipes[target.recipe_id], edge.resource_id, target, outputs=False)
    return 0.0  # storage -> storage (no recipe to rate it against)


def _rate(recipe: Recipe, resource_id: str, node: Node, *, outputs: bool) -> float:
    """The rate **one machine** of this node moves, in items/t or mB/t.

    Per instance, not per node: this feeds ``Port.rate``, and a port belongs to one physical machine.
    ``machineCount`` therefore stays out of it and belongs to :func:`_group_rate`, which is what a
    shared net carries. Keeping the two apart is the whole distinction a parallel node introduces.
    """
    # Inputs come from the node's effective list, not the recipe's own: an edge names the
    # concretised id ("minecraft:log@1") while the recipe still says "@32767", so matching against
    # the recipe here would sum nothing and rate the net at zero.
    pool = recipe.outputs if outputs else _effective_inputs(recipe, node)
    amount = sum(res.amount for res in pool if res.id == resource_id)
    # The duration this node actually runs at, not the recipe's base figure: overclocking halves it
    # per tier step, so the base value understates flow by the factor it understates draw.
    duration = _effective_duration(recipe, node)
    if duration <= 0:
        return 0.0
    return amount * node.parallel / duration


def _group_rate(recipe: Recipe, resource_id: str, node: Node, *, outputs: bool) -> float:
    """The rate the node's **whole group** moves: :func:`_rate` across all its machines.

    What a net carries, because the node's machines share one bus. A three-machine node feeding a
    downstream node moves three times one machine's output through that pipe, and sizing the pipe
    from a single instance would under-provision it by the machine count.
    """
    return _rate(recipe, resource_id, node, outputs=outputs) * node.machine_count


#: Multiplier on the summed footprint floor area when sizing the region's side (leaves routing
#: slack around densely first-fit-packed machines). 4x -> ~75% of the floor is free for channels.
_REGION_AREA_SLACK = 4
#: Cells of clear headroom above the tallest machine for routing runs over the top of the stack.
#: Tuned so an all-1x1x1 line (max height 1) keeps the historical region height of 4.
_REGION_HEIGHT_HEADROOM = 3


def _bounding_region(footprints: list[CellBox]) -> CellBox:
    """A region comfortably larger than the machines, sized from their ACTUAL footprints.

    Footprint-aware, not count-based: the height clears the *tallest* machine (a hardcoded 4 would
    make a 10-tall Distillation Tower infeasible before placement even runs), and the square floor
    holds the *summed* footprint areas with routing slack, never below the widest single machine nor
    the old count-based generosity. This is a generous feasibility bound, not a compactness target -
    the placement optimizer packs machines tightly inside it; the region only has to make a valid
    layout reachable. For an all-1x1x1 line the result is identical to the previous ``side x 4 x
    side`` sizing, so the shipped examples are unchanged.
    """
    if not footprints:
        return CellBox(sx=8, sy=4, sz=8)  # defensive: synthesis always adds >=1 machine in practice
    n = len(footprints)
    max_height = max(fp.sy for fp in footprints)
    widest = max(max(fp.sx, fp.sz) for fp in footprints)  # largest horizontal extent of one machine
    total_area = sum(fp.sx * fp.sz for fp in footprints)
    area_side = math.isqrt(total_area * _REGION_AREA_SLACK - 1) + 1  # ceil(sqrt(area * slack))
    side = max(8, n * 2, widest, area_side)
    return CellBox(sx=side, sy=max_height + _REGION_HEIGHT_HEADROOM, sz=side)
