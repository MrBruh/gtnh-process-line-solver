"""placement.feasibility - can this placement host its own connections at all?

The cost function in ``search`` prices *crowding* cheaply, because it runs once per annealing
step and cannot afford to be exact. What it approximates is a precise question:

    every connection on every machine needs its **own** free cell to dock on, and neighbouring
    machines compete for the same cells - so is there an assignment that gives all of them one?

That is a bipartite matching (a system of distinct representatives), and it is what this module
decides. It is far too slow to run per annealing step and comfortably cheap once per *candidate
layout*, which is exactly where the solver's feedback loop calls it - before routing, since a
placement that cannot host its connections will not route and there is no point paying for the
attempt to find out.

Catching it here is also what makes the diagnosis honest. Left to the routers, the shortage
surfaces as whichever net loses the race for the last free face, which names an arbitrary net on
an arbitrary machine and reads like a routing bug - the false trail #76 spent two diagnoses on.
"""

from __future__ import annotations

from collections.abc import Sequence

from gtnh_solver.ir import Commodity, InputIR, Machine, Placement
from gtnh_solver.ir.geometry import Cell
from gtnh_solver.router._grid import dock_candidates, obstacle_cells
from gtnh_solver.router.auto import assign_auto_outputs


def crowded_machines(problem: InputIR, placements: Sequence[Placement]) -> tuple[str, ...]:
    """Machines that cannot be given a distinct dock cell for every connection they carry.

    Returns their ids in placement order, empty when the placement can host everything. An empty
    result is a real guarantee about geometry - an assignment exists - not an opinion, though it
    is still only about *docking*: whether a pipe can then be routed between the docks is the
    router's question, not this one.

    Judged with the router's own :func:`dock_candidates`, so the cells counted here are the cells
    a hatch may really occupy (front face excluded, interior slots excluded, hatch-kind rules
    respected) rather than a second, drifting notion of a usable face.

    It also has to ask the same question the router asks *first*: which nets need a pipe at all.
    A net covered by a free auto-output connection needs **no dock cell** - the two machines are
    face to face and the source ejects straight into the sink - and an ME-toggled net needs none
    either. Charging those ports for a cell condemns exactly the layouts the placer is supposed to
    be looking for: the sand line auto-feeds every item net with zero pipes, and counting its
    three hammers as needing two pipe docks each declared its hand-built 3x2x2 crowded and drove
    the search to a box half again as large. What an auto-output *does* spend is a casing cell at
    each end, so those are passed through as already claimed.

    **Power is counted, but under its own rule**, because its cells are not exclusive the way a
    pipe's are. GT feeds every wired face adjacent to a cable block, so
    :func:`router.power.route_power` deliberately *taps* a trunk cell rather than laying a new
    leg, and sinks on several machines can share one cell. Two energy hatches on the **same**
    machine are still two casing cells, though. So power is settled after the pipes, per machine:
    it needs as many of its own candidate cells left over as it has power ports, and it does not
    care who else on the net is standing on them.

    Demanding a distinct cell per energy hatch across the whole net instead - the obvious reading
    - condemns layouts that build perfectly well, and turned two solved nitrobenzene lines into a
    flat ``INFEASIBLE``. Ignoring power altogether is no better: the pipes then get credited with
    cells that power is going to take, and the line strands an item net on a machine this said
    was fine.

    One deliberate slack: two connections on one multiblock are required to take distinct dock
    *cells* but not distinct casing cells, which is the stricter rule the routers apply
    (``_grid.claim_key``). Modelling that needs the matching to run over casing cells for
    multiblocks and dock cells for single blocks; until it does, this under-reports crowding on
    multiblocks rather than over-reporting it, which keeps a false infeasibility off layouts that
    build - the same direction the validator's hatch-cell ceiling errs in.
    """
    machines = {m.id: m for m in problem.machines}
    obstacles = obstacle_cells(problem, placements, machines)
    region = problem.bounding_region

    # The router's own auto-output decision, taken over the same placements: its covered nets are
    # piped by nobody, and its claimed casing cells are spent before any pipe docks.
    assignment = assign_auto_outputs(problem, placements)
    piped: dict[tuple[str, str], bool] = {}
    for net in problem.nets:
        needs_pipe = net.id not in assignment.covered and not problem.me_toggles.toggled(
            net.commodity
        )
        for endpoint in net.endpoints:
            key = (endpoint.machine_id, endpoint.port_id)
            piped[key] = piped.get(key, False) or needs_pipe

    def reachable(placement: Placement, machine: Machine, port_id: str) -> set[Cell]:
        return {
            t.cell.as_tuple()
            for t in dock_candidates(
                port_id,
                placement,
                machine,
                obstacles,
                set(),
                region,
                assignment.claimed.get(machine.id, ()),
            )
        }

    # One exclusive demand per (machine, pipe port); its options are the cells it could dock on.
    owners: list[str] = []
    options: list[tuple[Cell, ...]] = []
    power_demand: list[tuple[str, int, set[Cell]]] = []
    for placement in placements:
        machine = machines.get(placement.machine_id)
        if machine is None:
            continue
        power_cells: set[Cell] = set()
        power_ports = 0
        for port in machine.faces.ports:
            if port.commodity is Commodity.POWER:
                power_ports += 1
                power_cells |= reachable(placement, machine, port.id)
                continue
            if not piped.get((placement.machine_id, port.id), False):
                continue  # auto-output or ME: no pipe, so no dock cell to find
            owners.append(placement.machine_id)
            # Sorted, so the augmenting search is deterministic and so is the reported set.
            options.append(tuple(sorted(reachable(placement, machine, port.id))))
        if power_ports:
            power_demand.append((placement.machine_id, power_ports, power_cells))

    # Kuhn's augmenting-path matching. The graph is tiny - one node per connection, at most a
    # handful of cells each - so the O(V*E) form is far below the routing it saves, and unlike
    # Hopcroft-Karp it stays short enough to read.
    taken: dict[Cell, int] = {}
    crowded: list[str] = []
    for demand in range(len(owners)):
        if not _augment(demand, options, taken, set()):
            crowded.append(owners[demand])
    # Then power, on what the pipes left: shared across machines, distinct within one.
    for machine_id, ports, cells in power_demand:
        if len(cells - set(taken)) < ports:
            crowded.append(machine_id)
    return tuple(dict.fromkeys(crowded))  # de-duplicated, first occurrence order


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
