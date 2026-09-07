# IR - the data contracts

`gtnh_solver` has two versioned contracts. Everything couples to them, so they are defined
up front (minimal, not exhaustive) and grown with explicit version bumps. Implemented as
typed schemas in `src/gtnh_solver/ir/` (Pydantic v2).

> Status: **implemented** (Pydantic v2, `src/gtnh_solver/ir/`). The shapes below match the
> code: `InputIR` is at **v3**, `LayoutResult` at **v0** (the contract changelog lives at the
> bottom of `ir/__init__.py`). Bump the relevant `*_VERSION` on any breaking change.

## Input IR - the problem

What the solver consumes (produced by the adapter from gtnh-factory-flow's exported plan JSON,
recipes embedded, plus the physical-rules dataset). Source format: gtnh-factory-flow's plan
JSON (graph nodes/edges, fuel profiles, targets, and the exact recipes placed), which the
*upstream exporter* validates with Zod; the adapter re-parses that → InputIR with Pydantic.
(Pinning an explicit plan-schema + recipe-dataset version is Phase 2, lane A.)

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
  block_key: str | null             # GT controller block as "<registry_name>@<meta>"; the exact
                                    #  key the physical dataset joins on when the export supplies
                                    #  it, else null and lookup falls back to `type`. Added in
                                    #  InputIR v2 (additive). (GitHub #98.)
  footprint: CellBox                # 1 cell (single-block, default) or NxMxK (multiblock bbox)
  faces: FaceSpec                   # see DOMAIN.md: front (no I/O) + 5 usable
  voltage_tier: str                 # LV/MV/HV/... - sets cable voltage rating
  orientation_options: [Facing]     # solver picks one (front-face direction); >= 1
                                    # one instance per Machine; `count` was dropped in v1 -
                                    # multi-instance groups need instance-aware routing (Phase 2)
  eut: float                        # EU/t this machine draws; with voltage_tier it sets the
                                    #  amperage it pulls on a shared cable (dataset.amperage).
                                    #  0 for an unpowered block or a power source. Added in
                                    #  InputIR v1 (additive); load-bearing for the power path.
  hatch_cells: int | null           # how many cells of this machine's structure can host a
                                    #  hatch of ANY kind (a multiblock's casing cells are
                                    #  interchangeable, so item, fluid and power connections
                                    #  compete for one pool), i.e. the ceiling on its total
                                    #  connections; null when unknown (a single-block machine,
                                    #  or a plan adapted without the physical dataset). Added
                                    #  in InputIR v3 (additive).
  hatch_slots: [HatchSlot]          # WHERE those cells are; empty when unknown (same cases as
                                    #  hatch_cells being null, plus the 23 of 208 dumped
                                    #  controllers that record no slots). Added in InputIR v3
                                    #  (additive - an empty tuple reads exactly as the old
                                    #  behaviour did, so no bump).

