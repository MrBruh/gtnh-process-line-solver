"""How much an item pipe moves, per size - the rule data a route's gauge is chosen from (#165).

Sibling of :mod:`gtnh_solver.dataset.voltage`, and the same trade: shared rule DATA, so the router
sizes a pipe from it and a validator can re-derive the same verdict with its own arithmetic
(docs/ARCHITECTURE.md decision 4). Cables size by summed amperage; item pipes size by this.

**GT counts insertions, not items.** Every figure below is read from GT5-Unofficial at the tag the
pack pins (5.09.51.482 for 2.8.4; 5.09.54.20 for 2.9 has the same arithmetic), and the unit is the
first thing to get right, because the obvious one is wrong::

    MTEItemPipe.onPostTick          every 10 ticks, and the counter resets once per window
      (lines 198-202)                 of mTickTime ticks
    the transfer loop               one incrementTransferCounter(1) per successful
      (lines 212-224)                 sendItemStack, whatever it moved
    insertItemStackIntoTileEntity   moveMultipleItemStacks(..., 64, 1, 64, 1, 1): ONE stack
      (lines 326-337)                 of at most 64 items into ONE inventory
    pipeCapacityCheck               the pipe stops once its count reaches
      (lines 345-362)                 getPipeCapacity() = its slot count, per window
    scanPipes -> sortMapByValues    targets are tried nearest first
      (lines 215-217)

So a pipe's capacity is a number of *insertions* per window, each one landing in a single
inventory. GT's tooltip says "Item Capacity: 1 Stacks/2 sec", which reads as 32 items a second,
and by that reading the plain tin pipe carries the parallel sand line five times over. In game it
fed one hammer in three. Items-per-second is the wrong unit: an insertion that tops up one hammer
by four items spends the whole budget as surely as one that moves a full stack, and the nearest
hammer always has room for a few more.

**What GT settles, and what it does not.** GT fixes each size's insertions per window exactly
(:data:`ITEM_PIPE_CAPACITY`) and caps an insertion at one stack (:data:`ITEMS_PER_INSERTION`). It
does not fix how often an endpoint has to be served for its machine never to run dry: that depends
on the covers pushing items in, on recipe times and on buffer sizes, none of which a plan carries.
:data:`STREAM_SERVICE_TICKS` is therefore **not a GT figure**. It comes from the one measurement
there is, the maintainer's in-game build of the parallel sand line (tests/golden/schematic/README.md):
a plain tin pipe, which GT gives exactly one insertion per 40 ticks, fed exactly one of three stone
hammers. One insertion per 40 ticks is enough for one endpoint and not for two, so every endpoint is
taken to need one insertion per 40 ticks, the most conservative value that observation allows.
"""

from __future__ import annotations

import math

from gtnh_solver.ir import PipeSize

#: The stand-in item pipe's capacity per size, as GT registers it: ``(insertions, window_ticks)``,
#: i.e. how many insertions it makes per window. Tin is the item pipe the stand-in policy draws
#: (``dataset/pipes.py``) and GT's slowest, so a size chosen from these figures is never short for
#: a better material at the same size.
#:
#: GT builds every item pipe from one number, the huge size's slot count ``H``
#: (``LoaderMetaPipeEntities.registerItemPipes``: tin is ``H = 2`` at lines 777-781, mIDs 5589 up),
#: and scales the rest in ``ItemPipeBuilder.build`` as slots ``max(H / d, 1)`` per window
#: ``max(d / H, 1) * 20`` ticks, where ``d`` is 16/8/4/2 for tiny/small/normal/large. The huge size
#: takes ``H`` slots and the constructor's default 20-tick window (``MTEItemPipe`` lines 58-60):
#:
#: ====== ===== ======================== =============================== =====
#: size   mID   slots (builder line)     window ticks (builder line)     moves
#: ====== ===== ======================== =============================== =====
#: tiny   5589  max(2/16, 1) = 1 (1378)  max(16/2, 1)*20 = 160 (1381)    1/160
#: small  5590  max(2/8, 1)  = 1 (1391)  max(8/2, 1)*20  = 80  (1394)    1/80
#: normal 5591  max(2/4, 1)  = 1 (1405)  max(4/2, 1)*20  = 40  (1408)    1/40
#: large  5592  max(2/2, 1)  = 1 (1418)  max(2/2, 1)*20  = 20  (1421)    1/20
#: huge   5593  H            = 2 (1431)  constructor default 20          2/20
#: ====== ===== ======================== =============================== =====
ITEM_PIPE_CAPACITY: dict[PipeSize, tuple[int, int]] = {
    PipeSize.TINY: (1, 160),
    PipeSize.SMALL: (1, 80),
    PipeSize.NORMAL: (1, 40),
    PipeSize.LARGE: (1, 20),
    PipeSize.HUGE: (2, 20),
}

#: The most one insertion carries: a single stack of at most 64 items
#: (``MTEItemPipe.insertItemStackIntoTileEntity``, lines 326-337). An endpoint moving more than this
#: per service interval needs a second insertion in the same interval.
ITEMS_PER_INSERTION = 64

#: How often one endpoint must be served: one insertion per this many ticks. **Not a GT figure** -
#: see the module docstring. Taken from the in-game observation that a plain tin pipe (one insertion
#: per 40 ticks, :data:`ITEM_PIPE_CAPACITY`) fed one hammer of three: enough for one endpoint, not
#: for two, so an endpoint needs one per 40 ticks. Revisit when a second in-game measurement exists.
STREAM_SERVICE_TICKS = 40

#: Float slack for :func:`endpoint_insertions`: a rate that lands exactly on a stack boundary
#: (1.6 items/t is one full stack per 40 ticks) must not round up to a second insertion because
#: the product came out as 1.0000000000000002.
_EPS = 1e-9


def endpoint_insertions(rate: float) -> int:
    """Insertions one item endpoint needs per :data:`STREAM_SERVICE_TICKS`, at ``rate`` items/t.

    At least one, because an endpoint that is never reached never runs, however little it moves;
    and one more for every further stack it moves in the interval, because an insertion carries one
    stack at most. The analogue of :func:`~gtnh_solver.dataset.voltage.amp_load`: the per-endpoint
    figure a caller sums over the endpoints a run serves.
    """
    stacks = rate * STREAM_SERVICE_TICKS / ITEMS_PER_INSERTION
    return max(1, math.ceil(stacks - _EPS))


def item_pipe_insertions(size: PipeSize) -> float:
    """How many insertions a stand-in item pipe of ``size`` makes per :data:`STREAM_SERVICE_TICKS`.

    GT's own window varies by size (160 ticks for tiny, 20 for large), so each is scaled to the one
    service interval the demand side is expressed in: normal makes 1, large 2, huge 4.
    """
    insertions, window = ITEM_PIPE_CAPACITY[size]
    return insertions * STREAM_SERVICE_TICKS / window


def item_pipe_size_for(insertions: int) -> PipeSize | None:
    """The smallest item pipe that makes ``insertions`` per service interval, or ``None``.

    ``None`` means no size of the stand-in material is enough, which is a real answer rather than an
    error: the fix is a faster material (GT's brass, electrum and platinum pipes), which the stand-in
    policy does not choose yet, so the caller decides what to lay and says so.
    """
    for size in PipeSize:  # declaration order is the ladder, smallest first
        if item_pipe_insertions(size) >= insertions:
            return size
    return None
