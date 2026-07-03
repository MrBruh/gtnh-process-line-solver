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
- ``power``   -> synthesized, not in the export: a source machine + shared-amperage net per
                voltage tier feed the powered machines (``power`` submodule, docs/DOMAIN.md).

Footprints: single-block 1x1x1 by default, or a machine's real multiblock footprint when an
optional physical dataset (``dataset.load_physical_dataset``) is passed and knows the type - see
``to_input_ir(plan, physical=...)``. Still crude-on-purpose for Phase 1 (docs/ROADMAP.md): default
orientations for every machine (hint-derived face constraints stay on the dataset record), and
**multi-instance nodes (``machineCount > 1``) are rejected** rather than mapped - a net endpoint
cannot address one instance of a group until routing is instance-aware (InputIR v1 dropped
``Machine.count``; see ``ir/__init__.py``, docs/ROADMAP.md). The InputIR's own
referential-integrity check is the validation gate: a dangling edge or commodity mismatch fails
loud here, which is the adapter contract (docs/TESTING.md).
"""

from __future__ import annotations

import json
from pathlib import Path

from gtnh_solver.dataset import PhysicalDataset
from gtnh_solver.ir import (
    CellBox,
    Commodity,
    FaceSpec,
    InputIR,
    IODirection,
    Machine,
    MachineFaceRef,
    Net,
    Port,
)
from gtnh_solver.ir.enums import HORIZONTAL_FACINGS_ORDERED

from ._errors import AdapterError
from .plan import Edge, Node, Plan, Recipe
from .power import synthesize_power

# Crude single-block physical defaults until the dataset lane provides real footprints/faces.
_DEFAULT_FOOTPRINT = CellBox()  # 1x1x1
_DEFAULT_ORIENTATIONS = list(HORIZONTAL_FACINGS_ORDERED)  # front defaults to the first (NORTH)
_STORAGE_TIER = "LV"  # storages are unpowered; placeholder tier to satisfy the contract

_COMMODITY = {"item": Commodity.ITEM, "fluid": Commodity.FLUID}
# Boundary I/O blocks that accept I/O covers on their faces (keeps covers off pipes).
_STORAGE_TYPE = {"item": "Super Chest", "fluid": "Super Tank"}


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
    machines have square (NxN) bases, so their bbox is rotation-invariant.
    """
    if physical is not None:
        record = physical.get(machine_type)
        if record is not None:
            return record.footprint
    return _DEFAULT_FOOTPRINT


def to_input_ir(plan: Plan, *, physical: PhysicalDataset | None = None) -> InputIR:
    """Map a typed :class:`Plan` to an ``InputIR`` (referential integrity enforced on build).

    When ``physical`` is supplied, each node's machine footprint comes from that dataset if it knows
    the machine type; otherwise (and for boundary storages/buffers, which are never in the dataset)
    the crude 1x1x1 default stands.
    """
    recipes = {r.id: r for r in plan.recipes}
    nodes_by_id = {n.id: n for n in plan.nodes}
    storage_ids = {s.id for s in plan.storages}

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
        machines.append(
            Machine(
                id=node.id,
                type=recipe.machine_type,
                footprint=_footprint_for(recipe.machine_type, physical),
                faces=FaceSpec(ports=_recipe_ports(recipe, node)),
                voltage_tier=node.overclock_tier,
                orientation_options=_DEFAULT_ORIENTATIONS,
                # EU/t draw the power synthesis sizes amperage from. ``parallel`` runs the recipe
                # that many times at once, so the node draws ``recipe.eut`` per parallel - matching
                # how ``_rate`` scales throughput. (``machineCount`` is forced to 1 above.)
                eut=recipe.eut * node.parallel,
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
    return InputIR(bounding_region=_bounding_region(len(machines)), machines=machines, nets=nets)


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


def _bounding_region(n_machines: int) -> CellBox:
    """A region comfortably larger than the machine count, so the crude placer + router fit."""
    side = max(8, n_machines * 2)
    return CellBox(sx=side, sy=4, sz=side)
