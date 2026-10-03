"""previewer.scene - denormalize a (problem, layout) pair into a self-contained render scene.

The output-layout contract (``LayoutResult``) references machines by id and leaves their
geometry in the ``InputIR``; a renderer needs it all in one place. ``build_scene`` flattens both
into a plain dict the three.js viewer can draw with no further lookups (machine boxes, the hatches
and buses built into each one's casing, what a boundary storage holds, routes as the blocks they
are built from - each cell with the sides that connect, its gauge and GT's real cross-section
(``route_blocks``) - plus the resource each route carries at what rate (by its raw id, and by a
``label`` that puts the plan's own display name in front of it, #296), the raw
segments and terminals behind them, auto-output links, how each single block's outputs leave it
(``output_faces``: the one face it auto-outputs through and the faces that need a cover), the
region, a legend, and the ``io`` boundary summary - inputs to load, outputs to collect, summed
power, each flagged ``me`` when its commodity rides ME, since nothing is drawn for it). Every
surface that names resources also lists them one by one as ``resources``, so the viewer can put a
picture beside each: ``icons`` (filled by ``write_preview`` from a local icon index, #297) and the
plan's own ``resourceColors`` where there is none. This
is a *previewer-internal* format - NOT the versioned contract - so the un-testable
WebGL last mile stays a thin static template while the mapping here is pure and fully tested.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from gtnh_solver.dataset import tier_voltage
from gtnh_solver.hatch_locks import LOCK_SLOT, hatch_layers, hatch_locks
from gtnh_solver.ir import (
    CellBox,
    Commodity,
    Facing,
    InputIR,
    IODirection,
    LayoutResult,
    Machine,
    METoggles,
    Route,
)
from gtnh_solver.ir.geometry import Cell, rotated_footprint
from gtnh_solver.output_faces import BlockOutputs, output_faces
from gtnh_solver.route_blocks import route_cells
from gtnh_solver.system_io import (
    RATE_STEM,
    is_boundary_storage,
    net_label,
    net_resource,
    port_resource,
    resource_label,
    system_io,
)

#: Bump if the scene shape the viewer template expects changes.
SCENE_VERSION = 1

#: Distinct, readable-on-dark machine box colours, assigned per machine type (sorted, so the
#: same line always colours the same way).
_MACHINE_PALETTE = (
    "#6ca0dc",
    "#e07a5f",
    "#81b29a",
    "#f2cc8f",
    "#c5a3ff",
    "#9bc1bc",
    "#d4a373",
    "#a3b18a",
    "#e29578",
    "#bc6c25",
)


#: three.js ``BoxGeometry`` takes its six materials in this face order. A route box's open ends are
#: emitted as a matching six-slot list, so the viewer needs no normal lookup of its own - the same
#: split as ``textures._GT_SIDE_TO_THREE_SLOT``, which keeps renderer detail out of ``route_blocks``
#: (a pure derivation over the contract, which has no idea what three.js is).
_THREE_SLOT_NORMALS: tuple[Cell, ...] = (
    (1, 0, 0),
    (-1, 0, 0),
    (0, 1, 0),
    (0, -1, 0),
    (0, 0, 1),
    (0, 0, -1),
)

#: What :func:`block_face_cover` says about one face of a block cube. EXPOSED is always drawn.
#: COVERED sits against a block on its own layer, so nothing can ever see it. CAP is a top or bottom
#: face against a block on the next layer: hidden while every layer shows, but the only lid that
#: layer has once the slider isolates it, so the viewer draws it then and only then.
FACE_EXPOSED, FACE_COVERED, FACE_CAP = 0, 1, 2


def block_face_cover(cells: Iterable[Cell]) -> dict[Cell, tuple[int, ...]]:
    """For each block cell, what covers each of its six faces, in three.js ``BoxGeometry`` order.

    The viewer draws every block as a full, opaque unit cube, so a face flush against another block
    is invisible whatever the block is in game. On the nitrobenzene lines that is two faces in
    three, and the viewer used to draw each of them as a draw call of its own, which is what made a
    large preview lag. Only a block covers a face: a pipe is thinner than a block and a placeholder
    machine box is inset from its cell, so a face beside either still shows.

    A face is judged against the blocks alone, because the layer slider is the one thing that hides
    blocks, and it hides whole layers. So a face covered on its own layer stays covered in every view,
    and only a top or bottom face can be uncovered by it (``FACE_CAP``).
    """
    occupied = set(cells)
    cover: dict[Cell, tuple[int, ...]] = {}
    for x, y, z in occupied:
        slots = []
        for dx, dy, dz in _THREE_SLOT_NORMALS:
            if (x + dx, y + dy, z + dz) not in occupied:
                slots.append(FACE_EXPOSED)
            else:
                slots.append(FACE_CAP if dy else FACE_COVERED)
        cover[(x, y, z)] = tuple(slots)
    return cover


#: Route colours by commodity. The single source: routes carry their colour, and the scene's
#: ``routeLegend`` (below) carries the legend swatches, so the viewer no longer hard-codes a
def _reserved_size(footprint: CellBox, orientation: Facing) -> list[int]:
    """The reserved box a machine occupies as placed, ``[sx, sy, sz]`` after its yaw."""
    box = rotated_footprint(footprint, orientation)
    return [box.sx, box.sy, box.sz]


#: second copy of these hex values on the JS side.
_COMMODITY_COLOR = {
    Commodity.ITEM: "#3cb44b",
    Commodity.FLUID: "#4363d8",
    Commodity.POWER: "#ffd000",
}

#: How a boundary storage's port direction reads to the BUILDER, in the ``io`` panel's own two
#: words. It is the INVERSE of the port's own direction, which is stated from the machine's side: a
#: storage whose port OUTPUTS into the line is one the builder keeps stocked (an ``in``), and one
#: the line feeds is where a product collects (an ``out``). ``system_io`` splits the same two cases
#: the same way (``only_sources`` -> inputs, ``only_sinks`` -> outputs), so the hover tag and the
#: panel say "in: water" about the same tank.
_STORAGE_FLOW = {IODirection.OUTPUT: "in", IODirection.INPUT: "out"}

#: A hatch's flow is its own direction, stated from the machine's side as a builder reads it: an
#: input hatch is where the line feeds the machine ("in"), an output hatch where it leaves ("out").
_HATCH_FLOW = {IODirection.INPUT: "in", IODirection.OUTPUT: "out"}


def _hatch_label(kind: str) -> str:
    """A ``HatchElement`` kind as a builder names the block: ``OutputHatch`` -> "Output Hatch".

    GT's kinds drop the word on the plain hatches (``Energy``, ``Maintenance``, ``Muffler``), so it
    is put back: "Energy Hatch", as the block is called in game.
    """
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", kind)
    return words if words.endswith(("Hatch", "Bus")) else f"{words} Hatch"


def build_scene(
    problem: InputIR, layout: LayoutResult, *, extra_names: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Flatten ``problem`` + ``layout`` into the self-contained scene dict the viewer renders.

    ``extra_names`` names resources the plan does not (an icon index's, ``previewer.icons``): the
    plan's own name wins, then one of these, then the bare id. ``icons`` is left empty for
    ``write_preview`` to fill, which keeps this a pure function of its arguments.
    """
    machines = {m.id: m for m in problem.machines}
    names = {**(extra_names or {}), **problem.resource_names}
    types = sorted({m.type for m in problem.machines})
    color_for_type = {t: _MACHINE_PALETTE[i % len(_MACHINE_PALETTE)] for i, t in enumerate(types)}

    ports = {(m.id, p.id): p for m in problem.machines for p in m.faces.ports}
    locks = hatch_locks(problem, layout)
    layers = hatch_layers(problem, layout)
    hatches_by_machine: dict[str, list[dict[str, Any]]] = {}
    for hatch in layout.hatches:
        port = ports.get((hatch.machine_id, hatch.port_id)) if hatch.port_id else None
        # The port of a hatch that moves a fluid or item; an energy hatch's carries no resource.
        moved = port if port is not None and port.commodity is not Commodity.POWER else None
        lock = locks.get((hatch.machine_id, hatch.cell.as_tuple()))
        hatches_by_machine.setdefault(hatch.machine_id, []).append(
            {
                "cell": [hatch.cell.x, hatch.cell.y, hatch.cell.z],
                "kind": hatch.kind,
                "facing": hatch.facing.value,
                "port": hatch.port_id,
                # What the hover says about it (#120): its name, which way what it moves goes
                # (the hatch's own direction, unlike a storage's), the product it must be locked
                # to (None when it needs no lock) and the slot GT sets that in. ``layer`` is the
                # output layer a tower fills this hatch from (GT's index, from 0; None off a
                # tower's layers), and ``spare`` marks a tower's spare output hatch, which stands
                # on a layer no product uses so the tower forms (#299). ``resourceLabel`` and
                # ``lockLabel`` are the two resources as the hover prints them, the plan's name in
                # front of the id (#296); ``label`` is the hatch's own name.
                "label": _hatch_label(hatch.kind),
                "flow": _HATCH_FLOW[moved.direction] if moved is not None else None,
                "resource": port_resource(moved) if moved is not None else None,
                "resourceLabel": (
                    resource_label(port_resource(moved), names) if moved is not None else None
                ),
                "lock": lock,
                "lockLabel": resource_label(lock, names) if lock is not None else None,
                "lockSlot": LOCK_SLOT[hatch.kind] if lock is not None else None,
                "layer": layers.get((hatch.machine_id, hatch.cell.as_tuple())),
                "spare": hatch.kind == "OutputHatch" and hatch.port_id is None,
            }
        )

    outputs = output_faces(problem, layout)
    scene_machines = [
        {
            "id": pl.machine_id,
            "type": machines[pl.machine_id].type,
            # The controller block ("<registry>@<meta>") the adapter resolved this machine to, the
            # record its footprint was reserved from: the exact key the texture pass (and so the
            # .schematic export) joins to the structure dump on. `type` is the exporter's
            # recipe-map name, which is not the dump's controller name and can even be another
            # machine's ("Distillation Tower" for a Dangote Distillus, #205).
            "block_key": machines[pl.machine_id].block_key,
            "cell": [pl.cell.x, pl.cell.y, pl.cell.z],
            # The reserved box AS PLACED: a quarter turn swaps the horizontal extents, and this
            # size is what the texture pass clamps a machine's blocks against, so an unrotated one
            # silently deletes the cubes that stick out.
            "size": _reserved_size(machines[pl.machine_id].footprint, pl.orientation),
            "front": pl.orientation.value,
            # The machine's voltage tier (LV/MV/HV/...), carried so the texture pass can resolve a
            # generically named single-block machine to its GT tier-prefixed manifest entry
            # (e.g. "Forge Hammer" at LV -> "Basic Forge Hammer").
            "voltage_tier": machines[pl.machine_id].voltage_tier,
            # The GT recipe map it runs ("gt.recipe.orewasher"), which with the tier names a
            # single-block machine exactly where `type` ("Ore Washer") does not (#232).
            "recipe_map": machines[pl.machine_id].recipe_map,
            # The block each tiered part is built from, channel -> [block, meta] (#312): the texture
            # pass, and so the .schematic export, swaps that channel's cells for it.
            "structure_blocks": {
                channel: [chosen.block, chosen.meta]
                for channel, chosen in sorted(machines[pl.machine_id].structure_blocks.items())
            },
            "role": _role(machines[pl.machine_id]),
            # What a boundary storage holds, so a hover can tell four identical Super Tanks apart
            # (GitHub #155). Empty for every other machine - a machine's ports are its recipe, not
            # its contents. Resource ids verbatim, exactly as the plan carries them, each with the
            # ``label`` a person reads (``system_io.resource_label``, #296).
            "contents": _contents(machines[pl.machine_id], problem.me_toggles, names),
            # The items an Item Filter lets through (#249), which is how its slots must be set in
            # game; the hover lists them by ``filter_labels``. Empty for every other machine.
            "filter_items": list(machines[pl.machine_id].filter_items),
            "filter_labels": [
                resource_label(item, names) for item in machines[pl.machine_id].filter_items
            ],
            # How this single block's outputs leave it (``output_faces``): the one face it
            # auto-outputs through, which the viewer marks with the arrow whether it ejects into a
            # neighbour or into a pipe, and every other output face, which takes a cover and gets a
            # cover marker. None for a multiblock and for a block with no output on a face.
            "outputs": _outputs_entry(outputs.get(pl.machine_id)),
            "color": color_for_type[machines[pl.machine_id].type],
            # The hatches and buses built into this machine's casing, each at the CELL it replaces
            # and facing the way it works. The texture pass swaps them in for the casing cubes
            # underneath, so a bus renders as a bus rather than as the block it displaced.
            "hatches": hatches_by_machine.get(pl.machine_id, []),
        }
        for pl in layout.placements
        if pl.machine_id in machines
    ]

    nets = {n.id: n for n in problem.nets}
    scene_routes = []
    for route in layout.routes:
        net = nets.get(route.net_id)
        tps = route.thickness_per_segment
        segments = [
            {
                "from": [seg.start.x, seg.start.y, seg.start.z],
                "to": [seg.end.x, seg.end.y, seg.end.z],
                "thickness": tps[i] if tps is not None and i < len(tps) else None,
            }
            for i, seg in enumerate(route.segments)
        ]
        terminals = [
            {
                "machine": t.machine_id,
                # Which port this terminal serves, so a viewer can tie it to the hatch it docks
                # against (``hatch.cell == terminal.cell - FACE_DELTAS[face]``) instead of guessing
                # from geometry when a machine has several terminals on one face.
                "port": t.port_id,
                "face": t.face.value,
                "cell": [t.cell.x, t.cell.y, t.cell.z],
            }
            for t in route.terminals
        ]
        scene_routes.append(
            {
                "netId": route.net_id,
                "commodity": route.commodity.value,
                # What this pipe or cable actually moves, so hovering one answers it (GitHub #155):
                # the net's resource (``None`` on power, which names no fluid or item; a merged
                # item run's several, comma separated) and its typed throughput, with ``unit`` the
                # stem the viewer suffixes /t or /s onto - the same shape ``io`` below uses. The
                # resource id is verbatim, exactly as the plan carries it, and ``label`` is what
                # the viewer shows: the id with the plan's own display name in front where it
                # has one, "Toluene (liquid_toluene)" (#296). Never a name authored here: mapping
                # ``gregtech:gt.metaitem.01@2032`` to one would mean a table from memory (the
                # reason ``route_blocks`` keeps GT's unlocalized spellings), and an id a builder
                # can search NEI for beats a guessed name, which is why the id stays in the label.
                "resource": net_resource(net) if net is not None else None,
                "label": net_label(net, names) if net is not None else None,
                # The same, one entry per resource, for the picture beside each (#297).
                "resources": _resource_entries(net.resources if net is not None else (), names),
                "rate": net.throughput if net is not None else None,
                "unit": RATE_STEM[route.commodity],
                "color": _COMMODITY_COLOR[route.commodity],
                "segments": segments,
                "terminals": terminals,
                # The blocks this route is built from, one per cell - the shape the viewer draws.
                # Derived in ``route_blocks`` rather than in the template's JavaScript, which is
                # where it used to live: the max-thickness rule in it is a build instruction
                # (docs/DOMAIN.md), and a bill of materials has to agree with it block for block.
                "cells": [
                    {
                        "cell": list(rc.cell),
                        "dirs": [list(d) for d in sorted(rc.dirs)],
                        "thickness": rc.thickness,
                        # GT's own cross-section in blocks, so a 1x cable is the size it is in
                        # game rather than a bar scaled to look right.
                        "size": rc.thickness_blocks,
                        # The manifest join key the texture pass resolves, and the label a build
                        # guide prints; ``None`` when the route published no material.
                        "block": rc.block.dataset_name,
                        "label": rc.block.label,
                        # The cell's GT shape: a core cube plus an arm per connection, or one box
                        # for a straight run, none of them overlapping. Built in ``route_blocks``
                        # because a shape assembled in the template is a shape no test can check -
                        # and the one that was there grew its arms from the cell centre, so every
                        # arm swallowed half the core and their faces tore against each other.
                        "boxes": [
                            {
                                "center": list(b.center),
                                "size": list(b.size),
                                "open": [n in b.open_faces for n in _THREE_SLOT_NORMALS],
                            }
                            for b in rc.boxes
                        ],
                    }
                    for rc in route_cells(route)
                ],
                "material": _scene_material(route),
            }
        )

    scene_autos = [
        {
            "netId": ac.net_id,
            "source": ac.source_machine_id,
            "target": ac.target_machine_id,
            "sourceFace": ac.source_face.value,
            "targetFace": ac.target_face.value,
        }
        for ac in layout.auto_connections
    ]

    sysio = system_io(problem, layout)
    # Per-tier power feed spec: the FULL tier voltage (32 V for LV, always the whole tier, never a
    # machine's sub-tier draw) and the amps to supply. That is how a GT power feed is specified -
    # N amps at the tier voltage - so the builder reads it straight off ("LV 32V x 3A"). ``total``
    # is the EU/t that feed delivers (sum of tier voltage x amps), so it matches the breakdown
    # (32 V x 3 A -> 96 EU/t), not the machines' lower actual draw (``sysio.power_total``).
    power_by_tier = {
        tier: {"volts": tier_voltage(tier), "amps": amps}
        for tier, amps in sysio.power_amps_by_tier.items()
    }
    me = problem.me_toggles
    scene_io = {
        # ``rate`` is per-tick; ``unit`` is the stem (items/mB/EU) so the viewer can append /t or
        # /s for its toggle. ``me`` says the commodity rides ME (``--me``): the solver routes
        # nothing for it and no ME block is drawn yet, so the panel has to say how the flow gets
        # there, or a chest with no pipe reads as a line that forgot one. ``label`` is the resource
        # as the panel prints it (``system_io.resource_label``, #296).
        "inputs": [
            {
                "resource": f.resource,
                "label": ", ".join(resource_label(r, names) for r in f.resources),
                "resources": _resource_entries(f.resources, names),
                "rate": f.rate,
                "unit": RATE_STEM[f.commodity],
                "me": me.toggled(f.commodity),
            }
            for f in sysio.inputs
        ],
        "outputs": [
            {
                "resource": f.resource,
                "label": ", ".join(resource_label(r, names) for r in f.resources),
                "resources": _resource_entries(f.resources, names),
                "rate": f.rate,
                "unit": RATE_STEM[f.commodity],
                "me": me.toggled(f.commodity),
            }
            for f in sysio.outputs
        ],
        "power": {
            "total": sum(d["volts"] * d["amps"] for d in power_by_tier.values()),
            "byTier": power_by_tier,
            "me": me.toggled(Commodity.POWER),
        },
    }

    region = problem.bounding_region
    metrics = layout.metrics
    return {
        "version": SCENE_VERSION,
        "status": layout.status.value,
        "seed": layout.seed,
        "region": {"sx": region.sx, "sy": region.sy, "sz": region.sz},
        "bounds": _content_bounds(problem, layout, machines),
        "machines": scene_machines,
        "routes": scene_routes,
        "autoConnections": scene_autos,
        "io": scene_io,
        "legend": [{"label": t, "color": color_for_type[t]} for t in types],
        # The route-commodity legend swatches, so the viewer reads the colours from here instead of
        # keeping a second hard-coded copy (one source: ``_COMMODITY_COLOR``).
        "routeLegend": [
            {"commodity": commodity.value, "color": color}
            for commodity, color in _COMMODITY_COLOR.items()
        ],
        # A picture per resource (#297): the icon ``write_preview`` embeds from a local icon index,
        # else a dot in the plan's own colour. Both keyed by the raw id, like ``resources`` above.
        "icons": {},
        "resourceColors": dict(problem.resource_colors),
        "metrics": {
            "footprint": metrics.footprint,
            "layers": metrics.layers,
            "congestion": metrics.congestion,
            "buildability": metrics.buildability,
        },
    }


