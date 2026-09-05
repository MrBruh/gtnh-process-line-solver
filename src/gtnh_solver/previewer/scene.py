"""previewer.scene - denormalize a (problem, layout) pair into a self-contained render scene.

The output-layout contract (``LayoutResult``) references machines by id and leaves their
geometry in the ``InputIR``; a renderer needs it all in one place. ``build_scene`` flattens both
into a plain dict the three.js viewer can draw with no further lookups (machine boxes, the hatches
and buses built into each one's casing, routes as the blocks they are built from - each cell with
the sides that connect, its gauge and GT's real cross-section (``route_blocks``) - plus the raw
segments and terminals behind them, auto-output links, the region, a legend, and the ``io`` boundary
summary - inputs to load, outputs to collect, summed power). This
is a *previewer-internal* format - NOT the versioned contract - so the un-testable
WebGL last mile stays a thin static template while the mapping here is pure and fully tested.
"""

from __future__ import annotations

from typing import Any

from gtnh_solver.dataset import tier_voltage
from gtnh_solver.ir import (
    CellBox,
    Commodity,
    Facing,
    InputIR,
    LayoutResult,
    Machine,
    Route,
)
from gtnh_solver.ir.geometry import Cell, rotated_footprint
from gtnh_solver.route_blocks import route_cells
from gtnh_solver.system_io import RATE_STEM, is_boundary_storage, system_io

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
#: (shared with the build guide, which has no idea what three.js is).
_THREE_SLOT_NORMALS: tuple[Cell, ...] = (
    (1, 0, 0),
    (-1, 0, 0),
    (0, 1, 0),
    (0, -1, 0),
    (0, 0, 1),
    (0, 0, -1),
)


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


def build_scene(problem: InputIR, layout: LayoutResult) -> dict[str, Any]:
    """Flatten ``problem`` + ``layout`` into the self-contained scene dict the viewer renders."""
    machines = {m.id: m for m in problem.machines}
    types = sorted({m.type for m in problem.machines})
    color_for_type = {t: _MACHINE_PALETTE[i % len(_MACHINE_PALETTE)] for i, t in enumerate(types)}

    hatches_by_machine: dict[str, list[dict[str, Any]]] = {}
    for hatch in layout.hatches:
        hatches_by_machine.setdefault(hatch.machine_id, []).append(
            {
                "cell": [hatch.cell.x, hatch.cell.y, hatch.cell.z],
                "kind": hatch.kind,
                "facing": hatch.facing.value,
                "port": hatch.port_id,
            }
        )

    scene_machines = [
        {
            "id": pl.machine_id,
            "type": machines[pl.machine_id].type,
            # The controller block ("<registry>@<meta>") when the export carried one: the exact key
            # the texture pass joins to the structure dump on, since `type` is the exporter's
            # recipe-map name and the dump is keyed by the controller block's own name.
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
            "role": _role(machines[pl.machine_id]),
            "color": color_for_type[machines[pl.machine_id].type],
            # The hatches and buses built into this machine's casing, each at the CELL it replaces
            # and facing the way it works. The texture pass swaps them in for the casing cubes
            # underneath, so a bus renders as a bus rather than as the block it displaced.
            "hatches": hatches_by_machine.get(pl.machine_id, []),
        }
        for pl in layout.placements
        if pl.machine_id in machines
    ]

    scene_routes = []
    for route in layout.routes:
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
                "color": _COMMODITY_COLOR[route.commodity],
                "segments": segments,
                "terminals": terminals,
                # The blocks this route is built from, one per cell - the shape the viewer draws.
                # Derived in ``route_blocks`` rather than in the template's JavaScript, which is
                # where it used to live: the max-thickness rule in it is a build instruction
                # (docs/DOMAIN.md) and the build guide has to agree with it block for block.
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
    scene_io = {
        # ``rate`` is per-tick; ``unit`` is the stem (items/mB/EU) so the viewer can append /t or
        # /s for its toggle.
        "inputs": [
            {"resource": f.resource, "rate": f.rate, "unit": RATE_STEM[f.commodity]}
            for f in sysio.inputs
        ],
        "outputs": [
            {"resource": f.resource, "rate": f.rate, "unit": RATE_STEM[f.commodity]}
            for f in sysio.outputs
        ],
        "power": {
            "total": sum(d["volts"] * d["amps"] for d in power_by_tier.values()),
            "byTier": power_by_tier,
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
        "metrics": {
            "footprint": metrics.footprint,
            "layers": metrics.layers,
            "congestion": metrics.congestion,
            "buildability": metrics.buildability,
        },
    }


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
        for seg in route.segments:
            for cell in (
                [seg.start.x, seg.start.y, seg.start.z],
                [seg.end.x, seg.end.y, seg.end.z],
            ):
                grow(cell, [cell[i] + 1 for i in range(3)])

    if lo[0] is None:  # nothing placed or routed - frame the whole region instead
        region = problem.bounding_region
        return {"min": [0, 0, 0], "max": [region.sx, region.sy, region.sz]}
    return {"min": [v for v in lo if v is not None], "max": [v for v in hi if v is not None]}


def _role(machine: Machine) -> str:
    """Coarse render role: a power source, a boundary storage, or a plain machine. Reuses the
    shared predicates (``Machine.is_power_source``, ``system_io.is_boundary_storage``) so the role
    stays in step with the build guide instead of re-deriving them here."""
    if machine.is_power_source:
        return "source"
    if is_boundary_storage(machine.type):  # Super Chest / Super Tank boundary blocks
        return "storage"
    return "machine"
