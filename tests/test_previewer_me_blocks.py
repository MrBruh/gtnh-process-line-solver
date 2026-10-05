"""The ME blocks a layout builds, as the boxes AE2 draws them with (#338, ``previewer.me_blocks``).

Three kinds of claim, as for ``route_blocks``:

1. **The connections**: which sides of a cable AE2 joins, and to what. A part takes its side; a
   cable joins a cable of a joining colour with no part facing back, a controller, a GT ME hatch
   whose front faces it, and an attach stub joins the main network through its front.
2. **The shape**: AE2's own numbers (spike 8), turned to each side the way AE2 turns them, with
   the straight run rule per cable kind. Pinned against the Java switches by example.
3. **No two boxes of a cell share a volume**, whatever a cable carries: a property, because a shape
   that overlaps tears in the renderer and no example list reaches every combination.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gtnh_solver.dataset.ae_render import PART_DEVICES, load_ae_render
from gtnh_solver.dataset.me import PART_CABLES
from gtnh_solver.ir import (
    AEColor,
    CellBox,
    CellCoord,
    Facing,
    InputIR,
    LayoutResult,
    LayoutStatus,
    MECableCell,
    MECableKind,
    MEConfig,
    MEDeviceKind,
    MEMode,
    MENetworkLayout,
    MENetworkSpec,
    MEPlacedDevice,
)
from gtnh_solver.ir.geometry import FACE_DELTAS, OPPOSITE_FACE
from gtnh_solver.previewer.me_blocks import (
    SIDE_ORDER,
    Box,
    ConnectionTo,
    MECableBlock,
    MEConnection,
    MEPart,
    _ae_side,
    cable_boxes,
    me_blocks,
    part_box,
    toward,
)
from tests._me_fixtures import (
    MAIN,
    SUB,
    at,
    attached_line,
    comb,
    controller,
    gt_hatch_line,
)

RENDER = load_ae_render()


def _overlap(a: Box, b: Box) -> int:
    """The volume two boxes share, in cubic sixteenths; zero when they only touch."""
    volume = 1
    for axis in range(3):
        volume *= max(0, min(a[axis + 3], b[axis + 3]) - max(a[axis], b[axis]))
    return volume


def _reaches(box: Box, side: Facing) -> bool:
    """Whether ``box`` touches its block's face toward ``side``."""
    axis = next(i for i, d in enumerate(FACE_DELTAS[side]) if d)
    return box[axis + 3] == 16 if FACE_DELTAS[side][axis] > 0 else box[axis] == 0


def _blocks(problem: InputIR, layout: LayoutResult) -> dict[tuple[int, int, int], MECableBlock]:
    return {c.cell: c for c in me_blocks(problem, layout, RENDER).cables}


# --- frames ------------------------------------------------------------------------------------------


def test_toward_turns_a_cable_box_the_way_ae2_does() -> None:
    # PartCable.renderGlassConnection's own switch for the glass arm, case by case.
    arm = (6, 0, 6, 10, 6, 10)
    assert {side: toward(side, arm) for side in Facing} == {
        Facing.DOWN: (6, 0, 6, 10, 6, 10),
        Facing.EAST: (10, 6, 6, 16, 10, 10),
        Facing.NORTH: (6, 6, 0, 10, 10, 6),
        Facing.SOUTH: (6, 6, 10, 10, 10, 16),
        Facing.UP: (6, 10, 6, 10, 16, 10),
        Facing.WEST: (0, 6, 6, 6, 10, 10),
    }


def test_a_part_box_sits_on_the_face_it_is_given() -> None:
    # An import bus's outer plate, z 14..16 in its own frame, lies flat on the side it sits on.
    plate = (4, 4, 14, 12, 12, 16)
    placed = {side: part_box(RENDER.part_frame.axes[side.value], plate) for side in Facing}
    assert placed[Facing.DOWN] == (4, 0, 4, 12, 2, 12)
    assert placed[Facing.UP] == (4, 14, 4, 12, 16, 12)
    assert placed[Facing.NORTH] == (4, 4, 0, 12, 12, 2)
    assert placed[Facing.SOUTH] == (4, 4, 14, 12, 12, 16)
    assert placed[Facing.WEST] == (0, 4, 4, 2, 12, 12)
    assert placed[Facing.EAST] == (14, 4, 4, 16, 12, 12)


