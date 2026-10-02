"""hatch_locks - the product each multiblock output hatch or bus must be locked to (#120).

GT, not the layout, decides which hatch a product lands in. A multiblock fills its output hatches
first fit: in 2.8.4 ``MTEMultiBlockBase.addOutput`` walks ``mOutputHatches`` in the order the
structure check found them, skipping any that refuses the fluid (``dumpFluid``, lines 1532-1564),
and in 2.9 ``FluidEjectionHelper`` offers each fluid to the hatches filtered to it first and then to
the unfiltered ones (lines 84-159). Output buses take items the same way (``dumpItem`` /
``ItemEjectionHelper``). An unlocked hatch takes whatever arrives first, so on a machine with two
products of a kind the pipe the router laid from one hatch carries whichever product reached it,
not the one it was routed for. A player pins it by locking every such hatch to its own product:

- **a fluid output hatch** through its ``Locked Fluid`` slot, which also switches it to "Outputs 1
  specific Fluid" (``MTEHatchOutput``, mode 9). In 2.9 an empty locked hatch receives nothing at all
  (``isFilteredToFluid`` is false until a fluid is set), so the slot must be set, not left to lock
  itself on the first fluid the way 2.8.4 allowed;
- **an output bus** through its output filter slot (``MTEHatchOutputBus.lockedItem``, both packs).

Every hatch of the kind needs it, not all but one: a locked hatch that fills spills into any
unlocked one. A tower that fills by layer is first fit too, only per layer: it hands fluid output
``i`` to the hatches on layer ``i`` alone (``MTEDistillationTower.addFluidOutputs``), first fit
among them, so each layer competes on its own (#299). Which hatches need it::

    output hatches and buses of a placed multiblock, per kind and output layer
      |  (Port.output_layer: a tower's fluid outputs, one group per layer; everything else,
      |   a tower's output buses included, has none and groups by kind alone)
      v
    groups whose hatches carry two or more distinct products
      |  one product, or one hatch: none, every product of the group has only one place to go.
      v  A tower layer normally carries one product, so its hatch is left unlocked
    every hatch of such a group, locked to its own port's product

Two products land on one layer only on a tower time-sharing recipes whose different fluids share an
index; the recipes never run at once, but each hatch still needs its lock, or the pipe from one
carries the other's product whenever its recipe runs. A spare output hatch, which stands on a layer
no product uses so the tower forms, serves no port and is never locked.

The previewer shows the lock on the hatch's hover. A single block has no hatches: how its outputs
leave it is ``output_faces``.
"""

from __future__ import annotations

from collections import defaultdict

from gtnh_solver.ir import InputIR, IODirection, LayoutResult
from gtnh_solver.ir.geometry import Cell, rotated_slot
from gtnh_solver.ir.nets import placement_index
from gtnh_solver.system_io import port_resource

#: Where a player sets the lock, per output kind, in the words GT's own GUI uses: the fluid hatch's
#: slot is labelled "Locked Fluid" (``GT5U.machines.hatch_output.lockfluid.label`` in both packs),
#: and a bus's is its output filter ("Drag item from NEI to set output filter").
LOCK_SLOT = {"OutputHatch": "Locked Fluid slot", "OutputBus": "output filter slot"}

#: The hatch kinds a multiblock's products leave through (``gregtech.api.enums.HatchElement``).
OUTPUT_KINDS = frozenset(LOCK_SLOT)


def hatch_locks(problem: InputIR, layout: LayoutResult) -> dict[tuple[str, Cell], str]:
    """``(machine id, hatch cell) -> the product`` for every output hatch or bus that must be locked.

    A hatch is named by the casing cell it occupies (``PlacedHatch.cell``), which is unique within
    its machine. The product is its port's resource, verbatim as the plan spells it. Hatches with
    no port (maintenance, muffler, a tower's spare), input hatches, and a tower layer's hatch with
    that layer to itself never appear.
    """
    machines = {m.id for m in problem.machines}
    ports = {(m.id, p.id): p for m in problem.machines for p in m.faces.ports}
    groups: dict[tuple[str, str, int | None], list[tuple[Cell, str]]] = defaultdict(list)
    for hatch in layout.hatches:
        port = ports.get((hatch.machine_id, hatch.port_id)) if hatch.port_id else None
        if hatch.machine_id not in machines or port is None:
            continue
        if port.direction is not IODirection.OUTPUT or hatch.kind not in OUTPUT_KINDS:
            continue
        group = (hatch.machine_id, hatch.kind, port.output_layer)
        groups[group].append((hatch.cell.as_tuple(), port_resource(port)))
    return {
        (machine_id, cell): product
        for (machine_id, _, _), members in groups.items()
        if len({product for _, product in members}) > 1
        for cell, product in members
    }


def hatch_layers(problem: InputIR, layout: LayoutResult) -> dict[tuple[str, Cell], int]:
    """``(machine id, hatch cell) -> the output layer GT fills it from``, for every output hatch on
    a tower that fills by layer, a spare included (#299). Read off the slot the hatch stands on
    (``HatchSlot.output_layer``); a hatch on a cell in no layer (a tower's base, or any machine
    that fills first fit) does not appear.
    """
    machines = {m.id: m for m in problem.machines}
    placements = placement_index(layout.placements)
    layers: dict[tuple[str, Cell], int] = {}
    for machine_id, placement in placements.items():
        machine = machines.get(machine_id)
        if machine is None or not machine.output_layers:
            continue
        origin = placement.cell
        for slot in machine.hatch_slots:
            if slot.output_layer is None:
                continue
            dx, dy, dz = rotated_slot(
                slot.offset.as_tuple(), machine.footprint, placement.orientation
            )
            layers[(machine_id, (origin.x + dx, origin.y + dy, origin.z + dz))] = slot.output_layer
    return {
        (h.machine_id, h.cell.as_tuple()): layers[(h.machine_id, h.cell.as_tuple())]
        for h in layout.hatches
        if h.kind == "OutputHatch" and (h.machine_id, h.cell.as_tuple()) in layers
    }
