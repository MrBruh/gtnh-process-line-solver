"""placement.search - Phase 2 simulated-annealing + LNS placement with a routing-aware cost.

Starts from the constructive first-fit solution and improves it under a cost that proxies
buildability: half-perimeter wirelength (HPWL) per item/fluid net pulls connected machines
together (more auto-output, shorter pipes); an auto-output reward favours orientations whose
usable faces (``Machine.allowed_faces``) actually let a source eject into its sink; and
compactness is two independently weighted terms - the **footprint** (floor area, x-span times
z-span, shrunk by stacking vertically) and the total bounding-box **volume** (shrunk by staying
flat/cubic) - whose weights the selectable :data:`Objective` picks, since the two pull opposite
ways (docs/ROADMAP.md lane C). The default ``footprint`` objective drives the floor area down and
keeps volume as a mild tiebreak. The auto reward is the only orientation-dependent term, so
reorient moves carry a real cost signal - without it they were free random walk that could
finalize an orientation BLOCKING auto-output.

Power nets carry **no base cost term**: cheap center-distance proxies (HPWL, MST) cannot see
dock faces or shared cable taps, and measurably steer AWAY from low-cable layouts (a source
sitting on top of a machine row scores nearer its sinks than one whose dock cell the sinks can
tap, yet needs more cable). Re-measured under #123 at weights down to 0.1, the shipped sand line's
cable still went UP at every one (3 -> 4..6 cells), so this stands. The real per-segment cable cost
is judged where it is knowable, on a routed layout: the solver routes each candidate placement
and keeps the best by floor area plus route cells (``solver._structure.structure_quality``), and its
``solver.repair`` pass relocates each power source by really routing every candidate cell around
the sinks it feeds - which is how a source gets positioned without a proxy having to guess.
What remains here is a rescue path for a caller that re-places: a power net given a penalty
(``net_penalties``) switches on a minimum-spanning-tree pull over the net's members (a
shared-amperage trunk is a tree). The solver itself no longer passes penalties: its attempts are
independent (``solver.core``).

The neighbourhood mixes small moves (relocate / swap / reorient; orientation is a search variable)
with a **large neighbourhood search (LNS) ruin-and-recreate** move: rip out a *related* cluster of
machines (a net-connected neighbourhood, the ones that want to sit together) and greedily
re-insert each at the position + orientation that minimises the cost, biased toward cells next to
its already-placed net-neighbours. One LNS step reshapes a whole cluster at once, escaping the
local optima single-cell moves plateau in. The re-insertion prices compactness too (how much a
spot grows the build), which is what lets it fold the long strip the first-fit start lays a big
multiblock line out in (#254). Metropolis acceptance with geometric cooling keeps the best valid
layout seen.

**Best means the cheapest layout the crowding gate passes**, not the cheapest one seen. The
solver asks the gate (``placement.feasibility.crowded_machines``) of every attempt before routing
it and discards the placements it proves crowded, and the face-shortfall term here is only a
cheap stand-in for that proof: it misses crowding the gate can prove. On the iron line the
cheapest state seen was gated on 50 of 64 attempts, so most anneals handed back a placement the
solver threw away while cheaper, uncrowded ones had gone by; returning the cheapest one the gate
passes left 16 of 64 gated, and the line solves VALID on 7 of 8 seeds instead of 3. The gate is
asked once the walk is done, of the accepted states cheapest first, so it never steers the walk
itself: the anneal takes exactly the path it always did, and only the state it returns can differ.
When no state passes, it returns the cheapest one, and the solver's own gate and fallback decide
as before.

    units = parallel_groups -> one rigid column each, laid back to front; every other machine alone
    initial = constructive.place(lattice=True)   # a valid seed; each group seeded as its column,
                                                 # single blocks on a spaced lattice
    repeat for a seeded budget:
        cand = with prob p_lns:  ruin (remove a related cluster of UNITS) + recreate (greedy
                                 re-insert of each unit, priced on nets, auto-output and how much
                                 it grows the build)
               else:            relocate | nudge (single-block lines only) | swap | reorient
                                ONE unit, its members rigid
               (only ever a VALID candidate, else skip)
        accept if cheaper, or with prob exp(-d/T)   ; remember it ; cool T
    return the cheapest remembered state the crowding gate passes, else the cheapest

**The search moves units, not machines.** A plan node's parallel single blocks (``node#1`` ..
``node#N``, ``placement.groups``) move as one rigid column, laid **back to front**: each member's
front, which carries no I/O, is pressed against the previous member's back. Moved one at a time the
copies ended up side by side or scattered, and every side-by-side contact costs two usable faces,
one on each machine; back to front a contact costs one (a back), and one straight pipe or cable run
along the column can serve every member. It is the shape ``placement.banks`` lays a chain of banks
in. Nothing downstream needs telling: the only front rule is that it carries no I/O, and the face
term already sees a covered face. Every other machine is a unit of one, and a line with no group is
annealed exactly as it was, draw for draw (``tests/fixtures/no-group-anneals.json`` pins it).

**A line of single blocks starts spread out** (``constructive.place(lattice=True)``), on a
lattice with room to route between the blocks. Started from first-fit's single row it never left
the plane: iron.json's 30 blocks came out as a one-block-deep wall on every seed, because the
first move off the row doubles the floor area and the starting temperature never accepts that.
From the lattice its machines span about 10 by 12 cells instead of 26 by 1. Over 16 solve seeds
at full effort iron solved VALID on 15 either way, at a median of 346 floor plus route cells
against 366, and 37 of its 128 attempts routed VALID against 29. The floor term hardly moves a
lattice (each edge is a whole row of blocks), so it was left alone: priced as a smooth
((x + z) / 2) ** 2, or with a one-cell routing margin on each side, iron solved no better.

**The nudge** shifts one machine by one cell. Relocate draws a cell anywhere in the region, which
almost never lands anywhere useful (under 2% of relocates are accepted), so without a nudge the
search has no way to slide a machine along a wall or tuck it into a gap beside its neighbours. On a
line of single blocks the nudge takes half of relocate's share, and on parallel-sand with the bank
template off it improves 15 of 16 solves (median box 128 to 92). On a line with any multiblock it
is left out and the mix is exactly as it was: there it measured as noise at best, and nudging every
machine cost nitrobenzene a valid layout on one seed in eight (#246 saw the same on
ev-nitrobenzene).

Every accepted state is overlap/bounds/reserved-clean (moves build only valid candidates, and
recreate falls back to a machine's freed origin), so the validator still independently certifies
the output. A **power source** keeps its front face - the reserved external-feed face - flush on
the region boundary through every move (relocate/swap re-orient it back onto a wall when they
can, reorient only offers wall-facing options), the same hard constraint the constructive seed
satisfies and the validator enforces. Deterministic for a given ``seed``. The multi-start that
routes and ranks these placements lives in ``solver.core`` (docs/ROADMAP.md lane C + solver).
"""

from __future__ import annotations

import math
import random
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from itertools import product
from types import MappingProxyType
from typing import Literal, NamedTuple, Protocol

from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    Facing,
    InputIR,
    Machine,
    Placement,
)
from gtnh_solver.ir.enums import HORIZONTAL_FACINGS_ORDERED
from gtnh_solver.ir.geometry import (
    FACE_DELTAS,
    FACE_OFFSETS,
    Cell,
    Pose,
    Size,
    allowed_faces,
    box_cells,
    box_front_on_boundary,
    box_within,
    pose_of,
    rotated_footprint,
)
from gtnh_solver.router.auto import auto_candidates, auto_output_possible

from .constructive import PlacementResult, _fit, place
from .feasibility import crowded_machines
from .groups import column_offsets, column_size, parallel_groups

#: The six face-adjacent offsets, for growing LNS insertion candidates around placed neighbours.
_FACE_DELTAS = FACE_OFFSETS

# Cost weights. Wirelength (item/fluid HPWL) dominates: it drives auto-output and short pipes.
# The auto-output reward makes orientation matter (the front face carries no I/O, so the wrong
# orientation BLOCKS the free connection - reorient moves are a no-op on the other terms).
# Power nets have no weight here (the module docstring says why); their MST term activates only
# via feedback penalties. There is deliberately NO per-layer penalty: height is only ever paid
# through the volume term.
_W_WIRE = 1.0
_W_AUTO = 4.0
#: Weight on the face-shortfall term. Large on purpose, and only safe to be large because the
#: term no longer charges auto-output ports for cells they do not use: with that phantom gone it
#: reads zero on every layout that is genuinely fine, so it costs those nothing at all.
#:
#: Sized by measurement, not taste. The parallel line needs the equivalent of ~9x the old weight
#: of 8 before the placer will stop packing its nine machines into a wall, and pushing past that
#: buys nothing: at 48 it still lays 60 pipe cells, at 72 it lays 27, at 120 it drifts back up.
#: Re-measure over the solver's whole seed grid, never one seed, before changing it.
_W_FACES = 8.0

#: The selectable compactness objective. "Compact" is ambiguous and the two metrics pull opposite
#: ways - stacking a layer shrinks the floor but can grow the enclosing box - so the builder
#: picks: ``footprint`` = minimum floor area (stack tall; the default, the maintainer's target),
#: ``volume`` = minimum enclosing box (stay flat/cubic), ``balanced`` = both weighted.
Objective = Literal["footprint", "volume", "balanced"]

#: (footprint weight, volume weight) per objective. Each pure mode drives one term and keeps the
#: other as at most a mild tiebreak so equal winners still prefer the smaller build.
_OBJECTIVE_WEIGHTS: dict[str, tuple[float, float]] = {
    "footprint": (1.0, 0.02),
    "volume": (0.0, 1.0),
    "balanced": (0.5, 0.5),
}

# Annealing schedule (geometric cooling). Budget scales with machine count, clamped.
# _T0 is a fixed initial temperature (not scaled to the cost): at 2.0 a candidate costing +2 is
# still accepted ~e^-1 (37%) of the time, so early iterations explore before cooling tightens.
# Fixed; see #41.
_T0 = 2.0
_ALPHA = 0.995
_MIN_ITERS = 250
_PER_MACHINE = 60
_MAX_ITERS = 6000
_RELOCATE_TRIES = 20

# LNS ruin-and-recreate. ``_P_LNS`` is how often an iteration does a large move instead of a small
# one; ``_MAX_RUIN`` caps the cluster size (kept modest so recreate stays cheap and local, not a
# full re-placement); ``_LNS_RANDOM_CANDIDATES`` adds a few random insertion sites beyond the
# neighbour-adjacent ones so recreate is not purely greedy-local.
_P_LNS = 0.1
_MAX_RUIN = 6
_LNS_RANDOM_CANDIDATES = 8
# Approximate cap on neighbour-adjacent insertion sites so recreate stays cheap on hubs. It is
# checked once per placed neighbour, so one large neighbour's cells can spill a little past it -
# left approximate on purpose (enforcing it mid-neighbour would drop candidates and change results).
_MAX_CANDIDATES = 16