def test_a_block_turns_from_ae2s_frame_to_its_placed_front() -> None:
    # AE2 draws a block's front on its SOUTH face; placed facing north, that face is north.
    assert _ae_side(Facing.NORTH, Facing.NORTH) is Facing.SOUTH
    assert _ae_side(Facing.SOUTH, Facing.SOUTH) is Facing.SOUTH
    assert _ae_side(Facing.EAST, Facing.EAST) is Facing.SOUTH
    assert _ae_side(Facing.UP, Facing.EAST) is Facing.UP  # the top never turns


# --- connections -------------------------------------------------------------------------------------


def test_the_attached_line_connects_as_ae2_would() -> None:
    blocks = _blocks(*attached_line())
    stub = blocks[(0, 0, 2)]
    # The stub's front faces the main network outside the build, and its east side the row.
    assert [(c.side, c.to) for c in stub.connections] == [
        (Facing.WEST, "outside"),
        (Facing.EAST, "cable"),
    ]
    assert stub.role is not None
    assert stub.role.value == "attach"
    assert stub.machine_id == "stub"
    # (1,0,2) joins the cell north of it, and the row either side.
    assert {c.side for c in blocks[(1, 0, 2)].connections} == {
        Facing.NORTH,
        Facing.WEST,
        Facing.EAST,
    }
    # The export bus on (2,0,2) takes its north side from the cable: nothing connects through it.
    feed = blocks[(2, 0, 2)]
    assert [p.side for p in feed.parts] == [Facing.NORTH]
    assert Facing.NORTH not in {c.side for c in feed.connections}
    # The interface cell joins only the row below it; its east side holds the part.
    face = blocks[(1, 0, 1)]
    assert [c.side for c in face.connections] == [Facing.SOUTH]
    assert [(p.side, p.device.kind) for p in face.parts] == [(Facing.EAST, MEDeviceKind.INTERFACE)]


def test_a_cable_never_connects_through_a_side_a_part_takes() -> None:
    problem, layout = attached_line()
    # Put a part on (3,0,2)'s west side, facing the export bus cell: the two no longer join.
    network = layout.me_networks[0]
    blocker = MEPlacedDevice(
        machine_id="b",
        endpoint_id="extra",
        kind=MEDeviceKind.STORAGE_BUS,
        cell=CellCoord(x=3, y=0, z=2),
        side=Facing.WEST,
    )
    layout = layout.model_copy(
        update={
            "me_networks": [network.model_copy(update={"devices": [*network.devices, blocker]})]
        }
    )
    blocks = _blocks(problem, layout)
    assert Facing.EAST not in {c.side for c in blocks[(2, 0, 2)].connections}
    assert Facing.WEST not in {c.side for c in blocks[(3, 0, 2)].connections}


def test_a_gt_me_hatch_joins_only_the_cable_its_front_faces() -> None:
    problem, layout = gt_hatch_line()
    cable = _blocks(problem, layout)[(1, 0, 2)]
    hatch = next(c for c in cable.connections if c.to == "hatch")
    assert hatch.side is Facing.NORTH
    assert hatch.reports is None
    assert hatch.channels == 1
    # Turned to face west, its front meets no cable, and the cable draws nothing toward it.
    network = layout.me_networks[0]
    turned = network.model_copy(
        update={"devices": [d.model_copy(update={"side": Facing.WEST}) for d in network.devices]}
    )
    cable = _blocks(problem, layout.model_copy(update={"me_networks": [turned]}))[(1, 0, 2)]
    assert all(c.to != "hatch" for c in cable.connections)


def test_a_controller_joins_its_cable_and_carries_the_channels_into_it() -> None:
    problem, layout = comb(3, mode=MEMode.SUBNET, with_controller=True)
    network = layout.me_networks[0]
    counted = network.model_copy(
        update={
            "cables": [c.model_copy(update={"me_channels": 3}) for c in network.cables],
        }
    )
    blocks = _blocks(problem, layout.model_copy(update={"me_networks": [counted]}))
    root = blocks[(0, 0, 1)]
    block = next(c for c in root.connections if c.to == "block")
    assert block.side is Facing.NORTH
    assert block.reports is MECableKind.DENSE  # a controller reports a dense cable
    assert block.channels == 3
    assert block.colour is AEColor.ORANGE


