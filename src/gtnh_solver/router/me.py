"""router.me - the ME (AE2) cable-tree router (#334).

A net that rides ME is moved by an AE2 network instead of a pipe: each machine port on it has an ME
device to build (an AE2 part on a cable, or one of GT's own ME hatches; ``Machine.me_endpoints``),
and this router lays the cable that joins those devices to where the network's channels come from.
It is pure and not wired in yet (the end-to-end build, #335, does that). The validator's ME gate
(#333) re-derives every rule below from the blocks returned here, never from this bookkeeping. The
rules are cited in ``docs/spikes/329-me-ae2.md`` ("spike N" below) and listed in docs/DOMAIN.md.

**Each network is a forest with one root per tree**, at the place its channels enter:

- **attached**: each attach stub, a dense cable on the region's edge that the player's main network
  feeds through its front. One tree per stub, so at most 32 channels a stub (spike 1.1);
- **controller** (a subnet with one): every free cell beside the controller may root a tree of its
  own. A controller carries no channel itself: each cable touching it is a root (spike 2.2);
- **ad hoc** (a subnet without one): one tree, seeded at the first device's dock (at its link, when
  it has no device). AE ignores cable capacity here and counts devices grid-wide, so at most 8
  (spike 2.6).

The leaves are the devices, plus each ``link`` (a cable at the region's edge whose storage bus faces
the main network; once laid, a cable a leg may grow on from like any other) and each ``acceptor``
(a block the tree must touch once, carrying no channel). They are grown onto the forest one at a
time, the way ``router.power`` grows a trunk::

      main network (outside the region)
            | front
         [STUB] dense, 6 ........................ a root: the channels of its whole subtree
            |
          [ d ] dense, 6 ---- [ s ] smart, 2 ]--> machine B (B's export bus, facing B)
            |                        ]--> machine C (a TAP: C's interface shares B's cell)
          [ s ] smart, 4 ]--> hatch of multiblock D (an interface facing D's normal output bus)
            |
          [ s ] smart, 3 <-- GT ME hatch of multiblock E, on E's casing, front facing the cable
            |
          [ s ] smart, 2 ]--> machine F ...

    each leaf, in order:
      dock candidates   ``_grid.dock_candidates``: the faces its ports allow, hatch slots, claims
           |
           +-- a candidate is already a cable that can take it ......... TAP: no new cable
           +-- else a multi-goal A* leg from every cable with a channel
           |   to spare (and a subnet's controller) to a free candidate ... EXTEND, under the HALO
           +-- else none to dock on (face_reachability), no way there (routing), or only through
               cable already full (me_channels): rip up and retry, cable that ran full first taking
               no part, the failed leaves first; still failing, the network is refused

**The halo is the rule a GT pipe never needed.** A GT pipe connects only where the player wires it,
but an AE cable joins EVERY compatible neighbour on its own (spike 3). So a new cable may touch no
AE block of another network (its cable, an attach stub or link, a controller or acceptor), and of
its own network only the one cell it grows from: a second touch would give AE a cycle the router
never laid, and then which device gets a channel depends on AE's visiting order (spike 2.5). The
halo is kept between EVERY two networks, whatever their colours. Colour does keep two subnets'
cables apart, but not a subnet from a Fluix network, and not from a block device (a controller or
acceptor joins any colour); one uniform rule costs a cell of space and leaves nothing for the
validator to find. The player's main network outside the region is the builder's to keep clear.

**Channels are counted as AE counts them on a tree** (spike 2.4): a cable's ``me_channels`` is the
number of channel devices beyond it, itself included, and a tree that keeps every count within
capacity gives every device its channel, in any order. A part needs a smart cable (dense takes no
part, ``dataset.me.PART_CABLES``), so a cell holding one carries at most 8; any other cell carries
32 as dense cable. Growth refuses a leg or a tap that would push a cell past that, so the kinds are
read off at the end: an attach stub dense, a cell holding a part or carrying 8 or fewer smart, the
rest dense. A GT ME hatch is a leaf on the cable its front faces (any kind: it is no part), and a
GT ME hatch, a part and a link's storage bus are a channel each; a controller and an acceptor none.

Networks are laid in problem order, each one's cable then foreign to the next, and a network that
fails is retried first on the next pass (the failed-first rip-up/reroute of ``router.power``). The
budgets a network can never meet are refused before anything is laid: an ad-hoc network over 8
devices (``me_adhoc``), an attached one over its channel budget (``me_channel_budget``) or over 32
a stub (``me_channels``), and infrastructure no tree can join (``me_infrastructure``: blocks of
two networks touching, a stub on a subnet or a controller on an attached network, controllers AE
would not run as one cluster).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from gtnh_solver.dataset.me import ADHOC_MAX_DEVICES, CABLE_CAPACITY
from gtnh_solver.ir import (
    AEColor,
    CellBox,
    Facing,
    Infeasibility,
    InputIR,
    Machine,
    MECableCell,
    MECableKind,
    MEDeviceKind,
    MEEndpoint,
    MEMode,
    MENetworkLayout,
    MENetworkSpec,
    MEPlacedDevice,
    MERole,
    Placement,
    Terminal,
)
from gtnh_solver.ir.geometry import FACE_DELTAS, OPPOSITE_FACE, Cell, in_region, occupied_cells
from gtnh_solver.ir.nets import placement_index

from ._grid import (
    NEIGHBORS,
    astar_multi,
    body_cell,
    claim_key,
    coord,
    dock_candidates,
    filter_backs,
    manhattan,
    obstacle_cells,
)

#: Backstop on rip-up/reroute passes, per network and across networks; a repeated failure set
#: usually stops the retry first (``router.power``'s bound).
_MAX_PASSES = 16
#: Most channels through a cable that holds a part: parts sit only on smart cable (spike 3).
_PART_CAPACITY = CABLE_CAPACITY[MECableKind.SMART]
#: Most channels through any other cable this router lays, as dense cable (spike 1.1).
_TRUNK_CAPACITY = CABLE_CAPACITY[MECableKind.DENSE]
#: The parts a link may carry on its front: a storage bus, for items or for fluids.
_STORAGE_BUSES = frozenset({MEDeviceKind.STORAGE_BUS, MEDeviceKind.FLUID_STORAGE_BUS})


@dataclass(frozen=True)
class MERouteResult:
    """ME router output: each network's cable and devices, or a partial set plus why it stalled.

    ``networks`` are the networks laid whole, in problem order. A network with any leaf left
    unreached is laid not at all, since a part of one would only wall the others in; it is listed in
    ``failed_networks`` (problem order), and ``infeasibility`` carries the first one's reason, the
    shape ``PowerRouteResult`` has. ``terminals`` hold one per multiblock endpoint of a laid
    network: the dock cell and face of its hatch (a GT ME hatch, or the normal hatch an AE2 part
    stands in front of), on the endpoint's first port, which ``router.hatches.place_hatches`` turns
    into the hatch block like any route's terminal.
    """

    networks: tuple[MENetworkLayout, ...] = ()
    terminals: tuple[Terminal, ...] = ()
    infeasibility: Infeasibility | None = None
    failed_networks: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True iff every ME network with something to build was laid."""
        return self.infeasibility is None


