# IR - the data contracts

`gtnh_solver` has two versioned contracts. Everything couples to them, so they are defined
up front (minimal, not exhaustive) and grown with explicit version bumps. Implemented as
typed schemas in `src/gtnh_solver/ir/` (Pydantic v2).

> Status: **implemented** (Pydantic v2, `src/gtnh_solver/ir/`). The shapes below match the
> code: `InputIR` is at **v7**, `LayoutResult` at **v4** (the contract changelog lives at the
> bottom of `ir/__init__.py`). Bump the relevant `*_VERSION` on any breaking change.

## Input IR - the problem

What the solver consumes (produced by the adapter from gtnh-factory-flow's exported plan JSON,
recipes embedded, plus the physical-rules dataset; a ShadowTheAge calculator plan is first
converted into that same JSON by gtnh-shadow-convert, #293). Source format: gtnh-factory-flow's plan
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
  me: MEConfig                      # the ME (AE2) networks nets may ride, and whether power is
                                    #  the builder's (power_external). Which net rides which is
                                    #  Net.me_network. Default: none, everything built physically.
                                    #  InputIR v8 (BREAKING, #332) replaced the per-commodity
                                    #  me_toggles; see "Choosing ME per net" below.
  resource_names: { str: str }      # raw resource id -> display name ("liquid_toluene" ->
                                    #  "Toluene"), as the plan's exporter read it from the game,
                                    #  for the resources this problem moves. Display only: nothing
                                    #  joins on a name, and an id with no entry is shown as the id.
                                    #  InputIR v5 (additive, #296).
  resource_colors: { str: str }     # raw resource id -> "#rrggbb", the plan's dominantColor for
                                    #  the resources this problem moves, lowercased; anything
                                    #  else is refused. Display only: the previewer's swatch
                                    #  where it has no icon. InputIR v6 (additive, #297).
  pack_version: str | null          # the GTNH pack the plan was balanced against
                                    #  ("2.9.0-beta-2"), null when it states none or two. GT's
                                    #  defaults differ between packs in ways a build must know
                                    #  (output_faces.output_side_takes_input). No geometry
                                    #  reads it. InputIR v5 (additive, #278).

Machine
  id: str
  type: str                         # GT machine id (keys into dataset)
  block_key: str | null             # GT controller block as "<registry_name>@<meta>": the
                                    #  controller the adapter RESOLVED in the physical dataset
                                    #  (by the export's block id, a handler label, the recipe-map
                                    #  name or an alias), the same record the footprint and
                                    #  hatch slots come from, so the previewer and .schematic
                                    #  export draw that exact machine. With no resolved record
                                    #  it is the export's own key, or null when the export has
                                    #  none; a drawing lookup that misses falls back to `type`.
                                    #  Added in InputIR v2 (additive, #98); stamped from the
                                    #  resolved record since #205 (a clarification, no bump).
  recipe_map: str | null            # unlocalized id of the GT recipe map the machine runs
                                    #  ("gt.recipe.orewasher"), from the export's rawRecipeId.
                                    #  `type` is that map's localized name, which a single
                                    #  block's own name often lacks ("Basic Ore Washing Plant")
                                    #  and two maps can share ("Furnace"), so the previewer and
                                    #  .schematic export draw a single block by this and
                                    #  `voltage_tier` first. Null for storages, power sources
                                    #  and plans that do not state it. InputIR v3 (additive, #232).
  footprint: CellBox                # 1 cell (single-block, default) or NxMxK (multiblock bbox)
  faces: FaceSpec                   # see DOMAIN.md: front (no I/O) + 5 usable, unless a port
                                    #  is pinned (Port.faces)
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
  filter_items: [str]               # the items an Item Filter lets through (its nine slots in
                                    #  game); empty for every other machine. InputIR v4 (#249).
  outside_front: bool               # it stands for something built OUTSIDE the layout, which it
                                    #  faces with its front: a Crop Manager, whose field is not
                                    #  part of the build. Placement puts that front flush on the
                                    #  region boundary, as for a power source's feed face; read
                                    #  both through Machine.fronts_outside. InputIR v5 (#282)
  structure_blocks: {str: StructureBlock}   # the block each tiered part is built from, keyed
                                    #  by GT's channel id ("coil", "casing", "pipe",
                                    #  "machine_casing"). The node's coil on any multiblock with
                                    #  a coil channel; on a Chemical Plant also the solid casing
                                    #  its recipes' special value needs, the plan's pipe casing
                                    #  and the machine casing of its supplied tier. A channel it
                                    #  does not name is built as the dump draws it; empty for a
                                    #  single block or a plan adapted without the dataset.
                                    #  InputIR v7 (BREAKING, #312)
  me_endpoints: [MEEndpoint]        # the ME devices this machine needs built: one per AE2 part
                                    #  or GT ME hatch serving its ports on nets riding ME, and a
                                    #  link's one storage bus. InputIR v9 (BREAKING, #333)
  me_role: "attach" | "link" | "controller" | "acceptor" | null
                                    # an ME infrastructure block, not a machine of the line; an
                                    #  attach stub and a link are outside_front. InputIR v9
  me_network: str | null            # the ME network an infrastructure block belongs to, set
                                    #  exactly when me_role is. InputIR v9

MEEndpoint
  id: str                           # unique on its machine
  network: str                      # an InputIR.me network; every port it serves rides it
  ports: [str]                      # the machine's ports it serves: >= 1, or none for an
                                    #  infrastructure block's (a link's storage bus)
  device: MEDeviceSpec              # { kind: MEDeviceKind, gt_mid: int | null (a GT ME hatch's,
                                    #  and only one's), cards: MECards, config: [str] }
  hatch_kind: str | null            # a multiblock's: the hatch slot kind it takes, the GT ME
                                    #  hatch itself or the normal hatch its part faces
  share: float                      # its share of each port's rate, (0, 1]; a port too fast for
                                    #  one device gets two endpoints

  Machine.allowed_faces(port_id, orientation) -> set[Facing] is the ONE reading of the face
  rule, shared by the router, placement, the crowding gate and the validator: an unpinned port
  may dock on any face but the front, a pinned port on exactly its Port.faces turned with the
  machine (ir.geometry.absolute_face).

HatchSlot { offset: CellCoord, kinds: [str], output_layer: int | null }
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
  output_layer  which of the machine's per-layer output lists a hatch here is filed under (dataset
          schema v3): GT's own index, from 0, which is the recipe fluid output it receives. null
          for a cell in no list: no output hatch, a machine that fills first fit, or a tower's top
          centre or base. Machine.output_layers collects them; GT forms a tower only with an
          output hatch on every one. InputIR v6 (BREAKING, #299)

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
        no slot names the kind        -> ALL of them (the dump is silent, not prohibiting; a
                                         dump taken before #227 records the Chemical Plant with
                                         zero Energy cells, and it must still be powerable)
  ...except a port with an output_layer, which gets exactly the slots of its kind on that layer
  and no fallback: GT fills a tower by layer, so a hatch anywhere else takes another product or
  none, and an empty answer is reported rather than docked on the wrong layer (InputIR v6).

StructureBlock { block: str, meta: int }
  block   registry name ("gregtech:gt.blockcasings4"); meta >= 0. The identity a structure dump
          cell and a texture manifest entry share, so the previewer and the .schematic export swap
          every cell of that channel (matched by membership in the blocks it accepts,
          dataset.channel_blocks) for it, and hatches and the controller wear a swapped casing.

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
                                    #  hatch); null means the ceiling is UNKNOWN, not unlimited -
                                    #  every GT connection has one, the producer just could not
                                    #  name the rule. Checked at the DELIVERED voltage, where cable
                                    #  loss is known; a null connection makes its machine's intake
                                    #  unmeasurable, and the validator reports that rather than
                                    #  certifying it (ValidationReport.unverified_power_intake).
                                    #  Added in InputIR v3 (additive)
  faces: [RelativeFace] | null      # the only faces this port may dock on, from the machine's
                                    #  point of view: front | back | left | right | up | down
                                    #  (left/right are the machine's own, looking out of its
                                    #  front: facing north, left is west). null = any face but
                                    #  the front (every port before v4). An Item Filter's output
                                    #  is (back,), its input (front, left, right, up, down).
                                    #  Non-empty, no repeats. InputIR v4 (BREAKING, #249)
  output_layer: int | null          # the output layer GT fills this port's fluid from, on a
                                    #  tower that fills by layer: the fluid's place among the
                                    #  recipe's fluid outputs (Distillation Tower output i goes
                                    #  only to layer i's hatches). Only a fluid output may name
                                    #  one, it must be a layer the slots record, and on such a
                                    #  machine every fluid output names one. The adapter refuses
                                    #  a time-share putting one fluid at two indices
                                    #  (InfeasiblePlanError, constraint output_layer). InputIR v6
                                    #  (BREAKING, #299)

Net
  id: str
  commodity: "item" | "fluid" | "power"
  fluid_or_item: str | null         # which fluid/item (null for power and for a merged run)
  items: [str]                      # the items a MERGED run carries: one pipe from a single
                                    #  block's one output face to the Item Filters that sort
                                    #  them (#249), or a FEED run from their producers into a
                                    #  single block's one input face, whose consumers take all
                                    #  of it (#277; no filter on it). An item net names
                                    #  fluid_or_item OR items, exactly one; fluid and power nets
                                    #  never name items. Net.resources reads either. InputIR v4
                                    #  (BREAKING, #249)
  throughput: float                 # TYPED rate: mB/t (fluid), items/t (item), EU/t (power); >= 0
  endpoints: [MachineFaceRef]       # machine ports this net connects; >= 1
  me_network: str | null            # the ME network (an InputIR.me.networks id) this net rides
                                    #  instead of a pipe; null = built physically. Item and fluid
                                    #  nets only. Read through Net.rides_me, or
                                    #  InputIR.rides_me(net), which also counts a power net while
                                    #  power is the builder's. InputIR v8 (BREAKING, #332)

MEConfig
  networks: [MENetworkSpec]         # unique ids; at most one attached; no two subnets one colour
  power_external: bool              # the line's EU supply is the builder's: no source, no cable

MENetworkSpec
  id: str
  mode: "attached" | "subnet"       # join the player's main network, or stand apart from it
  storage: "link" | "chests"        # a subnet's storage: the main network through one storage
                                    #  bus (default), or its own boundary chests; attached: link
  power: "external" | "acceptor"    # the builder's, or an Energy Acceptor on the line's EU
  hatches: "tier_aware" | "always" | "never"   # when a multiblock gets GT's own ME hatches
  colour: AEColor | null            # attached: Fluix; a subnet: never Fluix, null = the first free
  me_channel_budget: int            # free channels the main network has for an attached one (32)
  super_speed: bool                 # allow Hyper-Acceleration Cards (still only from LuV)

MachineFaceRef { machine_id, port_id }   # resolved to a physical face by the solver
PinnedIO       { net_id, cell: CellCoord, kind: "input" | "output" }
```

`CellBox` is a size `{ sx, sy, sz }` (each >= 1), used for both `footprint` and
`bounding_region`. The IR enforces structural well-formedness + **referential integrity**
(unique ids; every endpoint/pinned ref resolves; a net's commodity matches the ports it
touches). It does **not** check geometry/rule validity (in-bounds, overlaps, tier caps,
face reachability) - that is the validator's independent job (docs/TESTING.md).

## Choosing ME per net - the NetList and the MEPlan

Which nets ride ME is the user's choice, per net (#332), against two more versioned contracts in
`ir.me`. Both carry `version` and are refused on parse at any other, like the IR roots.

```
NetList                             # gtnh-solve plan.json --list-nets (stdout, JSON)
  version: int                      # NETLIST_VERSION
  plan_digest: str                  # SHA-256 of the plan as parsed
  dataset_version: str | null       # "<pack>@<generated_at>" of the physical dataset, or null
  solver_version: str
  line_tier: str                    # the highest tier a machine of the line runs at
  nets: [NetEntry]                  # every item and fluid net; never a power net

NetEntry
  id: str                           # the adapter's PRE-MERGE net id (see below)
  kind: "boundary_input" | "boundary_output" | "internal"
  commodity: "item" | "fluid"
  resource: str
  resource_name: str | null
  rate: float                       # what the net carries per tick
  producers: [NetEnd]               # machine ends; a boundary Super Chest/Tank is not an end,
  consumers: [NetEnd]               #  since riding ME replaces it (kind says there was one)

NetEnd { machine_id, port_id, machine_type, multiblock: bool, rate, suggested: str }
                                    # suggested: the ME device that end would get on a default
                                    #  attached network, or why none keeps up

MEPlan                              # gtnh-solve plan.json --me-plan FILE
  version: int                      # ME_PLAN_VERSION
  plan_digest: str                  # copied from the NetList; must match the plan solved
  dataset_version: str | null       # copied from the NetList; must match the dataset solved
  networks: [MENetworkSpec]
  nets: { net id: network id }      # nets it does not name are built physically
```

**Net ids are the adapter's pre-merge ids**: an edge group's `+`-joined edge ids and an output
buffer's `output-net:...`, which a plan and a dataset fix. A net on ME is never merged afterwards
(it docks nothing and is sorted by nothing), so its id survives into the `InputIR` unchanged. A net
a plan or dataset change could rename is why both files carry `plan_digest` and `dataset_version`,
and a mismatch is refused (exit 2) rather than applied to nets the choice never saw.
`--me items` / `--me fluids` is shorthand for one attached network, `main`, carrying every net of
that commodity; `--me power` sets `power_external` and combines with either.

**A net is a pipe network, not a plan edge.** The adapter maps each plan edge to a net, except that
the edges meeting at one multiblock port become one net (their ids joined with `+`), because that
port is one hatch with one pipe behind it (docs/DOMAIN.md, #213). So a multiblock's
`(machine, port)` sits in at most one net, which the router and the validator rely on when each
pairs a port's hatch with its terminal. A boundary storage's port may still sit in several. This is
an adapter guarantee, not a schema rule, and it needed no schema change: `endpoints` was always
unbounded.

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

**A single block's item outputs may share one face (InputIR v4).** A single block has five faces
that can carry a connection, one per connection, and a basic machine ejects every item through
one output face. A machine with several item outputs (an Ore Washer: item in, fluid in, three item
outputs, power) is built in GT with one output face, one pipe, and an Item Filter per item sorting
them; the machines of one parallel node share that pipe and those filters. The adapter synthesizes exactly that as
ordinary IR: the machine gets one `output:items` port on a trunk net whose `items` lists what it
carries, and each item gets a `Machine` of type "Ultra Low Voltage Item Filter" whose `filter_items`
names it, whose input port takes the trunk on any face but its back and whose output port is pinned
to its back, where it sources that item's downstream net. Nothing downstream needs a filter concept:
the pins (`Port.faces`) say where each port docks, and the validator checks the sorting.

**A single block's item inputs may share one face too (#277, same contract).** A machine still short
of faces after that takes its item inputs on a **feed run**: the machine gets one `input:items` port,
and the nets that fed its item inputs become one net whose `items` lists them, from every producer
to that port. It is the same field in the other direction, so the contract does not change shape:
a merged run that reaches an Item Filter is sorted, and one that reaches none is a feed, which the
validator tells apart by that alone.

## Output layout schema - the solution

What the solver produces; consumed by the previewer and the `.schematic` export, and **published
by the CLI** (below). A first-class versioned contract, not a previewer-internal format.

**Published on stdout.** `gtnh-solve plan.json` with neither `--preview` nor `--schematic` prints
the `LayoutResult` as one JSON document on stdout, and nothing else: every warning, note and log
line goes to stderr, so the output always parses (`gtnh-solve plan.json | python -m json.tool`,
or `> layout.json` for a file). With either artifact flag stdout stays empty. The document is:

- the model's **field names** exactly as in the schema below (the contract declares no aliases);
- **every field**, defaults included, so `version` is always stated and a valid layout carries an
  explicit `"infeasibility": null`;
- indented by two spaces, with non-ASCII escaped (`\uXXXX`), so it survives a console or a
  redirect of any encoding;
- readable back with `LayoutResult.model_validate_json`, which is what the CLI's tests pin.

It is printed on **infeasible runs too** (exit 1), since it carries `status` and `infeasibility`;
the reason is also printed to stderr as before. When the adapter refuses the plan before any solve
(a line no layout can satisfy, #112), the document is the same shape the solver returns when the
machines do not fit at all: that `status` and `infeasibility`, the `seed`, and nothing placed.
Exit codes 2 (unloadable export) and 3 (internal error) print nothing on stdout. A consumer should
check `version` before reading further: the versioning rules below are what it can rely on.

```
LayoutResult
  version: int
  status: "valid" | "infeasible" | "partial_invalid"
  infeasibility: Infeasibility | null   # tightest violated constraint + suggested relaxation
  placements: [Placement]
  routes: [Route]                        # nets connected by a pipe
  auto_connections: [AutoConnection]     # nets connected by adjacency (no pipe)
  hatches: [PlacedHatch]                 # every hatch/bus the build needs (v1)
  me_networks: [MENetworkLayout]         # the ME blocks the build needs, per network (v5, #333)
  metrics: { footprint, layers, buildability, congestion, rounds?, ... }
                                         # rounds: how many rounds of the multi-start ran, only on
                                         #  a solve given a time budget or a round count (absent,
                                         #  not null, otherwise); --rounds that many replays it
  seed: int                              # for the seed-compare workflow

Placement   { machine_id, cell: CellCoord, orientation: Facing }   # orientation horizontal only
Route
  net_id: str
  commodity: "item" | "fluid" | "power"
  terminals: [Terminal]                  # where the route meets each machine endpoint (covers ride here)
  segments: [Segment]                    # cell-path; lowered to blocks only at export. Empty only
                                         #  on a one-block pipe (v3, below); a cable always has one
  thickness_per_segment: [int] | null    # power only (else null); 1/2/4/8/12/16, summed amperage
  material: RouteMaterial | null         # the STAND-IN it is drawn as; null = unspecified pipe
RouteMaterial
  family: "cable" | "fluid_pipe" | "item_pipe"   # must match commodity (cable<->power, ...)
  material: str                          # GT's unlocalized name ("tin"); dataset/pipes.py joins it
                                         # to the manifest under whichever name that dump records
  tier: str | null                       # cables only (required); the voltage tier the gauge rates
  size: "tiny" | "small" | "normal" | "large" | "huge" | null
                                         # pipes only (required), invalid on cables (whose gauge is
                                         #  thickness_per_segment). The pipe's GT size, REAL like a
                                         #  cable's thickness: the router sizes an item run from what
                                         #  its endpoints need (dataset/pipe_capacity.py). Added in
                                         #  LayoutResult v2 (BREAKING, see Versioning)
  stand_in: bool                         # always true - the MATERIAL is representative, NOT a build
                                         #  spec; the size and thickness are real
Terminal    { machine_id, port_id, face: Facing, cell: CellCoord }  # non-front face; cell just outside
MENetworkLayout { id, colour: AEColor, cables: [MECableCell], devices: [MEPlacedDevice] }
              # one InputIR.me network as built; a cell listed once. Its controller, acceptor,
              # stub and link are placements (Machine.me_role); the stub and the link are cables
              # too, so their cells are listed here. Which blocks join which network in game is
              # the validator's to rebuild from these blocks: AE joins whatever compatible blocks
              # touch. MENetworkLayout.cells() is every cable block.
MECableCell { cell: CellCoord, kind: MECableKind, me_channels: int }
              # me_channels: the solver's own count of channel devices routed through it, which
              # the previewer lights; the validator never trusts it
MEPlacedDevice { machine_id, endpoint_id, kind: MEDeviceKind, cell: CellCoord, side: Facing,
                 gt_mid: int | null, cards: MECards, config: [str] }
              # builds the named MEEndpoint. A part: the cable it sits on and the side it takes,
              # facing the block it works on. A GT ME hatch: the casing cell it takes and the way
              # its front faces, where AE reaches it
PlacedHatch { machine_id, kind: str, cell: CellCoord, facing: Facing, port_id: str | null }
              # cell is the BODY cell the hatch replaces, inside the footprint - not the dock
              # cell outside it. port_id is null for a hatch that serves no port: an upkeep
              # hatch (maintenance, muffler), which is why the record has to exist at all, or a
              # SPARE output hatch on a tower layer no product uses, since GT forms a tower only
              # with an output hatch on every layer (#299; additive, no LayoutResult bump).
Segment     { start: CellCoord, end: CellCoord, channel: int }   # >= 0 only; the per-edge channel cap is Phase 2, not yet enforced
AutoConnection { net_id, source_machine_id, source_face: Facing, target_machine_id, target_face: Facing }
Infeasibility { constraint: str, detail: str, suggested_relaxation: str | null }
```

`Facing` is one of `north|south|east|west|up|down`. A machine's `orientation` (front face) is
**horizontal only** (`north|south|east|west`) - GT machines never face up/down, though those
faces can still carry I/O. Each producer of a non-ME net reaches it **exactly once**: through a
pipe `Route`, or through an `AutoConnection` (the source machine auto-ejecting straight into an
adjacent target's input face - no pipe, no cover; `source_face` points source->target,
`target_face` is the opposite, both non-front). A net with one producer is therefore either routed
or auto-connected. A net with several producers and **one** consumer may be both (v4, #270): each
producer standing against the consumer has an `AutoConnection` of its own, and the net's route
carries terminals for the other producers and the consumer only, or is absent once every producer
is covered. A net with several consumers is never split, because a producer's auto-output reaches
one block. A `Terminal` records where a *pipe* docks: the non-front `face`
(covers ride on the machine face, never the pipe) and the adjacent `cell`. Several terminals of
one route may share a `cell` when they belong to different machines (one pipe block wired to
several neighbours, #164); two terminals of one machine may not, and neither may two routes. That
is documentation of what the schema always allowed, not a version bump. When **every** terminal of
a pipe route shares one cell, that block is the whole route and `segments` is empty: a
**one-block pipe**, wired straight to each machine, which is how GT builds a connection between two
machines with one free cell in common (v3). A cable route may not be one block, because its gauge
lives on its segments. So a route's blocks are its segments' cells, or, with no segments, its
terminals' cell; `Route.cells()` reads both forms, and a consumer should use it rather than walk
`segments` alone. `Segment` uses
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
- A net that rides ME (`InputIR.rides_me`) is removed from physical routing: no `Route` and no
  `AutoConnection` for it today, it is simply skipped everywhere. Placing the ME device that
  replaces it is the end-to-end build (#335). With `power_external` the adapter emits no power
  source and no power net at all (#225); the powered machines keep their power ports, which state
  the draw.

## Versioning

- `version` is an int on both IR roots. Additive fields can land without a bump; any change
  that breaks an existing consumer bumps it and updates all consumers in the same PR.
- "Breaks an existing consumer" includes breaking by *omission*. `LayoutResult` v1 added
  `hatches`, which is additive in shape, and still bumped: a consumer that ignores it renders a
  build for a machine with no maintenance hatch and no muffler, which will not run. Nothing
  raises; the build is simply wrong. `LayoutResult` v2 added `RouteMaterial.size` and bumped for
  the same reason: a consumer that ignores it builds every pipe at the normal size, and a normal
  tin pipe feeding three hammers from one chest fed one of them in game (#165). `LayoutResult` v3
  changed no field at all and still bumped: it lets a pipe route be one block with no segments,
  and a v2 consumer that builds a route's blocks from its segments builds nothing for that pipe.
  `LayoutResult` v4 is the same kind of bump: a net may now be partly routed and partly
  auto-connected, and a v3 consumer that reads a net as one or the other can drop the half it does
  not expect. `InputIR` v6 breaks by omission too: a v5 consumer that ignores `output_layer` docks
  a tower's outputs on any layer, so each product leaves through a hatch GT fills with another.
  So does `InputIR` v7: a v6 consumer that ignores `structure_blocks` draws a Chemical Plant as the
  dump's default build, a solid casing GT refuses the recipe on and machine casings below its
  hatches, on which the plant does not form. `InputIR` v8 breaks by removal: `me_toggles` is gone
  and ME is chosen per net (`InputIR.me`, `Net.me_network`). `InputIR` v9 and `LayoutResult` v5
  break by omission: a consumer that ignores `Machine.me_endpoints` or `LayoutResult.me_networks`
  builds a line whose nets on ME have no device and no cable.
- **An older layout is not upgraded on read.** v2's one new field would be easy to fill in for a
  v1 payload (every v1 pipe was normal), but the rule below is only worth having if it has no
  exceptions, so a v1 layout is refused and regenerated by re-solving its plan. Nor does `size`
  default within v2: a pipe that silently falls back to normal is the failure the bump ends.
- **`version` is enforced on parse.** A payload whose version is not this build's is rejected,
  in either direction: older, because a bump can change what an existing field *means* and not
  just which fields exist (v2 -> v3 did exactly that to a power port's `rate`); newer, because
  being unable to name what changed is a reason to refuse, not a reason to hope. `extra="forbid"`
  catches only the other half, a payload carrying fields this build does not know.
- Keep a short changelog of contract changes at the bottom of `src/gtnh_solver/ir/__init__.py`.