def test_two_networks_of_different_colours_do_not_join() -> None:
    problem, layout = comb(2, mode=MEMode.SUBNET)
    network = layout.me_networks[0]
    # A second subnet's cable butts onto the row's east end: orange and blue never join.
    other = MENetworkLayout(
        id="blue",
        colour=AEColor.BLUE,
        cables=[MECableCell(cell=CellCoord(x=6, y=0, z=1), kind=MECableKind.SMART)],
    )
    problem = problem.model_copy(
        update={
            "me": MEConfig(
                networks=[
                    *problem.me.networks,
                    MENetworkSpec(id="blue", mode=MEMode.SUBNET, colour=AEColor.BLUE),
                ]
            )
        }
    )
    blocks = _blocks(problem, layout.model_copy(update={"me_networks": [network, other]}))
    assert Facing.EAST not in {c.side for c in blocks[(5, 0, 1)].connections}
    assert blocks[(6, 0, 1)].connections == ()


# --- channels ----------------------------------------------------------------------------------------


def _counted_attached_line() -> dict[tuple[int, int, int], MECableBlock]:
    problem, layout = attached_line()
    network = layout.me_networks[0]
    counts = {(0, 0, 2): 3, (1, 0, 2): 3, (2, 0, 2): 2, (3, 0, 2): 1, (4, 0, 2): 1, (1, 0, 1): 1}
    cables = [
        c.model_copy(update={"me_channels": counts[c.cell.as_tuple()]}) for c in network.cables
    ]
    layout = layout.model_copy(
        update={"me_networks": [network.model_copy(update={"cables": cables})]}
    )
    return _blocks(problem, layout)


def test_a_connection_carries_the_count_of_the_cable_farther_from_the_root() -> None:
    blocks = _counted_attached_line()
    by_side = {c.side: c.channels for c in blocks[(2, 0, 2)].connections}
    assert by_side == {Facing.WEST: 2, Facing.EAST: 1}
    stub = blocks[(0, 0, 2)]
    # Out of the stub run all of the network's channels.
    assert {c.side: c.channels for c in stub.connections} == {
        Facing.WEST: 3,
        Facing.EAST: 3,
    }


def test_lights_show_the_count_a_side_carries() -> None:
    blocks = _counted_attached_line()
    cell = blocks[(1, 0, 2)]
    arm = next(
        b
        for b in cell.boxes
        if b.part is None and b.box == toward(Facing.WEST, (6, 0, 6, 10, 5, 10))
    )
    assert [light.icon for light in arm.lights] == [
        "appliedenergistics2:MECableSmart03",
        "appliedenergistics2:MECableSmart10",
    ]
    tints = RENDER.colours[AEColor.FLUIX]
    assert [light.tint for light in arm.lights] == [tints.black_variant, tints.white_variant]


def test_between_two_dense_cables_a_side_shows_its_channels_in_fours() -> None:
    connection = MEConnection(Facing.WEST, "cable", MECableKind.DENSE, AEColor.FLUIX, 12)
    _, boxes = cable_boxes(MECableKind.DENSE, AEColor.FLUIX, (connection,), (), RENDER)
    arm = next(b for b in boxes if b.lights)
    assert arm.lights[0].icon == "appliedenergistics2:MECableSmart03"  # 12 channels, in fours


def test_a_part_arm_shows_the_one_channel_its_part_takes() -> None:
    blocks = _counted_attached_line()
    arm = next(
        b for b in blocks[(1, 0, 1)].boxes if b.part == 0 and b.lights and len(b.lights) == 2
    )
    assert arm.lights[0].icon == "appliedenergistics2:MECableSmart01"


# --- shape -------------------------------------------------------------------------------------------


def _conn(
    side: Facing, to: ConnectionTo = "cable", reports: MECableKind | None = MECableKind.SMART
) -> MEConnection:
    return MEConnection(side, to, reports, AEColor.FLUIX, 0)