def route_me(
    problem: InputIR,
    placements: Sequence[Placement],
    *,
    extra_obstacles: Collection[Cell] = (),
    claimed_cells: Mapping[str, Collection[Cell]] = MappingProxyType({}),
    endpoint_faces: Mapping[tuple[str, str], Collection[Facing]] = MappingProxyType({}),
) -> MERouteResult:
    """Lay each of ``problem``'s ME networks as a forest of AE2 cable over ``placements``.

    ``extra_obstacles`` are cells other routes hold (the pipes and power cables laid first), and
    ``claimed_cells`` what each machine's other connections already spent, read as
    ``_grid.claim_key`` reads it (a multiblock's casing cells, a single block's dock cells), the two
    inputs ``route_power`` takes. ``endpoint_faces`` narrows where a device may dock, by
    ``(machine id, endpoint id)``, to the machine faces given, on top of what its ports allow: how
    the caller pins a single block's interface to the face its other auto-outputs leave by, since
    a single block auto-outputs through one face (docs/DOMAIN.md). Deterministic for a given input.
    """
    machines = {m.id: m for m in problem.machines}
    by_machine = placement_index(placements)
    infrastructure = _infrastructure_cells(problem, by_machine)
    prepared: dict[str, _Network | Infeasibility] = {}
    for spec in problem.me.networks:
        network = _prepare(problem, spec, by_machine, infrastructure.get(spec.id, frozenset()))
        if network is not None:
            prepared[spec.id] = network
    for a, b in _infrastructure_clashes(infrastructure):
        for network_id in (a, b):
            if isinstance(prepared.get(network_id), _Network):
                prepared[network_id] = _clash(network_id, a if network_id == b else b)
    if not prepared:
        return MERouteResult()

    base = (
        obstacle_cells(problem, placements, machines)
        | set(filter_backs(problem.nets, placements, machines))
        | set(extra_obstacles)
        | _front_guards(problem, by_machine)
    )
    ids = list(prepared)
    order = ids
    seen: set[frozenset[str]] = set()
    built: dict[str, _Built] = {}
    failures: dict[str, Infeasibility] = {}
    for _ in range(_MAX_PASSES):
        built, failures = _route_pass(
            order,
            prepared,
            infrastructure,
            problem.bounding_region,
            base,
            claimed_cells,
            endpoint_faces,
        )
        if not failures or frozenset(failures) in seen:
            break
        seen.add(frozenset(failures))
        order = [i for i in ids if i in failures] + [i for i in ids if i not in failures]

    failed = tuple(i for i in ids if i in failures)
    return MERouteResult(
        networks=tuple(built[i].layout for i in ids if i in built),
        terminals=tuple(t for i in ids if i in built for t in built[i].terminals),
        infeasibility=failures[failed[0]] if failed else None,
        failed_networks=failed,
    )


# --- what each network asks for ------------------------------------------------------------------


