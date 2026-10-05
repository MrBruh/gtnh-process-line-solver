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
power, each flagged ``me`` when it arrives over or leaves through an ME network rather than a
chest). ``me`` holds every ME network the layout builds (#338): per network its mode, colour,
channels and what its storage supplies and takes in (``system_io``), and per cable block and
controller or acceptor the boxes AE2 draws it as (``me_blocks``), each face naming the AE2 icon it
wears and each light pass its mask and tint; the texture pass embeds those (``me_textures``). Every
surface that names resources also lists them one by one as ``resources``, so the viewer can put a
picture beside each: ``icons`` (filled by ``write_preview`` from a local icon index, #297) and the
plan's own ``resourceColors`` where there is none. This
is a *previewer-internal* format - NOT the versioned contract - so the un-testable
WebGL last mile stays a thin static template while the mapping here is pure and fully tested.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from functools import cache
from typing import Any

from gtnh_solver.dataset import ADHOC_MAX_DEVICES, GT_ME_HATCHES, MEDeviceChoice, tier_voltage
from gtnh_solver.dataset.ae_render import AE_RENDER_PATH, AERender, load_ae_render
from gtnh_solver.dataset.me import CABLE_NAMES
from gtnh_solver.hatch_locks import LOCK_SLOT, hatch_layers, hatch_locks
from gtnh_solver.ir import (
    AEColor,
    CellBox,
    Commodity,
    Facing,
    InputIR,
    IODirection,
    LayoutResult,
    Machine,
    MEMode,
    MERole,
    Route,
)
from gtnh_solver.ir.geometry import Cell, rotated_footprint
from gtnh_solver.output_faces import BlockOutputs, output_faces
from gtnh_solver.route_blocks import route_cells
from gtnh_solver.system_io import (
    RATE_STEM,
    SystemIO,
    is_boundary_storage,
    net_label,
    net_resource,
    port_resource,
    resource_label,
    system_io,
)

from .me_blocks import RGB, SIDE_ORDER, MEBox, MEPart, me_blocks, plain_blocks

_log = logging.getLogger(__name__)

#: Bump if the scene shape the viewer template expects changes. 2: ``me`` (the ME networks a
#: layout builds, #338), each machine's ``meRole``, each hatch's ``gtMid``, and each I/O row's
#: ``network``. Every field version 1 had is still there.
SCENE_VERSION = 2

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
    # The ports whose net rides ME (#332), which the io panel and a storage's hover flag.
    me_ports = {
        (ep.machine_id, ep.port_id)
        for net in problem.nets
        if problem.rides_me(net)
        for ep in net.endpoints
    }
    types = sorted({m.type for m in problem.machines})
    # The types only ME infrastructure has, which the legend's machine list leaves out (#338).
    me_only = {m.type for m in problem.machines if m.me_role is not None} - {
        m.type for m in problem.machines if m.me_role is None
    }
    color_for_type = {t: _MACHINE_PALETTE[i % len(_MACHINE_PALETTE)] for i, t in enumerate(types)}

    ports = {(m.id, p.id): p for m in problem.machines for p in m.faces.ports}
    locks = hatch_locks(problem, layout)
    layers = hatch_layers(problem, layout)
    # GT's own ME hatches by the casing cell they take (#338): such a hatch is listed in
    # ``layout.hatches`` by the slot kind it fills, and only its device says which ME hatch it is.
    me_hatches = {
        (device.machine_id, device.cell.as_tuple()): device.gt_mid
        for network in layout.me_networks
        for device in network.devices
        if device.gt_mid is not None
    }
    hatches_by_machine: dict[str, list[dict[str, Any]]] = {}
    for hatch in layout.hatches:
        port = ports.get((hatch.machine_id, hatch.port_id)) if hatch.port_id else None
        # The port of a hatch that moves a fluid or item; an energy hatch's carries no resource.
        moved = port if port is not None and port.commodity is not Commodity.POWER else None
        lock = locks.get((hatch.machine_id, hatch.cell.as_tuple()))
        gt_mid = me_hatches.get((hatch.machine_id, hatch.cell.as_tuple()))
        hatches_by_machine.setdefault(hatch.machine_id, []).append(
            {
                "cell": [hatch.cell.x, hatch.cell.y, hatch.cell.z],
                "kind": hatch.kind,
                "facing": hatch.facing.value,
                "port": hatch.port_id,
                # The mID of GT's ME hatch built here, None for a normal hatch. The texture pass
                # draws an ME hatch by it (``TextureManifest.me_hatch``), never by its kind.
                "gtMid": gt_mid,
                # What the hover says about it (#120): its name, which way what it moves goes
                # (the hatch's own direction, unlike a storage's), the product it must be locked
                # to (None when it needs no lock) and the slot GT sets that in. ``layer`` is the
                # output layer a tower fills this hatch from (GT's index, from 0; None off a
                # tower's layers), and ``spare`` marks a tower's spare output hatch, which stands
                # on a layer no product uses so the tower forms (#299). ``resourceLabel`` and
                # ``lockLabel`` are the two resources as the hover prints them, the plan's name in
                # front of the id (#296); ``label`` is the hatch's own name, an ME hatch's GT's.
                "label": (
                    GT_ME_HATCHES[gt_mid].name
                    if gt_mid is not None and gt_mid in GT_ME_HATCHES
                    else _hatch_label(hatch.kind)
                ),
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
            # The ME infrastructure block this machine is (an attach stub, a link, a controller,
            # an acceptor), or None. The ME layer draws it (``me``), so the viewer gives it no
            # placeholder box and the texture pass looks for no GT block for it (#338).
            "meRole": _me_role(machines[pl.machine_id]),
            # What a boundary storage holds, so a hover can tell four identical Super Tanks apart
            # (GitHub #155). Empty for every other machine - a machine's ports are its recipe, not
            # its contents. Resource ids verbatim, exactly as the plan carries them, each with the
            # ``label`` a person reads (``system_io.resource_label``, #296).
            "contents": _contents(machines[pl.machine_id], me_ports, names),
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
    me_storages = {machine_id for machine_id, _ in me_ports}
    scene_io = {
        # ``rate`` is per-tick; ``unit`` is the stem (items/mB/EU) so the viewer can append /t or
        # /s for its toggle. ``me`` says the flow rides ME (#332): a chest an ME network's storage
        # bus reads, or what an ME network's storage supplies or takes in (#335), which has no
        # chest in the build at all, so it is listed here too, always ``me``, with the ``network``
        # it rides. Without the row a line fed over ME would read as one with no input. ``label``
        # is the resource as the panel prints it (``system_io.resource_label``, #296).
        "inputs": [
            *(
                _flow_entry(
                    f.resource, f.resources, f.commodity, f.rate, f.machine_id in me_storages, names
                )
                for f in sysio.inputs
            ),
            *(
                _flow_entry(
                    f.resource, f.resources, f.commodity, f.rate, True, names, network.network
                )
                for network in sysio.me
                for f in network.supplies
            ),
        ],
        "outputs": [
            *(
                _flow_entry(
                    f.resource, f.resources, f.commodity, f.rate, f.machine_id in me_storages, names
                )
                for f in sysio.outputs
            ),
            *(
                _flow_entry(
                    f.resource, f.resources, f.commodity, f.rate, True, names, network.network
                )
                for network in sysio.me
                for f in network.absorbs
            ),
        ],
        "power": {
            "total": sum(d["volts"] * d["amps"] for d in power_by_tier.values()),
            "byTier": power_by_tier,
            "me": problem.me.power_external,
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
        # Every ME network the layout builds, drawn block by block (#338); None without one.
        "me": _me_scene(problem, layout, sysio, names),
        # The machine types, bar the ME infrastructure (stubs, links, controllers, acceptors),
        # which the ME layer draws and the legend's ME section accounts for (#338).
        "legend": [{"label": t, "color": color_for_type[t]} for t in types if t not in me_only],
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
        # The credit the art of AE2 and AE2FluidCraft asks for (``me_textures.credit``), set by
        # ``write_preview`` once the page embeds any of it (#338); None until then.
        "credit": None,
        "metrics": {
            "footprint": metrics.footprint,
            "layers": metrics.layers,
            "congestion": metrics.congestion,
            "buildability": metrics.buildability,
        },
    }


def _flow_entry(
    resource: str,
    resources: tuple[str, ...],
    commodity: Commodity,
    rate: float | None,
    me: bool,
    names: Mapping[str, str],
    network: str | None = None,
) -> dict[str, Any]:
    """One row of the scene's I/O panel (``scene_io`` in :func:`build_scene`), and of an ME
    network's supplies and intake (``me.networks``). ``network`` names the ME network whose storage
    it comes from or goes to, where it is that network's rather than a chest's."""
    return {
        "resource": resource,
        "label": ", ".join(resource_label(r, names) for r in resources),
        "resources": _resource_entries(resources, names),
        "rate": rate,
        "unit": RATE_STEM[commodity],
        "me": me,
        "network": network,
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
    """The tight axis-aligned extent the layout actually occupies (machine bodies, route cells and
    ME cables).

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
    for network in layout.me_networks:  # an ME cable is as much the build as a pipe (#338)
        for x, y, z in network.cells():
            grow([x, y, z], [x + 1, y + 1, z + 1])

    if lo[0] is None:  # nothing placed or routed - frame the whole region instead
        region = problem.bounding_region
        return {"min": [0, 0, 0], "max": [region.sx, region.sy, region.sz]}
    return {"min": [v for v in lo if v is not None], "max": [v for v in hi if v is not None]}


def _contents(
    machine: Machine, me_ports: Collection[tuple[str, str]], names: Mapping[str, str]
) -> list[dict[str, Any]]:
    """What a boundary storage holds: each resource its ports carry (with the ``label`` the hover
    prints, from ``names``), which way it flows, and ``me`` when the port's net rides ME
    (``me_ports``; the io panel's flag, so the hover says what the panel says).

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
            "me": (machine.id, port.id) in me_ports,
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


def _me_role(machine: Machine) -> str | None:
    """The ME infrastructure role of ``machine`` (``Machine.me_role``), by name, or ``None``."""
    return machine.me_role.value if machine.me_role is not None else None


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


# --- the ME networks (#338) -------------------------------------------------------------------------


@cache
def _ae_render() -> AERender:
    """The committed AE2 render data, read and validated once a process."""
    return load_ae_render()


#: The sides in three.js ``BoxGeometry`` face order (``_THREE_SLOT_NORMALS``), and where each sits
#: in ``me_blocks``' own side order, so a box's faces land on the right slots.
_THREE_SLOT_SIDES = (Facing.EAST, Facing.WEST, Facing.UP, Facing.DOWN, Facing.SOUTH, Facing.NORTH)
_SLOT_OF = tuple(SIDE_ORDER.index(side) for side in _THREE_SLOT_SIDES)

#: The colour a part (or an acceptor) is painted where its icon did not arrive: a neutral grey, so
#: a part reads as a part rather than as more of the cable it sits on.
_ME_PART_COLOR = "#9aa0a8"

#: How a builder names each block an ME network needs of its own.
_ROLE_NAMES = {
    MERole.ATTACH: "attach stub",
    MERole.LINK: "link",
    MERole.CONTROLLER: "ME Controller",
    MERole.ACCEPTOR: "Energy Acceptor",
}


def _hex(rgb: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _swatch(render: AERender | None, colour: AEColor) -> str:
    """An AE colour as AE shows it plainly (its ``medium_variant``), or the part grey without the
    render data to say."""
    return _hex(render.colours[colour].medium_variant) if render is not None else _ME_PART_COLOR


def _colour_name(colour: AEColor) -> str:
    """An AE colour as a person names it: ``light_blue`` -> ``Light Blue``, Fluix as Fluix."""
    return colour.value.replace("_", " ").title()


def _me_scene(
    problem: InputIR, layout: LayoutResult, sysio: SystemIO, names: Mapping[str, str]
) -> dict[str, Any] | None:
    """The scene's ``me``: each ME network, its cable blocks and its controller and acceptor
    blocks, as ``me_blocks`` derives them; ``None`` for a layout with no ME network.

    ``lights`` is left for ``write_preview`` to fill with the light masks the texture pass embeds,
    which keeps this a pure function of its arguments and the committed render data.

    **Render data that will not load costs only the ME look.** The schematic export builds this
    scene too and needs nothing of AE2's, so a missing or stale ``render.json`` is a warning naming
    the file, and every ME block is drawn as a plain cube in a flat colour
    (``me_blocks.plain_blocks``), hover and legend intact.
    """
    if not layout.me_networks:
        return None
    render: AERender | None
    try:
        render = _ae_render()
    except (OSError, ValueError) as exc:  # missing, not JSON, another schema, or invalid
        _log.warning(
            "ME render data at %s cannot be read, so the ME blocks are drawn as plain cubes: %s; "
            "re-run tools/derive_ae_render.py",
            AE_RENDER_PATH,
            exc,
        )
        render = None
    blocks = (
        me_blocks(problem, layout, render) if render is not None else plain_blocks(problem, layout)
    )
    machines = {m.id: m for m in problem.machines}
    return {
        "networks": _me_networks(problem, layout, sysio, names, render),
        "cells": [
            {
                "cell": list(cable.cell),
                "network": cable.network,
                "kind": cable.kind.value,
                "label": f"{CABLE_NAMES[cable.kind]} ({_colour_name(cable.colour)})",
                "channels": cable.channels,
                "capacity": cable.capacity,
                # A stub or a link is a cable block placed as a machine: its role and its machine.
                "role": cable.role.value if cable.role is not None else None,
                "roleLabel": _ROLE_NAMES[cable.role] if cable.role is not None else None,
                "machine": cable.machine_id,
                # Where the main network meets a stub, which the hover names.
                "outside": [c.side.value for c in cable.connections if c.to == "outside"],
                "parts": [_me_part(part, machines, names) for part in cable.parts],
                "boxes": [_me_box(box, cable.cell, render) for box in cable.boxes],
            }
            for cable in blocks.cables
        ],
        "blocks": [
            {
                "cell": list(device.cell),
                "network": device.network,
                "machine": device.machine_id,
                "role": device.role.value,
                "label": _ROLE_NAMES[device.role],
                "look": device.look,
                "boxes": [
                    _me_box(
                        box,
                        device.cell,
                        render,
                        color=(
                            _swatch(render, device.colour)
                            if device.role is MERole.CONTROLLER
                            else _ME_PART_COLOR
                        ),
                    )
                    for box in device.boxes
                ],
            }
            for device in blocks.devices
        ],
        "lights": {},
    }


def _me_networks(
    problem: InputIR,
    layout: LayoutResult,
    sysio: SystemIO,
    names: Mapping[str, str],
    render: AERender | None,
) -> list[dict[str, Any]]:
    """The legend's entry for each ME network: what it is, its channels, and what it asks of the
    player's storage (``system_io``'s ME section, so the legend and the CLI say the same)."""
    asks = {n.network: n for n in sysio.me}
    specs = {n.id: n for n in problem.me.networks}
    out: list[dict[str, Any]] = []
    for built in layout.me_networks:
        spec = specs.get(built.id)
        ask = asks.get(built.id)
        roles = Counter(
            m.me_role for m in problem.machines if m.me_network == built.id and m.me_role
        )
        subnet = spec is not None and spec.mode is MEMode.SUBNET
        out.append(
            {
                "id": built.id,
                "mode": spec.mode.value if spec is not None else None,
                "colour": built.colour.value,
                "colourName": _colour_name(built.colour),
                "swatch": _swatch(render, built.colour),
                "storage": spec.storage.value if spec is not None else None,
                "power": spec.power.value if spec is not None else None,
                # An attached network spends the main network's free channels, up to its budget;
                # a subnet hands out its own, from a controller, or ad hoc to at most 8 devices.
                "budget": (
                    spec.me_channel_budget
                    if spec is not None and spec.mode is MEMode.ATTACHED
                    else None
                ),
                "adhocLimit": (
                    ADHOC_MAX_DEVICES if subnet and not roles[MERole.CONTROLLER] else None
                ),
                "devices": ask.devices if ask is not None else len(built.devices),
                "mainChannels": ask.main_channels if ask is not None else 0,
                "cables": len(built.cables),
                "stubs": roles[MERole.ATTACH],
                "links": roles[MERole.LINK],
                "controllers": roles[MERole.CONTROLLER],
                "acceptors": roles[MERole.ACCEPTOR],
                "supplies": [
                    _flow_entry(f.resource, f.resources, f.commodity, f.rate, True, names, built.id)
                    for f in (ask.supplies if ask is not None else ())
                ],
                "absorbs": [
                    _flow_entry(f.resource, f.resources, f.commodity, f.rate, True, names, built.id)
                    for f in (ask.absorbs if ask is not None else ())
                ],
            }
        )
    return out


def _me_box(
    box: MEBox, cell: Cell, render: AERender | None, *, color: str | None = None
) -> dict[str, Any]:
    """One ME box as the viewer draws it: its centre and size in blocks, the icon on each face in
    three.js slot order (``None``: not drawn; ``""``: drawn with no icon), the colour it falls back
    to where an icon did not arrive, its light passes and the part of its cell it belongs to."""
    x0, y0, z0, x1, y1, z1 = box.box
    if color is None:
        color = _ME_PART_COLOR if box.part is not None else _swatch(render, box.colour)
    return {
        "center": [
            cell[0] + (x0 + x1) / 32,
            cell[1] + (y0 + y1) / 32,
            cell[2] + (z0 + z1) / 32,
        ],
        "size": [(x1 - x0) / 16, (y1 - y0) / 16, (z1 - z0) / 16],
        "faces": [box.faces[i] for i in _SLOT_OF],
        "color": color,
        "lights": [
            {
                "icon": light.icon,
                "tint": _hex(light.tint),
                "faces": [light.faces[i] for i in _SLOT_OF],
            }
            for light in box.lights
        ],
        "part": box.part,
    }


def _me_part(
    part: MEPart, machines: Mapping[str, Machine], names: Mapping[str, str]
) -> dict[str, Any]:
    """What a part's hover says: the device and its cards, the machine it serves, which way it
    moves what, and the resources: its config where it is set to some (a bus's filter, a storage
    bus's partition), else what the ports it serves carry."""
    device = part.device
    machine = machines.get(device.machine_id)
    endpoint = (
        next((e for e in machine.me_endpoints if e.id == device.endpoint_id), None)
        if machine is not None
        else None
    )
    ports = {p.id: p for p in machine.faces.ports} if machine is not None else {}
    served = [ports[p] for p in endpoint.ports if p in ports] if endpoint is not None else []
    flows = {_HATCH_FLOW[p.direction] for p in served if p.commodity is not Commodity.POWER}
    resources = list(device.config) or [
        port_resource(p) for p in served if p.commodity is not Commodity.POWER
    ]
    return {
        "side": part.side.value,
        "kind": device.kind.value,
        "label": MEDeviceChoice(kind=device.kind, cards=device.cards, gt_mid=device.gt_mid).label,
        "machine": device.machine_id,
        "machineType": machine.type if machine is not None else device.machine_id,
        "machineRole": (machine.me_role.value if machine is not None and machine.me_role else None),
        "endpoint": device.endpoint_id,
        "ports": [p.id for p in served],
        # "in": it feeds the machine; "out": it takes from it; None: neither (a link's storage bus).
        "flow": flows.pop() if len(flows) == 1 else None,
        "resources": _resource_entries(dict.fromkeys(resources), names),
    }
