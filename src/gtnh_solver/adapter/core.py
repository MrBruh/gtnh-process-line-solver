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

Crude-on-purpose for Phase 1 (docs/ROADMAP.md): single-block 1x1x1 footprints and default
orientations for every machine (real footprints/faces come from the dataset lane), and
**multi-instance nodes (``machineCount > 1``) are rejected** rather than mapped - a net endpoint
cannot address one instance of a group until routing is instance-aware (InputIR v1 dropped
``Machine.count``; see ``ir/__init__.py``, docs/ROADMAP.md). The InputIR's own
referential-integrity check is the validation gate: a dangling edge or commodity mismatch fails
loud here, which is the adapter contract (docs/TESTING.md).
"""

from __future__ import annotations

import json
from pathlib import Path

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

from .plan import Edge, Node, Plan, Recipe
from .power import synthesize_power

# Crude single-block physical defaults until the dataset lane provides real footprints/faces.
_DEFAULT_FOOTPRINT = CellBox()  # 1x1x1
_DEFAULT_ORIENTATIONS = [Facing.NORTH, Facing.SOUTH, Facing.EAST, Facing.WEST]
_STORAGE_TIER = "LV"  # storages are unpowered; placeholder tier to satisfy the contract

_COMMODITY = {"item": Commodity.ITEM, "fluid": Commodity.FLUID}
# Boundary I/O blocks that accept I/O covers on their faces (keeps covers off pipes).
_STORAGE_TYPE = {"item": "Super Chest", "fluid": "Super Tank"}


class AdapterError(ValueError):
    """An exported plan could not be mapped to the IR (dangling reference, bad kind, ...)."""


def load_plan(path: str | Path) -> Plan:
    """Parse a gtnh-factory-flow exported plan JSON file into a typed :class:`Plan`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Plan.model_validate(data)


def adapt_file(path: str | Path) -> InputIR:
    """Load an exported plan file and map it to the solver's ``InputIR``."""
    return to_input_ir(load_plan(path))


def to_input_ir(plan: Plan) -> InputIR:
    """Map a typed :class:`Plan` to an ``InputIR`` (referential integrity enforced on build)."""
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
                footprint=_DEFAULT_FOOTPRINT,
                faces=FaceSpec(ports=_recipe_ports(recipe)),
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


def _recipe_ports(recipe: Recipe) -> list[Port]:
    """One input/output port per distinct recipe resource (deduped by id)."""
    ports: dict[str, Port] = {}
    for direction, pool in (
        (IODirection.INPUT, recipe.inputs),
        (IODirection.OUTPUT, recipe.outputs),
    ):
        for res in pool:
            pid = _port_id(direction, res.id)
            ports[pid] = Port(id=pid, commodity=_commodity(res.kind), direction=direction)
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