@dataclass(frozen=True)
class _Leaf:
    """One thing a network's forest must reach: a machine's ME device, a link, or an acceptor.

    ``endpoint`` is the device built (a link's is its storage bus); an acceptor has none.
    """

    key: str
    label: str
    machine: Machine
    placement: Placement | None
    endpoint: MEEndpoint | None = None
    role: MERole | None = None

    @property
    def gt_hatch(self) -> bool:
        """Whether the device is one of GT's ME hatches, which is no part but a hatch itself."""
        return self.endpoint is not None and self.endpoint.device.gt_mid is not None

    @property
    def multiblock(self) -> bool:
        """Whether the device takes a hatch slot: a GT ME hatch, or a part before a normal hatch."""
        return self.gt_hatch or (self.endpoint is not None and self.endpoint.hatch_kind is not None)


@dataclass(frozen=True)
class _Network:
    """One ME network, checked and ready to grow: its roots, its leaves, its blocks."""

    spec: MENetworkSpec
    colour: AEColor
    stubs: tuple[Cell, ...]
    controllers: frozenset[Cell]
    leaves: tuple[_Leaf, ...]  # the channel devices: machines' endpoints, then the links
    acceptors: tuple[_Leaf, ...]
    blocks: frozenset[Cell]  # every infrastructure block's cells: stubs, links, controllers, ...


def _prepare(
    problem: InputIR,
    spec: MENetworkSpec,
    by_machine: Mapping[str, Placement],
    blocks: frozenset[Cell],
) -> _Network | Infeasibility | None:
    """``spec`` as a network to grow, the reason it never can be, or None with nothing to build.

    Refused here, before any cable is laid, is what no placement of cable fixes: infrastructure
    that is unplaced or contradicts the network's mode, controllers that are not one cluster, a
    link that is not one storage bus, two of its own blocks touching (they would close a loop), and
    a channel count over the network's budget (spike 2.6, and the attached network's own budget and
    32 a stub).
    """
    nid = spec.id
    own = [m for m in problem.machines if m.me_network == nid]
    unplaced = [m.id for m in own if m.id not in by_machine]
    if unplaced:
        return _refuse(nid, f"its infrastructure {unplaced} is not placed", "place every block")

    def cells_of(role: MERole) -> list[Cell]:
        return [c for m in own if m.me_role is role for c in _body(m, by_machine[m.id])]

    stubs, controllers = cells_of(MERole.ATTACH), frozenset(cells_of(MERole.CONTROLLER))
    leaves: list[_Leaf] = []
    links: list[_Leaf] = []
    acceptors: list[_Leaf] = []
    for machine in problem.machines:
        mine = [e for e in machine.me_endpoints if e.network == nid]
        placement = by_machine.get(machine.id)
        if machine.me_role is None:
            leaves += [
                _Leaf(
                    f"{machine.id}/{e.id}",
                    f"endpoint {e.id!r} of {machine.id!r}",
                    machine,
                    placement,
                    e,
                )
                for e in mine
            ]
            continue
        if machine.me_network != nid:
            if mine:
                return _refuse(
                    nid,
                    f"its endpoint {mine[0].id!r} is on {machine.me_role.value} {machine.id!r} of "
                    f"another network",
                    "give the device to a machine of the line",
                )
            continue
        if machine.me_role is MERole.LINK:
            if len(mine) != 1 or mine[0].device.kind not in _STORAGE_BUSES:
                return _refuse(
                    nid,
                    f"link {machine.id!r} must carry exactly one storage bus on its front, not "
                    f"{[e.device.kind.value for e in mine]}",
                    "give each storage bus a link of its own",
                )
            links.append(
                _Leaf(machine.id, f"link {machine.id!r}", machine, placement, mine[0], MERole.LINK)
            )
        elif mine:
            return _refuse(
                nid,
                f"{machine.me_role.value} {machine.id!r} carries no device, but names endpoint "
                f"{mine[0].id!r}",
                "give the device to a machine of the line",
            )
        if machine.me_role is MERole.ACCEPTOR:
            acceptors.append(
                _Leaf(
                    machine.id,
                    f"acceptor {machine.id!r}",
                    machine,
                    placement,
                    None,
                    MERole.ACCEPTOR,
                )
            )
    if not (leaves or links or acceptors or stubs or controllers):
        return None

    refused = _check_roots(
        spec, stubs, controllers, len(leaves) + len(links), bool(leaves or links)
    )
    if refused is not None:
        return refused
    touching = _own_clash(stubs, links, acceptors, by_machine)
    if touching is not None:
        return _refuse(
            nid,
            f"{touching} touch, so AE joins them by a path the tree does not lay",
            "keep the network's attach stubs, links and acceptors a cell apart",
        )
    return _Network(
        spec=spec,
        colour=problem.me.colour(nid),
        stubs=tuple(stubs),
        controllers=controllers,
        leaves=(*leaves, *links),
        acceptors=tuple(acceptors),
        blocks=blocks,
    )