# Small-move mix for a non-LNS iteration: one uniform draw picks relocate below _P_RELOCATE, swap
# below _P_SWAP, else reorient - a ~1/3 : 1/3 : 1/3 split. Named so the mix lives in one place; the
# exact values are load-bearing for per-seed determinism, so keep them if you retune the split.
_P_RELOCATE = 0.34
_P_SWAP = 0.67
#: On a line of single blocks only, relocate keeps the draws below this and the one-cell nudge
#: takes the rest of its share, up to ``_P_RELOCATE`` (module docstring). Half of relocate's share.
_P_RELOCATE_WHEN_NUDGING = 0.17


#: A routed net as the placement cost sees it: ``(member machine ids, weight)``. Item/fluid nets
#: weigh ``1.0`` plus any feedback penalty; a power net appears here only once a feedback penalty
#: puts it in play, carrying that penalty (module docstring).
_WeightedNet = tuple[list[str], float]
_Centroid = tuple[float, float, float]


class _NetBox(NamedTuple):
    """One wire net's HPWL bounding box over the members already placed, and the net's weight.

    The half-perimeter of a net is the bounding box of *all* its members' centroids. During an
    insertion every member but the candidate is fixed, so their box is the same for every
    candidate origin and orientation: precompute it once and each candidate only has to widen it.
    """

    weight: float
    x0: float
    x1: float
    y0: float
    y1: float
    z0: float
    z1: float


class _Extent(NamedTuple):
    """The box the machines already placed span, in whole cells, bounds inclusive.

    What an insertion is measured against for compactness: the candidate grows it or it does not.
    Cells rather than centroids, because the floor area and volume ``_cost`` ranks on are counted
    in cells.
    """

    x0: int
    x1: int
    y0: int
    y1: int
    z0: int
    z1: int


class _PowerAttach(NamedTuple):
    """One penalized power net's weight and the centroids of its already-placed members.

    Unlike :class:`_NetBox` this cannot collapse to a box - the term is the distance to the
    *nearest* member (the increment Prim would pay), not a span - but the centroids themselves are
    still invariant across candidates, so they are computed once rather than per candidate.
    """

    weight: float
    centroids: list[_Centroid]


@dataclass(frozen=True, slots=True)
class _Body:
    """What the search reads of one machine, as plain values, gathered once per solve.

    ``Machine`` is a pydantic model, and on Python 3.14+ every field read off one takes the slow,
    unspecialized path (#256), while the search reads a machine's footprint, facings and ports per
    candidate and per cost evaluation. So it reads them from here instead. ``fronts_outside`` is
    the extreme case: a property that rescans the machine's ports on every read, for an answer that
    never changes during a solve.

    ``machine`` is kept for the callers that genuinely need the model: the auto-output rule, which
    keys its caches on it, and the first-fit fallback.

    It is also the per-problem **face table** (#249): which faces each port may use is read off the
    model once, through ``Machine.allowed_faces``, and kept here as shells of cell offsets, so the
    cost never asks the model per evaluation.
    """

    machine: Machine
    #: The footprint as it sits facing each way (:func:`rotated_footprint`), for every facing.
    sizes: Mapping[Facing, Size]
    #: :func:`_shell_offsets` of each of those sizes, facing that way, through every face some port
    #: may use: the non-front faces for a machine with no pinned port, which is every machine but
    #: an Item Filter.
    shells: Mapping[Facing, tuple[Cell, ...]]
    orientations: tuple[Facing, ...]
    fronts_outside: bool
    #: The ports a router will dock: on some net whose commodity is not on the ME network. A port on
    #: no net, or riding ME, is docked by nobody, so it asks for no cell - the rule the exact gate
    #: (``placement.feasibility``) already applies, and the cheap term must not tax what it exempts.
    port_ids: tuple[str, ...]
    #: Per facing, one shell per entry of :attr:`port_ids`, through that port's own faces only; None
    #: when no port is pinned (``Port.faces``), so an unpinned machine keeps the one shared shell.
    #: A pinned machine faces horizontally only, so only those facings are tabulated.
    port_shells: Mapping[Facing, tuple[tuple[Cell, ...], ...]] | None = None


def _body(machine: Machine, docked: Collection[str] | None = None) -> _Body:
    """``machine`` as the search reads it. ``docked`` names the ports a router will dock (None:
    all of them); :func:`optimize_placement` passes the ports on non-ME nets."""
    sizes: dict[Facing, Size] = {}
    for facing in Facing:
        box = rotated_footprint(machine.footprint, facing)
        sizes[facing] = (box.sx, box.sy, box.sz)
    ports = [p for p in machine.faces.ports if docked is None or p.id in docked]
    port_shells: dict[Facing, tuple[tuple[Cell, ...], ...]] | None = None
    if any(port.faces is not None for port in machine.faces.ports):
        facings = HORIZONTAL_FACINGS_ORDERED
        shells = {
            f: _shell_offsets(
                *sizes[f],
                frozenset().union(*(machine.allowed_faces(p.id, f) for p in machine.faces.ports)),
            )
            for f in facings
        }
        port_shells = {
            f: tuple(_shell_offsets(*sizes[f], machine.allowed_faces(p.id, f)) for p in ports)
            for f in facings
        }
    else:
        shells = {f: _shell_offsets(*size, allowed_faces(None, f)) for f, size in sizes.items()}
    return _Body(
        machine=machine,
        sizes=sizes,
        shells=shells,
        orientations=tuple(machine.orientation_options),
        fronts_outside=machine.fronts_outside,
        port_ids=tuple(port.id for port in ports),
        port_shells=port_shells,
    )


def _bodies(problem: InputIR) -> dict[str, _Body]:
    """Every machine of ``problem`` as a :class:`_Body`, charged only for the ports a router docks.

    A port docks when it sits on a net whose commodity is not on the ME network. The exact gate
    (``placement.feasibility.crowded_machines``) charges nothing for any other port, so neither may
    the face term that approximates it.
    """
    docked: dict[str, set[str]] = {m.id: set() for m in problem.machines}
    for n in problem.nets:
        if not problem.me_toggles.toggled(n.commodity):
            for e in n.endpoints:
                docked.setdefault(e.machine_id, set()).add(e.port_id)
    return {m.id: _body(m, docked[m.id]) for m in problem.machines}


def _placement(pose: Pose) -> Placement:
    """``pose`` back as the contract's ``Placement``, for the result."""
    x, y, z = pose.cell
    return Placement(
        machine_id=pose.machine_id, cell=CellCoord(x=x, y=y, z=z), orientation=pose.orientation
    )


class _Boxed(Protocol):
    """What a fit test reads of the thing it moves: a :class:`_Body` (one machine) or a
    :class:`_Unit` (a whole column), so one set of fit helpers serves both."""

    @property
    def sizes(self) -> Mapping[Facing, Size]: ...
    @property
    def orientations(self) -> tuple[Facing, ...]: ...
    @property
    def fronts_outside(self) -> bool: ...


#: A unit of one machine's member offsets: the machine sits at the unit's own origin.
_AT_ORIGIN: Mapping[Facing, tuple[Cell, ...]] = MappingProxyType(
    dict.fromkeys(Facing, ((0, 0, 0),))
)


@dataclass(frozen=True, slots=True)
class _Unit:
    """What one move picks and moves rigidly: a group's column, or a single machine.

    A unit's pose is its box's minimum corner and the facing every member shares, and each member
    sits at that corner plus its ``offsets`` for the facing (:func:`_relaid`). For a column those
    are :func:`~gtnh_solver.placement.groups.column_offsets`, so a turn of the unit relays it back
    to front along the new axis. A unit of one is its machine: offset ``(0, 0, 0)`` and its body's
    own sizes, facings and outside-front rule, so a multiblock moves exactly as it always did.
    """

    #: The first member's machine id: a unit of one is keyed by its machine's id.
    key: str
    #: Each member's index into the search's placements list, head first. The list never reorders
    #: (``_ruin_and_recreate`` hands back the original order), so the indices hold for a solve.
    members: tuple[int, ...]
    ids: tuple[str, ...]
    sizes: Mapping[Facing, Size]
    offsets: Mapping[Facing, tuple[Cell, ...]]
    orientations: tuple[Facing, ...]
    fronts_outside: bool
    #: The machines outside the unit that share a net with one of its members.
    neighbors: frozenset[str]


def _units(
    placements: list[Pose],
    bodies: Mapping[str, _Body],
    groups: tuple[tuple[str, ...], ...],
    adjacency: Mapping[str, set[str]],
) -> tuple[_Unit, ...]:
    """The search's units over the seed ``placements``, in the order their first member comes.

    Each of ``groups`` the seed laid as its column is one unit, and every other machine is a unit
    of one. A group the seed did not lay as its column (it had to dissolve it to fit the line,
    ``constructive._lay_unit``) is dissolved here too, into units of one, so the seed and the search
    agree without being told.
    """
    index = {p.machine_id: i for i, p in enumerate(placements)}
    columns: dict[int, _Unit] = {}
    for ids in groups:
        column = _column_unit(ids, placements, index, bodies, adjacency)
        if column is not None:
            columns[min(column.members)] = column
    in_column = {i for column in columns.values() for i in column.members}
    units: list[_Unit] = []
    for i, p in enumerate(placements):
        if i in columns:
            units.append(columns[i])
        elif i not in in_column:
            units.append(_single_unit(i, bodies[p.machine_id], adjacency))
    return tuple(units)


def _single_unit(index: int, body: _Body, adjacency: Mapping[str, set[str]]) -> _Unit:
    """The unit of one that is the machine of ``body``, at ``index`` in the placements."""
    machine_id = body.machine.id
    return _Unit(
        key=machine_id,
        members=(index,),
        ids=(machine_id,),
        sizes=body.sizes,
        offsets=_AT_ORIGIN,
        orientations=body.orientations,
        fronts_outside=body.fronts_outside,
        neighbors=frozenset(adjacency.get(machine_id, ())),
    )


def _column_unit(
    ids: tuple[str, ...],
    placements: list[Pose],
    index: Mapping[str, int],
    bodies: Mapping[str, _Body],
    adjacency: Mapping[str, set[str]],
) -> _Unit | None:
    """The group ``ids`` as one unit, or None when ``placements`` does not hold it as its column:
    every member facing one way, each at the box's minimum corner plus its column offset."""
    members = tuple(index[mid] for mid in ids)
    poses = [placements[i] for i in members]
    facing = poses[0].orientation
    x0 = min(p.cell[0] for p in poses)
    y0 = min(p.cell[1] for p in poses)
    z0 = min(p.cell[2] for p in poses)
    for p, (dx, dy, dz) in zip(poses, column_offsets(len(ids), facing), strict=True):
        if p.orientation is not facing or p.cell != (x0 + dx, y0 + dy, z0 + dz):
            return None
    orientations = bodies[ids[0]].orientations
    return _Unit(
        key=ids[0],
        members=members,
        ids=ids,
        sizes=MappingProxyType({f: column_size(len(ids), f) for f in orientations}),
        offsets=MappingProxyType({f: column_offsets(len(ids), f) for f in orientations}),
        orientations=orientations,
        fronts_outside=False,  # parallel_groups never groups a machine whose front faces outside
        neighbors=frozenset(nb for mid in ids for nb in adjacency.get(mid, ())) - set(ids),
    )


def _unit_adjacency(units: tuple[_Unit, ...]) -> dict[str, set[str]]:
    """Unit key -> the keys of the units it shares a net with, for the LNS related ruin."""
    unit_of = {mid: unit.key for unit in units for mid in unit.ids}
    return {u.key: {unit_of[nb] for nb in u.neighbors if nb in unit_of} for u in units}


