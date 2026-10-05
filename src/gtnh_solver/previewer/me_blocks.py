"""previewer.me_blocks - every ME (AE2) block a layout builds, as the boxes AE2 draws it with (#338).

The ME counterpart of :mod:`gtnh_solver.route_blocks`. ``LayoutResult.me_networks`` lists each
network's cable blocks and the devices on them, and says nothing about how they look: in game a
cable bus's shape follows from what touches it, and AE2 works that out in its renderer, where no test
can reach it. So it is worked out here, from the committed render data
(:mod:`gtnh_solver.dataset.ae_render`: every number read from AE2's source, ``docs/spikes/
329-me-ae2.md`` section 8), and the viewer template only draws the boxes::

    layout.me_networks + placements + problem.machines          data/ae2/<AE2>/render.json
        |                                                             |
        |-- connections  per cable side: a part there takes it;      |
        |                else a cable of a joining colour with no    |
        |                part facing back, a controller or acceptor, |
        |                a GT ME hatch whose front faces it, or (an  |
        |                attach stub's front) the main network       |
        |-- channels     per side: what that connection carries      |
        v                                                             v
    boxes   a straight bar (two opposite connections, AE2's rule per cable kind), or the core plus
            an arm per connection and a plug where it meets a device block; per part, the arm the
            cable draws out to it and the part's own boxes; the lights over each lit box
        |
        v
    MECableBlock per cable cell, MEBlockDevice per controller and acceptor

**Connections follow AE2's rules** (spike 3), the ones ``validator.me`` rebuilds its graph by;
they are restated here rather than imported, since the validator is the independent check and
shares only rule data (``dataset.me.colours_connect``). An attach stub's front is a connection too:
the player's main network meets it there, outside the build, and its arm says where.

**Channels per side.** A layout states one count per cable, ``MECableCell.me_channels``: the
channel devices routed through it, counted on the router's tree. A connection between two cables
carries the count of the one farther from the root, which on a tree is the smaller of the two; a
connection to a controller, or out of a stub, carries the cable's own count (the channels arrive
there); a part or a GT ME hatch takes one. What a side shows is :meth:`ChannelLights.shown_count`
of that: capped at 8, and in fours where both nodes have AE2's dense capacity (a dense cable and
another, the main network's, or a controller; :func:`_dense_link`). A network is drawn powered.

**No two boxes of a cell share a volume, and no shape has a hole** (two property tests). AE2 itself
overlaps a few boxes: a plug and the arm through it, and a dense cable's arms reaching two sixteenths
into its core. Shared volume means coplanar faces that tear in a renderer, so those are cut back
where AE2 hides them anyway: an arm starts at the plug's face, and nothing reaches into the core.
What shows is unchanged. A face AE2 leaves open (an arm's ends, a glass bar's) always lies against a
box at least as wide beyond it, in its own block or the next; a covered, smart or dense bar is wider
than the arm it meets, which is why AE2 draws its ends (``PartCableSmart.java:239-241``).

**Not modelled.** UV rotations and offsets (a straight run's stripes), and the glass icon an arm
takes toward a coloured glass neighbour: the router lays only smart and dense cable. An arm toward a
coloured neighbour cable does borrow its colour (spike 8.1), which a valid layout never shows, since
two colours meet only where AE would merge two networks.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from gtnh_solver.dataset.ae_render import AERender, ColourTints, MEBlock
from gtnh_solver.dataset.me import CABLE_CAPACITY, colours_connect
from gtnh_solver.ir import (
    AEColor,
    Facing,
    InputIR,
    LayoutResult,
    Machine,
    MECableCell,
    MECableKind,
    MENetworkLayout,
    MEPlacedDevice,
    MERole,
    Placement,
)
from gtnh_solver.ir.geometry import FACE_DELTAS, OPPOSITE_FACE, Cell, occupied_cells

#: A box in sixteenths of its block, ``(x0, y0, z0, x1, y1, z1)``, block-local.
Box = tuple[int, int, int, int, int, int]
RGB = tuple[int, int, int]

#: The six sides in ForgeDirection order, the order AE2 visits them in: a straight run's lights show
#: the first connected side's count. Every per-face tuple here is in this order.
SIDE_ORDER: tuple[Facing, ...] = (
    Facing.DOWN,
    Facing.UP,
    Facing.NORTH,
    Facing.SOUTH,
    Facing.WEST,
    Facing.EAST,
)

#: A whole block, for a controller or an acceptor.
FULL_BLOCK: Box = (0, 0, 0, 16, 16, 16)

#: The axis each side lies on (x 0, y 1, z 2) and which way along it the side points.
_AXIS: dict[Facing, tuple[int, int]] = {
    Facing.WEST: (0, -1),
    Facing.EAST: (0, 1),
    Facing.DOWN: (1, -1),
    Facing.UP: (1, 1),
    Facing.NORTH: (2, -1),
    Facing.SOUTH: (2, 1),
}

_DENSE = frozenset({MECableKind.DENSE, MECableKind.DENSE_COVERED})

#: A part's own axes, in the order its boxes give them.
_LOCAL_AXES: tuple[Literal["x", "y", "z"], ...] = ("x", "y", "z")

#: The infrastructure roles drawn as a whole AE2 block, and the render data's block for each.
_BLOCK_OF_ROLE: dict[MERole, MEBlock] = {
    MERole.CONTROLLER: "controller",
    MERole.ACCEPTOR: "energy_acceptor",
}

#: Horizontal sides clockwise from above, to turn a block's faces from AE2's frame (front south).
_CLOCKWISE = (Facing.NORTH, Facing.EAST, Facing.SOUTH, Facing.WEST)

#: A white tint: a controller's lights are drawn uncoloured (``RenderBlockController``).
_WHITE: RGB = (255, 255, 255)

#: What a cable side connects to.
ConnectionTo = Literal["cable", "block", "hatch", "outside"]


@dataclass(frozen=True, slots=True)
class MELight:
    """One fullbright pass over a box: the white mask ``icon`` tinted ``tint``, on the faces
    ``faces`` marks (:data:`SIDE_ORDER`)."""

    icon: str
    tint: RGB
    faces: tuple[bool, ...]


@dataclass(frozen=True, slots=True)
class MEBox:
    """One box to draw: where it is, the icon on each face (``None``: that face is not drawn,
    because AE2 leaves it open or it lies against another box), the colour it is drawn in where an
    icon is missing, the lights over it, and ``part``, the index of the part of its cell it draws
    (``None`` for the cable or block itself)."""

    box: Box
    faces: tuple[str | None, ...]
    colour: AEColor
    lights: tuple[MELight, ...] = ()
    part: int | None = None


@dataclass(frozen=True, slots=True)
class MEConnection:
    """One connected side of a cable: what is there, the cable type it reports to AE2 (a cable its
    kind, a block its own: a controller dense, an acceptor covered; a GT ME hatch's front smart,
    ``MTEHatchOutputBusME.java:315`` and its three siblings), its colour, and the channels the
    connection carries."""

    side: Facing
    to: ConnectionTo
    reports: MECableKind
    colour: AEColor
    channels: int


@dataclass(frozen=True, slots=True)
class MEPart:
    """An AE2 part on one side of a cable: the device the layout places there."""

    side: Facing
    device: MEPlacedDevice


@dataclass(frozen=True, slots=True)
class MECableBlock:
    """One cable block of an ME network, ready to draw.

    ``role`` and ``machine_id`` are set where the block is an attach stub or a link, which are
    cables placed as machines (``Machine.me_role``). ``straight`` says AE2 draws it as one bar.
    """

    cell: Cell
    network: str
    colour: AEColor
    kind: MECableKind
    channels: int
    capacity: int
    role: MERole | None
    machine_id: str | None
    connections: tuple[MEConnection, ...]
    parts: tuple[MEPart, ...]
    straight: bool
    boxes: tuple[MEBox, ...]


@dataclass(frozen=True, slots=True)
class MEBlockDevice:
    """One AE2 block an ME network needs of its own: a controller or an energy acceptor.

    ``look`` is a controller's look in the render data (``powered``, a column, the inside of a
    cluster), ``None`` for an acceptor, which has one.
    """

    cell: Cell
    machine_id: str
    role: MERole
    network: str
    colour: AEColor
    block: MEBlock
    look: str | None
    boxes: tuple[MEBox, ...]


@dataclass(frozen=True, slots=True)
class MEBlocks:
    """Every ME block a layout builds: its cable blocks and its block devices, in layout order."""

    cables: tuple[MECableBlock, ...]
    devices: tuple[MEBlockDevice, ...]

    def icon_names(self) -> frozenset[str]:
        """Every icon these blocks draw a face with."""
        return frozenset(icon for box in self.boxes() for icon in box.faces if icon is not None)

    def light_icons(self) -> frozenset[str]:
        """Every icon these blocks draw a light pass with."""
        return frozenset(light.icon for box in self.boxes() for light in box.lights)

    def boxes(self) -> Iterable[MEBox]:
        """Every box, cables first."""
        for cable in self.cables:
            yield from cable.boxes
        for device in self.devices:
            yield from device.boxes


# --- frames ----------------------------------------------------------------------------------------


def toward(side: Facing, box: Box) -> Box:
    """``box``, given toward DOWN (``y = 0`` the connected face, the render data's cable frame),
    turned to face ``side``: the mapping AE2's own six cases follow (``tools/derive_ae_render.py``
    checks it against every switch it reads)."""
    x0, y0, z0, x1, y1, z1 = box
    turned: dict[Facing, Box] = {
        Facing.DOWN: box,
        Facing.UP: (x0, 16 - y1, z0, x1, 16 - y0, z1),
        Facing.NORTH: (x0, z0, y0, x1, z1, y1),
        Facing.SOUTH: (x0, z0, 16 - y1, x1, z1, 16 - y0),
        Facing.WEST: (y0, x0, z0, y1, x1, z1),
        Facing.EAST: (16 - y1, x0, z0, 16 - y0, x1, z1),
    }
    return turned[side]


def part_box(axes: Mapping[Literal["x", "y", "z"], str], box: Box) -> Box:
    """A part's ``box`` (its own frame, ``z = 16`` the face it sits on) in its block's frame, given
    where its ``x``, ``y`` and ``z`` axes point (``AERender.part_frame`` for its side)."""
    lo = [0, 0, 0]
    hi = [0, 0, 0]
    for local, name in enumerate(_LOCAL_AXES):
        axis, sign = _AXIS[Facing(axes[name])]
        a, b = box[local], box[local + 3]
        lo[axis], hi[axis] = (a, b) if sign > 0 else (16 - b, 16 - a)
    return (lo[0], lo[1], lo[2], hi[0], hi[1], hi[2])


def _outside(box: Box, side: Facing, core: Box) -> Box | None:
    """``box`` cut back so it does not reach into ``core`` past the core's face toward ``side``;
    ``None`` when nothing is left."""
    axis, sign = _AXIS[side]
    lo, hi = list(box[:3]), list(box[3:])
    if sign < 0:
        hi[axis] = min(hi[axis], core[axis])
    else:
        lo[axis] = max(lo[axis], core[axis + 3])
    if lo[axis] >= hi[axis]:
        return None
    return (lo[0], lo[1], lo[2], hi[0], hi[1], hi[2])


def _beyond(box: Box, side: Facing, plug: Box) -> Box | None:
    """``box`` (an arm toward ``side``) starting where ``plug`` ends, so the two share no volume;
    ``None`` when nothing is left."""
    axis, sign = _AXIS[side]
    lo, hi = list(box[:3]), list(box[3:])
    if sign < 0:
        lo[axis] = max(lo[axis], plug[axis + 3])
    else:
        hi[axis] = min(hi[axis], plug[axis])
    if lo[axis] >= hi[axis]:
        return None
    return (lo[0], lo[1], lo[2], hi[0], hi[1], hi[2])


def _faces(icon: str, drawn: Iterable[Facing]) -> tuple[str | None, ...]:
    """``icon`` on every face in ``drawn``, nothing on the others."""
    shown = set(drawn)
    return tuple(icon if side in shown else None for side in SIDE_ORDER)


def _except(*hidden: Facing) -> tuple[Facing, ...]:
    return tuple(side for side in SIDE_ORDER if side not in hidden)


def _step(cell: Cell, side: Facing) -> Cell:
    dx, dy, dz = FACE_DELTAS[side]
    return (cell[0] + dx, cell[1] + dy, cell[2] + dz)


def _tint(tints: ColourTints, name: str) -> RGB:
    """The ``AEColor`` field the render data names (``ChannelLights.pass_tints``)."""
    return {
        "black_variant": tints.black_variant,
        "medium_variant": tints.medium_variant,
        "white_variant": tints.white_variant,
    }[name]


def _channel_lights(
    render: AERender, colour: AEColor, count: int, faces: tuple[str | None, ...]
) -> tuple[MELight, ...]:
    """The two passes that show ``count`` channels, tinted for ``colour``, on the drawn faces."""
    lights = render.channel_lights
    tints = render.colours[colour]
    mask = tuple(face is not None for face in faces)
    return tuple(
        MELight(icon, _tint(tints, tint), mask)
        for icon, tint in zip(lights.icons(count), lights.pass_tints, strict=True)
    )


# --- the blocks ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Device:
    """A controller or acceptor block, as a neighbour of a cable."""

    machine: Machine
    role: MERole
    network: str
    colour: AEColor
    block: MEBlock
    front: Facing


@dataclass(frozen=True, slots=True)
class _Infrastructure:
    """A stub's or link's cable cell: its machine, its role and the way its front faces."""

    machine_id: str
    role: MERole
    front: Facing


class _World:
    """What is at each cell, as far as ME connections go."""

    def __init__(self, problem: InputIR, layout: LayoutResult) -> None:
        placements: dict[str, Placement] = {p.machine_id: p for p in layout.placements}
        self.cables: dict[Cell, tuple[MENetworkLayout, MECableCell]] = {}
        for network in layout.me_networks:
            for cable in network.cables:
                # Two networks on one cell is a clash the validator reports; the first is drawn.
                self.cables.setdefault(cable.cell.as_tuple(), (network, cable))
        self.parts: dict[Cell, dict[Facing, MEPlacedDevice]] = defaultdict(dict)
        self.hatches: dict[Cell, tuple[Facing, AEColor]] = {}
        for network in layout.me_networks:
            for device in network.devices:
                if device.gt_mid is not None:
                    self.hatches[device.cell.as_tuple()] = (device.side, network.colour)
                else:
                    self.parts[device.cell.as_tuple()].setdefault(device.side, device)
        self.devices: dict[Cell, _Device] = {}
        self.infrastructure: dict[Cell, _Infrastructure] = {}
        for machine in problem.machines:
            placement = placements.get(machine.id)
            if machine.me_role is None or machine.me_network is None or placement is None:
                continue
            cells = occupied_cells(placement.cell, machine.footprint, placement.orientation)
            block = _BLOCK_OF_ROLE.get(machine.me_role)
            if block is None:  # a stub or a link: a cable block, listed in the network's cables
                for cell in cells:
                    self.infrastructure[cell] = _Infrastructure(
                        machine.id, machine.me_role, placement.orientation
                    )
                continue
            colour = (
                problem.me.colour(machine.me_network)
                if machine.me_role is MERole.CONTROLLER
                else AEColor.FLUIX  # an acceptor is uncoloured, so it joins any network
            )
            for cell in cells:
                self.devices[cell] = _Device(
                    machine,
                    machine.me_role,
                    machine.me_network,
                    colour,
                    block,
                    placement.orientation,
                )


def me_blocks(problem: InputIR, layout: LayoutResult, render: AERender) -> MEBlocks:
    """Every ME block ``layout`` builds, with the boxes AE2 draws it as (module docstring)."""
    world = _World(problem, layout)
    cables = tuple(
        _cable_block(cell, network, cable, world, render)
        for cell, (network, cable) in world.cables.items()
    )
    return MEBlocks(cables=cables, devices=_block_devices(world, render))


def _connections(
    cell: Cell, network: MENetworkLayout, cable: MECableCell, world: _World, render: AERender
) -> tuple[MEConnection, ...]:
    """The sides of ``cable`` AE2 connects, each with what it reaches (module docstring)."""
    mine = world.parts.get(cell, {})
    infrastructure = world.infrastructure.get(cell)
    found: list[MEConnection] = []
    for side in SIDE_ORDER:
        if side in mine:
            continue  # a part on this side takes it from the cable
        there = _step(cell, side)
        back = OPPOSITE_FACE[side]
        neighbour = world.cables.get(there)
        if neighbour is not None:
            other_network, other = neighbour
            if back not in world.parts.get(there, {}) and colours_connect(
                network.colour, other_network.colour
            ):
                channels = min(cable.me_channels, other.me_channels)
                found.append(
                    MEConnection(side, "cable", other.kind, other_network.colour, channels)
                )
            continue
        device = world.devices.get(there)
        if device is not None:
            if colours_connect(network.colour, device.colour):
                carried = cable.me_channels if device.role is MERole.CONTROLLER else 0
                reports = render.blocks[device.block].cable_type
                found.append(MEConnection(side, "block", reports, device.colour, carried))
            continue
        hatch = world.hatches.get(there)
        if hatch is not None:
            front, colour = hatch
            if front is back and colours_connect(network.colour, colour):
                # GT's ME hatches report a smart cable through their front (``isOutputFacing ?
                # SMART : NONE``), so a dense cable draws its smart arm and plug there, lit.
                found.append(MEConnection(side, "hatch", MECableKind.SMART, colour, 1))
            continue
        if (
            infrastructure is not None
            and infrastructure.role is MERole.ATTACH
            and side is infrastructure.front
        ):
            # The main network's cable outside the build; a stub is dense, and so is what feeds it.
            found.append(
                MEConnection(side, "outside", MECableKind.DENSE, AEColor.FLUIX, cable.me_channels)
            )
    return tuple(found)


def _dense_link(kind: MECableKind, connection: MEConnection) -> bool:
    """Whether a side shows its channels in fours: AE2 divides by four only where both nodes have
    ``DENSE_CAPACITY`` (``PartCable.java:386-392``). A dense or dense covered cable has it
    (``PartDenseCable.java:54``, ``PartDenseCableCovered.java:53``), and so does a controller
    (``TileController.java:55``), the one block that reports a dense cable type; the main network's
    cable out of a stub is taken to be dense. So both sides reporting a dense type is the test."""
    return kind in _DENSE and connection.reports in _DENSE


def _runs_straight(
    kind: MECableKind, connections: tuple[MEConnection, ...], parts: tuple[MEPart, ...]
) -> bool:
    """Whether AE2 draws this cable as one bar (spike 8.1): two opposite connections, and per kind
    glass with no device block reporting anything but glass beside it (AE2's parts report glass),
    covered and smart with no part, dense with dense on both sides."""
    if len(connections) != 2 or connections[0].side is not OPPOSITE_FACE[connections[1].side]:
        return False
    if kind is MECableKind.GLASS:
        return not any(
            c.to in ("block", "hatch") and c.reports is not MECableKind.GLASS for c in connections
        )
    if kind in _DENSE:
        return all(c.reports in _DENSE for c in connections)
    return not parts


def _cable_block(
    cell: Cell,
    network: MENetworkLayout,
    cable: MECableCell,
    world: _World,
    render: AERender,
) -> MECableBlock:
    """One cable block and its boxes."""
    parts = tuple(
        MEPart(side, device)
        for side, device in sorted(
            world.parts.get(cell, {}).items(), key=lambda item: SIDE_ORDER.index(item[0])
        )
    )
    connections = _connections(cell, network, cable, world, render)
    straight, boxes = cable_boxes(
        cable.kind, network.colour, connections, parts, render, channels=cable.me_channels
    )
    role = world.infrastructure.get(cell)
    return MECableBlock(
        cell=cell,
        network=network.id,
        colour=network.colour,
        kind=cable.kind,
        channels=cable.me_channels,
        capacity=CABLE_CAPACITY[cable.kind],
        role=role.role if role is not None else None,
        machine_id=role.machine_id if role is not None else None,
        connections=connections,
        parts=parts,
        straight=straight,
        boxes=boxes,
    )


def cable_boxes(
    kind: MECableKind,
    colour: AEColor,
    connections: tuple[MEConnection, ...],
    parts: tuple[MEPart, ...],
    render: AERender,
    *,
    channels: int = 0,
) -> tuple[bool, tuple[MEBox, ...]]:
    """Whether a ``kind`` cable in ``colour`` runs straight, and the boxes AE2 draws it as, given
    its ``connections`` (in :data:`SIDE_ORDER`) and the ``parts`` on its other sides. ``channels``
    is its own count, which only a lit core would show (no AE2 cable lights its core)."""
    style = render.cables[kind]
    core = style.core.box
    boxes: list[MEBox] = []
    for index, part in enumerate(parts):
        boxes.extend(_part_boxes(index, part, kind, colour, core, render))
    straight = _runs_straight(kind, connections, parts)
    if straight:
        bar = style.straight
        # AE2 leaves a glass bar's ends open where the run goes on (``renderFacesExceptAxis``,
        # PartCable.java:335): the glass beyond is as wide. Every other kind draws its ends
        # (PartCableSmart.java:239-241), being wider than the arm it meets.
        open_ends = (
            [c.side for c in connections if c.to != "outside"] if kind is MECableKind.GLASS else []
        )
        faces = _faces(render.cable_icon(bar.icons, colour), _except(*open_ends))
        first = connections[0]
        count = render.channel_lights.shown_count(first.channels, dense=_dense_link(kind, first))
        lights = _channel_lights(render, colour, count, faces) if bar.lights else ()
        boxes.append(MEBox(toward(first.side, bar.box), faces, colour, lights))
        return True, tuple(boxes)
    faces = _faces(render.cable_icon(style.core.icons, colour), SIDE_ORDER)
    count = render.channel_lights.shown_count(channels)
    lights = _channel_lights(render, colour, count, faces) if style.core.lights else ()
    boxes.append(MEBox(core, faces, colour, lights))
    for connection in connections:
        boxes.extend(_connection_boxes(connection, kind, colour, core, render))
    return False, tuple(boxes)


def _connection_boxes(
    connection: MEConnection,
    kind: MECableKind,
    colour: AEColor,
    core: Box,
    render: AERender,
) -> list[MEBox]:
    """The arm toward one connected side, and the plug where it meets a device block."""
    side = connection.side
    style = render.cables[kind].connection(connection.reports)
    count = render.channel_lights.shown_count(
        connection.channels, dense=_dense_link(kind, connection)
    )
    boxes: list[MEBox] = []
    plug_box: Box | None = None
    if style.plug is not None and connection.to in ("block", "hatch"):
        plug_box = _outside(toward(side, style.plug.box), side, core)
        if plug_box is not None:
            # Its face against the device block is AE2's to skip, and lies on that block anyway.
            faces = _faces(render.cable_icon(style.plug.icons, colour), _except(side))
            lights = _channel_lights(render, colour, count, faces) if style.plug.lights else ()
            boxes.append(MEBox(plug_box, faces, colour, lights))
    arm_box = _outside(toward(side, style.arm.box), side, core)
    if arm_box is not None and plug_box is not None:
        arm_box = _beyond(arm_box, side, plug_box)
    if arm_box is None:
        return boxes
    # Toward a coloured neighbour cable, the arm wears that cable's colour (spike 8.1).
    worn = (
        connection.colour
        if connection.to == "cable" and connection.colour is not AEColor.FLUIX
        else colour
    )
    # Both ends are open: one runs into the core, the other on into the neighbour. Out of a stub
    # nothing is drawn beyond, so its far end is closed.
    ends = (OPPOSITE_FACE[side],) if connection.to == "outside" else (side, OPPOSITE_FACE[side])
    faces = _faces(render.cable_icon(style.arm.icons, worn), _except(*ends))
    lights = _channel_lights(render, worn, count, faces) if style.arm.lights else ()
    boxes.append(MEBox(arm_box, faces, worn, lights))
    return boxes


def _part_boxes(
    index: int,
    part: MEPart,
    kind: MECableKind,
    colour: AEColor,
    core: Box,
    render: AERender,
) -> list[MEBox]:
    """The arm a cable draws out to a part on its side, and the part's own boxes."""
    side = part.side
    shape = render.parts.get(part.device.kind)
    if shape is None:  # not an AE2 part (a GT ME hatch is a block of its own)
        return []
    boxes: list[MEBox] = []
    arm = render.cables[kind].part_arm
    reach = arm.box(shape.arm_length) if arm is not None else None
    if arm is not None and reach is not None:
        arm_box = _outside(toward(side, reach), side, core)
        if arm_box is not None:
            faces = _faces(render.cable_icon(arm.icons, colour), _except(side, OPPOSITE_FACE[side]))
            # A part spends one channel, and the arm to it shows that one.
            count = render.channel_lights.shown_count(1)
            lights = _channel_lights(render, colour, count, faces) if arm.lights else ()
            boxes.append(MEBox(arm_box, faces, colour, lights, part=index))
    axes = render.part_frame.axes[side.value]
    sides_mask = tuple(s not in (side, OPPOSITE_FACE[side]) for s in SIDE_ORDER)
    for drawn in shape.render:
        box = _outside(part_box(axes, drawn.box), side, core)
        if box is None:
            continue
        faces = tuple(
            drawn.front if s is side else drawn.back if s is OPPOSITE_FACE[side] else drawn.sides
            for s in SIDE_ORDER
        )
        status: tuple[MELight, ...] = ()
        if drawn.status_lights:
            # Lit in the colour's dark tint, as AE2 draws a powered part with its channel.
            tint = render.colours[colour].black_variant
            status = (MELight(shape.status_lights, tint, sides_mask),)
        boxes.append(MEBox(box, faces, colour, status, part=index))
    return boxes


def _block_devices(world: _World, render: AERender) -> tuple[MEBlockDevice, ...]:
    """Each controller and acceptor cell as one whole block with AE2's icons on its faces."""
    controllers: dict[str, set[Cell]] = defaultdict(set)
    for cell, device in world.devices.items():
        if device.role is MERole.CONTROLLER:
            controllers[device.network].add(cell)
    out: list[MEBlockDevice] = []
    for cell, device in world.devices.items():
        shape = render.blocks[device.block]
        look: str | None = None
        lights: tuple[MELight, ...] = ()
        if device.role is MERole.CONTROLLER and shape.states is not None:
            look = _controller_look(cell, controllers[device.network])
            state = shape.states[look]
            faces: tuple[str | None, ...] = _faces(state.icon, SIDE_ORDER)
            if state.lights is not None:
                lights = (MELight(state.lights, _WHITE, (True,) * len(SIDE_ORDER)),)
        else:
            faces = tuple(shape.faces[_ae_side(side, device.front).value] for side in SIDE_ORDER)
        out.append(
            MEBlockDevice(
                cell=cell,
                machine_id=device.machine.id,
                role=device.role,
                network=device.network,
                colour=device.colour,
                block=device.block,
                look=look,
                boxes=(MEBox(FULL_BLOCK, faces, device.colour, lights),),
            )
        )
    return tuple(out)


def _ae_side(side: Facing, front: Facing) -> Facing:
    """Which of a block's faces in AE2's own frame (front south) shows on world ``side`` when the
    block's front faces ``front``. Vertical fronts keep AE2's frame."""
    if side not in _CLOCKWISE or front not in _CLOCKWISE:
        return side
    steps = _CLOCKWISE.index(front) - _CLOCKWISE.index(Facing.SOUTH)
    return _CLOCKWISE[(_CLOCKWISE.index(side) - steps) % 4]


def _controller_look(cell: Cell, cluster: set[Cell]) -> str:
    """A powered controller's look, by the controllers of its network beside it
    (``RenderBlockController``): a column when it has one on both sides along exactly one axis,
    the inside of a cluster when along two or more (alternating like a checkerboard), else alone."""
    x, y, z = cell
    along = [
        (x - 1, y, z) in cluster and (x + 1, y, z) in cluster,
        (x, y - 1, z) in cluster and (x, y + 1, z) in cluster,
        (x, y, z - 1) in cluster and (x, y, z + 1) in cluster,
    ]
    if sum(along) >= 2:
        return "inside_a" if (abs(x) + abs(y) + abs(z)) % 2 == 0 else "inside_b"
    if sum(along) == 1:
        return "column_powered"
    return "powered"