def _check_roots(
    spec: MENetworkSpec,
    stubs: Sequence[Cell],
    controllers: Collection[Cell],
    devices: int,
    any_leaf: bool,
) -> Infeasibility | None:
    """Whether the network's channels have somewhere to come from, and enough of them."""
    nid = spec.id
    if spec.mode is MEMode.ATTACHED:
        if controllers:
            return _refuse(
                nid,
                "it is attached to the main network, so a controller of its own would conflict with "
                "the main network's",
                "make it a subnet, or drop its controller",
            )
        if any_leaf and not stubs:
            return _refuse(
                nid,
                "it is attached but has no attach stub for the main network's channels to enter by",
                "add an attach stub on the region's edge",
            )
        if devices > spec.me_channel_budget:
            return Infeasibility(
                constraint="me_channel_budget",
                detail=f"attached ME network {nid!r} needs {devices} channels, over the "
                f"{spec.me_channel_budget} its plan says the main network has free",
                suggested_relaxation="raise the network's channel budget if the main network has "
                "the channels, or move some of its nets to a subnet",
            )
        if devices > _TRUNK_CAPACITY * len(stubs):
            return Infeasibility(
                constraint="me_channels",
                detail=f"attached ME network {nid!r} needs {devices} channels, over the "
                f"{_TRUNK_CAPACITY} each of its {len(stubs)} attach stub(s) carries",
                suggested_relaxation="add an attach stub",
            )
        return None
    if stubs:
        return _refuse(
            nid,
            "it is a subnet, so an attach stub would join it to the main network",
            "make it attached, or drop its stub",
        )
    if controllers and not _one_cluster(controllers):
        return _refuse(
            nid,
            "its controllers are not the one cluster AE accepts (touching face to face, within 7 "
            "blocks a side, none with controllers on both sides along two axes), so AE would give "
            "every device no channel",
            "build the subnet's controllers as one block of touching controllers",
        )
    if not controllers and devices > ADHOC_MAX_DEVICES:
        return Infeasibility(
            constraint="me_adhoc",
            detail=f"ME subnet {nid!r} has no controller and {devices} channel devices: an ad-hoc "
            f"network gives every one of them no channel past {ADHOC_MAX_DEVICES}",
            suggested_relaxation="give the subnet a controller, or split its nets across subnets",
        )
    return None


def _one_cluster(controllers: Collection[Cell]) -> bool:
    """Whether a subnet's controller blocks form the one cluster AE runs a network from (spike
    2.2): joined face to face, within 7 blocks along every axis, and no block with controllers on
    both sides along two axes (which AE switches off)."""
    cells = set(controllers)
    start = min(cells)
    seen, stack = {start}, [start]
    while stack:
        for n in _neighbours(stack.pop()):
            if n in cells and n not in seen:
                seen.add(n)
                stack.append(n)
    if seen != cells:
        return False
    if any(max(c[a] for c in cells) - min(c[a] for c in cells) >= 7 for a in range(3)):
        return False
    for x, y, z in cells:
        axes = sum(
            (x + dx, y + dy, z + dz) in cells and (x - dx, y - dy, z - dz) in cells
            for dx, dy, dz in ((1, 0, 0), (0, 1, 0), (0, 0, 1))
        )
        if axes >= 2:
            return False
    return True


def _own_clash(
    stubs: Sequence[Cell],
    links: Sequence[_Leaf],
    acceptors: Sequence[_Leaf],
    by_machine: Mapping[str, Placement],
) -> str | None:
    """Two of a network's own blocks that touch and that the tree could not keep a tree: two
    stubs (a loop through the main network), or a link or acceptor beside another of those.

    A link or acceptor beside a stub or a controller is fine: it joins there, as its one touch.
    """
    groups: list[tuple[bool, str, list[Cell]]] = [(True, f"attach stub {c}", [c]) for c in stubs]
    groups += [
        (False, leaf.label, _body(leaf.machine, by_machine[leaf.machine.id]))
        for leaf in (*links, *acceptors)
    ]
    for i, (a_stub, a, a_cells) in enumerate(groups):
        around = {n for c in a_cells for n in _neighbours(c)}
        for b_stub, b, b_cells in groups[i + 1 :]:
            if a_stub is b_stub and around.intersection(b_cells):
                return f"{a} and {b}"
    return None


def _infrastructure_cells(
    problem: InputIR, by_machine: Mapping[str, Placement]
) -> dict[str, frozenset[Cell]]:
    """Each network's placed infrastructure blocks, by network: what every other network keeps a
    halo from from the start, whether or not its own network is ever laid."""
    cells: dict[str, set[Cell]] = {}
    for machine in problem.machines:
        placement = by_machine.get(machine.id)
        if machine.me_network is not None and placement is not None:
            cells.setdefault(machine.me_network, set()).update(_body(machine, placement))
    return {nid: frozenset(c) for nid, c in cells.items()}


def _infrastructure_clashes(blocks: Mapping[str, frozenset[Cell]]) -> list[tuple[str, str]]:
    """Every pair of networks whose infrastructure blocks touch, which AE joins into one network."""
    owner = {cell: nid for nid, cells in blocks.items() for cell in cells}
    pairs: set[tuple[str, str]] = set()
    for cell, nid in owner.items():
        for n in _neighbours(cell):
            other = owner.get(n)
            if other is not None and other != nid:
                pairs.add((min(nid, other), max(nid, other)))
    return sorted(pairs)