def _units_of(placements: list[Pose], ctx: _SearchContext) -> tuple[_Unit, ...]:
    """``ctx``'s units, or every machine of ``placements`` as a unit of one when the context was
    built without them (a test that calls a move directly)."""
    if ctx.units is not None:
        return ctx.units
    return tuple(
        _single_unit(i, ctx.bodies[p.machine_id], ctx.adjacency) for i, p in enumerate(placements)
    )


def _unit_pose(unit: _Unit, placements: list[Pose]) -> Pose:
    """``unit``'s own pose in ``placements``: its key, its box's minimum corner, its facing. A unit
    of one's pose is its machine's."""
    head = placements[unit.members[0]]
    if len(unit.members) == 1:
        return head
    dx, dy, dz = unit.offsets[head.orientation][0]
    x, y, z = head.cell
    return Pose(unit.key, (x - dx, y - dy, z - dz), head.orientation)


def _member_poses(unit: _Unit, origin: Cell, orientation: Facing) -> Iterator[tuple[int, Pose]]:
    """Each member's index and pose with ``unit`` at ``origin`` facing ``orientation``."""
    x, y, z = origin
    for i, mid, (dx, dy, dz) in zip(unit.members, unit.ids, unit.offsets[orientation], strict=True):
        yield i, Pose(mid, (x + dx, y + dy, z + dz), orientation)


def _relaid(placements: list[Pose], *moves: tuple[_Unit, Cell, Facing]) -> list[Pose]:
    """A copy of ``placements`` with each ``(unit, origin, orientation)`` of ``moves`` laid there,
    every member at its offset."""
    new = list(placements)
    for unit, origin, orientation in moves:
        for i, pose in _member_poses(unit, origin, orientation):
            new[i] = pose
    return new


@dataclass(frozen=True)
class _SearchContext:
    """Immutable per-solve context threaded through the neighbourhood + recreate helpers.

    Built once in :func:`optimize_placement`, it bundles the read-only lookups every move shares -
    the machines by id, the bounding region, the reserved cells, the net adjacency, and the
    per-machine net/power/auto views the LNS recreate ranks insertions with - so the helpers take a
    few varying arguments (the placements, the growing occupied set, the rng) instead of threading a
    dozen constants three levels deep. Purely a container: it changes no value the cost computes.
    """

    bodies: dict[str, _Body]
    region: CellBox
    bounds: Size  # ``region``'s extents as plain ints, for the per-candidate bounds tests
    reserved: set[Cell]
    adjacency: dict[str, set[str]]
    machine_nets: dict[str, list[_WeightedNet]]
    machine_power: dict[str, list[_WeightedNet]]
    machine_auto: dict[str, list[_AutoPair]]
    #: The objective's ``(footprint weight, volume weight)`` (:data:`_OBJECTIVE_WEIGHTS`), so the
    #: LNS recreate can price what an insertion does to the build's size, as ``_cost`` does.
    weights: tuple[float, float]
    #: Whether the small moves include the one-cell nudge: only when every machine is a single
    #: block (module docstring).
    nudges: bool = False
    #: What the moves pick and move (:class:`_Unit`): every group's column and every other machine
    #: alone. None makes every machine a unit of one, for a context a test builds by hand.
    units: tuple[_Unit, ...] | None = None
    #: :func:`_unit_adjacency` of ``units``, which the LNS ruin grows its cluster along; None reads
    #: ``adjacency``, which it equals when every unit is one machine.
    unit_adjacency: dict[str, set[str]] | None = None


def optimize_placement(
    problem: InputIR,
    *,
    seed: int = 0,
    net_penalties: dict[str, float] | None = None,
    face_penalties: dict[str, float] | None = None,
    objective: Objective = "footprint",
    max_iterations: int | None = None,
) -> PlacementResult:
    """Anneal the constructive placement toward a lower routing-aware cost (seeded, validated).

    ``max_iterations`` caps the annealing schedule (which scales with the machine count); None runs
    the whole schedule. The solver's ``minimal`` effort passes a small cap: a shorter anneal cools
    less far and so places worse, but every move still only ever builds a valid candidate.

        ``net_penalties`` (net id -> extra weight) boosts a net's wirelength term so its machines pull
        tighter, so a caller that re-places after a failed routing can cluster the nets the router
        could not lay (shorter routes, or adjacency that auto-outputs). The solver used to; its
        attempts are now independent and pass no penalties (``solver.core``).
        ``objective`` selects what "compact" means (:data:`Objective`): minimum floor area
        (``footprint``, the default - stack tall), minimum enclosing box (``volume`` - stay flat), or
        ``balanced`` (both weighted).

    ``face_penalties`` (machine id -> extra weight) is the same signal for crowding: machines the
        crowding gate found nowhere to put a connection, weighted so the next placement gives
        *them* room.

        Per machine, and emphatically not a global dial. An earlier version scaled the whole face term
        up on every rejection, which quietly broke the multi-start: the attempts are independent seeds
        meant to be ranked against **one** objective, and re-weighting between them meant the layouts
        reaching the end were whichever had been annealed under the most distorted setting. The
        parallel line came out 57% larger that way than the same search at a flat weight (footprint 84
        against 36), because only the sprawled late attempts survived to be ranked.
    """
    base = place(problem, lattice=True)
    if not base.ok or len(base.placements) < 2:
        return base  # infeasible, or nothing to optimize (0/1 machine)

    bodies = _bodies(problem)
    region = problem.bounding_region
    bounds = (region.sx, region.sy, region.sz)
    reserved = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    penalties = net_penalties or {}
    # Nets that are physically routed (skip ME-toggled): each is (machine ids, weight), where a
    # penalized net weighs more so the optimizer shortens it preferentially. Item/fluid nets pay
    # HPWL. Power nets have NO base term (module docstring says why) - one enters the cost, as
    # an MST trunk-length pull, only once the router fails it and the feedback penalizes it.
    wire_nets: list[_WeightedNet] = []
    power_nets: list[_WeightedNet] = []
    for n in problem.nets:
        if problem.me_toggles.toggled(n.commodity):
            continue
        ids = [e.machine_id for e in n.endpoints]
        if n.commodity is Commodity.POWER:
            if penalties.get(n.id):
                power_nets.append((ids, penalties[n.id]))
        else:
            wire_nets.append((ids, 1.0 + penalties.get(n.id, 0.0)))
    # Directed source->sink pairs that COULD auto-output (simple 1->1 item/fluid nets, like the
    # router's own rule): the cost rewards each pair the current placement+orientation makes
    # face-adjacent, so orientation has a gradient toward enabling the free connection.
    auto_pairs = _auto_candidate_pairs(problem)
    # The anneal holds plain poses and hands back ``Placement``s only at the end (see Pose).
    current = [pose_of(p) for p in base.placements]
    adjacency = _net_adjacency(problem)
    # What the moves move: each group the seed laid as its column, and every other machine alone.
    units = _units(current, bodies, parallel_groups(problem), adjacency)
    # The immutable per-solve context every move + recreate helper shares, built once and threaded
    # instead of a dozen loose parameters: adjacency (LNS grows a *related* ruin cluster along the
    # net edges), and the per-machine net/power/auto views recreate ranks insertions with cheaply,
    # without a full cost recompute.
    ctx = _SearchContext(
        bodies=bodies,
        region=region,
        bounds=bounds,
        reserved=reserved,
        adjacency=adjacency,
        machine_nets=_machine_nets(problem, wire_nets),
        machine_power=_machine_nets(problem, power_nets),
        machine_auto=_machine_auto(problem, auto_pairs),
        weights=_OBJECTIVE_WEIGHTS[objective],
        nudges=all(body.sizes[Facing.NORTH] == (1, 1, 1) for body in bodies.values()),
        units=units,
        unit_adjacency=_unit_adjacency(units),
    )
    weights = ctx.weights
    rng = random.Random(seed)
    # ``current``'s occupied-cell set, maintained incrementally: relocate/swap test a candidate
    # against it (temporarily lifting the moved machine's own cells) instead of rebuilding the whole
    # set per proposal, and each accepted move folds in only its delta (see _apply_occupied_delta).
    occupied = _occupied(current, bodies)
    faces_penalty = face_penalties or {}
    current_cost = _cost(
        current,
        bodies,
        wire_nets,
        power_nets,
        auto_pairs,
        weights,
        bounds,
        reserved,
        faces_penalty,
    )
    best, best_cost = current, current_cost
    # Every state the walk accepts, with its cost and turn, so the cheapest one the crowding gate
    # passes can be picked once the walk is done (module docstring); ``best`` is the fallback.
    accepted: list[tuple[float, int, list[Pose]]] = [(current_cost, 0, current)]
    iters = min(_MAX_ITERS, max(_MIN_ITERS, _PER_MACHINE * len(current)))
    if max_iterations is not None:
        iters = min(iters, max_iterations)
    temp = _T0
    for _ in range(iters):
        if rng.random() < _P_LNS:
            cand = _ruin_and_recreate(current, ctx, rng)
        else:
            cand = _move(current, ctx, occupied, rng)
        if cand is not None:
            cand_cost = _cost(
                cand,
                bodies,
                wire_nets,
                power_nets,
                auto_pairs,
                weights,
                bounds,
                reserved,
                faces_penalty,
            )
            delta = cand_cost - current_cost
            if delta < 0 or rng.random() < math.exp(-delta / temp):
                _apply_occupied_delta(occupied, current, cand, bodies)
                current, current_cost = cand, cand_cost
                if current_cost < best_cost:
                    best, best_cost = current, current_cost
                accepted.append((current_cost, len(accepted), current))
        temp *= _ALPHA
    chosen = _cheapest_uncrowded(problem, accepted, best)
    return PlacementResult(placements=tuple(_placement(p) for p in chosen))


def _cheapest_uncrowded(
    problem: InputIR, accepted: list[tuple[float, int, list[Pose]]], fallback: list[Pose]
) -> list[Pose]:
    """The cheapest ``accepted`` state the crowding gate passes, else ``fallback``.

    Cheapest first and, among equal costs, the one the walk reached first: the state the anneal
    would keep if it asked the gate of every new low as it went. Asking afterwards finds that same
    state while skipping every state a cheaper passing one makes moot, and a state the walk came
    back to is asked about once. Over 24 anneals of each line that cut the gate's calls from 7265
    to 2654 on iron and from 1249 to 24 on parallel-sand, for identical placements.
    """
    asked: set[tuple[Pose, ...]] = set()
    for *_, state in sorted(accepted, key=lambda entry: (entry[0], entry[1])):
        key = tuple(state)
        if key in asked:
            continue
        asked.add(key)
        if not crowded_machines(problem, [_placement(p) for p in state]):
            return state
    return fallback


def _cells(pose: Pose, body: _Boxed) -> Iterator[Cell]:
    """Every cell ``pose``'s body (or unit) covers - ``occupied_cells`` for a pose and its body."""
    return box_cells(pose.cell, body.sizes[pose.orientation])


def _occupied(poses: list[Pose], bodies: Mapping[str, _Body]) -> set[Cell]:
    """Every cell the bodies at ``poses`` cover."""
    occupied: set[Cell] = set()
    for p in poses:
        occupied.update(_cells(p, bodies[p.machine_id]))
    return occupied