HatchSlot { offset: CellCoord, kinds: [str] }
  offset  from the machine's UNROTATED minimum corner (the corner Placement.cell names), so a
          placed slot's world cell is placement.cell + rotated_slot(offset, footprint,
          placement.orientation). Kept unrotated because orientation is a placement decision.
  kinds   HatchElement names (OutputHatch, InputBus, Energy, Maintenance, Muffler, ...), sorted.
          A LOWER BOUND, never a whitelist: a GT hatch adder built from a bare method reference
          exposes no filter, so its cell is recorded without that kind. Treating an absent kind
          as a prohibition manufactures false infeasibilities across a third of the dataset.
          Nor is the enum closed - ~30 further IHatchElement implementations live outside
          gregtech.api.enums.HatchElement (TecTech's EnergyMulti/InputData, gtPlusPlus's set,
          per-controller ones, and HatchElementEither's "A or B"), any of which a dump may name.

  Which kinds a port needs (ir.input_ir.HATCH_KINDS; the bus/hatch split is lexical in GT and
  means items/fluids):
        item   input -> InputBus      fluid input  -> InputHatch
        item  output -> OutputBus     fluid output -> OutputHatch
        power  input -> Energy | ExoticEnergy | MultiAmpEnergy   (34 of 208 controllers record
                                                                  only the TecTech spelling)
        power output -> Dynamo
  Machine.hatch_slots_for(port_id) applies that in three levels, and the third is load-bearing:
        no slots recorded at all      -> None; every body cell stays a candidate
        some slot names the kind      -> exactly those slots
        no slot names the kind        -> ALL of them (the dump is silent, not prohibiting; the
                                         Chemical Plant records zero Energy cells and must still
                                         be powerable)

FaceSpec     { ports: [Port] }      # catalog of required I/O; the physical face is a solver choice
Port
  id: str
  commodity: "item" | "fluid" | "power"
  direction: "input" | "output"
  cover: str | null                 # conveyor/pump/regulator that drives this port, if any
                                    # (auto-output is a solver decision -> output's AutoConnection,
                                    #  not a Port input; is_auto_output was dropped in v2)
  rate: float | null                # throughput moved: items/t, mB/t, or (since v3) EU/t on a
                                    #  power port; null when unknown. Adapter fills it; surfaces
                                    #  boundary I/O rates (added v2, additive). On a power port
                                    #  it is the share of the machine's eut arriving through THIS
                                    #  connection, and a machine's power input ports must sum to
                                    #  its eut (v3, BREAKING)
  max_amps: float | null            # most amps this one connection accepts (2 for a GT energy
                                    #  hatch); null for no per-connection ceiling. Checked at the
                                    #  DELIVERED voltage, where cable loss is known. Added in
                                    #  InputIR v3 (additive)

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

**A machine's power intake is per connection (InputIR v3).** A GT energy hatch accepts 2 amps
and a multiblock's intake is the sum over its hatches, so a machine that draws more than one
hatch can take carries several power INPUT ports, each with its own `rate` (its share of `eut`)
and its own `max_amps`. Read a port's share with `Machine.port_eut(port_id)`: it returns that port's
`rate` and falls back to the machine's whole `eut` when the port carries none, so a
single-connection machine (and every pre-v3 problem, where power ports had no rate) sizes exactly
as it did. The contract enforces the split itself: a machine's power input ports either all carry
a rate or none do (a partly rated machine would leave part of its draw unsized), and the rated
ones must sum to `eut`. What it does **not** decide is how many hatches a machine gets (the
adapter, from the draw and the tier) or whether its structure can host them (the validator,
against `hatch_cells`).

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
  hatches: [PlacedHatch]                 # every hatch/bus the build needs (v1)
  metrics: { footprint, layers, buildability, congestion, ... }
  seed: int                              # for the seed-compare workflow

Placement   { machine_id, cell: CellCoord, orientation: Facing }   # orientation horizontal only
Route
  net_id: str
  commodity: "item" | "fluid" | "power"
  terminals: [Terminal]                  # where the route meets each machine endpoint (covers ride here)
  segments: [Segment]                    # cell-path; lowered to blocks only at export
  thickness_per_segment: [int] | null    # power only (else null); 1/2/4/8/12/16, summed amperage
  material: RouteMaterial | null         # the STAND-IN it is drawn as; null = unspecified pipe
RouteMaterial
  family: "cable" | "fluid_pipe" | "item_pipe"   # must match commodity (cable<->power, ...)
  material: str                          # GT's unlocalized name ("tin"); the manifest keys on it
  tier: str | null                       # cables only (required); the voltage tier the gauge rates
  stand_in: bool                         # always true in v1 - representative, NOT a build spec
Terminal    { machine_id, port_id, face: Facing, cell: CellCoord }  # non-front face; cell just outside
PlacedHatch { machine_id, kind: str, cell: CellCoord, facing: Facing, port_id: str | null }
              # cell is the BODY cell the hatch replaces, inside the footprint - not the dock
              # cell outside it. port_id is null for an upkeep hatch (maintenance, muffler),
              # which belongs to no net and is why the record has to exist at all.
Segment     { start: CellCoord, end: CellCoord, channel: int }   # >= 0 only; the per-edge channel cap is Phase 2, not yet enforced
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
- A power port's `rate` + `max_amps` let the validator check the *other* direction as well:
  that enough EU/t actually **arrives** once cable loss has shrunk every packet
  (`POWER_SUPPLY_INSUFFICIENT`), and that a machine is not wired more connections than its
  `hatch_cells` can host (`HATCH_CELLS_EXCEEDED`).
- `me_toggles` removes a commodity from physical routing (no `Route` for that commodity today - a
  toggled commodity is simply skipped everywhere). Placing the ME endpoint that replaces it on a
  machine face is Phase 2.

## Versioning

- `version` is an int on both IR roots. Additive fields can land without a bump; any change
  that breaks an existing consumer bumps it and updates all consumers in the same PR.
- "Breaks an existing consumer" includes breaking by *omission*. `LayoutResult` v1 added
  `hatches`, which is additive in shape, and still bumped: a consumer that ignores it renders a
  build for a machine with no maintenance hatch and no muffler, which will not run. Nothing
  raises; the build is simply wrong.
- Keep a short changelog of contract changes at the bottom of `src/gtnh_solver/ir/__init__.py`.
