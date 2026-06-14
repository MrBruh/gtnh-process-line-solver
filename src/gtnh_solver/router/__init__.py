"""router — free-form per-commodity routing on the cell grid.

A* (not Lee BFS) with a Manhattan heuristic on the region-bounded grid; rip-up-and-reroute
under congestion. Enforces throughput/tier caps, one-fluid-per-line, ME-toggle skip +
endpoint placement, and the channels-per-edge cap + cell->block realizability that keep the
coarse-cell abstraction from certifying unbuildable layouts (docs/ARCHITECTURE.md #6, #7).

Power has its own primitive — see router/power.py.

TODO(router): implement A* per net, capacity/rule constraints, rip-up-and-reroute, the
channels-per-edge invariant, realizability feedback, and ME handling.
"""