def _apply_occupied_delta(
    occupied: set[Cell],
    before: list[Pose],
    after: list[Pose],
    bodies: Mapping[str, _Body],
) -> None:
    """Fold an accepted move into ``occupied`` in place, instead of rebuilding it.

    ``before`` and ``after`` hold the same machines but not necessarily in the same order (an
    accepted LNS move lists the kept machines first and the reinserted ones last), so the diff is
    keyed by machine id: a machine whose **pose** changed vacates its old footprint and claims the
    new one. Pose, not cell: a reorient leaves the cell alone and still moves the cells a non-cubic
    machine covers, so keying on the cell would leave ``occupied`` drifting out of sync with the
    layout for the rest of the anneal. Every vacated cell is removed before any new cell is added,
    so two machines swapping into each other's footprints stay occupied. The result is exactly the
    full-rebuild occupied set of ``after`` - every layout the loop holds is overlap-free - only far
    cheaper to reach."""
    before_pose = {p.machine_id: p for p in before}
    removed: set[Cell] = set()
    added: set[Cell] = set()
    for new_p in after:
        old_p = before_pose[new_p.machine_id]
        if (old_p.cell, old_p.orientation) != (new_p.cell, new_p.orientation):
            body = bodies[new_p.machine_id]
            removed.update(_cells(old_p, body))
            added.update(_cells(new_p, body))
    occupied.difference_update(removed)
    occupied.update(added)


def _net_adjacency(problem: InputIR) -> dict[str, set[str]]:
    """Machine -> the machines it shares a net with (any commodity), for LNS related removal."""
    adj: dict[str, set[str]] = {m.id: set() for m in problem.machines}
    for net in problem.nets:
        ids = [e.machine_id for e in net.endpoints if e.machine_id in adj]
        for a in ids:
            for b in ids:
                if a != b:
                    adj[a].add(b)
    return adj


def _machine_nets(problem: InputIR, nets: list[_WeightedNet]) -> dict[str, list[_WeightedNet]]:
    """Machine -> the (routed) nets it belongs to, so LNS can score an insertion from only the
    machine's own nets instead of re-summing every net's HPWL."""
    by_machine: dict[str, list[_WeightedNet]] = {m.id: [] for m in problem.machines}
    for entry in nets:
        for mid in entry[0]:
            if mid in by_machine:
                by_machine[mid].append(entry)
    return by_machine


def _machine_auto(problem: InputIR, auto_pairs: list[_AutoPair]) -> dict[str, list[_AutoPair]]:
    """Machine -> the auto-output candidates it is an endpoint of (either side), so LNS can
    check just the machine's own pairs for the orientation-dependent auto-output reward."""
    by_machine: dict[str, list[_AutoPair]] = {m.id: [] for m in problem.machines}
    for pair in auto_pairs:
        for mid in (pair.source_id, pair.sink_id):
            if mid in by_machine:
                by_machine[mid].append(pair)
    return by_machine


@dataclass(frozen=True, slots=True)
class _AutoPair:
    """One directed auto-output candidate: which port on which machine, on each side.

    The ports are the part that used to be dropped. They decide which casing cells could host the
    two hatches, and therefore whether the connection is possible at all on a multiblock, so a
    (machine, machine) pair cannot answer the question the router actually asks (#107).

    Slotted rather than a ``NamedTuple``: the cost reads these fields by name per evaluation, and
    a named-tuple field is a descriptor the interpreter will not specialize the load of.
    """

    source_id: str
    source_port: str
    sink_id: str
    sink_port: str


def _auto_candidate_pairs(problem: InputIR) -> list[_AutoPair]:
    """Directed auto-output candidates for the 1->1 item/fluid nets: the router's own candidates
    (``router.auto.auto_candidates``; power/ME never auto-feed) less the shared ones.

    Which of them is *possible* is then asked of the router itself
    (``router.auto.auto_output_possible``), so the reward and the decision share one rule.

    **A producer of a net it shares with others is not rewarded** for standing against the net's
    consumer, though the router covers it when it does (#270). Measured, not assumed: rewarding it
    pulled ev-nitrobenzene's multiblock producers against their tanks at the expense of every
    other net, 6 better and 6 worse over 12 seeds with the median floor plus route cells from 847.5
    to 858, while leaving it to the router was 3 better, 7 the same and 2 worse. Such a pair saves
    only that producer's leg of a pipe the rest still need, not a whole pipe as a 1->1 pair does.
    """
    return [
        _AutoPair(c.source.machine_id, c.source.port_id, c.sink.machine_id, c.sink.port_id)
        for c in auto_candidates(problem)
        if not c.shared
    ]


def _cost(
    placements: list[Pose],
    bodies: Mapping[str, _Body],
    wire_nets: list[_WeightedNet],
    power_nets: list[_WeightedNet],
    auto_pairs: list[_AutoPair],
    weights: tuple[float, float],
    bounds: Size,
    reserved: set[Cell],
    face_penalties: Mapping[str, float],
) -> float:
    """Routing-aware cost: weighted item/fluid HPWL + compactness per the objective, minus an
    auto-output reward (the only orientation-dependent term, so reorient moves are not free),
    plus an MST pull for each feedback-penalized power net, plus a face-shortfall penalty.

    The shortfall term (:func:`_face_shortfall`) is what keeps the search from packing machines
    into a wall where they have no room left for their own connections - the failure #76 hit,
    where nine machines each needed three connections and a solid row left them two free cells.
    No routing order can rescue that, so it has to be priced here, where the geometry is chosen.

    Compactness is two independently weighted terms - ``weights`` is the objective's
    ``(footprint weight, volume weight)`` pair (:data:`_OBJECTIVE_WEIGHTS`): the floor area
    (x-span times z-span, shrunk by stacking) and the full bounding-box volume (shrunk by staying
    flat/cubic). ``power_nets`` holds only the nets the router failed and the solver penalized:
    each pays its penalty times the minimum-spanning-tree length over the member centers (a
    shared-amperage trunk is a tree), pulling the net tight until it routes. Un-penalized power
    nets cost nothing here - the real cable cost is judged on routed layouts by the solver, whose
    repair pass also places the sources themselves (module docstring)."""
    pos = {p.machine_id: p for p in placements}
    wire = 0.0
    for machine_ids, weight in wire_nets:
        centers = [_center(pos[mid], bodies[mid]) for mid in machine_ids if mid in pos]
        if len(centers) < 2:
            continue
        for axis in range(3):
            coords = [c[axis] for c in centers]
            wire += weight * (max(coords) - min(coords))

    cable = 0.0
    for machine_ids, weight in power_nets:
        centers = [_center(pos[mid], bodies[mid]) for mid in machine_ids if mid in pos]
        cable += weight * _mst_length(centers)

    # Bounding box from each footprint's two extreme corners (its origin and origin+size-1) rather
    # than enumerating every occupied cell: for axis-aligned footprints the min/max over the corners
    # equals the min/max over all their cells, so footprint area and volume are bit-identical - but
    # this is O(machines), not O(total cell volume), on the hottest path in the solver.
    # Rotated extents: a turned non-cubic machine reaches a different distance along each axis, so
    # the declared footprint would misreport the floor area and volume these objectives rank on.
    boxes = [(p.cell, bodies[p.machine_id].sizes[p.orientation]) for p in placements]
    min_x = min(c[0] for c, _ in boxes)
    max_x = max(c[0] + s[0] - 1 for c, s in boxes)
    min_y = min(c[1] for c, _ in boxes)
    max_y = max(c[1] + s[1] - 1 for c, s in boxes)
    min_z = min(c[2] for c, _ in boxes)
    max_z = max(c[2] + s[2] - 1 for c, s in boxes)
    footprint = (max_x - min_x + 1) * (max_z - min_z + 1)
    volume = footprint * (max_y - min_y + 1)

    auto = 0
    free_ports: set[tuple[str, str]] = set()
    for pair in auto_pairs:
        sp, tp = pos.get(pair.source_id), pos.get(pair.sink_id)
        if sp is None or tp is None:
            continue
        if auto_output_possible(
            sp,
            bodies[pair.source_id].machine,
            pair.source_port,
            tp,
            bodies[pair.sink_id].machine,
            pair.sink_port,
        ):
            auto += 1
            # This pair ejects straight across, so neither end needs a cell to dock a pipe on.
            free_ports.add((pair.source_id, pair.source_port))
            free_ports.add((pair.sink_id, pair.sink_port))
    faces = _face_shortfall(placements, bodies, bounds, reserved, free_ports, face_penalties)
    w_footprint, w_volume = weights
    return (
        _W_WIRE * wire
        + cable
        + w_footprint * footprint
        + w_volume * volume
        - _W_AUTO * auto
        + _W_FACES * faces
    )


def _dockable_cells(
    pose: Pose,
    body: _Body,
    occupied: set[Cell],
    bounds: Size,
    reserved: set[Cell],
) -> set[Cell]:
    """The free cells this machine could put a connection on, through the faces its ports may use.

    For an unpinned machine that is every face but the front; a pinned one (an Item Filter) goes
    through :func:`_pinned_demand` instead, since each of its ports has faces of its own.

    The *cells*, not the faces: two faces of one body cell reach two different cells, and two body
    cells can reach the same cell from different sides, so a cell set is the honest account of
    what a hatch could dock onto. Returned as the set rather than its size because neighbouring
    machines share candidates, and :func:`_face_shortfall` has to see the overlap to price it. Deliberately **generous**, the same way the validator's
    hatch-cell ceiling is (``validator.core._check_hatch_cells``): it ignores which slots accept
    which hatch kind, so it only ever over-counts. An over-count means the penalty fires strictly
    less often than it could - it never invents a shortfall that is not real.
    """
    ox, oy, oz = pose.cell
    rx, ry, rz = bounds
    cells: set[Cell] = set()
    for dx, dy, dz in body.shells[pose.orientation]:
        x, y, z = cand = (ox + dx, oy + dy, oz + dz)
        if cand in occupied or cand in reserved:
            continue
        if 0 <= x < rx and 0 <= y < ry and 0 <= z < rz:
            cells.add(cand)
    return cells


@cache
def _shell_offsets(sx: int, sy: int, sz: int, faces: frozenset[Facing]) -> tuple[Cell, ...]:
    """Offsets from a body's origin to the cells one step outside it through any of ``faces``.

    ``faces`` is what ``Machine.allowed_faces`` grants: every face but the front for an unpinned
    port, and a pinned port's own faces otherwise, so a filter's output shell is the one cell
    behind it. What :func:`_dockable_cells` scans, worked out once per rotated box rather than per
    call.
    Stepping every body cell through every face mostly lands back inside the body (on a 3x3x3, 90
    of 135 steps), and those cells are always occupied - by the machine itself - so they are dropped
    here instead of being built and hashed to be rejected. A cell two body cells reach turns up once.

    **The order is load-bearing.** It is each cell's first appearance in the face-major,
    body-cell-minor scan the dockable set used to be built from, so filtering this sequence inserts
    the same cells into the set in the same order, and the set iterates identically. That matters
    because :func:`_face_shortfall` sums floats over it: a different order can round differently,
    and a cost that moves in its last bit can flip an annealing decision.
    """
    seen: set[Cell] = set()
    shell: list[Cell] = []
    for face, (dx, dy, dz) in FACE_DELTAS.items():
        if face not in faces:  # unpinned: the front, which carries no I/O
            continue
        for bx, by, bz in product(range(sx), range(sy), range(sz)):
            x, y, z = cell = (bx + dx, by + dy, bz + dz)
            if 0 <= x < sx and 0 <= y < sy and 0 <= z < sz:
                continue  # inside the machine's own body, so never a free cell
            if cell not in seen:
                seen.add(cell)
                shell.append(cell)
    return tuple(shell)


