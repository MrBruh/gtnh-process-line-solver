"""router.item_pipes - the insertions GT charges each block of a laid item pipe (#200).

``_pipe_size`` (``router/core.py``) sizes a run from its endpoints, and that alone can undersize
one: when a producer splits its output between consumers, GT's nearest-first delivery makes more
streams than either side has endpoints, and the block nearest that producer pays for all of them.
This module is the router's reading of where GT's transfer loop sends a run's items and which
blocks pay for each delivery (``MTEItemPipe.onPostTick``, GT5-Unofficial 5.09.51.482 lines 210-224;
5.09.54.20 runs the same loop), so the router can lay a size no block of the run outgrows::

    terminals + legs
      |  [1] joins     every block of the laid tree and the blocks its legs join it to
      v
      |  [2] sides     each terminal's port: an output is a sender, an input a target; each side's
      |                endpoints get their fraction of it, and the pipe its flow in items/t
      v
      |  [3] hops      from every sender's block to every block of the tree, breadth first
      v
      |  [4] streams   sender-target pairs, nearest first: each pair moves what both still have
      |                (on a merged run, every sender feeds every filter its share instead)
      v
      [5] charges      a stream's insertions land on every block no farther from its sender than
                       its target, ties included: the blocks GT's scan reached first

The issue's own case, two producers splitting 0.2 items/t between two consumers. Nearest first,
``a`` serves ``c1``, ``b`` serves ``c2``, and ``a``'s rest goes the long way to ``c2``: three
streams, where either side has two endpoints. Each stream moves under a stack per 40 ticks, so
each costs one insertion, and the middle blocks pay for three::

    c1 (0.1 in) -- a (0.15 out) -- b (0.05 out) -- c2 (0.1 in)
        2               3               3               2         insertions per 40 ticks

**The same GT rule as the validator, written separately** (docs/ARCHITECTURE.md decision 4).
``validator/core.py`` (``_check_item_pipe_throughput``) derives the same charges from the published
route, and this module shares none of its code: it imports only the rule data and
:func:`~gtnh_solver.dataset.endpoint_insertions` from ``dataset``, and the validator never calls
that. A bug in either reading then shows up as a size the router lays and the validator refuses
(``partial_invalid``), never as a pipe both agree on wrongly. Change the matching, the tie order,
the split flow or the merged-run rule in one, and change it in the other.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from itertools import pairwise

from gtnh_solver.dataset import endpoint_insertions
from gtnh_solver.dataset.pipe_capacity import _EPS
from gtnh_solver.ir import IODirection, Machine, Net, Terminal
from gtnh_solver.ir.geometry import Cell


def block_insertions(
    net: Net,
    terminals: Sequence[Terminal],
    legs: Sequence[Sequence[Cell]],
    machines: Mapping[str, Machine],
    *,
    split: bool,
) -> dict[Cell, float]:
    """Insertions per ``STREAM_SERVICE_TICKS`` GT charges each block of ``net``'s laid pipe.

    ``terminals`` and ``legs`` are the route the router is about to publish: a terminal per
    endpoint, in the order the route lists them (which is the order pairs at equal distance are
    taken in), and the cell paths its segments follow. A route with no legs is a one-block pipe,
    and every stream charges that block. The tree is connected (the router lays one tree per net),
    so every target is some number of hops from every sender.

    Each matched sender-target pair is a stream, charged :func:`endpoint_insertions` of what it
    moves: one insertion, one more per further stack, never more than its items. ``split`` is a
    net some of whose producers auto-output into its consumer (#270): its pipe moves only what the
    producers on it send, since the consumer's intake counts the others too. A terminal whose
    machine or port the router cannot resolve is left out, as ``_pipe_size`` leaves it out, and a
    run with no sender or no target moves nothing: every block is charged 0.
    """
    joins = _joins(terminals, legs)
    charged = dict.fromkeys(joins, 0.0)
    senders, targets = _docks(terminals, machines)
    if not senders or not targets:
        return charged

    supply, supplied = _shares([rate for _, rate in senders], net.throughput)
    wanted, consumed = _shares([rate for _, rate in targets], net.throughput)
    flow = supplied if split else max(supplied, consumed)  # items/t the pipe moves
    hops = {cell: _hops(joins, cell) for cell in {cell for cell, _ in senders}}
    pairs = sorted(
        (hops[s][t], i, j) for i, (s, _) in enumerate(senders) for j, (t, _) in enumerate(targets)
    )
    for reach, i, j in pairs:
        if net.items:
            # A merged run's consumers are Item Filters, each taking only its own item, so every
            # producer feeds every filter its share whatever lies nearer (validator: _item_streams).
            share = supply[i] * wanted[j]
        else:
            share = min(supply[i], wanted[j])
            supply[i] -= share
            wanted[j] -= share
        if share <= _EPS:
            continue  # one of the two is already served
        need = endpoint_insertions(share * flow)
        for cell, distance in hops[senders[i][0]].items():
            if distance <= reach:
                charged[cell] += need
    return charged


def _joins(terminals: Sequence[Terminal], legs: Sequence[Sequence[Cell]]) -> dict[Cell, set[Cell]]:
    """Each block of the laid tree and the blocks a leg joins it to (none, for a one-block pipe)."""
    joins: dict[Cell, set[Cell]] = {t.cell.as_tuple(): set() for t in terminals}
    for leg in legs:
        for cell in leg:
            joins.setdefault(cell, set())
        for a, b in pairwise(leg):
            joins[a].add(b)
            joins[b].add(a)
    return joins


def _docks(
    terminals: Sequence[Terminal], machines: Mapping[str, Machine]
) -> tuple[list[tuple[Cell, float | None]], list[tuple[Cell, float | None]]]:
    """The run's senders and targets, in terminal order: each one's block and its port's rate."""
    senders: list[tuple[Cell, float | None]] = []
    targets: list[tuple[Cell, float | None]] = []
    for t in terminals:
        machine = machines.get(t.machine_id)
        port = (
            next((p for p in machine.faces.ports if p.id == t.port_id), None)
            if machine is not None
            else None
        )
        if port is None:
            continue
        side = senders if port.direction is IODirection.OUTPUT else targets
        side.append((t.cell.as_tuple(), port.rate))
    return senders, targets


def _shares(rates: Sequence[float | None], throughput: float) -> tuple[list[float], float]:
    """Each endpoint's fraction of its side, and the side's total items/t.

    A port with no recorded rate takes an even share of the net's ``throughput``, which is what the
    adapter writes for a node of identical machines. A side whose rates add up to nothing is split
    evenly, so each of its endpoints is still served once.
    """
    items = [throughput / len(rates) if rate is None else rate for rate in rates]
    total = sum(items)
    if total <= _EPS:
        return [1 / len(items)] * len(items), total
    return [x / total for x in items], total


def _hops(joins: Mapping[Cell, set[Cell]], start: Cell) -> dict[Cell, int]:
    """Hops from ``start`` to every block of the tree, breadth first."""
    hops = {start: 0}
    queue = deque([start])
    while queue:
        cell = queue.popleft()
        for nxt in joins[cell]:
            if nxt not in hops:
                hops[nxt] = hops[cell] + 1
                queue.append(nxt)
    return hops
