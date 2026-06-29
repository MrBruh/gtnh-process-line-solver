# IR - the data contracts

`gtnh_solver` has two versioned contracts. Everything couples to them, so they are defined
up front (minimal, not exhaustive) and grown with explicit version bumps. Implemented as
typed schemas in `src/gtnh_solver/ir/` (Pydantic v2).

> Status: **v0 draft.** Fields below are the intended starting shape. Refine during the
> integration spike; bump `version` on any breaking change.

## Input IR - the problem

What the solver consumes (produced by the adapter from gtnh-factory-flow's exported plan JSON,
recipes embedded, plus the physical-rules dataset). Source format: gtnh-factory-flow's
Zod-validated plan JSON (graph nodes/edges, fuel profiles, targets, and the exact recipes
placed); the adapter maps that → InputIR and pins the plan-schema + recipe-dataset versions.

```
InputIR
  version: int                      # contract version
  bounding_region: CellBox          # max extent the layout must fit (cells)
  machines: [Machine]
  nets: [Net]
  pinned: [PinnedIO]                # fixed input/output chest locations
  reserved_cells: [CellCoord]       # off-limits cells
  me_toggles: { items: bool, fluids: bool, power: bool }   # per-commodity (default all false)

Machine
  id: str
  type: str                         # GT machine id (keys into dataset)
  footprint: CellBox                # 1 cell (single-block, default) or NxMxK (multiblock bbox)
  faces: FaceSpec                   # see DOMAIN.md: front (no I/O) + 5 usable
  voltage_tier: str                 # LV/MV/HV/... - sets cable voltage rating
  orientation_options: [Facing]     # solver picks one (front-face direction); >= 1
                                    # one instance per Machine; `count` was dropped in v1 -
                                    # multi-instance groups need instance-aware routing (Phase 2)

FaceSpec     { ports: [Port] }      # catalog of required I/O; the physical face is a solver choice
Port
  id: str
  commodity: "item" | "fluid" | "power"
  direction: "input" | "output"
  cover: str | null                 # conveyor/pump/regulator that drives this port, if any
                                    # (auto-output is a solver decision -> output's AutoConnection,
                                    #  not a Port input; is_auto_output was dropped in v2)

Net
  id: str
  commodity: "item" | "fluid" | "power"
  fluid_or_item: str | null         # which fluid/item (null for power; required otherwise)
  throughput: float                 # TYPED rate: mB/t (fluid), items/t (item), EU/t (power); >= 0
  endpoints: [MachineFaceRef]       # machine ports this net connects; >= 1

MachineFaceRef { machine_id, port_id }   # resolved to a physical face by the solver
PinnedIO       { net_id, cell: CellCoord, kind: "input" | "output" }
```

`CellBox` is a size `{ sx, sy, sz }` (each >= 1), used for both `footprint` and
`bounding_region`. The IR enforces structural well-formedness + **referential integrity**
(unique ids; every endpoint/pinned ref resolves; a net's commodity matches the ports it
touches). It does **not** check geometry/rule validity (in-bounds, overlaps, tier caps,
face reachability) - that is the validator's independent job (docs/TESTING.md).

## Output layout schema - the solution

What the solver produces; consumed by previewer, build guide, and (later) export. A
first-class versioned contract, not a previewer-internal format.

```
LayoutResult
  version: int
  status: "valid" | "infeasible" | "partial_invalid"
  infeasibility: Infeasibility | null   # tightest violated constraint + suggested relaxation
  placements: [Placement]
  routes: [Route]                        # nets connected by a pipe
  auto_connections: [AutoConnection]     # nets connected by adjacency (no pipe)
  metrics: { footprint, layers, buildability, congestion, ... }
  seed: int                              # for the seed-compare workflow

Placement   { machine_id, cell: CellCoord, orientation: Facing }   # orientation horizontal only
Route
  net_id: str
  commodity: "item" | "fluid" | "power"
  terminals: [Terminal]                  # where the route meets each machine endpoint (covers ride here)
  segments: [Segment]                    # cell-path; lowered to blocks only at export
  thickness_per_segment: [int] | null    # power only (else null); 1/2/4/8/16, summed amperage
Terminal    { machine_id, port_id, face: Facing, cell: CellCoord }  # non-front face; cell just outside
Segment     { start: CellCoord, end: CellCoord, channel: int }   # channel < per-edge cap; >= 0
AutoConnection { net_id, source_machine_id, source_face: Facing, target_machine_id, target_face: Facing }
Infeasibility { constraint: str, detail: str, suggested_relaxation: str | null }
```

`Facing` is one of `north|south|east|west|up|down`. A machine's `orientation` (front face) is
**horizontal only** (`north|south|east|west`) - GT machines never face up/down, though those
faces can still carry I/O. Each non-ME net is satisfied by **exactly one** of: a pipe `Route`,
or an `AutoConnection` (the source machine auto-ejecting straight into an adjacent target's
input face - no pipe, no cover; `source_face` points source->target, `target_face` is the
opposite, both non-front). A `Terminal` records where a *pipe* docks: the non-front `face`
(covers ride on the machine face, never the pipe) and the adjacent `cell`. `Segment` uses
`start`/`end` (`from` is a Python keyword). `status`/`infeasibility` are coupled: a `valid`
result carries no infeasibility; `infeasible`/`partial_invalid` must carry one.

## Rules the schemas must encode (cross-ref [`DOMAIN.md`](DOMAIN.md))

- A net's `throughput` is **typed** - the router needs the real rate, not just connectivity.
- `Machine.faces` distinguishes the front face (no I/O) from the five usable faces; required
  output faces are HARD constraints in placement/validation.
- Power routes carry per-segment `thickness`; the validator checks summed amperage ≤ tier cap.
- `me_toggles` removes a commodity from physical routing; the solver places ME endpoints
  instead (no `Route` for that commodity).

## Versioning

- `version` is an int on both IR roots. Additive fields can land without a bump; any change
  that breaks an existing consumer bumps it and updates all consumers in the same PR.
- Keep a short changelog of contract changes at the bottom of `src/gtnh_solver/ir/__init__.py`.