def _face_shortfall(
    placements: list[Pose],
    bodies: Mapping[str, _Body],
    bounds: Size,
    reserved: set[Cell],
    free_ports: Collection[tuple[str, str]] = (),
    penalties: Mapping[str, float] = MappingProxyType({}),
) -> float:
    """Connections with nowhere to sit, summed over every machine: the unbuildability measure.

    A machine needs one free adjacent cell per connection (item in, item out, power in, ...) and
    its front face carries none of them (for a pinned port, only its own faces do: an Item Filter's
    output leaves by its back alone, so a filter walled at its back is short however much room it
    has elsewhere - :func:`_pinned_demand`). Pack it so that neighbours and region walls leave it
    fewer free cells than it has ports and the layout cannot be built - the routers then report
    whichever net happens to lose the race for the last face, which names the wrong machine and
    reads like a routing bug. Priced here instead, where the packing decision is actually made.

    ``penalties`` (machine id -> extra weight) scales one machine's own shortfall, so the solver
    can lean on exactly the machines its crowding gate named without re-weighting the whole layout
    (see :func:`optimize_placement`).

    ``free_ports`` are the ``(machine, port)`` pairs an auto-output covers on this placement, and
    they are **not** demand: the connection ejects straight from one machine into the next, so no
    cell is docked at either end. Leaving them in was a standing tax on exactly the tight,
    zero-pipe layouts this search is supposed to find - it charged the sand line's fully
    auto-fed chain a phantom shortfall of 3.00 on a layout the exact gate agrees is fine, which
    forced the weight down, which in turn forced the solver to escalate its way out on lines that
    were really crowded. The over-exemption is deliberate and safe: a single block has only one
    auto-output face, so a machine with two possible pairs has both exempted here though only one
    can really be free. That under-reports rather than invents, and the exact gate
    (``placement.feasibility``) is what actually rules.

    Counting each machine's candidates in isolation is not enough, and #76 is exactly where that
    shows: every machine can clear its own bar while two neighbours are counting **the same** free
    cell, which can host only one of them. So a contested cell is shared out rather than credited
    to each in full.

    Only machines with **no slack** contend for it. Dividing a cell among every machine that could
    reach it is far too pessimistic - a neighbour with five candidates for three ports is never
    going to fight over this one, and counting it as a rival manufactured a shortfall on layouts
    that were perfectly buildable, which cost the sand line a third of its compactness. A machine
    that already has more candidates than ports is therefore not a contender; one that is exactly
    at its limit is, because every cell it can reach is a cell it needs.

    That makes this a heuristic, not a decision procedure: the real question is whether a system
    of distinct representatives exists (each connection needing its own cell), and settling that
    means a matching, which is far too slow per annealing step. It is tuned to stay silent on
    layouts that build - a false shortfall costs compactness on every line - and to speak up on
    the crowding that #76 hit.
    """
    occupied = _occupied(placements, bodies)
    exempt = set(free_ports)
    # (machine, connections needing a cell, the cells they could take, pinned ports with none).
    demand: list[tuple[str, int, set[Cell], int]] = []
    for p in placements:
        body = bodies[p.machine_id]
        if body.port_shells is not None:
            needed, cells, stranded = _pinned_demand(
                p,
                body.port_ids,
                body.port_shells[p.orientation],
                occupied,
                bounds,
                reserved,
                exempt,
            )
            if needed:
                demand.append((p.machine_id, needed, cells, stranded))
            continue
        needed = sum(1 for port_id in body.port_ids if (p.machine_id, port_id) not in exempt)
        if needed:
            demand.append(
                (p.machine_id, needed, _dockable_cells(p, body, occupied, bounds, reserved), 0)
            )
    contenders: dict[Cell, int] = {}
    for _mid, needed, cells, _stranded in demand:
        if len(cells) > needed:
            continue  # has room to spare, so it will not be fighting anyone for a particular cell
        for cell in cells:
            contenders[cell] = contenders.get(cell, 0) + 1
    short = 0.0
    for mid, needed, cells, stranded in demand:
        share = sum(1.0 / max(1, contenders.get(cell, 0)) for cell in cells)
        gap = max(0.0, needed - share)
        if stranded > gap:  # a pinned port with no free cell of its own is short outright
            gap = float(stranded)
        short += (1.0 + penalties.get(mid, 0.0)) * gap
    return short


def _pinned_demand(
    pose: Pose,
    port_ids: tuple[str, ...],
    port_shells: tuple[tuple[Cell, ...], ...],
    occupied: set[Cell],
    bounds: Size,
    reserved: set[Cell],
    exempt: Collection[tuple[str, str]],
) -> tuple[int, set[Cell], int]:
    """A pinned machine's ``(connections needing a cell, the cells they could take, stranded)``.

    Each non-exempt port looks only through its own faces (``port_shells``, the entry of
    :attr:`_Body.port_shells` for the pose's facing, aligned with ``port_ids``), and the
    cells are the union of what they see: a filter fed through its front has that cell to offer,
    which the unpinned rule would never count. ``stranded`` counts the ports that see no free cell
    at all. The shared count cannot notice one - a filter walled at its back still has five free
    cells for its two connections - yet that port has nowhere to dock, and the exact gate says so
    (``crowded_machines`` docks through the same faces), so the term has to as well.
    """
    ox, oy, oz = pose.cell
    rx, ry, rz = bounds
    needed = stranded = 0
    cells: set[Cell] = set()
    for port_id, shell in zip(port_ids, port_shells, strict=True):
        if (pose.machine_id, port_id) in exempt:
            continue
        needed += 1
        seen_free = False
        for dx, dy, dz in shell:
            x, y, z = cand = (ox + dx, oy + dy, oz + dz)
            if cand in occupied or cand in reserved:
                continue
            if 0 <= x < rx and 0 <= y < ry and 0 <= z < rz:
                cells.add(cand)
                seen_free = True
        if not seen_free:
            stranded += 1
    return needed, cells, stranded


def _mst_length(centers: list[tuple[float, float, float]]) -> float:
    """Manhattan minimum-spanning-tree length over ``centers`` (Prim, O(n^2); n is a power net's
    member count, so small). The trunk-length proxy for a shared-amperage power net: the router
    grows the trunk as a tree, so its cable count scales with the Steiner tree over the members,
    which the MST approximates from above (within 1.5x for Manhattan metrics)."""
    n = len(centers)
    if n < 2:
        return 0.0
    dist = [_manhattan(centers[0], c) for c in centers]
    in_tree = [False] * n
    in_tree[0] = True
    total = 0.0
    for _ in range(n - 1):
        best_i = min((i for i in range(n) if not in_tree[i]), key=lambda i: dist[i])
        total += dist[best_i]
        in_tree[best_i] = True
        for i in range(n):
            if not in_tree[i]:
                d = _manhattan(centers[best_i], centers[i])
                if d < dist[i]:
                    dist[i] = d
    return total