def _resource_entries(resources: Iterable[str], names: Mapping[str, str]) -> list[dict[str, str]]:
    """Each of ``resources`` as ``{"id", "label"}``, in order: what a surface names one by one, so
    the viewer can draw each one's picture and label without splitting a joined string (a fluid id
    can hold a comma itself, ``1,3dimethylbenzene``)."""
    return [{"id": r, "label": resource_label(r, names)} for r in resources]


def _scene_material(route: Route) -> dict[str, Any] | None:
    """The cable or pipe material this route is drawn as, for the legend's stand-in footnote.

    Route-level, unlike ``cells[].block``, because the *identity* is one per route while the block
    changes with the gauge. ``standIn`` rides along because a preview that draws Tin without saying
    the material was chosen for recognisability reads as a specification (docs/DOMAIN.md).
    """
    if route.material is None:
        return None
    return {
        "family": route.material.family.value,
        "material": route.material.material,
        "tier": route.material.tier,
        "standIn": route.material.stand_in,
    }


def _content_bounds(
    problem: InputIR, layout: LayoutResult, machines: dict[str, Machine]
) -> dict[str, list[int]]:
    """The tight axis-aligned extent the layout actually occupies (machine bodies + route cells).

    The solver's ``bounding_region`` is deliberately oversized scratch space; the previewer frames
    on what is *built*, so the build area shown matches the structure, not the search box. Falls
    back to the full region when nothing is placed or routed.
    """
    lo: list[int | None] = [None, None, None]
    hi: list[int | None] = [None, None, None]

    def grow(corner_min: list[int], corner_max: list[int]) -> None:
        for i in range(3):
            cur_lo, cur_hi = lo[i], hi[i]
            lo[i] = corner_min[i] if cur_lo is None else min(cur_lo, corner_min[i])
            hi[i] = corner_max[i] if cur_hi is None else max(cur_hi, corner_max[i])

    for pl in layout.placements:
        m = machines.get(pl.machine_id)
        if m is None:
            continue
        cell = [pl.cell.x, pl.cell.y, pl.cell.z]
        size = _reserved_size(m.footprint, pl.orientation)
        grow(cell, [cell[i] + size[i] for i in range(3)])
    for route in layout.routes:
        for x, y, z in route.cells():  # a one-block pipe has a block and no segment
            grow([x, y, z], [x + 1, y + 1, z + 1])

    if lo[0] is None:  # nothing placed or routed - frame the whole region instead
        region = problem.bounding_region
        return {"min": [0, 0, 0], "max": [region.sx, region.sy, region.sz]}
    return {"min": [v for v in lo if v is not None], "max": [v for v in hi if v is not None]}