@pytest.mark.parametrize(
    ("kind", "connections", "parts", "straight"),
    [
        # Two opposite cables and nothing else: a straight bar, every kind.
        (MECableKind.SMART, (_conn(Facing.WEST), _conn(Facing.EAST)), False, True),
        (MECableKind.GLASS, (_conn(Facing.WEST), _conn(Facing.EAST)), False, True),
        # A bend is never straight.
        (MECableKind.SMART, (_conn(Facing.NORTH), _conn(Facing.EAST)), False, False),
        # A part on a smart cable breaks the bar; on glass it does not (AE2's parts report glass).
        (MECableKind.SMART, (_conn(Facing.WEST), _conn(Facing.EAST)), True, False),
        (MECableKind.GLASS, (_conn(Facing.WEST), _conn(Facing.EAST)), True, True),
        # Glass beside a device block that reports more than glass draws its core and arms.
        (
            MECableKind.GLASS,
            (_conn(Facing.WEST, "block", MECableKind.DENSE), _conn(Facing.EAST)),
            False,
            False,
        ),
        # Dense runs straight only between dense neighbours.
        (MECableKind.DENSE, (_conn(Facing.WEST), _conn(Facing.EAST)), False, False),
        (
            MECableKind.DENSE,
            (
                _conn(Facing.WEST, reports=MECableKind.DENSE),
                _conn(Facing.EAST, "outside", MECableKind.DENSE),
            ),
            False,
            True,
        ),
    ],
)
def test_the_straight_run_rule_per_cable_kind(
    kind: MECableKind, connections: tuple[MEConnection, ...], parts: bool, straight: bool
) -> None:
    on = (MEPart(Facing.NORTH, _device(MEDeviceKind.EXPORT_BUS, Facing.NORTH)),) if parts else ()
    assert cable_boxes(kind, AEColor.FLUIX, connections, on, RENDER)[0] is straight


def test_a_plug_is_drawn_at_a_device_block_and_never_at_a_cable() -> None:
    plug = toward(Facing.NORTH, (5, 0, 5, 11, 4, 11))
    _, at_block = cable_boxes(
        MECableKind.SMART,
        AEColor.FLUIX,
        (_conn(Facing.NORTH, "block", MECableKind.DENSE),),
        (),
        RENDER,
    )
    _, at_cable = cable_boxes(MECableKind.SMART, AEColor.FLUIX, (_conn(Facing.NORTH),), (), RENDER)
    assert plug in [b.box for b in at_block]
    assert plug not in [b.box for b in at_cable]
    # The arm through the plug starts at the plug's face, so the two share nothing.
    assert toward(Facing.NORTH, (6, 4, 6, 10, 5, 10)) in [b.box for b in at_block]


def test_a_dense_cable_draws_no_plug_at_a_controller() -> None:
    # Dense to dense (a controller reports dense) is the one connection AE2 draws without a plug.
    _, boxes = cable_boxes(
        MECableKind.DENSE,
        AEColor.FLUIX,
        (_conn(Facing.NORTH, "block", MECableKind.DENSE),),
        (),
        RENDER,
    )
    assert [b.box for b in boxes] == [(3, 3, 3, 13, 13, 13), (4, 4, 0, 12, 12, 3)]


def test_a_dense_arm_stops_at_the_core_instead_of_reaching_into_it() -> None:
    # AE2's dense arm runs to y = 5 inside a core that starts at 3; drawn, it stops at the core.
    _, boxes = cable_boxes(
        MECableKind.DENSE,
        AEColor.FLUIX,
        (_conn(Facing.DOWN, reports=MECableKind.DENSE),),
        (),
        RENDER,
    )
    assert (4, 0, 4, 12, 3, 12) in [b.box for b in boxes]


