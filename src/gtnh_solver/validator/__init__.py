"""validator — the only automated correctness gate (no headless GT simulator).

Independent CHECKING LOGIC over SHARED rule DATA (docs/ARCHITECTURE.md #4), so it can catch
router bugs. Checks:
  - geometric: no overlaps, within bounding region, pinned I/O honored,
  - rule: throughput/tier caps, one-fluid-per-line, summed amperage <= thickness,
    required-I/O-face reachability (HARD), ME-toggled commodities excluded + endpoint-placed.
Reports partial-invalid layouts explicitly; never passes a silently-invalid one.

TODO(validator): implement the checks independently of the router; back them with property
tests (hypothesis) and the golden corpus (docs/TESTING.md).
"""