def _front_guards(problem: InputIR, by_machine: Mapping[str, Placement]) -> set[Cell]:
    """The in-region cell in front of each attach stub and link: none, when they sit where they
    must, flush on the region's edge. Kept out of every leg, so a stub or link misplaced inward
    still never takes cable on the side its main-network feed or storage bus uses."""
    region = problem.bounding_region
    guards: set[Cell] = set()
    for machine in problem.machines:
        placement = by_machine.get(machine.id)
        if machine.me_role in (MERole.ATTACH, MERole.LINK) and placement is not None:
            front = _step_to(placement.cell.as_tuple(), placement.orientation)
            if in_region(front, region):
                guards.add(front)
    return guards


# --- laying the networks ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Built:
    """One network laid whole: its layout, its multiblock terminals, the cells and claims it took."""

    layout: MENetworkLayout
    terminals: tuple[Terminal, ...]
    cells: frozenset[Cell]
    claimed: Mapping[str, set[Cell]]


@dataclass(frozen=True)
class _Context:
    """What one network grows against. ``blocked`` (machines, reserved and filter-back cells, other
    routes, laid networks' cable) and ``foreign`` (other networks' AE blocks: the halo's) are read,
    never written; ``faces`` is the caller's per-endpoint face pin (``route_me``)."""

    region: CellBox
    blocked: set[Cell]
    foreign: frozenset[Cell]
    claimed: Mapping[str, Collection[Cell]]
    faces: Mapping[tuple[str, str], Collection[Facing]]


def _route_pass(
    order: Sequence[str],
    prepared: Mapping[str, _Network | Infeasibility],
    infrastructure: Mapping[str, frozenset[Cell]],
    region: CellBox,
    base: set[Cell],
    claimed_cells: Mapping[str, Collection[Cell]],
    endpoint_faces: Mapping[tuple[str, str], Collection[Facing]],
) -> tuple[dict[str, _Built], dict[str, Infeasibility]]:
    """Lay the networks once, in ``order``, each one's cable foreign to every network after it."""
    laid: set[Cell] = set()
    claimed: dict[str, set[Cell]] = {m: set(c) for m, c in claimed_cells.items()}
    built: dict[str, _Built] = {}
    failures: dict[str, Infeasibility] = {}
    for nid in order:
        network = prepared[nid]
        if isinstance(network, Infeasibility):
            failures[nid] = network
            continue
        others = frozenset(
            c for other, cells in infrastructure.items() if other != nid for c in cells
        )
        context = _Context(region, base | laid, others | laid, claimed, endpoint_faces)
        result = _route_network(network, context)
        if isinstance(result, Infeasibility):
            failures[nid] = result
            continue
        built[nid] = result
        laid |= result.cells
        for machine_id, cells in result.claimed.items():
            claimed.setdefault(machine_id, set()).update(cells)
    return built, failures


def _route_network(network: _Network, context: _Context) -> _Built | Infeasibility:
    """Grow one network's forest, with a failed-first rip-up/reroute over its leaves.

    A pass that leaves a leaf unreached is retried with the unreached leaves first, and with every
    cell that ran out of channels only because it held a part (8 channels, where bare cable carries
    32) barred from holding one: the part that took it docks elsewhere and the cell stays trunk.
    A repeated failure set and bar set means retrying is cycling, so the first leaf still failing,
    in leaf order, is the network's reason.
    """
    leaves = list(network.leaves)
    order = leaves
    no_part: frozenset[Cell] = frozenset()
    seen: set[tuple[frozenset[str], frozenset[Cell]]] = set()
    while True:
        grower = _Grower(network, context, no_part)
        grower.run(order)
        if not grower.failures:
            return grower.built()
        state = (frozenset(grower.failures), no_part)
        if state in seen or len(seen) + 1 >= _MAX_PASSES:
            break
        seen.add(state)
        no_part |= grower.bottlenecks
        order = [x for x in leaves if x.key in grower.failures] + [
            x for x in leaves if x.key not in grower.failures
        ]
    first = next(x for x in (*leaves, *network.acceptors) if x.key in grower.failures)
    return grower.failures[first.key]


@dataclass
class _Forest:
    """A network's laid cable: every cell's parent toward its root (None at a root), its load
    (channel devices at or beyond it), and the parts it holds."""

    parent: dict[Cell, Cell | None] = field(default_factory=dict)
    load: dict[Cell, int] = field(default_factory=dict)
    order: list[Cell] = field(default_factory=list)  # laid order: deterministic output and seeds
    parts: set[Cell] = field(default_factory=set)
    sides: dict[Cell, set[Facing]] = field(default_factory=dict)  # sides holding a part or a hatch
    stubs: set[Cell] = field(default_factory=set)
    links: set[Cell] = field(default_factory=set)  # take no part but their own storage bus

    def lay(self, cells: Sequence[Cell], parent: Cell | None) -> None:
        for cell in cells:
            self.parent[cell] = parent
            self.load[cell] = 0
            self.order.append(cell)
            parent = cell

    def chain(self, cell: Cell) -> Iterator[Cell]:
        """``cell`` and every cell above it, up to its root."""
        node: Cell | None = cell
        while node is not None:
            yield node
            node = self.parent[node]

    def room(self, cell: Cell, *, part: bool) -> bool:
        """Whether one more channel device at ``cell`` (a part on it, if ``part``) keeps every cell
        on its way to the root within capacity: 8 where a part sits, 32 elsewhere."""
        for node in self.chain(cell):
            holds = node in self.parts or (part and node == cell)
            if self.load[node] >= (_PART_CAPACITY if holds else _TRUNK_CAPACITY):
                return False
        return True

    def add(self, cell: Cell, side: Facing, *, part: bool) -> None:
        """One channel device at ``cell``, on ``side`` of it."""
        self.sides.setdefault(cell, set()).add(side)
        if part:
            self.parts.add(cell)
        for node in self.chain(cell):
            self.load[node] += 1

    def saturated(self, cell: Cell) -> set[Cell]:
        """The cells from ``cell`` up that hold a part and so are full at 8: the ones that would
        carry the channel as bare cable."""
        return {n for n in self.chain(cell) if n in self.parts and self.load[n] >= _PART_CAPACITY}