def test_a_bus_on_a_smart_cable_gets_no_arm_and_an_interface_gets_a_short_one() -> None:
    # A bus reaches the smart core's face (arm length 5 = the core's 5), so AE2 draws nothing
    # between them; an interface stops at 4 and gets a one-sixteenth arm.
    def arms(kind: MEDeviceKind) -> list[Box]:
        part = MEPart(Facing.DOWN, _device(kind, Facing.DOWN))
        _, boxes = cable_boxes(MECableKind.SMART, AEColor.FLUIX, (), (part,), RENDER)
        sides = RENDER.parts[kind].render
        own = {part_box(RENDER.part_frame.axes["down"], r.box) for r in sides}
        return [b.box for b in boxes if b.part == 0 and b.box not in own]

    assert arms(MEDeviceKind.EXPORT_BUS) == []
    assert arms(MEDeviceKind.INTERFACE) == [(6, 4, 6, 10, 5, 10)]


def test_a_part_wears_its_front_toward_its_block_and_its_back_toward_the_cable() -> None:
    part = MEPart(Facing.EAST, _device(MEDeviceKind.EXPORT_BUS, Facing.EAST))
    _, boxes = cable_boxes(MECableKind.SMART, AEColor.FLUIX, (), (part,), RENDER)
    plate = next(b for b in boxes if b.part == 0)
    faces = dict(zip(SIDE_ORDER, plate.faces, strict=True))
    assert faces[Facing.EAST] == "appliedenergistics2:ItemPart.ExportBus"
    assert faces[Facing.WEST] == "appliedenergistics2:PartMonitorBack"
    assert faces[Facing.UP] == "appliedenergistics2:PartExportSides"


def test_an_fc_part_wears_its_own_front() -> None:
    part = MEPart(Facing.UP, _device(MEDeviceKind.DUAL_INTERFACE, Facing.UP))
    _, boxes = cable_boxes(MECableKind.SMART, AEColor.FLUIX, (), (part,), RENDER)
    fronts = {dict(zip(SIDE_ORDER, b.faces, strict=True))[Facing.UP] for b in boxes if b.part == 0}
    assert "ae2fc:part_fluid_interface" in fronts


def test_a_subnet_wears_its_colour_and_an_attached_network_fluix() -> None:
    _, boxes = cable_boxes(MECableKind.SMART, AEColor.ORANGE, (_conn(Facing.UP),), (), RENDER)
    assert {f for b in boxes for f in b.faces if f} == {
        "appliedenergistics2:MECovered_Orange",  # a smart core wears the covered icon
        "appliedenergistics2:MESmart_Orange",
    }
    _, boxes = cable_boxes(MECableKind.SMART, AEColor.FLUIX, (_conn(Facing.UP),), (), RENDER)
    assert "appliedenergistics2:ItemPart.CableSmart" in {f for b in boxes for f in b.faces}


def test_an_arm_toward_a_coloured_neighbour_borrows_its_colour() -> None:
    # Fluix beside orange: AE would merge them, but the arm is drawn in the neighbour's colour.
    orange = MEConnection(Facing.UP, "cable", MECableKind.SMART, AEColor.ORANGE, 0)
    _, boxes = cable_boxes(MECableKind.SMART, AEColor.FLUIX, (orange,), (), RENDER)
    arm = next(b for b in boxes if b.box == toward(Facing.UP, (6, 0, 6, 10, 5, 10)))
    assert arm.colour is AEColor.ORANGE
    assert "appliedenergistics2:MESmart_Orange" in arm.faces


def test_a_stubs_outside_end_is_closed_and_a_run_end_is_open() -> None:
    blocks = _blocks(*attached_line())
    stub = blocks[(0, 0, 2)]
    out = next(b for b in stub.boxes if _reaches(b.box, Facing.WEST))
    inner = next(b for b in stub.boxes if _reaches(b.box, Facing.EAST))
    faces = dict(zip(SIDE_ORDER, out.faces, strict=True))
    assert faces[Facing.WEST] is not None
    assert faces[Facing.EAST] is None
    assert dict(zip(SIDE_ORDER, inner.faces, strict=True))[Facing.EAST] is None


# --- block devices -----------------------------------------------------------------------------------