def _manhattan(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def _center(p: Pose, body: _Body) -> tuple[float, float, float]:
    # Rotated: a wrong centroid feeds HPWL and the power MST, so it would steer the search.
    x, y, z = p.cell
    sx, sy, sz = body.sizes[p.orientation]
    return (x + sx / 2, y + sy / 2, z + sz / 2)


def _feed_ok(body: _Boxed, origin: Cell, orientation: Facing, bounds: Size) -> bool:
    """Whether placing ``body`` here honors the outside-front rule (trivially true for a machine
    without one): a power source's front is its reserved external-feed face, and a Crop Manager's
    faces its field outside the build (#282), so either must lie flush on the region boundary
    (``Machine.fronts_outside``; docs/DOMAIN.md; validator-enforced)."""
    return not body.fronts_outside or box_front_on_boundary(
        origin, body.sizes[orientation], orientation, bounds
    )


def _feed_orientation(body: _Boxed, origin: Cell, current: Facing, bounds: Size) -> Facing | None:
    """The orientation ``body`` should take at ``origin``: ``current`` when it is legal there,
    else the first option that puts a source's feed face back on the boundary, else ``None``
    (the move cannot place this machine here)."""
    if _feed_ok(body, origin, current, bounds):
        return current
    return next((o for o in body.orientations if _feed_ok(body, origin, o, bounds)), None)


def _move(
    placements: list[Pose],
    ctx: _SearchContext,
    occupied: set[Cell],
    rng: random.Random,
) -> list[Pose] | None:
    """Propose one small move of one unit; return a VALID candidate layout, or None if it could not
    be made.

    ``occupied`` is ``placements``' occupied-cell set (owned by the annealing loop): relocate and
    swap borrow it to test a candidate and restore it before returning, so the caller keeps the
    single incrementally-maintained copy. Each move lifts the unit's whole box, tests the box where
    it would land, and relays every member there (:func:`_relaid`), so a column stays rigid."""
    roll = rng.random()
    if roll < _P_RELOCATE:
        if ctx.nudges and roll >= _P_RELOCATE_WHEN_NUDGING:
            return _nudge(placements, ctx, occupied, rng)
        return _relocate(placements, ctx, occupied, rng)
    if roll < _P_SWAP:
        return _swap(placements, ctx, occupied, rng)
    return _reorient(placements, ctx, occupied, rng)


def _relocate(
    placements: list[Pose],
    ctx: _SearchContext,
    occupied: set[Cell],
    rng: random.Random,
) -> list[Pose] | None:
    units = _units_of(placements, ctx)
    unit = units[rng.randrange(len(units))]
    p = _unit_pose(unit, placements)
    # Lift the unit's own cells out of the shared occupied set so a candidate may reuse them; the
    # remainder is exactly the other units' cells (what ``others`` was). Restored in the finally.
    own = set(_cells(p, unit))
    occupied.difference_update(own)
    try:
        for _ in range(_RELOCATE_TRIES):
            origin = _rand_origin(unit, ctx.bounds, p.orientation, rng)
            if origin is None:
                return None
            # Orientation first: which cells a turned non-cubic machine covers depends on it, so
            # the fit test cannot run before it is known. _feed_orientation draws no randomness, so
            # hoisting it above the test leaves the RNG trajectory (and every existing layout)
            # exactly as it was.
            orientation = _feed_orientation(unit, origin, p.orientation, ctx.bounds)
            if orientation is None:
                continue  # a source relocated off the boundary: no legal feed face, keep trying
            size = unit.sizes[orientation]
            if not box_within(origin, size, ctx.bounds):
                continue  # the body would hang off the region: cheaper to reject than to expand
            cells = list(box_cells(origin, size))
            if ctx.reserved.isdisjoint(cells) and occupied.isdisjoint(cells):
                return _relaid(placements, (unit, origin, orientation))
        return None
    finally:
        occupied.update(own)


#: The six one-cell steps a nudge may take, in ``FACE_DELTAS`` order (the nudge shuffles them).
_NUDGE_STEPS = tuple(FACE_DELTAS.values())


def _nudge(
    placements: list[Pose],
    ctx: _SearchContext,
    occupied: set[Cell],
    rng: random.Random,
) -> list[Pose] | None:
    """Shift one unit by one cell, trying the six directions in a random order.

    The local move relocate is not (module docstring). The unit keeps its orientation, except
    that a power source turns back onto the boundary the way relocate turns it; the first direction
    that fits wins, and a unit boxed in on every side yields ``None``.
    """
    units = _units_of(placements, ctx)
    unit = units[rng.randrange(len(units))]
    p = _unit_pose(unit, placements)
    # As in _relocate: lift the unit's own cells so it may step into one it covers now.
    own = set(_cells(p, unit))
    occupied.difference_update(own)
    try:
        steps = list(_NUDGE_STEPS)
        rng.shuffle(steps)
        x, y, z = p.cell
        for dx, dy, dz in steps:
            origin = (x + dx, y + dy, z + dz)
            orientation = _feed_orientation(unit, origin, p.orientation, ctx.bounds)
            if orientation is None:
                continue  # a source stepped off the boundary with no feed-legal facing there
            size = unit.sizes[orientation]
            if not box_within(origin, size, ctx.bounds):
                continue
            cells = list(box_cells(origin, size))
            if ctx.reserved.isdisjoint(cells) and occupied.isdisjoint(cells):
                return _relaid(placements, (unit, origin, orientation))
        return None
    finally:
        occupied.update(own)


def _swap(
    placements: list[Pose],
    ctx: _SearchContext,
    occupied: set[Cell],
    rng: random.Random,
) -> list[Pose] | None:
    """Exchange two units' minimum corners, each keeping its facing where it may."""
    units = _units_of(placements, ctx)
    if len(units) < 2:
        return None  # one unit holds every machine: there is nothing to swap it with
    i, j = rng.sample(range(len(units)), 2)
    bi, bj = units[i], units[j]
    pi, pj = _unit_pose(bi, placements), _unit_pose(bj, placements)
    # Lift both units' current cells so the remainder is the other units' (what ``others`` was);
    # restored in the finally on every exit.
    own = set(_cells(pi, bi)) | set(_cells(pj, bj))
    occupied.difference_update(own)
    try:
        # Orientation first, as in _relocate: each swapped body's cells depend on the orientation
        # it lands with, and neither call draws randomness.
        oi = _feed_orientation(bi, pj.cell, pi.orientation, ctx.bounds)
        oj = _feed_orientation(bj, pi.cell, pj.orientation, ctx.bounds)
        if oi is None or oj is None:
            return None  # the swap would strand a source's feed face off the boundary
        # Each body's own in-region test first, off the boxes: a swap that lands a bigger machine
        # in a smaller one's slot usually fails right here, and then neither body is ever expanded.
        size_i, size_j = bi.sizes[oi], bj.sizes[oj]
        if not box_within(pj.cell, size_i, ctx.bounds) or not box_within(
            pi.cell, size_j, ctx.bounds
        ):
            return None
        moved = list(box_cells(pj.cell, size_i)) + list(box_cells(pi.cell, size_j))
        if (
            len(set(moved)) != len(moved)  # the two swapped bodies overlap each other
            or not ctx.reserved.isdisjoint(moved)
            or not occupied.isdisjoint(moved)
        ):
            return None
        return _relaid(placements, (bi, pj.cell, oi), (bj, pi.cell, oj))
    finally:
        occupied.update(own)


def _turn_fits(
    body: _Boxed, p: Pose, orientation: Facing, ctx: _SearchContext, occupied: set[Cell]
) -> bool:
    """Whether ``body`` (a machine or a unit) still fits at its own origin once turned to
    ``orientation``.

    Short-circuits the common case: a turn that leaves the extents alone cannot change which cells
    are covered, so every 1x1x1 block, every square-base multiblock and a column's half turn skip
    the test and the hot path is untouched. A column's quarter turn swaps 1x1xN for Nx1x1, so it
    takes the real test and needs room along the new axis.
    """
    size = body.sizes[orientation]
    if size == body.sizes[p.orientation]:
        return True
    if not box_within(p.cell, size, ctx.bounds):
        return False  # the turn swings the body out of the region; no need to expand either set
    own = set(_cells(p, body))
    cells = list(box_cells(p.cell, size))
    return ctx.reserved.isdisjoint(cells) and (occupied - own).isdisjoint(cells)


def _reorient(
    placements: list[Pose],
    ctx: _SearchContext,
    occupied: set[Cell],
    rng: random.Random,
) -> list[Pose] | None:
    """Turn one unit about its own minimum corner; a column turns whole, back to front again."""
    units = _units_of(placements, ctx)
    candidates: list[tuple[int, list[Facing]]] = []
    for k, unit in enumerate(units):
        p = _unit_pose(unit, placements)
        # A source only reorients among feed-legal facings (its front must stay on the boundary),
        # and ANY unit only among facings it still fits at. A quarter turn swaps a non-cubic
        # box's horizontal extents, so a turn can push it out of the region, onto a reserved
        # cell or into a neighbour. Nothing checked that while rotation was a no-op, and an
        # accepted state that broke it would violate this loop's overlap-free invariant in silence.
        alts = [
            o
            for o in unit.orientations
            if o != p.orientation
            and _feed_ok(unit, p.cell, o, ctx.bounds)
            and _turn_fits(unit, p, o, ctx, occupied)
        ]
        if alts:
            candidates.append((k, alts))
    if not candidates:
        return None
    k, alts = candidates[rng.randrange(len(candidates))]
    unit = units[k]
    return _relaid(placements, (unit, _unit_pose(unit, placements).cell, rng.choice(alts)))


def _ruin_and_recreate(
    placements: list[Pose],
    ctx: _SearchContext,
    rng: random.Random,
) -> list[Pose] | None:
    """Ruin a related cluster of units and greedily re-insert them (the LNS large move).

    Removes a net-connected cluster (2..``_MAX_RUIN`` units), then re-inserts each at the
    position + orientation minimising its marginal cost, preferring cells beside its already-placed
    net-neighbours; a column goes back whole (:func:`_marginal_unit_cost`). Every insertion is
    validity-checked, so the result is a complete, overlap/bounds/reserved-clean placement in the
    original machine order - or ``None`` if a unit cannot be re-placed at all (a freed origin can
    be retaken by an earlier re-insert), in which case the caller just skips the move.
    """
    units = _units_of(placements, ctx)
    n = len(units)
    if n < 2:
        return None
    adjacency = ctx.adjacency if ctx.unit_adjacency is None else ctx.unit_adjacency
    keys = [unit.key for unit in units]
    ruined = _related_cluster(keys, adjacency, rng.randint(2, min(n, _MAX_RUIN)), rng)
    lifted = {i for k in ruined for i in units[k].members}
    kept = [p for i, p in enumerate(placements) if i not in lifted]

    occupied = _occupied(kept, ctx.bodies)
    # One grid for the whole recreate, updated in step with `occupied` as each machine lands,
    # rather than rebuilt per insertion: `_best_insertion` only reads it.
    grid = _occupancy_grid(ctx.region, occupied, ctx.reserved)
    row, plane = ctx.bounds[0], ctx.bounds[0] * ctx.bounds[1]

    placed = list(kept)
    placed_keys = {key for k, key in enumerate(keys) if k not in ruined}
    # Re-insert the most-constrained first (most already-placed net-neighbours) so the strongest
    # pulls choose their spot before the freer units fill in around them.
    to_insert = sorted(
        (units[k] for k in sorted(ruined)),
        key=lambda unit: -_placed_neighbor_count(unit.key, adjacency, placed_keys),
    )
    for unit in to_insert:
        p = _unit_pose(unit, placements)
        if len(unit.members) == 1:
            spot = _best_insertion(p, placed, occupied, grid, ctx, rng)
        else:
            spot = _best_insertion(p, placed, occupied, grid, ctx, rng, unit=unit)
        if spot is None:
            return None  # could not re-place this unit; abandon the move, the loop skips it
        placed_keys.add(unit.key)
        for _, landed in _member_poses(unit, *spot):
            placed.append(landed)
            for cell in _cells(landed, ctx.bodies[landed.machine_id]):
                occupied.add(cell)
                grid[cell[0] + cell[1] * row + cell[2] * plane] = 1

    by_id = {p.machine_id: p for p in placed}
    return [by_id[p.machine_id] for p in placements]  # preserve the original ordering


def _related_cluster(
    keys: list[str], adjacency: Mapping[str, set[str]], k: int, rng: random.Random
) -> set[int]:
    """Indices of a net-connected cluster of ``k`` units (of ``keys``) grown from a random seed;
    padded with random units when the seed's net-component is smaller than ``k`` (e.g. isolated
    machines)."""
    idx_of = {key: i for i, key in enumerate(keys)}
    seed_i = rng.randrange(len(keys))
    chosen = {seed_i}
    frontier = [keys[seed_i]]
    while len(chosen) < k and frontier:
        neighbors = sorted(adjacency.get(frontier.pop(0), set()))
        rng.shuffle(neighbors)
        for nb in neighbors:
            if len(chosen) >= k:
                break
            j = idx_of.get(nb)
            if j is not None and j not in chosen:
                chosen.add(j)
                frontier.append(nb)
    if len(chosen) < k:  # the seed's component is smaller than k: pad with random other units
        rest = [i for i in range(len(keys)) if i not in chosen]
        rng.shuffle(rest)
        chosen.update(rest[: k - len(chosen)])
    return chosen


def _placed_neighbor_count(
    key: str, adjacency: Mapping[str, set[str]], placed_keys: set[str]
) -> int:
    """How many of unit ``key``'s net-neighbours are already placed (its re-insertion priority)."""
    return sum(1 for nb in adjacency.get(key, set()) if nb in placed_keys)


def _placed_invariants(
    machine_id: str, placed_pos: dict[str, Pose], ctx: _SearchContext
) -> tuple[list[_NetBox], list[_PowerAttach]]:
    """Everything :func:`_marginal_insertion_cost` needs that does NOT depend on the candidate.

    An insertion evaluates ~50 (origin, orientation) pairs, and the wire and cable terms were
    re-deriving the same already-placed centroids for every one of them. Both terms reduce to a
    fixed summary of the placed members - a bounding box for HPWL, the centroid list for the MST
    pull - so this computes each once per insertion and the candidate loop just reads them.
    """
    centroids = {mid: _center(q, ctx.bodies[mid]) for mid, q in placed_pos.items()}

    def placed_centroids(ids: list[str]) -> list[_Centroid]:
        return [centroids[mid] for mid in ids if mid != machine_id and mid in placed_pos]

    net_boxes = []
    for ids, weight in ctx.machine_nets[machine_id]:
        pts = placed_centroids(ids)
        if not pts:
            continue  # nothing placed to span yet: the net costs nothing until one of them is
        net_boxes.append(
            _NetBox(
                weight,
                min(c[0] for c in pts),
                max(c[0] for c in pts),
                min(c[1] for c in pts),
                max(c[1] for c in pts),
                min(c[2] for c in pts),
                max(c[2] for c in pts),
            )
        )
    power = [
        _PowerAttach(weight, placed_centroids(ids)) for ids, weight in ctx.machine_power[machine_id]
    ]
    return net_boxes, power


#: The centroids of a unit's members on one net, as offsets from the unit's origin: the least and
#: greatest along x, then y, then z.
_Reach = tuple[float, float, float, float, float, float]


class _UnitNet(NamedTuple):
    """One wire net of a unit: :class:`_NetBox` over the members placed OUTSIDE the unit, and the
    reach of the unit's own members on the net, by facing.

    The box is empty (each bound at its far infinity) when nothing outside is placed yet, so the
    candidate's own members span it alone. The reach is per net because siblings can sit on
    different nets (a ``power:MV#k`` split, ``adapter.power``), so only the members actually on a
    net may widen it.
    """

    weight: float
    x0: float
    x1: float
    y0: float
    y1: float
    z0: float
    z1: float
    reach: Mapping[Facing, _Reach]


class _UnitPower(NamedTuple):
    """One penalized power net of a unit: :class:`_PowerAttach` over the members placed outside
    it, and which of the unit's members (their index in the unit) are on the net."""

    weight: float
    centroids: list[_Centroid]
    members: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _UnitTerms:
    """Everything :func:`_marginal_unit_cost` needs that does not depend on the candidate: what
    :func:`_placed_invariants` is for one machine, gathered over a unit's members."""

    unit: _Unit
    nets: list[_UnitNet]
    power: list[_UnitPower]
    #: Each auto-output pair a member is on, once across the members: the pair, which member (its
    #: index in the unit) and whether that member is the pair's source.
    auto: list[tuple[_AutoPair, int, bool]]


def _unit_invariants(
    unit: _Unit, placed_pos: Mapping[str, Pose], ctx: _SearchContext
) -> _UnitTerms:
    """:func:`_placed_invariants` for a whole unit: each net and auto pair any member is on, once.

    A net two siblings share is one net, priced once over every member on it. The nets come off the
    per-machine views, which list the same net entry for every member, so they are told apart by
    identity; the auto pairs by value.
    """
    inside = set(unit.ids)
    centroids = {mid: _center(q, ctx.bodies[mid]) for mid, q in placed_pos.items()}

    def outside(ids: list[str]) -> list[_Centroid]:
        return [centroids[mid] for mid in ids if mid not in inside and mid in placed_pos]

    def on(ids: list[str]) -> tuple[int, ...]:
        return tuple(j for j, mid in enumerate(unit.ids) if mid in ids)

    nets: list[_UnitNet] = []
    power: list[_UnitPower] = []
    seen: set[int] = set()
    for mid in unit.ids:
        for entry in ctx.machine_nets[mid]:
            if id(entry) in seen:
                continue
            seen.add(id(entry))
            ids, weight = entry
            pts = outside(ids)
            members = on(ids)
            reach = {f: _reach(unit.offsets[f], members) for f in unit.orientations}
            if pts:
                xs, ys, zs = [c[0] for c in pts], [c[1] for c in pts], [c[2] for c in pts]
                box = (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))
            else:
                box = (math.inf, -math.inf, math.inf, -math.inf, math.inf, -math.inf)
            nets.append(_UnitNet(weight, *box, reach=reach))
        for entry in ctx.machine_power[mid]:
            if id(entry) not in seen:
                seen.add(id(entry))
                ids, weight = entry
                power.append(_UnitPower(weight, outside(ids), on(ids)))
    auto: dict[_AutoPair, tuple[int, bool]] = {}
    for j, mid in enumerate(unit.ids):
        for pair in ctx.machine_auto[mid]:
            auto.setdefault(pair, (j, pair.source_id == mid))
    return _UnitTerms(unit, nets, power, [(pair, j, src) for pair, (j, src) in auto.items()])


