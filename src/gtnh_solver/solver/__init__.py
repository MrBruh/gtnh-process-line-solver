"""solver — orchestrates placement and routing.

The place->route->retry feedback loop (docs/ARCHITECTURE.md #1): place, route, and if a net
is unroutable/over-capacity feed a penalty back to perturb placement. Anytime behavior
(#6): on the wall-clock budget, return the best VALID layout found so far, or an explicit
"no valid layout yet" report — never hang, never return a silently-invalid layout.

TODO(solver): implement the feedback loop, convergence/give-up logic, and the anytime budget
tracking best-valid-so-far. Merge placement + router behind a stable interface.
"""
