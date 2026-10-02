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
unlocked one. Which hatches need it::

    output hatches and buses of a placed multiblock, per kind
      |  a layered tower: none. It fills layer i with output i whatever is locked
      v  (MTEDistillationTower.addFluidOutputs), and a lock that disagrees voids the product
      |
    kinds whose hatches carry two or more distinct products
      |  one product, or one hatch: none, every product of the kind has only one place to go
      v
    every hatch of such a kind, locked to its own port's product

The previewer shows the lock on the hatch's hover. A single block has no hatches: how its outputs
leave it is ``output_faces``.
"""

from __future__ import annotations

from collections import defaultdict

from gtnh_solver.ir import InputIR, IODirection, LayoutResult, Machine
from gtnh_solver.ir.geometry import Cell
from gtnh_solver.system_io import port_resource

#: Where a player sets the lock, per output kind, in the words GT's own GUI uses: the fluid hatch's
#: slot is labelled "Locked Fluid" (``GT5U.machines.hatch_output.lockfluid.label`` in both packs),
#: and a bus's is its output filter ("Drag item from NEI to set output filter").
LOCK_SLOT = {"OutputHatch": "Locked Fluid slot", "OutputBus": "output filter slot"}

#: The hatch kinds a multiblock's products leave through (``gregtech.api.enums.HatchElement``).
OUTPUT_KINDS = frozenset(LOCK_SLOT)

#: The recipe maps of the towers that send output ``i`` to the hatches on structure layer ``i``
#: rather than first fit, so a lock there is never what decides where a product goes. The
#: distillation tower map is run by GT's Distillation Tower, both Mega Distillation Towers (the
#: bartworks one in 2.8.4, GT's own in 2.9) and GT++'s Dangote Distillus in tower mode; the
#: sparging map by GT++'s Sparge Tower (``addFluidOutputs`` overrides in each).
LAYERED_RECIPE_MAPS = frozenset({"gt.recipe.distillationtower", "gtpp.recipe.lftr.sparging"})


def fills_by_layer(machine: Machine) -> bool:
    """Whether ``machine`` sends each output to its own structure layer instead of first fit."""
    return machine.recipe_map in LAYERED_RECIPE_MAPS


def hatch_locks(problem: InputIR, layout: LayoutResult) -> dict[tuple[str, Cell], str]:
    """``(machine id, hatch cell) -> the product`` for every output hatch or bus that must be locked.

    A hatch is named by the casing cell it occupies (``PlacedHatch.cell``), which is unique within
    its machine. The product is its port's resource, verbatim as the plan spells it. Hatches with
    no port (maintenance, muffler), input hatches, and hatches of a machine that fills by layer
    never appear.
    """
    machines = {m.id: m for m in problem.machines}
    ports = {(m.id, p.id): p for m in problem.machines for p in m.faces.ports}
    groups: dict[tuple[str, str], list[tuple[Cell, str]]] = defaultdict(list)
    for hatch in layout.hatches:
        machine = machines.get(hatch.machine_id)
        port = ports.get((hatch.machine_id, hatch.port_id)) if hatch.port_id else None
        if machine is None or port is None or port.direction is not IODirection.OUTPUT:
            continue
        if hatch.kind not in OUTPUT_KINDS or fills_by_layer(machine):
            continue
        groups[(hatch.machine_id, hatch.kind)].append((hatch.cell.as_tuple(), port_resource(port)))
    return {
        (machine_id, cell): product
        for (machine_id, _), members in groups.items()
        if len({product for _, product in members}) > 1
        for cell, product in members
    }
