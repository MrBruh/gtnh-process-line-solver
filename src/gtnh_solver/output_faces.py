"""output_faces - the one face each single block auto-outputs through, and the faces that need a cover.

GT empties a single block in one of two ways, and a build has to say which, per face:

- **auto-output**, through exactly one face. A basic machine pushes items AND fluids out of its
  output face (``mFacing``) and nowhere else (``MTEBasicMachine`` 2.8.4 lines 583-610, 2.9 lines
  612-633). A Super Tank pushes fluid out of its front (``MTEDigitalTankBase``, ``mOutputFluid``).
  An Item Filter pushes out of its back, with no toggle at all (``MTEBuffer.moveItems``);
- **a cover** on any other face: a conveyor for items, a pump for fluids. A Super Chest has no
  auto-output (``MTEDigitalChestBase`` only moves stock between its own slots), so every item taken
  out of one leaves through a conveyor cover.

The layout says which faces each output leaves a block through (an ``AutoConnection``'s source face,
an output route's terminal face). This module turns that into one reading::

    output faces of a placed single block
      |  a Super Chest: none auto-outputs, every one is a conveyor
      v
    the auto face: the face an AutoConnection ejects through if there is one (GT ejects there or the
      |            connection is not free), else the face carrying the most outputs, ties broken in
      |            the routers' FACE_ORDER
      v
    every other output face: a cover, conveyor for an item, pump for a fluid

The previewer draws the arrow on the auto face and a cover marker on each cover face; the
``.schematic`` export writes the auto face as the block's output facing and warns about each cover.
Reading it from here is what keeps the picture and the build from disagreeing (docs/DOMAIN.md). A
multiblock is left out: its outputs leave through hatches, each with its own front, not through a
face of the controller's box.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from gtnh_solver.ir import Commodity, Facing, InputIR, IODirection, LayoutResult

#: What a cover on an output face is, by what leaves through it.
COVER_FOR = {Commodity.ITEM: "conveyor", Commodity.FLUID: "pump"}

#: The block types that never auto-output, so every output face of one takes a cover.
NO_AUTO_OUTPUT_TYPES = frozenset({"Super Chest"})

#: Face tie-break order, the routers' own (``router._grid.FACE_ORDER``), restated so this module
#: stays importable by the renderers without pulling in the router.
_FACE_ORDER = (Facing.SOUTH, Facing.NORTH, Facing.EAST, Facing.WEST, Facing.UP, Facing.DOWN)


@dataclass(frozen=True)
class CoverFace:
    """An output face GT empties with a cover: which face, which cover, and the nets through it."""

    face: Facing
    cover: str  # "conveyor" (items) or "pump" (fluids), COVER_FOR
    net_ids: tuple[str, ...]


@dataclass(frozen=True)
class BlockOutputs:
    """How one placed single block's outputs leave it.

    ``auto_face`` is the one face it auto-outputs through, or ``None`` when it auto-outputs through
    none (a Super Chest, or a block with no output on a face at all); ``auto_items`` and
    ``auto_fluids`` say what leaves through it, which is what a basic machine's ``mItemTransfer`` and
    ``mFluidTransfer`` switch on. ``covers`` are the other output faces, in face order.
    """

    machine_id: str
    auto_face: Facing | None
    auto_items: bool
    auto_fluids: bool
    covers: tuple[CoverFace, ...]


def output_faces(problem: InputIR, layout: LayoutResult) -> dict[str, BlockOutputs]:
    """``machine_id -> BlockOutputs`` for every placed single block with an output on a face.

    Power is not an output here (a cable is not emptied by a cover or an auto-output), and neither is
    a commodity riding ME, which leaves through no face. A block whose every output rides ME, or
    that has none, is absent.
    """
    machines = {m.id: m for m in problem.machines}
    placed = {p.machine_id for p in layout.placements}
    ports = {(m.id, p.id): p for m in problem.machines for p in m.faces.ports}
    nets = {n.id: n for n in problem.nets}
    # machine -> face -> [(commodity, net id)] for every output leaving through that face.
    leaving: dict[str, dict[Facing, list[tuple[Commodity, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    auto_at: dict[str, Facing] = {}
    for connection in layout.auto_connections:
        auto_at.setdefault(connection.source_machine_id, connection.source_face)
        net = nets.get(connection.net_id)
        if net is not None:
            leaving[connection.source_machine_id][connection.source_face].append(
                (net.commodity, net.id)
            )
    for route in layout.routes:
        for terminal in route.terminals:
            port = ports.get((terminal.machine_id, terminal.port_id))
            if port is None or port.direction is not IODirection.OUTPUT:
                continue
            if port.commodity is Commodity.POWER:
                continue
            leaving[terminal.machine_id][terminal.face].append((port.commodity, route.net_id))

    result: dict[str, BlockOutputs] = {}
    for machine_id, by_face in leaving.items():
        machine = machines.get(machine_id)
        if machine is None or machine_id not in placed or machine.footprint.volume != 1:
            continue
        auto_face = (
            None
            if machine.type in NO_AUTO_OUTPUT_TYPES
            else _auto_face(machine_id, by_face, auto_at)
        )
        auto = by_face.get(auto_face, []) if auto_face is not None else []
        covers = tuple(
            CoverFace(
                face=face,
                cover=COVER_FOR[by_face[face][0][0]],
                net_ids=tuple(dict.fromkeys(net_id for _, net_id in by_face[face])),
            )
            for face in _FACE_ORDER
            if face in by_face and face is not auto_face
        )
        result[machine_id] = BlockOutputs(
            machine_id=machine_id,
            auto_face=auto_face,
            auto_items=any(c is Commodity.ITEM for c, _ in auto),
            auto_fluids=any(c is Commodity.FLUID for c, _ in auto),
            covers=covers,
        )
    return result


def _auto_face(
    machine_id: str,
    by_face: dict[Facing, list[tuple[Commodity, str]]],
    auto_at: dict[str, Facing],
) -> Facing:
    """The face ``machine_id`` auto-outputs through (module docstring): an AutoConnection's, else
    the face carrying the most outputs, ties in face order."""
    if machine_id in auto_at:
        return auto_at[machine_id]
    faces = [face for face in _FACE_ORDER if face in by_face]
    return max(faces, key=lambda face: (len(by_face[face]), -faces.index(face)))
