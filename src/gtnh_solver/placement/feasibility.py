"""placement.feasibility - can this placement host its own connections at all?

The cost function in ``search`` prices *crowding* cheaply, because it runs once per annealing
step and cannot afford to be precise. This module asks the precise question, once per candidate
layout, which is where the solver's feedback loop calls it: before routing, since a placement that
cannot host its connections will not route and there is no point paying for the attempt to find
out. The question is

    every connection a router will dock needs a free cell next to its machine - so is there a
    cell for each of them, when three rules say who may stand on the same one?

      - two connections of ONE machine never may: one face does one thing
        (``TERMINAL_FACE_CONTENTION``; for a multiblock the unit is really the casing cell);
      - two connections on DIFFERENT nets never may: a cell carries one route
        (``ROUTE_CELL_COLLISION``);
      - two connections of ONE net on different machines may: one pipe block or cable wired to
        several neighbours (#164).

Catching a shortage here is also what makes the diagnosis honest. Left to the routers, it surfaces
as whichever net loses the race for the last free face, which names an arbitrary net on an
arbitrary machine and reads like a routing bug - the false trail #76 spent two diagnoses on.

**Why this is no longer one matching.** Until #164 no two pipe connections could share a cell, so
"a cell for each" was a bipartite matching over every connection, and this module decided it
exactly. The third rule ends that. A shared cell has to carry *one* net, so fitting a placement
means choosing which net each contested cell carries: a labelling problem, not a matching, and in
general a hard one (it contains set splitting - machines that each need a cell of net A and a cell
of net B from the same few cells). So the gate no longer decides the question. It proves
shortfalls, and names a machine only when it has proved one.

**What it proves.** A matching is still exact over any set of connections no two of which may
share a cell. The gate builds one such set per machine, *the view from* ``m``::

    m's own connections        in  out  power     one machine: a cell each
    a stand-in per net m does  gravel*  sand*     a net m does not carry still needs a cell
    not carry, among m's                          around m, and no connection of m can use it
    neighbours
                    |
                    v   matching: stand-ins first, then m's own
         m is crowded iff one of its own is left without a cell

A stand-in may take any cell that net's connections on those neighbours could dock on, and there
are as many of them as the most connections one neighbour has on that net. The view is sound: in
any legal docking m's own connections hold distinct cells, and each stand-in can take a cell a
neighbour docked its net on, which is distinct from m's (their nets differ) and from every other
stand-in's (so do theirs). A legal docking therefore seats the whole view, and a view that cannot
be seated proves m cannot be hosted. A stand-in left unseated names nobody, since that shortfall is
between m's neighbours.

**What it cannot see**, all in the direction of saying less. The gate is advisory: the solver
routes a placement it doubted rather than call a line unsolvable (``solver.core``), so a false
alarm costs the search its best placements while a missed one costs a single routing attempt.

- contention further than one machine from m;
- m's neighbours' connections on m's own nets, which may share m's cells or may not;
- that a piped net needs two cells to be a route at all;
- the multiblock casing cell (see :func:`crowded_machines`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from gtnh_solver.ir import InputIR, Placement
from gtnh_solver.ir.geometry import Cell
from gtnh_solver.ir.nets import placement_index
from gtnh_solver.router._grid import dock_candidates, obstacle_cells
from gtnh_solver.router.auto import assign_auto_outputs


@dataclass(frozen=True)
class _Connection:
    """One terminal a router will dock: whose it is, which net it serves, where it could sit."""

    machine_id: str
    net_id: str
    cells: tuple[Cell, ...]  # sorted, so the augmenting search runs the same way every time


def crowded_machines(problem: InputIR, placements: Sequence[Placement]) -> tuple[str, ...]:
    """Machines that provably cannot dock every connection they carry, in placement order.

    A name is a proof: that machine's own connections, plus a cell for each net around it that it
    does not carry, do not fit (module docstring). An empty result is not the converse. The gate
    proves shortfalls rather than deciding the question, and what it cannot see it does not
    report. It is also only about *docking*: whether a pipe can then be routed between the docks
    is the router's question, not this one. The verdict depends on the geometry alone, never on
    the order the placements arrive in.

    Judged with the router's own :func:`dock_candidates`, so the cells counted here are the cells
    a hatch may really occupy (front face excluded, interior slots excluded, hatch-kind rules
    respected) rather than a second, drifting notion of a usable face.

    It also has to ask the same question the routers ask *first*: which connections they dock at
    all. A net covered by a free auto-output connection needs **no dock cell** - the two machines
    are face to face and the source ejects straight into the sink - and neither does a net whose
    commodity rides the ME network, power included. Charging those ports for a cell condemns
    exactly the layouts the placer is supposed to be looking for: the sand line auto-feeds every
    item net with zero pipes, and counting its three hammers as needing two pipe docks each
    declared its hand-built 3x2x2 crowded and drove the search to a box half again as large. What
    an auto-output *does* spend is a casing cell at each end, so those are passed through as
    already claimed. A port on no net is docked by nobody, so it is charged nothing.

    **Power is judged by the same three rules as the pipes**, and in the same view. GT feeds every
    wired face next to a cable block, so :func:`router.power.route_power` taps a trunk cell and
    sinks on several machines share it (the precedent #164 then followed for pipes); two energy
    hatches on one machine are still two cells, and a cable cell is not a pipe cell. Power got its
    own rule wrong twice while it had one. Demanding a distinct cell per energy hatch across the
    whole net condemned layouts that build perfectly well, and turned two solved nitrobenzene lines
    into a flat ``INFEASIBLE``. Then settling power *after* the pipes, on whatever cells their
    matching happened to leave, made the verdict hang on which of two equally good cells a pipe
    took first: on the maintainer's proven parallel-sand build the pipes took every cell around the
    power source and named it crowded, alongside five hammers the build runs fine (#164). Leaving
    power out altogether is no better, since the pipes are then credited with cells an energy
    hatch is going to take.

    One deliberate slack: two connections on one multiblock are required to take distinct dock
    *cells* but not distinct casing cells, which is the stricter rule the routers apply
    (``_grid.claim_key``). Modelling that needs the matching to run over casing cells for
    multiblocks and dock cells for single blocks; until it does, this under-reports crowding on
    multiblocks rather than over-reporting it, which keeps a false infeasibility off layouts that
    build - the same direction the validator's hatch-cell ceiling errs in.
    """
    by_machine: dict[str, list[_Connection]] = {}
    reach: dict[
        Cell, set[str]
    ] = {}  # cell -> every machine with a connection that could dock on it
    for connection in _docked_connections(problem, placements):
        by_machine.setdefault(connection.machine_id, []).append(connection)
        for cell in connection.cells:
            reach.setdefault(cell, set()).add(connection.machine_id)

    crowded = [
        placement.machine_id
        for placement in placements
        if placement.machine_id in by_machine
        and not _hosts(
            by_machine[placement.machine_id], _stand_ins(placement.machine_id, by_machine, reach)
        )
    ]
    return tuple(dict.fromkeys(crowded))  # de-duplicated, first occurrence order


def _docked_connections(problem: InputIR, placements: Sequence[Placement]) -> list[_Connection]:
    """Every net endpoint a router will dock, with the cells it could dock on."""
    machines = {m.id: m for m in problem.machines}
    placement_by_machine = placement_index(placements)
    obstacles = obstacle_cells(problem, placements, machines)
    region = problem.bounding_region

    # The router's own auto-output decision, taken over the same placements: its covered nets are
    # piped by nobody, and its claimed casing cells are spent before anything else docks.
    assignment = assign_auto_outputs(problem, placements)
    connections: list[_Connection] = []
    for net in problem.nets:
        if net.id in assignment.covered or problem.me_toggles.toggled(net.commodity):
            continue  # an auto-output or the ME network carries it: nothing to dock
        for endpoint in net.endpoints:
            placement = placement_by_machine.get(endpoint.machine_id)
            machine = machines.get(endpoint.machine_id)
            if placement is None or machine is None:
                continue  # an unplaced machine is the placer's problem to report, not ours
            cells = {
                t.cell.as_tuple()
                for t in dock_candidates(
                    endpoint.port_id,
                    placement,
                    machine,
                    obstacles,
                    set(),
                    region,
                    assignment.claimed.get(machine.id, ()),
                )
            }
            connections.append(_Connection(endpoint.machine_id, net.id, tuple(sorted(cells))))
    return connections


def _stand_ins(
    machine_id: str,
    by_machine: dict[str, list[_Connection]],
    reach: dict[Cell, set[str]],
) -> list[tuple[Cell, ...]]:
    """The other half of the view from ``machine_id``: the cells the nets around it need.

    A neighbour is a machine with a connection that could dock on a cell one of ``machine_id``'s
    could. Each net ``machine_id`` does not carry gets as many stand-ins as the most connections
    one neighbour has on it - two energy hatches of one machine are two cells whoever else is on
    the net - and each may take any cell that net's connections on those neighbours could.

    A neighbour's connection on one of ``machine_id``'s own nets is left out. It may share a cell
    with ``machine_id`` or need one of its own, and which depends on a choice the view does not
    make, so counting it either way would be a guess.
    """
    own_nets = {c.net_id for c in by_machine[machine_id]}
    neighbours = {m for c in by_machine[machine_id] for cell in c.cells for m in reach[cell]}
    neighbours.discard(machine_id)
    cells: dict[str, set[Cell]] = {}
    count: dict[str, int] = {}
    for neighbour in neighbours:
        on_net: dict[str, int] = {}
        for connection in by_machine[neighbour]:
            if connection.net_id in own_nets:
                continue
            cells.setdefault(connection.net_id, set()).update(connection.cells)
            on_net[connection.net_id] = on_net.get(connection.net_id, 0) + 1
        for net_id, n in on_net.items():
            count[net_id] = max(count.get(net_id, 0), n)
    return [tuple(sorted(cells[net_id])) for net_id in sorted(cells) for _ in range(count[net_id])]


def _hosts(own: Sequence[_Connection], stand_ins: Sequence[tuple[Cell, ...]]) -> bool:
    """Whether every connection in ``own`` can be seated once the ``stand_ins`` have been.

    Kuhn's augmenting-path matching over the view. The graph is tiny - one node per connection, at
    most a handful of cells each - so the O(V*E) form is far below the routing it saves, and unlike
    Hopcroft-Karp it stays short enough to read.

    Stand-ins go first, and one left unseated is not held against ``own``. An augmenting path can
    move a seated demand but never unseats one, so an own connection fails exactly when the whole
    view seats fewer than the stand-ins can alone plus every own connection. That is a count, which
    is why the verdict does not depend on the order anything is tried in.
    """
    options = [*stand_ins, *(c.cells for c in own)]
    taken: dict[Cell, int] = {}
    for demand in range(len(stand_ins)):
        _augment(demand, options, taken, set())
    return all(_augment(d, options, taken, set()) for d in range(len(stand_ins), len(options)))


def _augment(
    demand: int,
    options: Sequence[tuple[Cell, ...]],
    taken: dict[Cell, int],
    seen: set[Cell],
) -> bool:
    """Try to seat ``demand``, displacing already-seated connections that have somewhere else.

    ``taken`` maps a cell to the demand holding it, and ``seen`` guards against revisiting a cell
    within one search. Standard augmenting path: a cell is available if nothing holds it, or if
    whatever holds it can be re-seated elsewhere.
    """
    for cell in options[demand]:
        if cell in seen:
            continue
        seen.add(cell)
        if cell not in taken or _augment(taken[cell], options, taken, seen):
            taken[cell] = demand
            return True
    return False