def _contents(machine: Machine, me: METoggles, names: Mapping[str, str]) -> list[dict[str, Any]]:
    """What a boundary storage holds: each resource its ports carry (with the ``label`` the hover
    prints, from ``names``), which way it flows, and ``me`` when that commodity rides ME (the io
    panel's flag, so the hover says what the panel says).

    A Super Chest/Tank is a buffer for one resource, and the port it exposes is the only record of
    which - ``adapter.core`` encodes it into the port id (``"input:liquid_toluene"``) and
    ``system_io.port_resource`` is the inverse, so this reads the same encoding the other surfaces
    do rather than re-deriving it. Power ports are skipped: a storage's power connection is not its
    contents. Empty for every other machine, whose ports state a recipe rather than a stock.

    ``flow`` is what makes two same-resource buffers tell apart: the nitrobenzene line has a Super
    Tank the builder fills with water AND one the line fills with water, and "water" alone says the
    same thing about both.
    """
    if not is_boundary_storage(machine.type):
        return []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for port in machine.faces.ports:
        if port.commodity is Commodity.POWER:  # a hatch, not something the buffer holds
            continue
        resource, flow = port_resource(port), _STORAGE_FLOW[port.direction]
        entry = {
            "resource": resource,
            "label": resource_label(resource, names),
            "flow": flow,
            "me": me.toggled(port.commodity),
        }
        seen.setdefault((resource, flow), entry)
    return list(seen.values())