def _reach(offsets: tuple[Cell, ...], members: tuple[int, ...]) -> _Reach:
    """The :data:`_Reach` of ``members`` at ``offsets``. Every member is a single block, so its
    centroid is half a cell past its origin on each axis (as :func:`_center` has it)."""
    xs = [offsets[j][0] + 0.5 for j in members]
    ys = [offsets[j][1] + 0.5 for j in members]
    zs = [offsets[j][2] + 0.5 for j in members]
    return (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))


def _placed_extent(placed: list[Pose], ctx: _SearchContext) -> _Extent | None:
    """The :class:`_Extent` of ``placed``, or ``None`` when nothing is placed yet.

    Corner-based like ``_cost``'s own box, so the two agree on what "the build" measures.
    """
    if not placed:
        return None
    boxes = [(p.cell, ctx.bodies[p.machine_id].sizes[p.orientation]) for p in placed]
    return _Extent(
        min(c[0] for c, _ in boxes),
        max(c[0] + s[0] - 1 for c, s in boxes),
        min(c[1] for c, _ in boxes),
        max(c[1] + s[1] - 1 for c, s in boxes),
        min(c[2] for c, _ in boxes),
        max(c[2] + s[2] - 1 for c, s in boxes),
    )


def _occupancy_grid(region: CellBox, occupied: set[Cell], reserved: set[Cell]) -> bytearray:
    """``occupied | reserved`` as a flat byte per region cell, indexed ``x + y*sx + z*sx*sy``.

    The fit test in :func:`_best_insertion` asked ``isdisjoint`` of a freshly materialised cell
    list, which meant building one tuple per cell of the body per candidate. That is cheap when a
    machine is 1x1x1 and ruinous when the real dataset gives it a 7x7x7 body: it was 90% of all
    ``occupied_cells`` yields in a solve and the single hottest line in the solver. A byte grid
    answers the same question by indexing, with no allocation and no hashing per candidate.

    Unpadded on purpose. ``box_within`` already gates every test with six comparisons on the
    rotated box's corners, so an index built from a passing origin is always in range; a padded
    border would buy a bounds check that has already been paid for.
    """
    sx, sy, sz = region.sx, region.sy, region.sz
    plane = sx * sy
    grid = bytearray(plane * sz)
    for x, y, z in occupied:
        grid[x + y * sx + z * plane] = 1  # every placed body cleared box_within to get here
    for x, y, z in reserved:  # caller-supplied, so this one is guarded
        if 0 <= x < sx and 0 <= y < sy and 0 <= z < sz:
            grid[x + y * sx + z * plane] = 1
    return grid


def _box_offsets(size: Size, bounds: Size) -> tuple[int, ...]:
    """Flat offsets from an origin to every cell a box of the (rotated) ``size`` covers, in a
    region of extents ``bounds``."""
    sx, sy, sz = size
    row, plane = bounds[0], bounds[0] * bounds[1]
    return tuple(
        dx + dy * row + dz * plane for dz in range(sz) for dy in range(sy) for dx in range(sx)
    )


def _best_insertion(
    p: Pose,
    placed: list[Pose],
    occupied: set[Cell],
    grid: bytearray,
    ctx: _SearchContext,
    rng: random.Random,
    unit: _Unit | None = None,
) -> tuple[Cell, Facing] | None:
    """The valid (origin, orientation) for ``p``'s machine that minimises its *marginal* cost, over
    candidate cells beside its placed net-neighbours plus a few random ones; falls back to any
    first-fit free slot, or ``None`` if the machine cannot be placed at all.

    Ranking uses only the terms that depend on this machine (its nets + auto pairs), so an insertion
    costs O(machine degree), not a full O(all nets) recompute - the loop's ``_cost`` still gates
    acceptance globally.

    ``unit`` is a group's column to insert whole (a unit of one is passed as its machine alone):
    ``p`` is then the unit's pose, the candidates are for its box, the price is
    :func:`_marginal_unit_cost`, and the last resort is :func:`_fit_unit`.
    """
    body = ctx.bodies[p.machine_id]
    box: _Boxed = body if unit is None else unit
    placed_pos = {q.machine_id: q for q in placed}
    if unit is None:
        net_boxes, power_attach = _placed_invariants(p.machine_id, placed_pos, ctx)
        terms = None
    else:
        net_boxes, power_attach = [], []
        terms = _unit_invariants(unit, placed_pos, ctx)
    extent = _placed_extent(placed, ctx)
    bounds = ctx.bounds
    offsets_by_size: dict[Size, tuple[int, ...]] = {}
    row, plane = bounds[0], bounds[0] * bounds[1]
    best: tuple[Cell, Facing] | None = None
    best_cost = math.inf
    neighbor_ids = None if unit is None else unit.neighbors
    for origin in _candidate_origins(p, box, placed, ctx, rng, neighbor_ids):
        # The fit test moved inside the orientation loop, because which cells the machine covers
        # depends on how it is turned - but it depends ONLY on the rotated box, and four facings
        # yield at most two of those. Memoizing per box keeps this at one test per origin for a
        # square-base machine (every machine in both shipped examples) instead of four.
        fits: dict[Size, bool] = {}
        for orientation in box.orientations:
            if not _feed_ok(box, origin, orientation, bounds):
                continue  # a source's feed face must stay on the boundary
            size = box.sizes[orientation]
            ok = fits.get(size)
            if ok is None:
                # In-region off the box first: this is the hottest fit test in the solve (one per
                # candidate origin, and _candidate_origins offers plenty that hang off the region),
                # and only a candidate that clears it is worth expanding into cells.
                ok = box_within(origin, size, bounds)
                if ok:
                    offsets = offsets_by_size.get(size)
                    if offsets is None:
                        offsets = offsets_by_size[size] = _box_offsets(size, bounds)
                    x, y, z = origin
                    base = x + y * row + z * plane
                    for off in offsets:
                        if grid[base + off]:
                            ok = False
                            break
                fits[size] = ok
            if not ok:
                continue
            if terms is None:
                cost = _marginal_insertion_cost(
                    p.machine_id,
                    origin,
                    orientation,
                    body,
                    placed_pos,
                    net_boxes,
                    power_attach,
                    ctx,
                    bound=best_cost,
                    extent=extent,
                )
            else:
                cost = _marginal_unit_cost(
                    terms, origin, orientation, placed_pos, ctx, bound=best_cost, extent=extent
                )
            if cost < best_cost:
                best_cost, best = cost, (origin, orientation)
    if best is not None:
        return best
    if unit is not None:
        return _fit_unit(unit, ctx, occupied)
    # Last resort: any free slot in first-fit order (a source additionally requires a slot +
    # orientation with its feed face on the boundary - the same rule the constructive seed used).
    fit = _fit(body.machine, ctx.region, occupied | ctx.reserved)
    return None if fit is None else (fit[0].as_tuple(), fit[1])


def _fit_unit(unit: _Unit, ctx: _SearchContext, occupied: set[Cell]) -> tuple[Cell, Facing] | None:
    """A column's last resort, as :func:`~gtnh_solver.placement.constructive._fit` is a machine's:
    the first origin in first-fit scan order (the floor first, then rows, then up) where the column
    fits at one of its facings, tried in order. None when it fits nowhere."""
    blocked = occupied | ctx.reserved
    rx, ry, rz = ctx.bounds
    for y in range(ry):
        for z in range(rz):
            for x in range(rx):
                for orientation in unit.orientations:
                    size = unit.sizes[orientation]
                    if box_within((x, y, z), size, ctx.bounds) and blocked.isdisjoint(
                        box_cells((x, y, z), size)
                    ):
                        return (x, y, z), orientation
    return None