class _Grower:
    """One pass over one network: its forest grown leaf by leaf, in a given order."""

    def __init__(self, network: _Network, context: _Context, no_part: frozenset[Cell]) -> None:
        self.network = network
        self.context = context
        self.no_part = no_part
        self.forest = _Forest()
        self.blocked = set(context.blocked)  # grows with the forest: legs never cross it
        self.halo = set(context.foreign) | network.blocks  # no new cable may touch these
        self.claimed: dict[str, set[Cell]] = {m: set(c) for m, c in context.claimed.items()}
        self.devices: list[MEPlacedDevice] = []
        self.terminals: list[Terminal] = []
        self.failures: dict[str, Infeasibility] = {}
        self.bottlenecks: set[Cell] = set()
        for stub in network.stubs:
            self._lay([stub], None)
            self.forest.stubs.add(stub)

    def run(self, order: Sequence[_Leaf]) -> None:
        for i, leaf in enumerate(order):
            toward = order[i + 1].placement if i + 1 < len(order) else None
            aim = toward.cell.as_tuple() if toward is not None else None
            if leaf.role is MERole.LINK:
                self._link(leaf)
            else:
                self._device(leaf, aim)
        for acceptor in self.network.acceptors:
            self._acceptor(acceptor)

    def built(self) -> _Built:
        cables = [
            MECableCell(
                cell=coord(cell),
                kind=MECableKind.DENSE
                if cell in self.forest.stubs
                else _cable_kind(self.forest.load[cell], holds_part=cell in self.forest.parts),
                me_channels=self.forest.load[cell],
            )
            for cell in self.forest.order
        ]
        spec = self.network.spec
        layout = MENetworkLayout(
            id=spec.id, colour=self.network.colour, cables=cables, devices=self.devices
        )
        return _Built(layout, tuple(self.terminals), frozenset(self.forest.order), self.claimed)

    # --- the leaves ---

    def _device(self, leaf: _Leaf, aim: Cell | None) -> None:
        """A machine's device: a part on a cable cell facing the machine, or a GT ME hatch on the
        machine's casing facing a cable cell. Taps a cell already laid where one can take it."""
        endpoint, placement, machine = leaf.endpoint, leaf.placement, leaf.machine
        assert endpoint is not None  # a device leaf always has one
        if placement is None:
            self._fail(leaf, "face_reachability", "is not placed", "place the machine")
            return
        # A Dual Interface serving an item and a fluid port docks on a face both may use, and the
        # caller may pin a device to fewer (``route_me``'s ``endpoint_faces``).
        faces = frozenset(self.context.faces.get((machine.id, endpoint.id), Facing))
        for port in endpoint.ports:
            faces &= machine.allowed_faces(port, placement.orientation)
        part = not leaf.gt_hatch
        found = [
            t
            for t in dock_candidates(
                endpoint.ports[0],
                placement,
                machine,
                self.context.blocked,
                set(),
                self.context.region,
                self.claimed.get(machine.id, ()),
            )
            if t.face in faces
        ]
        usable = [t for t in found if not part or _cell(t) not in self.no_part]
        if not usable:
            if found:  # only cells that have to carry the trunk, which takes no part
                self._fail(
                    leaf,
                    "me_channels",
                    "can dock only on cells that must carry more channels than a cable holding a "
                    "part does",
                    "move the machine off the network's trunk, or give the network another root",
                )
            else:
                self._fail(
                    leaf,
                    "face_reachability",
                    "has no free cell on a face its port may use",
                    "free up cells next to the machine",
                )
            return
        taps = [t for t in usable if self._can_tap(t, part=part)]
        if taps:
            self._place(leaf, min(taps, key=lambda t: _depth(self.forest, _cell(t))), part=part)
            return
        goals = {_cell(t) for t in usable if _cell(t) not in self.forest.parent}
        if not self.forest.parent and not self.network.controllers:
            # Ad hoc, nothing laid yet: this device's dock seeds the one tree, as near the next
            # leaf as its faces allow.
            seeds = [t for t in usable if self._clear(_cell(t))]
            if not seeds:
                self._fail(
                    leaf,
                    "routing",
                    "has no dock cell clear of other ME networks",
                    "leave a free cell around the machine",
                )
                return
            seed = seeds[0] if aim is None else min(seeds, key=lambda t: manhattan(_cell(t), aim))
            self._lay([_cell(seed)], None)
            self._place(leaf, seed, part=part)
            return
        path = self._search(self._starts(channel=True), goals)
        if path is None:
            full = [_cell(t) for t in usable if _cell(t) in self.forest.parent]
            self._unreached(leaf, full, goals)
            return
        self._lay_path(path)
        self._place(leaf, next(t for t in usable if _cell(t) == path[-1]), part=part)

    def _link(self, leaf: _Leaf) -> None:
        """A link: its own cell joins the forest as a leaf, its storage bus on its front side."""
        endpoint, placement = leaf.endpoint, leaf.placement
        assert endpoint is not None  # checked by _prepare
        assert placement is not None  # checked by _prepare
        cell, front = placement.cell.as_tuple(), placement.orientation
        if not self.forest.parent and not self.network.controllers:
            # An ad-hoc tree seeded here, with nothing AE beside it: another network's block there
            # is refused up front (``_infrastructure_clashes``), as is one of this network's own
            # (``_own_clash``), and every other network's cable keeps the halo from it.
            self._lay([cell], None)
        else:
            mine = frozenset({cell})
            path = self._search(self._starts(channel=True), {cell}, exempt=mine, own_goal=True)
            if path is None:
                self._unreached(leaf, [], {cell}, exempt=mine)
                return
            self._lay_path(path)
        self.forest.links.add(cell)
        self.forest.add(cell, front, part=True)
        device = endpoint.device
        self.devices.append(
            MEPlacedDevice(
                machine_id=leaf.machine.id,
                endpoint_id=endpoint.id,
                kind=device.kind,
                cell=coord(cell),
                side=front,
                cards=device.cards,
                config=device.config,
            )
        )

    def _acceptor(self, leaf: _Leaf) -> None:
        """An Energy Acceptor: a block the forest touches exactly once (it joins on every side), and
        which carries no channel. Touching it twice would close a loop through it."""
        placement = leaf.placement
        assert placement is not None  # checked by _prepare
        body = set(_body(leaf.machine, placement))
        around = sorted({n for c in body for n in _neighbours(c)} - body)
        touches = sum(1 for n in around if n in self.forest.parent) + any(
            n in self.network.controllers for n in around
        )
        if touches > 1:
            self._fail(
                leaf,
                "me_infrastructure",
                f"touches the network {touches} times, which closes a loop through it",
                "move the acceptor so one cable or the controller touches it",
            )
            return
        starts = self._starts(channel=False)
        if touches == 1 or not starts:
            return  # joined already, or an ad-hoc network with no cable for it to join
        goals = {n for n in around if in_region(n, self.context.region) and n not in self.blocked}
        path = self._search(starts, goals, exempt=frozenset(body))
        if path is None:
            self._fail(
                leaf,
                "routing",
                "cannot be reached without touching another ME network or closing a loop",
                "move the acceptor nearer the network's cable",
            )
            return
        self._lay_path(path)

    # --- growing ---

    def _starts(self, *, channel: bool) -> list[Cell]:
        """Where a leg may grow from: every laid cable, and only one with a channel to spare when
        the leaf brings one; plus a subnet's controller, beside which a leg roots a tree of its own.

        A link is a cable like any other here: an ad-hoc subnet with no device before it is seeded
        at its link, and the rest of the tree has nowhere else to grow from."""
        starts = [c for c in self.forest.order if not channel or self.forest.room(c, part=False)]
        return starts + sorted(self.network.controllers)

    def _search(
        self,
        starts: Sequence[Cell],
        goals: set[Cell],
        *,
        exempt: frozenset[Cell] = frozenset(),
        own_goal: bool = False,
    ) -> list[Cell] | None:
        """The shortest leg from ``starts`` to ``goals`` under the halo, or None. ``exempt`` are
        cells the leg may touch (the acceptor or link it reaches); ``own_goal`` lets it end on a
        block cell of the network's own (a link)."""
        if not starts or not goals:
            return None
        blocked = self.blocked - goals if own_goal else self.blocked
        return astar_multi(
            starts, goals, blocked, self.context.region, step=self._halo_step(exempt)
        )

    def _halo_step(self, exempt: frozenset[Cell]) -> Callable[[Cell, Cell], bool]:
        """The halo, as an A* move rule: a new cable may touch no AE block but the one it grows
        from (a whole controller, when it roots there) and ``exempt``.

        That is what keeps a leg a branch: its first cell touches only its start, every later cell
        touches nothing laid, and since every cell costs the same a shortest leg never touches
        itself either (a touch would be a shortcut).
        """
        halo, controllers = self.halo, self.network.controllers

        def step(cur: Cell, nxt: Cell) -> bool:
            rooting = cur in controllers
            x, y, z = nxt
            for dx, dy, dz in NEIGHBORS:
                n = (x + dx, y + dy, z + dz)
                if n == cur or n in exempt or (rooting and n in controllers):
                    continue
                if n in halo:
                    return False
            return True

        return step

    def _clear(self, cell: Cell) -> bool:
        """Whether a cable at ``cell`` would touch no AE block at all: a seed for an ad-hoc tree."""
        return not any(n in self.halo for n in _neighbours(cell))

    def _lay_path(self, path: Sequence[Cell]) -> None:
        """A found leg: ``path[0]`` is the cable it grows from, or a controller it roots beside."""
        self._lay(path[1:], path[0] if path[0] in self.forest.parent else None)

    def _lay(self, cells: Sequence[Cell], parent: Cell | None) -> None:
        self.forest.lay(cells, parent)
        self.blocked.update(cells)
        self.halo.update(cells)

    def _can_tap(self, terminal: Terminal, *, part: bool) -> bool:
        """Whether the laid cable at a dock candidate can take the device: the side facing the
        machine is free, and one more channel there stays within capacity (8, once it holds a part,
        so a part never taps dense cable)."""
        cell = _cell(terminal)
        return (
            cell in self.forest.parent
            and cell not in self.forest.links
            and OPPOSITE_FACE[terminal.face] not in self.forest.sides.get(cell, ())
            and self.forest.room(cell, part=part)
        )

    def _place(self, leaf: _Leaf, terminal: Terminal, *, part: bool) -> None:
        """Build ``leaf``'s device at a dock: a part on the dock cell's side facing the machine, or a
        GT ME hatch on the casing cell behind it, front facing the dock."""
        endpoint, machine = leaf.endpoint, leaf.machine
        assert endpoint is not None  # a device leaf always has one
        cell = _cell(terminal)
        toward_machine = OPPOSITE_FACE[terminal.face]
        self.forest.add(cell, toward_machine, part=part)
        self.claimed.setdefault(machine.id, set()).add(claim_key(terminal, machine))
        device = endpoint.device
        self.devices.append(
            MEPlacedDevice(
                machine_id=machine.id,
                endpoint_id=endpoint.id,
                kind=device.kind,
                cell=terminal.cell if part else coord(body_cell(terminal)),
                side=toward_machine if part else terminal.face,
                gt_mid=device.gt_mid,
                cards=device.cards,
                config=device.config,
            )
        )
        if leaf.multiblock:
            self.terminals.append(terminal)

    def _unreached(
        self,
        leaf: _Leaf,
        full_taps: Sequence[Cell],
        goals: set[Cell],
        *,
        exempt: frozenset[Cell] = frozenset(),
    ) -> None:
        """Why no tap or leg reached ``leaf``: asked again with every laid cable a start, a leg (or
        one of ``full_taps``, laid cells it docks on that had no room) found then means the way is
        there but full (``me_channels``), and the cells full only for holding a part are noted for
        the next pass. Otherwise there is no way at all (``routing``)."""
        own_goal = leaf.role is MERole.LINK
        path = self._search(self._starts(channel=False), goals, exempt=exempt, own_goal=own_goal)
        via = list(full_taps) + ([path[0]] if path is not None else [])
        if not via:
            self._fail(
                leaf,
                "routing",
                "cannot be reached without touching another ME network or closing a loop",
                "enlarge the bounding region, or move the machine nearer the network",
            )
            return
        for cell in via:
            if cell in self.forest.parent:
                self.bottlenecks |= self.forest.saturated(cell)
        self._fail(
            leaf,
            "me_channels",
            "can be reached only through cable already carrying all the channels it can (8 on a "
            "cable holding a part, 32 on dense cable)",
            "give the network another attach stub or controller face, or move the machine nearer "
            "its root",
        )

    def _fail(self, leaf: _Leaf, constraint: str, why: str, relax: str) -> None:
        self.failures.setdefault(
            leaf.key,
            Infeasibility(
                constraint=constraint,
                detail=f"ME network {self.network.spec.id!r}: {leaf.label} {why}",
                suggested_relaxation=relax,
            ),
        )


