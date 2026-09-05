"""route_blocks - a route as the blocks it is built from: one per cell, with its connections.

A ``Route`` is a list of hops; a *build* is a list of blocks. Turning one into the other means
answering four questions per cell - which sides connect, which gauge the cable is, which block that
is, and what shape it occupies - and both first-party consumers need them:

- the **build guide** counts them ("12 x 2x tin cable"), and
- the **previewer** draws them (a cross with one arm per connected side, at GT's real thickness).

Deriving that twice would let the two surfaces disagree about what the same layout is made of, which
is the failure ``system_io`` exists to prevent for the line's boundary; this is the same shape for
its routing, so it lives beside it as a pure function of the contract, and the renderers only
format the answer.

It also *moves* the derivation. Until now the per-cell connection mask lived in the viewer template
(``previewer/html.py``) as a JavaScript ``Map`` keyed ``"x,y,z"`` - the un-CI-testable last mile, so
the one rule in it that is a real build instruction (below) was pinned by nothing at all.

**The boxes moved for the same reason, and less theoretically.** The shape was assembled in the
template too, and it was wrong: each arm was grown from the cell *centre* rather than from the core's
surface, so every arm overlapped the core and their side faces fought for the same pixels. Flat
coloured bars hid it perfectly - both quads were one colour - and it became a mess of tearing stripes
the instant those faces carried textures. :func:`route_boxes` builds the shape here instead, where
"no two boxes of a cell overlap" is a test rather than a hope.

**Two rules decide the block, both from docs/DOMAIN.md:**

1. *A cell incident to two gauges is built at the thicker one.* A shared power trunk that splits
   carries different summed amperage on either side of the split, so one cell can be incident to a
   2x hop and a 1x hop at once. A cell is one block, the fattest incident cable is the one that
   physically meets it, and under-sizing is what burns.
2. *The material is a labelled stand-in, never a build spec.* GT gives a voltage tier many cables;
   ``dataset/pipes.py`` picks the representative one and every block built here carries
   :attr:`RouteBlock.stand_in` so the guide can say so. Counts and gauges are real.

Connections are **player state** in GT (a pipe never auto-connects; the player wires each side with
a wire cutter or soldering iron), so this derives them from the route rather than pretending to read
them off a block: the sides that connect are the ones the solver actually routed through.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from gtnh_solver.dataset import (
    CABLE_THICKNESS_BLOCKS,
    DEFAULT_PIPE_THICKNESS_BLOCKS,
    cable_display_name,
    pipe_display_name,
)
from gtnh_solver.ir import Commodity, LayoutResult, Route, RouteMaterial
from gtnh_solver.ir.geometry import FACE_DELTAS, Cell


@dataclass(frozen=True)
class RouteBlock:
    """One kind of block a route is built from - the count key in the bill of materials.

    Deliberately plain strings rather than the :class:`RouteMaterial` model: this is a hashable
    tally key, and it has to stay meaningful for a route that published no material at all.
    """

    #: What to call it in a build guide, e.g. ``"2x tin cable"``, ``"bronze fluid pipe"``.
    #:
    #: The material is GT's *unlocalized* spelling, uncapitalised, because that is the string the
    #: dataset actually carries. Rendering ``niobiumtitanium`` as the "Niobium-Titanium" a player
    #: sees in game would mean authoring a second name table from memory - a small instance of
    #: exactly the plausible-confident-wrong failure this whole lane is guarding against.
    label: str

    #: The dataset name that joins this to its texture manifest entry (``"cable.tin.02"``), or
    #: ``None`` when the route published no material and the block is unspecified.
    dataset_name: str | None

    #: True when ``label``'s *material* is representative rather than chosen (docs/DOMAIN.md). The
    #: gauge and the count beside it are real either way.
    stand_in: bool


@dataclass(frozen=True)
class RouteBox:
    """One axis-aligned box of a cell's GT shape, in world coordinates.

    GT draws a pipe as a core cube plus one arm per connected side running out to the block edge,
    every part at the *same* cross-section - there is no fatter node (docs/DOMAIN.md). A straight
    run collapses to a single box through the block, which is how GT draws it too.
    """

    #: Centre of the box in world space (a cell's own centre is ``cell + 0.5`` on each axis).
    center: tuple[float, float, float]
    #: Full extent on each axis. Never zero: a cell with no connections is still its core cube.
    size: tuple[float, float, float]
    #: Unit normals of the faces showing an **open end** - the wire core, or the pipe's bore. Every
    #: other face of the box is closed (solid insulation, or the barrel).
    open_faces: frozenset[Cell]


@dataclass(frozen=True)
class RouteCell:
    """One cell of a route, as the block that gets built there."""

    cell: Cell

    #: Unit steps toward everything this cell connects to: the neighbouring route cells, and - for
    #: a terminal cell - the machine face it docks against. What the previewer grows arms toward.
    dirs: frozenset[Cell]

    #: The cable gauge (1/2/4/8/12/16), by rule 1 above. Always 1 for item and fluid routes, which
    #: v1 does not size.
    thickness: int

    #: The block's rendered thickness in blocks, GT's own ladder (docs/DOMAIN.md), for the
    #: previewer's cross-section. Cables use the *insulated* ladder; a route above UV is carried by
    #: bare wire, which is one step thinner, but that route also publishes no material and so keeps
    #: its flat bar rather than being drawn as a block at all.
    thickness_blocks: float

    block: RouteBlock

    #: The GT shape of this cell, as axis-aligned boxes that do not overlap each other.
    boxes: tuple[RouteBox, ...]


def route_cells(route: Route) -> list[RouteCell]:
    """``route`` as one :class:`RouteCell` per cell it occupies, ordered by coordinate.

    A **non-unit** segment contributes its two endpoints but no connection: it cannot be an arm to
    an adjacent block, and drawing one would run a cable through whatever is in between. The
    validator reports it as ``ROUTE_SEGMENT_NOT_UNIT``; this renders the honest shape of a layout
    that has one, rather than assuming it away (the contract does not forbid it - only the gate
    does, and previews are drawn of invalid layouts too).
    """
    gauges = route.thickness_per_segment
    dirs: dict[Cell, set[Cell]] = {}
    thickness: dict[Cell, int] = {}

    def touch(cell: Cell, gauge: int) -> None:
        dirs.setdefault(cell, set())
        thickness[cell] = max(thickness.get(cell, 1), gauge)  # rule 1: the thicker cable wins

    for i, seg in enumerate(route.segments):
        start, end = seg.start.as_tuple(), seg.end.as_tuple()
        gauge = gauges[i] if gauges is not None and i < len(gauges) else 1
        touch(start, gauge)
        touch(end, gauge)
        step = (end[0] - start[0], end[1] - start[1], end[2] - start[2])
        if sum(abs(d) for d in step) == 1:
            dirs[start].add(step)
            dirs[end].add((-step[0], -step[1], -step[2]))

    for terminal in route.terminals:
        cell = terminal.cell.as_tuple()
        touch(cell, 1)  # a dock cell with no segment (the source's own cell) is the thin case
        dx, dy, dz = FACE_DELTAS[terminal.face]
        dirs[cell].add((-dx, -dy, -dz))  # the lead runs from the cell *into* the machine face

    out = []
    for cell in sorted(dirs):
        connections = frozenset(dirs[cell])
        blocks_thick = _thickness_blocks(route.commodity, thickness[cell])
        out.append(
            RouteCell(
                cell=cell,
                dirs=connections,
                thickness=thickness[cell],
                thickness_blocks=blocks_thick,
                block=route_block(route.commodity, thickness[cell], route.material),
                boxes=route_boxes(cell, connections, blocks_thick),
            )
        )
    return out


def route_boxes(cell: Cell, dirs: frozenset[Cell], thickness_blocks: float) -> tuple[RouteBox, ...]:
    """The GT shape of one cell: a core cube plus an arm per connection, or one box for a straight run.

    Every box is axis-aligned and **no two of them overlap**, which is the whole point of computing
    this here. An arm starts at the core's surface rather than at the cell centre: grown from the
    centre it would swallow half the core, and the two boxes' side faces would then be coplanar *and*
    overlapping on the outside of the shape - two quads fighting for the same pixels, each stretching
    the sprite over a different length.

    A straight run - exactly two connections, opposite each other - is one box spanning the block,
    matching GT and sparing the commonest cell in any layout a seam that says nothing. Both of its
    end faces are open ends; an arm's single outward face is; a core cube has none, its connected
    faces being covered by the arms attached to them.
    """
    center = (cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5)
    half = thickness_blocks / 2
    ordered = sorted(dirs)

    if len(ordered) == 2 and ordered[0] == tuple(-d for d in ordered[1]):
        axis = next(i for i, d in enumerate(ordered[1]) if d)
        size = [thickness_blocks, thickness_blocks, thickness_blocks]
        size[axis] = 1.0
        return (RouteBox(center, (size[0], size[1], size[2]), frozenset(ordered)),)

    core = RouteBox(center, (thickness_blocks, thickness_blocks, thickness_blocks), frozenset())
    length = 0.5 - half
    if length <= 0:
        # A pipe at least a block thick already fills its cell; GT renders it as a full cube and
        # there is no arm left to draw (docs/DOMAIN.md). Nothing in v1's ladders reaches this.
        return (core,)
    boxes = [core]
    offset = half + length / 2  # from the cell centre to the arm's own centre
    for step in ordered:
        dx, dy, dz = step
        boxes.append(
            RouteBox(
                center=(
                    center[0] + dx * offset,
                    center[1] + dy * offset,
                    center[2] + dz * offset,
                ),
                size=(
                    length if dx else thickness_blocks,
                    length if dy else thickness_blocks,
                    length if dz else thickness_blocks,
                ),
                open_faces=frozenset({step}),
            )
        )
    return tuple(boxes)


def route_block_counts(layout: LayoutResult) -> list[tuple[RouteBlock, int]]:
    """Every block ``layout``'s routes are built from, tallied, ordered by label.

    Counted per **cell**, not per route: one cell is one block. Routes cannot legally share a cell
    (``ROUTE_CELL_COLLISION``), but an invalid layout still gets a bill of materials, so a shared
    cell is charged once - at the fatter gauge, rule 1 one level up.
    """
    by_cell: dict[Cell, RouteCell] = {}
    for route in layout.routes:
        for rc in route_cells(route):
            seen = by_cell.get(rc.cell)
            if seen is None or rc.thickness > seen.thickness:
                by_cell[rc.cell] = rc
    tally = Counter(rc.block for rc in by_cell.values())
    return sorted(tally.items(), key=lambda kv: kv[0].label)


def route_block(commodity: Commodity, thickness: int, material: RouteMaterial | None) -> RouteBlock:
    """The block one cell of such a route is built as.

    ``material is None`` is not an error - it is what every route said before the field existed,
    and what a trunk with no single tier still says - so the block keeps the gauge (real) and drops
    only the material, reading as ``"2x power cable"``: the wording the build guide used before any
    of this, which is the point.
    """
    if material is None:
        gauge = f"{thickness}x " if commodity is Commodity.POWER else ""
        return RouteBlock(label=f"{gauge}{_GENERIC[commodity]}", dataset_name=None, stand_in=False)
    if commodity is Commodity.POWER:
        return RouteBlock(
            label=f"{thickness}x {material.material} cable",
            dataset_name=cable_display_name(material.material, thickness),
            stand_in=material.stand_in,
        )
    return RouteBlock(
        label=f"{material.material} {commodity.value} pipe",
        dataset_name=pipe_display_name(material.material),
        stand_in=material.stand_in,
    )


#: What to call a route whose material is unspecified. The wording the build guide has always used.
_GENERIC: dict[Commodity, str] = {
    Commodity.ITEM: "item pipe",
    Commodity.FLUID: "fluid pipe",
    Commodity.POWER: "power cable",
}


def _thickness_blocks(commodity: Commodity, thickness: int) -> float:
    if commodity is not Commodity.POWER:
        return DEFAULT_PIPE_THICKNESS_BLOCKS
    return CABLE_THICKNESS_BLOCKS[thickness]
