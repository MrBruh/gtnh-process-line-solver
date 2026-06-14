"""placement — SA/LNS placement over the coarse cell grid.

Machine orientation is a placement variable (keeps the front face out of the way). Cost =
compactness + a CHEAP INCREMENTAL routing estimate (half-perimeter wirelength + a congestion
proxy) + buildability, with required-I/O-face reachability as a HARD constraint. The estimate
must be ~O(1) per move — never a full re-route per move (docs/ARCHITECTURE.md #1, #6).

Pluggable backend interface; a CP-SAT exact backend for small sub-blocks is deferred (v1.1).

TODO(placement): implement the cell grid, move operators (translate + rotate), the cost
function, and the SA/LNS schedule with per-seed determinism.
"""