def _marginal_insertion_cost(
    machine_id: str,
    origin: Cell,
    orientation: Facing,
    body: _Body,
    placed_pos: dict[str, Pose],
    net_boxes: list[_NetBox],
    power_attach: list[_PowerAttach],
    ctx: _SearchContext,
    bound: float = math.inf,
    extent: _Extent | None = None,
) -> float:
    """The cost terms that change with where ``machine_id`` goes: the weighted HPWL of its own
    item/fluid nets over their already-placed members (this candidate included), plus for each of
    its feedback-penalized power nets the Manhattan distance to the nearest placed member (the
    increment Prim would pay to attach this machine to the trunk MST), plus how much the candidate
    grows the build (``extent``, below), minus the auto-output reward for the pairs the candidate
    makes face-adjacent. A cheap marginal proxy of ``_cost`` for ranking candidate insertions; the
    annealing loop's full ``_cost`` still gates acceptance.

    **The growth term is what makes the LNS move compact anything** (#254). ``extent`` is the box
    the machines already placed span, and a candidate pays the objective's own compactness
    weights on whatever it adds to that box's floor area and volume, the same terms ``_cost``
    ranks on. Without it an insertion that extends the build costs the same as one tucked inside
    it, so the recreate put a machine wherever its nets were cheapest, often at the far end of the
    strip the first-fit start lays down, and the loop's acceptance could only reject that. Measured
    over eight solve seeds, ev-nitrobenzene's median floor went from 508.5 to 441 with this term,
    with fewer pipe and cable blocks, and no example line lost a valid seed. ``None`` (nothing
    placed yet) adds nothing, since the first machine back defines the box.

    ``net_boxes`` and ``power_attach`` come from :func:`_placed_invariants` and summarise the
    members that are already placed - the part of both terms that is the same for every candidate.
    Only the auto reward is irreducibly per-candidate: face adjacency depends on this origin and
    orientation, which is what the term is there to measure.

    ``bound`` is the incumbent's cost, and returning ``inf`` above it is a pure early-out: the
    caller only keeps a strictly cheaper candidate, so a candidate that provably cannot get there
    need not be scored exactly. **The bound has to account for the auto reward being subtracted.**
    The running ``wire + cable + growth`` total is an *upper* bound on the result, not a lower one, so
    comparing it against ``bound`` directly would discard candidates the reward would have made
    best. Subtracting the largest reward still available - every remaining pair scoring - makes the
    test admissible, and the layouts it produces are identical to scoring every candidate in full.
    What it skips is the auto term entirely: the ``Pose`` this function would have to build to ask
    with, and a ``router.auto.auto_output_possible`` call per pair. That rule got substantially
    dearer when it started asking the question the router actually answers (#107), which is what
    makes the early-out worth having - it takes a nitrobenzene solve from 6.32s to 4.36s."""
    x, y, z = origin
    sx, sy, sz = body.sizes[orientation]  # same centroid rule as _center
    cx = x + sx / 2
    cy = y + sy / 2
    cz = z + sz / 2
    # Widening the precomputed box with this candidate's centroid *is* the HPWL, so the span is
    # identical to taking min/max over the members plus the candidate - the same two operands,
    # just not rebuilt per candidate. Inlined conditionals rather than min()/max(): this runs
    # ~236k times a solve and the call overhead was measurable.
    wire = 0.0
    for weight, x0, x1, y0, y1, z0, z1 in net_boxes:
        wire += weight * (
            (cx if cx > x1 else x1)
            - (cx if cx < x0 else x0)
            + (cy if cy > y1 else y1)
            - (cy if cy < y0 else y0)
            + (cz if cz > z1 else z1)
            - (cz if cz < z0 else z0)
        )
    cable = 0.0
    for weight, centroids in power_attach:
        attach = min((_manhattan((cx, cy, cz), c) for c in centroids), default=0.0)
        cable += weight * attach
    growth = 0.0
    if extent is not None:
        # The placed box with this candidate's corners folded in, against the box without it.
        ex0, ex1, ey0, ey1, ez0, ez1 = extent
        wx, wy, wz = ex1 - ex0 + 1, ey1 - ey0 + 1, ez1 - ez0 + 1
        nx = (x + sx - 1 if x + sx - 1 > ex1 else ex1) - (x if x < ex0 else ex0) + 1
        ny = (y + sy - 1 if y + sy - 1 > ey1 else ey1) - (y if y < ey0 else ey0) + 1
        nz = (z + sz - 1 if z + sz - 1 > ez1 else ez1) - (z if z < ez0 else ez0) + 1
        w_footprint, w_volume = ctx.weights
        growth = w_footprint * (nx * nz - wx * wz) + w_volume * (nx * ny * nz - wx * wy * wz)
    # Summed once, in this order, for both uses below: the early-out and the result must agree.
    rest = cable + growth
    pairs = ctx.machine_auto[machine_id]
    if _W_WIRE * wire + rest - _W_AUTO * len(pairs) >= bound:
        return math.inf  # even every pair scoring cannot beat the incumbent
    auto = 0
    here = Pose(machine_id, origin, orientation)
    m = body.machine
    for pair in pairs:
        is_source = pair.source_id == machine_id
        other = pair.sink_id if is_source else pair.source_id
        op = placed_pos.get(other)
        if op is None:
            continue
        om = ctx.bodies[other].machine
        if is_source:
            possible = auto_output_possible(here, m, pair.source_port, op, om, pair.sink_port)
        else:
            possible = auto_output_possible(op, om, pair.source_port, here, m, pair.sink_port)
        if possible:
            auto += 1
    return _W_WIRE * wire + rest - _W_AUTO * auto


def _marginal_unit_cost(
    terms: _UnitTerms,
    origin: Cell,
    orientation: Facing,
    placed_pos: Mapping[str, Pose],
    ctx: _SearchContext,
    bound: float = math.inf,
    extent: _Extent | None = None,
) -> float:
    """:func:`_marginal_insertion_cost` for a whole column at ``origin`` facing ``orientation``.

    The same four terms, over the members: each net's HPWL widened by the centroids of the members
    on it (only those: siblings can sit on different power nets), the attach distance of the
    nearest member on each penalized power net, the growth of the build by the column's box, and
    the auto-output reward of every pair a member is on, each pair once. The early-out subtracts
    that distinct pair count, the most the reward can still take off, so it stays admissible. A
    pair whose other end is a member too is never scored (the other end is not placed); it only
    loosens the bound.
    """
    unit = terms.unit
    x, y, z = origin
    wire = 0.0
    for weight, x0, x1, y0, y1, z0, z1, reach in terms.nets:
        lx, hx, ly, hy, lz, hz = reach[orientation]
        wire += weight * (
            max(x + hx, x1)
            - min(x + lx, x0)
            + max(y + hy, y1)
            - min(y + ly, y0)
            + max(z + hz, z1)
            - min(z + lz, z0)
        )
    offsets = unit.offsets[orientation]
    cable = 0.0
    if terms.power:
        centers = [(x + dx + 0.5, y + dy + 0.5, z + dz + 0.5) for dx, dy, dz in offsets]
        for weight, centroids, members in terms.power:
            nearest = (_manhattan(centers[j], c) for j in members for c in centroids)
            cable += weight * min(nearest, default=0.0)
    growth = 0.0
    if extent is not None:
        # The growth term exactly as _marginal_insertion_cost prices it, on the column's box.
        sx, sy, sz = unit.sizes[orientation]
        ex0, ex1, ey0, ey1, ez0, ez1 = extent
        wx, wy, wz = ex1 - ex0 + 1, ey1 - ey0 + 1, ez1 - ez0 + 1
        nx = max(x + sx - 1, ex1) - min(x, ex0) + 1
        ny = max(y + sy - 1, ey1) - min(y, ey0) + 1
        nz = max(z + sz - 1, ez1) - min(z, ez0) + 1
        w_footprint, w_volume = ctx.weights
        growth = w_footprint * (nx * nz - wx * wz) + w_volume * (nx * ny * nz - wx * wy * wz)
    rest = cable + growth
    if _W_WIRE * wire + rest - _W_AUTO * len(terms.auto) >= bound:
        return math.inf  # even every pair scoring cannot beat the incumbent
    auto = 0
    for pair, j, is_source in terms.auto:
        other = pair.sink_id if is_source else pair.source_id
        op = placed_pos.get(other)
        if op is None:
            continue
        dx, dy, dz = offsets[j]
        here = Pose(unit.ids[j], (x + dx, y + dy, z + dz), orientation)
        m, om = ctx.bodies[unit.ids[j]].machine, ctx.bodies[other].machine
        if is_source:
            possible = auto_output_possible(here, m, pair.source_port, op, om, pair.sink_port)
        else:
            possible = auto_output_possible(op, om, pair.source_port, here, m, pair.sink_port)
        if possible:
            auto += 1
    return _W_WIRE * wire + rest - _W_AUTO * auto


def _candidate_origins(
    p: Pose,
    body: _Boxed,
    placed: list[Pose],
    ctx: _SearchContext,
    rng: random.Random,
    neighbor_ids: Collection[str] | None = None,
) -> list[Cell]:
    """Origins to try inserting ``body`` at: its freed origin, the cells face-adjacent to each
    placed net-neighbour (to cluster / auto-output), and a few random free origins - deduped,
    in-region. ``neighbor_ids`` are a unit's net-neighbours; None reads ``p``'s machine's."""
    if neighbor_ids is None:
        neighbor_ids = ctx.adjacency.get(p.machine_id, set())
    rx, ry, rz = ctx.bounds
    origins: list[Cell] = []
    seen: set[Cell] = set()

    def add(c: Cell) -> None:
        x, y, z = c
        if c not in seen and 0 <= x < rx and 0 <= y < ry and 0 <= z < rz:
            seen.add(c)
            origins.append(c)

    add(p.cell)  # freed origin (may be retaken; validity checked by caller)
    for q in placed:
        if len(origins) >= _MAX_CANDIDATES:
            break  # enough neighbour-adjacent sites; keep recreate cheap
        if q.machine_id in neighbor_ids:
            for bx, by, bz in _cells(q, ctx.bodies[q.machine_id]):
                for dx, dy, dz in _FACE_DELTAS:
                    add((bx + dx, by + dy, bz + dz))
    for _ in range(_LNS_RANDOM_CANDIDATES):
        origin = _rand_origin(body, ctx.bounds, p.orientation, rng)
        if origin is not None:
            add(origin)
    return origins


def _rand_origin(
    body: _Boxed, bounds: Size, orientation: Facing, rng: random.Random
) -> Cell | None:
    # The rotated extents, not the declared ones: an east-facing 5x1x2 needs 2 of x and 5 of z, so
    # bounding by the declared box would both reject origins that fit and offer origins that do
    # not - and the draws below consume randomness, so a wrong bound shifts every later draw.
    sx, sy, sz = body.sizes[orientation]
    rx, ry, rz = bounds
    if sx > rx or sy > ry or sz > rz:
        return None
    # A tuple display evaluates left to right, so the draws come x, y, z, as they always have.
    return (rng.randrange(rx - sx + 1), rng.randrange(ry - sy + 1), rng.randrange(rz - sz + 1))