def test_a_lone_controller_is_drawn_powered_with_its_lights() -> None:
    problem, layout = comb(2, mode=MEMode.SUBNET, with_controller=True)
    (device,) = me_blocks(problem, layout, RENDER).devices
    assert device.look == "powered"
    assert device.machine_id == "ctrl"
    (box,) = device.boxes
    assert box.box == (0, 0, 0, 16, 16, 16)
    assert set(box.faces) == {"appliedenergistics2:BlockControllerPowered"}
    assert [light.icon for light in box.lights] == ["appliedenergistics2:BlockControllerLights"]


def _controller_looks(cells: list[tuple[int, int, int]]) -> dict[tuple[int, int, int], str | None]:
    """The look of each controller of a subnet built of controllers at ``cells``."""
    problem, layout = comb(1, mode=MEMode.SUBNET)
    extra = [controller(f"c{i}") for i in range(len(cells))]
    problem = problem.model_copy(
        update={
            "machines": [*problem.machines, *extra],
            "bounding_region": CellBox(sx=7, sy=3, sz=3),
        }
    )
    placed = [at(m.id, *cell, Facing.NORTH) for m, cell in zip(extra, cells, strict=True)]
    layout = layout.model_copy(update={"placements": [*layout.placements, *placed]})
    return {d.cell: d.look for d in me_blocks(problem, layout, RENDER).devices}


def test_controllers_side_by_side_draw_as_a_column_and_a_cluster() -> None:
    # A row of three along x: the middle one has a controller on both sides along one axis.
    row = _controller_looks([(1, 1, 0), (2, 1, 0), (3, 1, 0)])
    assert row == {(1, 1, 0): "powered", (2, 1, 0): "column_powered", (3, 1, 0): "powered"}
    # A plus sign: its centre has neighbours both ways along x and along z, the inside of a cluster.
    plus = _controller_looks([(2, 1, 1), (1, 1, 1), (3, 1, 1), (2, 1, 0), (2, 1, 2)])
    assert plus[(2, 1, 1)] == "inside_a"  # (2 + 1 + 1) is even
    assert plus[(1, 1, 1)] == "powered"


def test_every_icon_drawn_is_one_the_render_data_names() -> None:
    known = RENDER.icon_names()
    for problem, layout in (attached_line(), comb(4, mode=MEMode.SUBNET, with_controller=True)):
        blocks = me_blocks(problem, layout, RENDER)
        assert blocks.icon_names() <= known
        assert blocks.light_icons() <= known
        assert blocks.light_icons()  # channel lights at least


# --- properties --------------------------------------------------------------------------------------


def _device(kind: MEDeviceKind, side: Facing) -> MEPlacedDevice:
    return MEPlacedDevice(
        machine_id="m", endpoint_id="e", kind=kind, cell=CellCoord(x=0, y=0, z=0), side=side
    )


_PART_KINDS = sorted(PART_DEVICES, key=lambda k: k.value)


@st.composite
def _cable_sides(
    draw: st.DrawFn,
) -> tuple[MECableKind, tuple[MEConnection, ...], tuple[MEPart, ...]]:
    """A cable of any kind with any mix of connections and (on a part-taking cable) parts."""
    kind = draw(st.sampled_from(list(MECableKind)))
    connections: list[MEConnection] = []
    parts: list[MEPart] = []
    for side in SIDE_ORDER:
        what = draw(st.sampled_from(["none", "cable", "block", "hatch", "outside", "part"]))
        if what == "part":
            if kind in PART_CABLES:
                parts.append(MEPart(side, _device(draw(st.sampled_from(_PART_KINDS)), side)))
            continue
        if what == "none":
            continue
        reports = None if what == "hatch" else draw(st.sampled_from([*MECableKind]))
        connections.append(
            MEConnection(
                side,
                what,  # type: ignore[arg-type]
                reports,
                draw(st.sampled_from([AEColor.FLUIX, AEColor.ORANGE])),
                draw(st.integers(min_value=0, max_value=40)),
            )
        )
    return kind, tuple(connections), tuple(parts)


@given(_cable_sides())
def test_no_two_boxes_of_a_cell_overlap(
    sides: tuple[MECableKind, tuple[MEConnection, ...], tuple[MEPart, ...]],
) -> None:
    """Shared volume means coplanar faces fighting for the same pixels; touching is the legal case."""
    kind, connections, parts = sides
    _, boxes = cable_boxes(kind, AEColor.FLUIX, connections, parts, RENDER)
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert _overlap(a.box, b.box) == 0, f"{kind.value}: {a.box} overlaps {b.box}"