# --- small helpers ---------------------------------------------------------------------------------


def _cable_kind(load: int, *, holds_part: bool) -> MECableKind:
    """The cable a cell is built from: smart where it holds a part (dense takes none) or carries no
    more than smart cable does, dense otherwise. Growth keeps both within capacity."""
    if holds_part or load <= CABLE_CAPACITY[MECableKind.SMART]:
        return MECableKind.SMART
    return MECableKind.DENSE


def _depth(forest: _Forest, cell: Cell) -> int:
    return sum(1 for _ in forest.chain(cell))


def _body(machine: Machine, placement: Placement) -> list[Cell]:
    return list(occupied_cells(placement.cell, machine.footprint, placement.orientation))


def _neighbours(cell: Cell) -> Iterator[Cell]:
    x, y, z = cell
    for dx, dy, dz in NEIGHBORS:
        yield (x + dx, y + dy, z + dz)


def _step_to(cell: Cell, face: Facing) -> Cell:
    dx, dy, dz = FACE_DELTAS[face]
    return (cell[0] + dx, cell[1] + dy, cell[2] + dz)


def _cell(terminal: Terminal) -> Cell:
    return terminal.cell.as_tuple()


def _refuse(network_id: str, why: str, relax: str) -> Infeasibility:
    return Infeasibility(
        constraint="me_infrastructure",
        detail=f"ME network {network_id!r}: {why}",
        suggested_relaxation=relax,
    )


def _clash(network_id: str, other: str) -> Infeasibility:
    return _refuse(
        network_id,
        f"an infrastructure block touches one of ME network {other!r}'s, so AE would join the two",
        "keep different networks' attach stubs, links, controllers and acceptors a cell apart",
    )
