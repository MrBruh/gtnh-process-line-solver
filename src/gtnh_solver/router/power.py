"""router.power — the shared-amperage power routing primitive.

Power is NOT a disjoint per-pipe flow. Amperage SUMS along shared cable segments
(Steiner-tree-like). Per segment (docs/DOMAIN.md):
  - voltage tier follows the served machine's voltage tier (cable rated >= that voltage),
  - thickness (1x/2x/4x/8x/16x, 16x max) is sized to the SUMMED amperage through the segment,
  - a segment needing > 16x splits into parallel runs or moves to a higher voltage tier.

TODO(router.power): build the shared-conductor net (summed-load edges), thickness sizing,
the 16x cap with split/upgrade handling, and burn detection for the validator to cross-check.
"""