@given(_cable_sides())
def test_the_shape_stays_in_its_block_and_reaches_every_connection_and_part(
    sides: tuple[MECableKind, tuple[MEConnection, ...], tuple[MEPart, ...]],
) -> None:
    kind, connections, parts = sides
    _, boxes = cable_boxes(kind, AEColor.FLUIX, connections, parts, RENDER)
    for box in boxes:
        x0, y0, z0, x1, y1, z1 = box.box
        assert 0 <= x0 < x1 <= 16
        assert 0 <= y0 < y1 <= 16
        assert 0 <= z0 < z1 <= 16
        assert len(box.faces) == 6
        assert any(box.faces)
        for light in box.lights:
            assert len(light.faces) == 6
    for connection in connections:
        assert any(_reaches(b.box, connection.side) for b in boxes if b.part is None)
    for index, part in enumerate(parts):
        assert any(_reaches(b.box, part.side) for b in boxes if b.part == index)


_GRID = st.tuples(*(st.integers(min_value=0, max_value=2) for _ in range(3)))


@st.composite
def _networks(draw: st.DrawFn) -> tuple[InputIR, LayoutResult]:
    """Cables scattered over a 3x3x3 grid in one or two networks, with parts on their sides."""
    cells = draw(st.lists(_GRID, min_size=1, max_size=14, unique=True))
    cables: dict[str, list[MECableCell]] = {MAIN: [], SUB: []}
    devices: dict[str, list[MEPlacedDevice]] = {MAIN: [], SUB: []}
    for cell in cells:
        network = draw(st.sampled_from([MAIN, SUB]))
        kind = draw(st.sampled_from(list(MECableKind)))
        cables[network].append(
            MECableCell(
                cell=CellCoord(x=cell[0], y=cell[1], z=cell[2]),
                kind=kind,
                me_channels=draw(st.integers(min_value=0, max_value=32)),
            )
        )
        for side in draw(st.lists(st.sampled_from(list(Facing)), max_size=3, unique=True)):
            devices[network].append(
                replace_cell(_device(draw(st.sampled_from(_PART_KINDS)), side), cell)
            )
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=3, sz=3),
        machines=[],
        nets=[],
        me=MEConfig(
            networks=[
                MENetworkSpec(id=MAIN, mode=MEMode.ATTACHED),
                MENetworkSpec(id=SUB, mode=MEMode.SUBNET, colour=AEColor.ORANGE),
            ]
        ),
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        me_networks=[
            MENetworkLayout(
                id=MAIN, colour=AEColor.FLUIX, cables=cables[MAIN], devices=devices[MAIN]
            ),
            MENetworkLayout(
                id=SUB, colour=AEColor.ORANGE, cables=cables[SUB], devices=devices[SUB]
            ),
        ],
    )
    return problem, layout


def replace_cell(device: MEPlacedDevice, cell: tuple[int, int, int]) -> MEPlacedDevice:
    return device.model_copy(update={"cell": CellCoord(x=cell[0], y=cell[1], z=cell[2])})


@given(_networks())
def test_connections_are_mutual_and_never_through_a_part(
    built: tuple[InputIR, LayoutResult],
) -> None:
    """AE2 joins two cables or neither: a side one of them draws an arm through, the other must too
    (or the run breaks at the joint), and a side holding a part joins nothing."""
    blocks = {c.cell: c for c in me_blocks(*built, RENDER).cables}
    for cell, block in blocks.items():
        for connection in block.connections:
            assert connection.side not in {p.side for p in block.parts}
            if connection.to != "cable":
                continue
            dx, dy, dz = FACE_DELTAS[connection.side]
            there = blocks[(cell[0] + dx, cell[1] + dy, cell[2] + dz)]
            back = OPPOSITE_FACE[connection.side]
            assert back in {c.side for c in there.connections}
        for i, a in enumerate(block.boxes):
            for b in block.boxes[i + 1 :]:
                assert _overlap(a.box, b.box) == 0
