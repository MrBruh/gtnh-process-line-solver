"""previewer.scene - denormalize a (problem, layout) pair into a self-contained render scene.

The output-layout contract (``LayoutResult``) references machines by id and leaves their
geometry in the ``InputIR``; a renderer needs it all in one place. ``build_scene`` flattens both
into a plain dict the three.js viewer can draw with no further lookups (machine boxes, route
segments coloured by commodity + sized by cable thickness, auto-output links, the region, a
legend). This is a *previewer-internal* format - NOT the versioned contract - so the un-testable
WebGL last mile stays a thin static template while the mapping here is pure and fully tested.
"""

from __future__ import annotations

from typing import Any

from gtnh_solver.ir import Commodity, InputIR, IODirection, LayoutResult, Machine

#: Bump if the scene shape the viewer template expects changes.
SCENE_VERSION = 0

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

#: Route colours by commodity (the viewer's legend mirrors these).
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

    scene_machines = [
        {
            "id": pl.machine_id,
            "type": machines[pl.machine_id].type,
            "cell": [pl.cell.x, pl.cell.y, pl.cell.z],
            "size": [
                machines[pl.machine_id].footprint.sx,
                machines[pl.machine_id].footprint.sy,
                machines[pl.machine_id].footprint.sz,
            ],
            "front": pl.orientation.value,
            "role": _role(machines[pl.machine_id]),
            "color": color_for_type[machines[pl.machine_id].type],
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
        scene_routes.append(
            {
                "netId": route.net_id,
                "commodity": route.commodity.value,
                "color": _COMMODITY_COLOR[route.commodity],
                "segments": segments,
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

    region = problem.bounding_region
    metrics = layout.metrics
    return {
        "version": SCENE_VERSION,
        "status": layout.status.value,
        "seed": layout.seed,
        "region": {"sx": region.sx, "sy": region.sy, "sz": region.sz},
        "machines": scene_machines,
        "routes": scene_routes,
        "autoConnections": scene_autos,
        "legend": [{"label": t, "color": color_for_type[t]} for t in types],
        "metrics": {
            "footprint": metrics.footprint,
            "layers": metrics.layers,
            "congestion": metrics.congestion,
            "buildability": metrics.buildability,
        },
    }


def _role(machine: Machine) -> str:
    """Coarse render role: a power source, a boundary storage, or a plain machine."""
    if any(
        p.commodity is Commodity.POWER and p.direction is IODirection.OUTPUT
        for p in machine.faces.ports
    ):
        return "source"
    if machine.type.startswith("Super "):  # Super Chest / Super Tank boundary blocks
        return "storage"
    return "machine"