def _outputs_entry(block: BlockOutputs | None) -> dict[str, Any] | None:
    """A scene machine's ``outputs``: its auto face, what leaves through it, and its cover faces."""
    if block is None:
        return None
    return {
        "autoFace": block.auto_face.value if block.auto_face is not None else None,
        "autoItems": block.auto_items,
        "autoFluids": block.auto_fluids,
        "covers": [
            {"face": c.face.value, "cover": c.cover, "nets": list(c.net_ids)} for c in block.covers
        ],
        # Its output face shares a pipe with another machine's outputs on a pack where it takes
        # input there, so the builder must set "Input from Output Side forbidden" (#278).
        "forbidInput": block.forbid_input_from_output,
    }


def _role(machine: Machine) -> str:
    """Coarse render role: a power source, a boundary storage, an Item Filter, or a plain machine.
    Reuses the shared predicates (``Machine.is_power_source``, ``system_io.is_boundary_storage``)
    so the role stays in step with the boundary summary instead of re-deriving them here. A filter
    is known by what it lets through (``Machine.filter_items``), never by its type string."""
    if machine.is_power_source:
        return "source"
    if is_boundary_storage(machine.type):  # Super Chest / Super Tank boundary blocks
        return "storage"
    if machine.filter_items:  # an Item Filter the adapter placed to sort a merged run (#249)
        return "filter"
    return "machine"
