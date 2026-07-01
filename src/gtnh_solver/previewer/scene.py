"""previewer.scene - denormalize a (problem, layout) pair into a self-contained render scene.

The output-layout contract (``LayoutResult``) references machines by id and leaves their
geometry in the ``InputIR``; a renderer needs it all in one place. ``build_scene`` flattens both
into a plain dict the three.js viewer can draw with no further lookups (machine boxes, route
segments coloured by commodity + sized by cable thickness, auto-output links, the region, a
legend, and the ``io`` boundary summary - inputs to load, outputs to collect, summed power). This
is a *previewer-internal* format - NOT the versioned contract - so the un-testable
WebGL last mile stays a thin static template while the mapping here is pure and fully tested.
"""

from __future__ import annotations

from typing import Any

from gtnh_solver.dataset import tier_voltage
from gtnh_solver.ir import Commodity, InputIR, IODirection, LayoutResult, Machine
from gtnh_solver.system_io import RATE_STEM, system_io

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
        terminals = [
            {"machine": t.machine_id, "face": t.face.value, "cell": [t.cell.x, t.cell.y, t.cell.z]}
            for t in route.terminals
        ]
        scene_routes.append(
            {
                "netId": route.net_id,
                "commodity": route.commodity.value,
                "color": _COMMODITY_COLOR[route.commodity],
                "segments": segments,
                "terminals": terminals,
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
        "metrics": {
            "footprint": metrics.footprint,
            "layers": metrics.layers,
            "congestion": metrics.congestion,
            "buildability": metrics.buildability,
        },
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
        size = [m.footprint.sx, m.footprint.sy, m.footprint.sz]
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
    """Coarse render role: a power source, a boundary storage, or a plain machine."""
    if any(
        p.commodity is Commodity.POWER and p.direction is IODirection.OUTPUT
        for p in machine.faces.ports
    ):
        return "source"
    if machine.type.startswith("Super "):  # Super Chest / Super Tank boundary blocks
        return "storage"
    return "machine"
