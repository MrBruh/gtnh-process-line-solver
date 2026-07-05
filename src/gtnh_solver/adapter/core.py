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
                recipe rate.
- ``power``   -> a source machine + shared-amperage net per voltage tier feed the powered
                machines (``power`` submodule, docs/DOMAIN.md); the export carries no source.
                Each machine's EU/t draw comes from a v2 export's ``resolved`` block when it
                covers the node (the exporter's balancer models overclocking, which
                ``recipe.eut`` cannot); ``recipe.eut * parallel`` is the v1 fallback and the
                cross-check - a mismatch warns (``AdapterWarning``) but resolved wins (#2).

Footprints: single-block 1x1x1 by default, or a machine's real multiblock footprint when an
optional physical dataset (``dataset.load_physical_dataset``) is passed and knows the type - see
``to_input_ir(plan, physical=...)``; the bounding region is then sized to fit those footprints
(``_bounding_region``). Still crude-on-purpose for Phase 1 (docs/ROADMAP.md): all four horizontal
orientations for a square-base machine, but a non-square-base multiblock is pinned to one
orientation until ``occupied_cells`` rotates (``_orientations_for``); hint-derived face constraints
stay on the dataset record; and **multi-instance nodes (``machineCount > 1``) are rejected**
rather than mapped - a net endpoint
cannot address one instance of a group until routing is instance-aware (InputIR v1 dropped
``Machine.count``; see ``ir/__init__.py``, docs/ROADMAP.md). The InputIR's own
referential-integrity check is the validation gate: a dangling edge or commodity mismatch fails
loud here, which is the adapter contract (docs/TESTING.md).
"""

from __future__ import annotations

import json
import math
import warnings
from pathlib import Path

from gtnh_solver.dataset import PhysicalDataset
from gtnh_solver.ir import (
    CellBox,
    Commodity,
    FaceSpec,
    Facing,
    InputIR,
    IODirection,
    Machine,
    MachineFaceRef,
    Net,
    Port,
)
from gtnh_solver.ir.enums import HORIZONTAL_FACINGS_ORDERED

from ._errors import AdapterError, AdapterWarning
from .plan import Edge, Node, Plan, Recipe, ResolvedMachine
from .power import synthesize_power

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


def load_plan(path: str | Path) -> Plan:
    """Parse a gtnh-factory-flow exported plan JSON file into a typed :class:`Plan`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Plan.model_validate(data)


def adapt_file(path: str | Path, *, physical: PhysicalDataset | None = None) -> InputIR:
    """Load an exported plan file and map it to the solver's ``InputIR``.

    ``physical`` is an optional multiblock dataset (``dataset.load_physical_dataset``); when given,
    a node whose machine type it knows gets that machine's real footprint (see :func:`to_input_ir`).
    """
    return to_input_ir(load_plan(path), physical=physical)


def _footprint_for(machine_type: str, physical: PhysicalDataset | None) -> CellBox:
    """The footprint for a machine type: the dataset's real one if known, else the 1x1x1 default.

    Opt-in by design - with no dataset every machine stays single-block (the Phase 1 behaviour), so
    the solver runs whether or not a ``data/multiblocks/`` dump is present. Orientation handling of
    non-cubic footprints is a placement TODO (``ir/geometry.occupied_cells``); the current dataset
    machines have square (NxN) bases, so their bbox is rotation-invariant. A footprint whose base is
    *not* square is pinned to one orientation by :func:`_orientations_for` until that TODO lands.
    """
    if physical is not None:
        record = physical.get(machine_type)
        if record is not None:
            return record.footprint
    return _DEFAULT_FOOTPRINT


def _orientations_for(footprint: CellBox) -> list[Facing]:
    """Legal front-face orientations for a machine with this footprint.

    ``ir.geometry.occupied_cells`` does NOT yet rotate a non-cubic footprint (its documented TODO),
    and the validator's independent safety net shares that primitive - so a horizontally-rotated
    non-square-base multiblock would reserve the wrong cells on BOTH the solver and its gate, a
    *shared* blind spot the gate could not catch. Until ``occupied_cells`` is rotation-aware
    (recorded as the follow-up there), a footprint whose base is not square (``sx != sz``) is pinned
    to a single default orientation so its reserved box always matches reality. A square-base
    footprint - every 1x1x1 block and every current dataset multiblock (EBF 3x3x4, Vacuum Freezer
    3x3x3) - keeps all four horizontal facings, since a vertical-axis turn leaves its bbox unchanged;
    this is the safe, lower-risk path (no dataset machine is non-square today, so shipping behaviour
    is unchanged, and the guard is in place the moment one is added).
    """
    if footprint.sx == footprint.sz:
        return list(_DEFAULT_ORIENTATIONS)
    return [_DEFAULT_ORIENTATIONS[0]]  # non-square base: one orientation until rotation is modelled


def to_input_ir(plan: Plan, *, physical: PhysicalDataset | None = None) -> InputIR:
    """Map a typed :class:`Plan` to an ``InputIR`` (referential integrity enforced on build).

    When ``physical`` is supplied, each node's machine footprint comes from that dataset if it knows
    the machine type; otherwise (and for boundary storages/buffers, which are never in the dataset)
    the crude 1x1x1 default stands.
    """
    recipes = {r.id: r for r in plan.recipes}
    nodes_by_id = {n.id: n for n in plan.nodes}
    storage_ids = {s.id for s in plan.storages}
    resolved_machines = (
        {rm.node_id: rm for rm in plan.resolved.machines} if plan.resolved is not None else {}
    )

    machines: list[Machine] = []
    for node in plan.nodes:
        recipe = recipes.get(node.recipe_id)
        if recipe is None:
            raise AdapterError(f"node {node.id!r} references unknown recipe {node.recipe_id!r}")
        if node.machine_count != 1:
            raise AdapterError(
                f"node {node.id!r} has machineCount={node.machine_count}; multi-instance nodes "
                f"are not supported yet (instance-aware routing is Phase 2 - see docs/ROADMAP.md). "
                f"Split it into single-machine nodes in the export."
            )
        footprint = _footprint_for(recipe.machine_type, physical)
        machines.append(
            Machine(
                id=node.id,
                type=recipe.machine_type,
                footprint=footprint,
                faces=FaceSpec(ports=_recipe_ports(recipe, node)),
                voltage_tier=node.overclock_tier,
                # A non-square multiblock is pinned to one orientation until occupied_cells rotates.
                orientation_options=_orientations_for(footprint),
                # EU/t draw the power synthesis sizes amperage from (see _node_eut).
                eut=_node_eut(recipe, node, resolved_machines),
            )
        )

    storage_ports = _storage_ports(plan, storage_ids)
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

    nets = [_net_for_edge(edge, nodes_by_id, recipes) for edge in plan.edges]
    machines, nets = _add_output_buffers(
        machines, nets
    )  # close the line: collect each output (#16)
    machines, nets = synthesize_power(machines, nets)  # the export has no power source; invent it
    _check_resolved_power(plan, nets)
    region = _bounding_region([m.footprint for m in machines])
    return InputIR(bounding_region=region, machines=machines, nets=nets)


def _node_eut(recipe: Recipe, node: Node, resolved: dict[str, ResolvedMachine]) -> float:
    """The EU/t a node draws, which the power synthesis sizes amperage from.

    Synthesized as ``recipe.eut * parallel``: ``parallel`` runs the recipe that many times at
    once, matching how ``_rate`` scales throughput (``machineCount`` is forced to 1 upstream).
    When a v2 export's ``resolved`` block covers the node, its ``totalEut`` is trusted instead -
    the exporter's balancer models overclocking, which the raw recipe figure cannot - but is
    cross-checked against the synthesis: a mismatch beyond float tolerance warns and the
    resolved figure still wins (#2). v1 plans (no ``resolved``) always use the synthesis.
    """
    computed = recipe.eut * node.parallel
    machine = resolved.get(node.id)
    if machine is None:
        return computed
    if not math.isclose(
        machine.total_eut,
        computed,
        rel_tol=_RESOLVED_EUT_TOLERANCE,
        abs_tol=_RESOLVED_EUT_TOLERANCE,
    ):
        warnings.warn(
            f"resolved EU/t for node {node.id!r} is {machine.total_eut}, but the recipe "
            f"synthesizes {computed} (eut {recipe.eut} x parallel {node.parallel}); trusting "
            f"the resolved figure",
            AdapterWarning,
            stacklevel=2,
        )
    return machine.total_eut


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


def _recipe_ports(recipe: Recipe, node: Node) -> list[Port]:
    """One input/output port per distinct recipe resource (deduped by id), each carrying the
    throughput it moves (items/t or mB/t) so boundary rates - notably a dangling output's product,
    which no net records - are reportable downstream."""
    ports: dict[str, Port] = {}
    for direction, pool, outputs in (
        (IODirection.INPUT, recipe.inputs, False),
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


def _storage_ports(plan: Plan, storage_ids: set[str]) -> dict[str, list[Port]]:
    """Ports a storage needs, inferred from the edges touching it (source->out, target->in)."""
    by_storage: dict[str, dict[str, Port]] = {sid: {} for sid in storage_ids}
    for edge in plan.edges:
        commodity = _commodity(edge.resource_kind)
        if edge.source in storage_ids:
            pid = _port_id(IODirection.OUTPUT, edge.resource_id)
            by_storage[edge.source][pid] = Port(
                id=pid, commodity=commodity, direction=IODirection.OUTPUT
            )
        if edge.target in storage_ids:
            pid = _port_id(IODirection.INPUT, edge.resource_id)
            by_storage[edge.target][pid] = Port(
                id=pid, commodity=commodity, direction=IODirection.INPUT
            )
    return {sid: list(ports.values()) for sid, ports in by_storage.items()}


def _add_output_buffers(
    machines: list[Machine], nets: list[Net]
) -> tuple[list[Machine], list[Net]]:
    """Close the line: synthesize a boundary Super Chest/Tank + net to collect each unconsumed
    system output - a machine OUTPUT port (item/fluid) that no net already sources (GitHub #16), so
    a final product is placed and wired to a buffer instead of exiting into thin air. The net's rate
    is the port's recorded throughput (``Port.rate``)."""
    wired = {(ep.machine_id, ep.port_id) for net in nets for ep in net.endpoints}
    buffers: list[Machine] = []
    buffer_nets: list[Net] = []
    for machine in machines:
        if machine.type.startswith("Super "):  # a storage's own output is a boundary, not collected
            continue
        for port in machine.faces.ports:
            if port.direction is not IODirection.OUTPUT or port.commodity is Commodity.POWER:
                continue
            if (machine.id, port.id) in wired:
                continue  # already consumed by a net
            resource = _port_resource(port.id)  # strip the "output:" prefix -> the resource id
            buffer_id = f"output-buffer:{machine.id}:{resource}"
            in_pid = _port_id(IODirection.INPUT, resource)
            buffers.append(
                Machine(
                    id=buffer_id,
                    type=_STORAGE_TYPE[port.commodity.value],
                    footprint=_DEFAULT_FOOTPRINT,
                    faces=FaceSpec(
                        ports=[
                            Port(
                                id=in_pid,
                                commodity=port.commodity,
                                direction=IODirection.INPUT,
                                rate=port.rate,
                            )
                        ]
                    ),
                    voltage_tier=_STORAGE_TIER,
                    orientation_options=_DEFAULT_ORIENTATIONS,
                )
            )
            buffer_nets.append(
                Net(
                    id=f"output-net:{machine.id}:{resource}",
                    commodity=port.commodity,
                    fluid_or_item=resource,
                    throughput=port.rate or 0.0,
                    endpoints=[
                        MachineFaceRef(machine_id=machine.id, port_id=port.id),
                        MachineFaceRef(machine_id=buffer_id, port_id=in_pid),
                    ],
                )
            )
    return machines + buffers, nets + buffer_nets


def _net_for_edge(edge: Edge, nodes_by_id: dict[str, Node], recipes: dict[str, Recipe]) -> Net:
    return Net(
        id=edge.id,
        commodity=_commodity(edge.resource_kind),
        fluid_or_item=edge.resource_id,
        throughput=_throughput(edge, nodes_by_id, recipes),
        endpoints=[
            MachineFaceRef(
                machine_id=edge.source, port_id=_port_id(IODirection.OUTPUT, edge.resource_id)
            ),
            MachineFaceRef(
                machine_id=edge.target, port_id=_port_id(IODirection.INPUT, edge.resource_id)
            ),
        ],
    )


def _throughput(edge: Edge, nodes_by_id: dict[str, Node], recipes: dict[str, Recipe]) -> float:
    """Typed rate for an edge: the producing node's output rate, else the consumer's demand."""
    source = nodes_by_id.get(edge.source)
    if source is not None:
        return _rate(recipes[source.recipe_id], edge.resource_id, source, outputs=True)
    target = nodes_by_id.get(edge.target)
    if target is not None:
        return _rate(recipes[target.recipe_id], edge.resource_id, target, outputs=False)
    return 0.0  # storage -> storage (no recipe to rate it against)


def _rate(recipe: Recipe, resource_id: str, node: Node, *, outputs: bool) -> float:
    pool = recipe.outputs if outputs else recipe.inputs
    amount = sum(res.amount for res in pool if res.id == resource_id)
    if recipe.duration_ticks <= 0:
        return 0.0
    return amount * node.parallel * node.machine_count / recipe.duration_ticks


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
