# IR — the data contracts

`gtnh_solver` has two versioned contracts. Everything couples to them, so they are defined
up front (minimal, not exhaustive) and grown with explicit version bumps. Implemented as
typed schemas in `src/gtnh_solver/ir/` (Pydantic v2).

> Status: **v0 draft.** Fields below are the intended starting shape. Refine during the
> integration spike; bump `version` on any breaking change.

## Input IR — the problem

What the solver consumes (produced by the adapter from gtnh-flow + the dataset).

```
InputIR
  version: int                      # contract version
  bounding_region: Box              # max extent the layout must fit (cells)
  machines: [Machine]
  nets: [Net]
  pinned: [PinnedIO]                # fixed input/output chest locations
  reserved_cells: [CellCoord]       # off-limits cells
  me_toggles: { items: bool, fluids: bool, power: bool }   # per-commodity

Machine
  id: str
  type: str                         # GT machine id (keys into dataset)
  footprint: CellBox                # 1 cell (single-block) or NxMxK (multiblock bbox)
  faces: FaceSpec                   # see DOMAIN.md: front (no I/O) + 5 usable
  voltage_tier: str                 # LV/MV/HV/... — sets cable voltage rating
  orientation_options: [Orientation]  # solver picks one (front-face placement)
  count: int                        # how many of this machine (from gtnh-flow balance)

Net
  id: str
  commodity: "item" | "fluid" | "power"
  fluid_or_item: str | null         # which fluid/item (null for power)
  throughput: float                 # TYPED rate: mB/t (fluid), items/t (item), EU/t+A (power)
  endpoints: [MachineFaceRef]       # which machine faces this net connects

PinnedIO     { net_id, cell: CellCoord, kind: "input" | "output" }
```

## Output layout schema — the solution

What the solver produces; consumed by previewer, build guide, and (later) export. A
first-class versioned contract, not a previewer-internal format.

```
LayoutResult
  version: int
  status: "valid" | "infeasible" | "partial_invalid"
  infeasibility: Infeasibility | null   # tightest violated constraint + suggested relaxation
  placements: [Placement]
  routes: [Route]
  metrics: { footprint, layers, buildability, congestion, ... }
  seed: int                              # for the seed-compare workflow

Placement   { machine_id, cell: CellCoord, orientation: Orientation }
Route
  net_id: str
  commodity: "item" | "fluid" | "power"
  segments: [Segment]                    # cell-path; lowered to blocks only at export
  # power only:
  thickness_per_segment: [int]           # 1/2/4/8/16, sized to summed amperage
Segment     { from: CellCoord, to: CellCoord, channel: int }   # channel < per-edge cap
```

## Rules the schemas must encode (cross-ref [`DOMAIN.md`](DOMAIN.md))

- A net's `throughput` is **typed** — the router needs the real rate, not just connectivity.
- `Machine.faces` distinguishes the front face (no I/O) from the five usable faces; required
  output faces are HARD constraints in placement/validation.
- Power routes carry per-segment `thickness`; the validator checks summed amperage ≤ tier cap.
- `me_toggles` removes a commodity from physical routing; the solver places ME endpoints
  instead (no `Route` for that commodity).

## Versioning

- `version` is an int on both IR roots. Additive fields can land without a bump; any change
  that breaks an existing consumer bumps it and updates all consumers in the same PR.
- Keep a short changelog of contract changes at the bottom of `src/gtnh_solver/ir/__init__.py`.
