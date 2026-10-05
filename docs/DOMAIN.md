# Domain - GT:NH rules the solver encodes

The knowledge a contributor (or the dataset author) needs but won't necessarily know. These
rules live as *data* in `dataset/` and as *checking logic* in `validator/` (shared data,
independent logic - see [`ARCHITECTURE.md`](ARCHITECTURE.md)).

> If you play GT:NH and spot an error here, fix it - this doc is the reference both the
> router and the validator are built against, so a wrong rule here propagates everywhere.

## Platform

- GT:NH is **Minecraft 1.7.10 / Forge**. Block identity is numeric ID + metadata
  (pre-flattening), which matters for the eventual export.
- **Litematica does NOT support 1.7.10.** The in-game schematic consumer is
  **Schematica (GT:NH's fork)**. Target classic `.schematic`, not `.litematic`. What the fork
  does with one, read from its source:
  - A loaded file is a client-side **hologram**. Its printer places each block by a simulated
    right-click with the block's pick-block item and **applies no tile-entity NBT**, and it has no
    paste command. GT then places the block as if by hand: a machine is the right machine (the
    pick-block reads the hologram's `mID`) but fronts the player, and a pipe connects only to the
    block it was clicked against. So the facings, pipe wiring and auto-output an export records
    show in the hologram only; the player sets them in game.
  - A **GUI save reads the client world.** On GT 2.9 that keeps the pipe wiring, the facings and
    which cover sits on which side, the same as a server-side save. On GT 2.8 (5.09.51) every pipe
    in it reads unwired: GT synced a pipe's connections to the client only on its base tile entity,
    and an item pipe or cable then saves its own unsynced copy over them. What a GUI save lacks on
    any version is live state (what is in a pipe, stored energy, progress) and a cover's
    **settings** (a conveyor's import or export mode, its tick rate): GT sends the client only each
    cover's id, so they save as defaults unless the client happened to receive them, by opening the
    cover's GUI or looking at the block with WAILA. `tests/golden/schematic/README.md` has the
    measurements. `/schematicaSave x1 y1 z1 x2 y2 z2 name` reads the server world instead. In
    single-player it writes to the same `schematics/` folder as the GUI; on a dedicated server, to
    that player's own folder on the server.
  - Only **block ids** are remapped on load (through `SchematicaMapping`). Tile-entity NBT loads
    as written, so anything in it that names an item by number, a cover's `id` or an inventory
    slot, is right only in a world with the same item ids. A wrong cover id loses the cover, swaps
    in another, or can crash the client drawing it. FML assigns item ids per world (two worlds of
    one 2.9 instance put `gt.metaitem.01` at 7639 and 7436), and each world's `level.dat` lists
    its own.
  - Block metadata is kept to **4 bits**, which loses a GT 2.9 frame's material (#212).
  - Under Angelica (GT:NH 2.9) the **hologram vanishes at some view angles**, whatever the file
    holds (a copy with its covers stripped vanished the same way). Schematica culls each
    16x16x16 piece of the hologram with one `Frustrum` it keeps for the whole session
    (`RendererSchematicGlobal`). Vanilla's `Frustrum` asks the clipping helper that
    `EntityRenderer` refreshes every frame, but Angelica 2.2.10 (`MixinFrustrum`) freezes a
    `Frustrum`'s camera when it is built, so Schematica culls against its first frame's view.
    The maintainer's Schematica fork builds a new `Frustrum` each frame (branch
    `fix/hologram-frustum`), confirmed in game on 2026-10-04.
  - Stock Schematica draws a GT controller that faces **north upside down** (and one facing east
    mirrored), whatever the file holds. GT orients a controller's front texture from the tile
    entity it finds in the player's *real* world at the hologram's local coordinates
    (`GTRenderedTexture.getExtendedFacing`). There is none there, so it falls back to a flip table
    meant for plain blocks, which flips a north face vertically. That is also why a file's
    `eRotation` changed nothing in the hologram. The maintainer's Schematica fork adds a mixin that
    reads the hologram's own tile entity instead (branch `fix/controller-facing`), confirmed in game
    on 2026-10-05; with it, `eRotation` and `eFlip` take effect in the hologram too.
  - An **AE2 cable bus is all tile-entity NBT**: its cable, its colour and every part on it, each
    part's cards and filter included (#339). So the printer places at best an empty cable bus, and
    an exported ME network is in the hologram only; the builder places its cables and parts by
    hand from it. Every item in a cable bus is named by the world's numeric id as well, so the
    export writes cable buses only with `--world`, as it does covers (ME networks, below).
- There is **no headless GT simulator**, so true correctness is only verifiable in-game.

## Machine faces

### A single-block machine

- A machine has six faces. The **front face** (set by orientation) is the working face and
  carries **no item/fluid I/O**. The solver chooses orientation so required I/O faces stay
  routable. A block whose faces do fixed jobs (the Item Filter below) states them per port instead
  (`Port.faces`, read through `Machine.allowed_faces`), and may then use its front.
- The **other five faces** can each be input OR output of items or fluids. Routing a specific
  commodity onto a face may require a **cover** (conveyor for items, pump/regulator for
  fluids); a cover occupies that face. The layout records the face each pipe docks on (the
  route's terminal); the cover follows from the commodity and the port's direction, and no output
  names it yet.
- A machine **auto-outputs to a single face**, carrying **either items or fluids, not both**.
  A machine emitting both an item and a fluid output uses auto-output for one and a
  cover-driven output on another non-front face (or ME) for the other.
- **Several machines can auto-output into one block**, each from its own side of it: a Super Chest
  standing among three Forge Hammers takes from every hammer touching it, with no pipe. The solver
  covers such a net producer by producer (#270): each producer standing against the consumer ejects
  into it, and the rest share one pipe into it. Only when the consumer is a single block, since on a
  multiblock each connection is a hatch, and only from a single block with no other output of the
  kind, since its output face ejects every slot of that kind.
- **One face is one connection, but one pipe block can serve several machines.** A pipe block
  wired to faces of several machines on the same net is a manifold, and a real build uses it
  freely: the maintainer's parallel-sand build puts 20 item connections on 12 pipe blocks that way
  (#164). Two connections of *one* machine through one face are not modelled: the solver gives
  each connection a face of its own, and the validator rejects two on one face
  (`terminal_face_contention`). Whether a basic machine's output face also takes input differs by
  pack: `mAllowInputFromOutputSide` is **off** by default on 2.8.4 (`MTEBasicMachine.java:118`)
  and **on** by default on 2.9 (`:123`), where, with the machine's input filter also off by
  default, the output face accepts any item into an input slot that is empty or holds the same item
  (`allowPutStack`, 5.09.54.20 lines 965-979; only with the filter on is it limited to recipe
  inputs). A layout that relied on either default would run on one pack only. And two **nets** never share a
  pipe block: a GT item pipe delivers to any wired inventory that accepts the stack, and nothing in
  a plan says two nets carry the same item, so a shared block would cross-feed them.
- **A single block with several item outputs sends them out of one face, sorted by Item Filters
  (#249).** Five usable faces, one per connection, is not enough for a machine like the Ore Washer
  (item in, fluid in, three item outputs, power), and a machine that has faces to spare still
  ejects through only one of them. A GT basic machine with item
  auto-output on ejects *every* item slot through its output face, so the build is one pipe from
  that face (the **trunk**) to one **Item Filter** per item. The filter
  (`MTEFilter` < `MTEBuffer`; the ULV one is mID 9240 in both packs and needs no power) takes items
  on every face but its back, its front included, keeps only what its slots name, and pushes one
  stack at a time out of its **back** into whatever is there, with no toggle
  (`MTEBuffer.moveItems`). A GT item pipe skips an inventory that refuses a stack and never pushes
  back to the side it received from (`MTEItemPipe.sendItemStack`), so each item reaches the one
  filter that takes it. The adapter builds this for every machine that has at least two item
  outputs and is **proven** a single block (its handler says `single`, or a census dataset for the
  plan's pack lacks it): a 1x1x1 box that might be a multiblock missing from the dataset is left as
  it is and reported. It used to merge only a machine with no face to spare, leaving the others a
  face per output, which is a real build only with a cover pulling each extra output out; with the
  filters shared per node that cost more than merging them all. The machines of a parallel node share one trunk and one filter
  per item, which is how the maintainer builds iron.json's washers: every output face on one pipe,
  the filters sorting all of them. On 2.8.4 a sibling's output face refuses the others' items. On
  2.9 it takes any item into an empty input slot (above), so a stocked machine never does, but one
  that runs dry can take a stray output and jam; a screwdriver right-click on each sibling's output
  face ("Input from Output Side forbidden") rules it out. The same holds for fluids
  (`isLiquidInput`) and for any pipe several machines' outputs share, a feed run (#270) as much as
  a trunk. The preview draws each such machine's auto-output arrow red, with the step on its hover,
  and the `.schematic` export lists them, for a plan balanced against 2.9 (`output_faces`, #278). The filter's faces are pinned in the IR (`Port.faces`: input on front, left,
  right, up, down; output on back), and the validator checks that every trunk item has exactly one
  filter, that each filter's output carries only its items, and that nothing but its own output
  sits behind it (`FILTER_ITEM_UNSORTED`, `FILTER_BACK_NOT_ITS_OUTPUT`). The routers hold that
  cell for the net the filter feeds: every other net, and every cable, treats it as a wall. Two
  limits: a filter faces horizontally only, because every machine orientation is horizontal, so its
  back is never up or down; and fluids are never merged, since GT has no fluid filter block. #248's
  Item Distributor would ride the same face pins.
- **A single block still short of faces takes its inputs through fewer of them (#277).** Real plans
  draw machines that need more than five connections on the input side too: platline's Chemical
  Reactors take one dust from two producers (a face per feed), and a fertilizer Mixer takes three
  item inputs (a face each). GT builds both with fewer pipes. Two feeds of one input join one pipe
  before the face. And a basic machine takes items on any face but its front and, with multi-stack
  off (`mDisableMultiStack`, on by default and written by the `.schematic` export), keeps each kind
  of item to one input slot (`MTEBasicMachine.allowPutStack`), so one pipe can carry all its item
  inputs without one item filling every slot and starving the rest; a GT pipe pushes a stack only
  where it is taken, so a full slot holds back its own producer and no other. The adapter does the
  first for any input of a machine short of faces, and the second, a **feed run** (a merged run,
  `Net.items`, whose consumer is the machine instead of filters), only for a machine still short
  after it. Both apply only to a proven single block with **more** connections than faces, not
  merely none to spare as on the output side, so a line that lays out today is left exactly as it
  is. A feed run takes only nets that carry one item into that machine alone and that no Item
  Filter sources, and only if every machine on it takes every item on it, so no item reaches a
  machine that did not ask for it.
- **A single block auto-outputs through one face; any other output face is a cover (#249).** A
  basic machine pushes items and fluids out of its output face (`mFacing`) and nowhere else
  (`MTEBasicMachine` 2.8.4 lines 583-610, 2.9 lines 612-633); its working face is `mMainFacing`, the
  solver's front. A Super Tank pushes fluid out of its front (`MTEDigitalTankBase`, `mOutputFluid`),
  an Item Filter out of its back with no toggle, and a Super Chest nowhere at all
  (`MTEDigitalChestBase` only moves stock between its own slots), so taking items out of a Super
  Chest always needs a conveyor cover. `output_faces` is the one reading of which face each single
  block auto-outputs through (an auto-connection's face, else the face carrying the most outputs)
  and which of its other output faces need a cover (a conveyor for items, a pump for fluids). The
  previewer draws the auto-output arrow on that face on **every** single block, piped or not, so a
  builder never reads a conveyor where none is meant, and an amber marker on each cover face; the
  `.schematic` export writes that face as the block's output facing and warns about each cover,
  naming the tier that keeps up with what leaves through it (`dataset/covers.py`). It writes the
  covers themselves only with `--world`: a cover names its item by the world's numeric item id,
  which Schematica does not remap (see Platform above), so the export reads the target world's own
  table from its `level.dat`, and the file is then right for that world only. A machine can still pipe outputs out of two faces where
  they are not merged (an item output beside a fluid one, or a 1x1x1 machine not proven a single
  block): that build needs one cover, and says so.
- **Required-I/O-face reachability is a HARD constraint** - a blocked required output face
  means the line doesn't run. "Convenient access" is a soft preference.

### A multiblock's hatches: the five faces are NOT interchangeable

Everything above is a single-block machine's rule. A multiblock does no I/O of its own: every
connection is a **hatch or bus**, which is one casing cell of the structure replaced by a
different block, with its own front facing. Three consequences the solver has to honour, none of
which GT will catch for you.

- **Items and power are front-face-only, in both directions.** An input bus accepts items only on
  its own front (`MTEHatchInputBus.allowPutStack`), an output bus pushes only on its own front,
  every 8 ticks, into whatever inventory is adjacent (`MTEHatchOutputBus.onPostTick`), and an
  energy hatch takes power only on its front (`MTEHatchEnergy.isInputFacing`). A dynamo is the
  same in reverse. So for these, a face is usable only if the receiver is *on* it.
- **Fluids are omnidirectional, and that is a footgun rather than a freedom.** `isLiquidInput` /
  `isLiquidOutput` default to true on every side, and a fluid *input* hatch does not override
  `isLiquidOutput` - so a pipe that merely touches one can **drain** it. (A fluid *output* hatch
  does override `isLiquidInput` to false, so it cannot be back-filled.)
- **The facing is entirely ours to get right.** `IStructureElement.check` takes no facing and every
  GT hatch returns `isFacingValid = true`, so a multiblock forms perfectly happily with every
  hatch pointing into its own structure, and then moves nothing. GT's own survival auto-builder
  uses a heuristic worth mirroring: the first face not contained in the structure piece,
  preferring a horizontal one.

Three more structural facts follow from a hatch *being* a casing cell:

- **A maintenance hatch is needed exactly once, and this one bites at formation time**, unlike the
  muffler below, which is a runtime rule. GT reads the *count*: 57 of the 64 controllers that touch
  `mMaintenanceHatches` assert `size() == 1` in `checkMachine` and the remaining 7 demand `<= 1`, so
  none of them forms with two. Both directions are therefore errors, and they are separate codes
  because they are separate fixes: `MAINTENANCE_MISSING` for none and `MAINTENANCE_DUPLICATE` for a
  spare. Note what the spare is not: two maintenance hatches on two different casing cells break no
  other rule at all (both sit on real body cells, both face outward, neither shares a block), so
  nothing but the count catches it. As with the muffler, "the dump records a `Maintenance`-capable
  cell" is the proxy for "this machine needs one", which over-asks on the 7 that accept zero.
- **One cell is one block.** An input bus and an energy hatch cannot share a cell, not even by
  facing two different ways, so a machine's connections all compete for one pool of casing cells
  (`HATCH_CELLS_EXCEEDED`, and `terminal_hatch_contention` for the per-cell case).
- **Position can carry meaning.** A Distillation Tower routes recipe fluid output `i` to the
  output hatches on its `i`-th layer above the base and to no others
  (`MTEDistillationTower.addFluidOutputs`), and it **does not form** while any layer has no output
  hatch, a layer no product uses included (`checkMachine`: "layer without output hatch"). Which
  layer a cell feeds is GT's own bookkeeping, read from the dump (`HatchSlot.output_layer`, from the
  machine's own structure check), never assumed from height: a Mega tower's layer is a five-high
  band, and a tower's top centre takes an output hatch that feeds no layer. So each fluid output
  docks on its own layer's cells alone (`Port.output_layer`, `OUTPUT_HATCH_WRONG_LAYER`), and a
  layer no product uses gets a **spare** output hatch that receives nothing (`OUTPUT_LAYER_EMPTY`
  without one). An Assembly Line feeds its `n`th input bus from the `n`th recipe input. Which cells
  accept which hatch kinds is dumped per controller (`Machine.hatch_slots`), and those kinds are a
  **lower bound**: an adder built from a bare method reference exposes no filter, so a cell is
  recorded without a kind rather than as refusing it.

Two more bind the machine at **runtime**, where a structure that formed perfectly still misbehaves:

- **A muffler needs literal air in front of it.** `MTEHatchMuffler` tests `getAirAtSide(front)`, so
  a cable, a pipe, a casing or a neighbouring machine in that cell makes `polluteEnvironment` fail
  and stops the machine with `POLLUTION_FAIL`. The cell in front of a muffler is therefore a routing
  **keep-out** (`MUFFLER_BLOCKED`), a constraint class nothing else in the solver has. Where the
  muffler can face only one way (an Electric Blast Furnace's is the top centre, facing up), the
  routers treat that cell as an obstacle from the start; a muffler with several choices takes
  whichever the finished routes left open (#228). GT offers the
  muffler element only to a controller that pollutes, so "the dump records a `Muffler`-capable cell"
  is the usable proxy for "this machine needs one" (`MUFFLER_MISSING`) - it over-places on the few
  that accept one without asserting it, which is the safe direction: a spare muffler costs a casing
  cell, a missing one stops the machine. Unlike the maintenance hatch, **several mufflers are
  legal**: `MTEMultiBlockBase.polluteEnvironment` divides the vent batch across all of them, and
  some controllers assert 2 (Nuclear Salt Processing Plant) or 4 (Nuclear Reactor, the larger
  turbines), so there is no duplicate-muffler error to report.
- **Which hatch a product lands in is the machine's choice, not ours.** `addOutput` takes the first
  hatch that can store the stack, so with two output hatches nothing guarantees the pipe we routed
  from one carries the product we routed it for. Pinning it is a player action: lock **every**
  output hatch of a kind to its own product, since a locked hatch that fills spills into an
  unlocked one. A fluid hatch is locked through its `Locked Fluid` slot (in 2.9 an empty locked
  hatch receives nothing, so the slot must be set), a bus through its output filter, which both
  packs have. `hatch_locks` derives which hatches need it: those of a kind carrying two or more
  distinct products, on any machine but a tower that fills by layer (the distillation tower and
  sparging recipe maps), where a lock decides nothing and one that disagrees voids the product. The
  preview shows the lock on the hatch's hover (#120).

**One port is one hatch, so it is one net** (#213). A plan draws an edge per consumer, so an output
feeding two machines arrives as two edges out of one port. In game that port is one hatch with one
pipe network behind it, and a hatch per consumer would not be the same thing: for an input it
would work, but for an output GT fills the first hatch that can take the product (above) and the
second consumer starves. So the adapter maps every set of edges that meets at a multiblock port to
one net, transitively, and names it by the edges' ids joined with `+`. Such a net carries what its
producers move, each counted once. A boundary storage's shared port stays one net per edge: a Super
Chest or Tank has no hatch, and its covers can push through several faces.

## Fluids and items (pipes)

- A fluid pipe line carries **one fluid type**; a pipe has a per-tick throughput cap by tier
  (hard constraint). Items routed physically have an analogous per-tick cap by tier.
- v1 ships **single-channel** GT pipes/cables. Transport is **pluggable per commodity**;
  planned backends: GT++ quadruple (4-channel) and nonuple (9-channel) fluid pipes (turn
  per-cell fluid routing into channel-packing), and EnderIO conduits for early/mid-game.

### An item pipe's capacity is insertions, not items (#165)

Proven in game the expensive way: the maintainer built our export of the parallel sand line with
plain tin pipes, and only **one of three** stone hammers was ever fed. By item count that pipe
should have carried the line five times over. The unit was wrong, and GT's source says why
(`MTEItemPipe`, at the tag the pack pins):

- **A pipe counts insertions.** Every successful `sendItemStack` increments its transfer counter
  by one whatever it moved, and the pipe stops once the counter reaches its slot count, until the
  window resets every `mTickTime` ticks. The tooltip calls these "Stacks".
- **One insertion is one stack into one inventory**, at most 64 items
  (`insertItemStackIntoTileEntity` calls `moveMultipleItemStacks(..., 1)`).
- **The nearest inventory is tried first** (`scanPipes`, sorted by routing distance). A working
  machine always has room for a few more items, so the nearest one takes an insertion every window
  it is offered one, and a pipe with too few insertions feeds the near machines and starves the far
  ones. That is the one-hammer-in-three.
- **A pipe block refuses a new stack until it is empty** (`allowPutStack`), so each source that
  feeds a run fills its own block, and that block needs an insertion to drain it.

GT builds every item pipe from its huge size's slot count `H` (`ItemPipeBuilder`; tin has
`H = 2`), and for tin that gives, per size:

| size | mID | insertions per window | per 40 ticks |
|---|---|---|---|
| tiny | 5589 | 1 per 160 ticks | 0.25 |
| small | 5590 | 1 per 80 ticks | 0.5 |
| normal | 5591 | 1 per 40 ticks | 1 |
| large | 5592 | 1 per 20 ticks | 2 |
| huge | 5593 | 2 per 20 ticks | 4 |

**What GT does not settle** is how often an endpoint must be served for its machine never to run
dry, which depends on covers, recipe times and buffers a plan does not carry. The solver takes it
from the one measurement there is: the plain pipe (1 per 40 ticks) fed one hammer and not two, so
**each endpoint needs one insertion per 40 ticks**, plus one more for each further stack it moves in
that time, **but never more insertions than items it moves**. GT counts an insertion only when the
send succeeded and moved at least one item (`MTEItemPipe` lines 221-223, 326-337), so a consumer
takes insertions only as fast as its machine eats items: the near hammer that starved the other two
ate 4 items per 40 ticks, while a machine eating one item per 400 ticks spends a tenth of an
insertion and leaves the rest to the far ones (#249; iron.json's washers feed four Thermal
Centrifuges 0.01 items/t in all, which a plain pipe carries). Every calibrated case moves at least
an item per 40 ticks, so the cap changes none of them; that a slower stream is served at its item
rate is read from GT's source, not yet measured in game. A net with no throughput recorded states
no rate, and each of its endpoints keeps the whole insertion. A run's demand is the larger of its two sides, summed over their endpoints: all the
sinks it tops up, or all the source blocks it drains. The router lays the smallest size that meets
it, for the run as a whole, and never below its busiest block's charge (`router/core.py`,
`_pipe_size`, and `router/item_pipes.py`, #200; the figures are `dataset/pipe_capacity.py`).

Consequences worth knowing:

- **Sized per run, not per segment**, unlike a cable. Where GT's nearest-first routing sends items
  depends on buffers the layout does not model, so the run takes the size of the point where every
  stream on its crowded side can meet. A run whose producers and consumers pair off along it (the
  maintainer's hand build, one producer and one consumer per pipe block) needs less, and may be
  over-sized by one step. Over-sizing never starves anything.
- **But never below its busiest block's charge** (#200). The per-run point counts endpoints, and a
  producer that splits its output between consumers makes more streams than either side has: two
  producers at 0.15 and 0.05 items/t into two consumers at 0.1 make three, and the blocks between
  them pay for all three, where the per-run bound asks for two. So the router also reads each
  block's charge the way the validator does (below), and lays the larger of the two. That only
  raises a run the validator would refuse at the per-run size, so every other run keeps its size.
- **Past huge tin there is nothing bigger in the stand-in material.** A run needing more is laid
  huge; the answer is a faster material (brass, electrum, platinum make 2x, 4x, 8x tin's
  insertions), which the stand-in policy does not choose yet. Whether a laid size is enough is the
  validator's question, not the router's, and it answers it per block (below).
- **Sizes are sized against tin, GT's slowest item pipe**, so a builder who swaps in a better
  material at the same size is never short.
- **Fluid pipes are not sized yet** and are laid at the normal size. A fluid pipe's capacity is a
  plain mB/t figure (a normal bronze pipe takes 120), and the shipped lines' busiest fluid net
  moves 31.25 mB/t, so nothing shipped is short, but it is not yet a rule.

### What the validator checks: the streams through each block (#190)

The router's per-run bound is safe for laying pipe and wrong as a refusal threshold: it would refuse
the maintainer's working build, which has large pipes where that bound asks for huge. So the
validator reads what each pipe block is actually charged for, from the same transfer loop
(`MTEItemPipe.onPostTick`, lines 210-224 at the pinned tag) and on its own arithmetic. The router
reads the same charges to set its floor (`router/item_pipes.py`, #200), but the two share no code,
so a mistake in either shows up as a size the validator refuses:

- **A delivery charges every block its sender's scan reached no later than the target.** The sender
  is the block the producer pushed into. It scans the run nearest first, adds each block it passes
  to a list, and every successful insertion charges everything on that list. So a delivery pays at
  the sender, at the target and at every block between them, and also at any other block no farther
  from the sender. Blocks exactly as far as the target are ordered by a `HashMap` keyed on the pipes'
  identity hashes, which can change between sessions, so the validator counts them as charged: a
  build that works only when that order breaks its way does not reliably work.
- **Deliveries go nearest first, and onward only when a consumer is full.** An insertion a consumer
  cannot take fails, charges nothing, and the sender tries the next block. In steady state that
  matches producers to consumers nearest pair first, each consumer taking only its share of the
  net. Each matched pair is a **stream**, needing one insertion per 40 ticks plus one per further
  stack it moves, and never more than the items it moves in that time.
- **A block's demand is the sum over the streams it pays for**, against its size's insertions per 40
  ticks. A saturated block drops out of every scan and stops the scan passing through it
  (`IMetaTileEntityItemPipe.Util.scanPipes`, lines 57-58), which is how the far consumers starve.

On the parallel sand build the stone chest docks on the end block of its run, so stone for the two
far hammers crosses that block: 3 streams, huge. The sand run is the mirror image. The cobblestone
and gravel runs pair each producer with the consumer beside it, 1 stream a block, which the large
pipes that were built carry with room to spare (a normal pipe would too, by the same calibration,
though only large has been built). The plain-pipe export is refused on its stone and sand runs and
the working build is accepted.

The check covers **items only**: GT moves fluid as a volume per tick split across every accepting
neighbour (`MTEFluidPipe.distributeFluid`), not by insertions, and there is no fluid capacity data
yet. A route with no material states no size, so it is not judged; the router always publishes one
and the `.schematic` exporter refuses a route without one.

## Power (shared-amperage net)

Power is **not** a disjoint per-pipe flow. Multiple machines pull amperage down a shared
conductor; the cable burns if total amperage exceeds its rating. Model it as a shared net
where load **sums** along shared segments (Steiner-tree-like):

- **Voltage tier** of a cable segment follows the **machine voltage tier** it serves (cable
  rated ≥ that voltage).
- **Machines draw fractional amps on average.** A machine pulls whole packets (1 amp = one packet
  of up to tier voltage) into an internal buffer only when it has room, so a 16 EU/t LV machine
  takes a 32-EU packet every other tick - an **average of 0.5 amps**, not a whole amp. Its load on
  the net is `eut / delivered_voltage`, un-rounded; loads **sum** along shared segments and only
  the aggregate rounds up to whole amps (per segment for cable thickness, per tier for the source
  feed). Rounding per machine would overstate the draw - three 16 EU/t hammers run on a 2 A LV
  feed, not 3 A (confirmed in game).
- **Voltage loss over distance.** A cable loses voltage per block travelled, so a machine `d`
  blocks from the source receives `tier_voltage - loss·d`, not the full tier voltage. The solver
  keeps the source at the machine's tier and *thickens the cable to compensate*: a machine's
  load is sized at its **delivered** voltage (`eut / (tier_voltage - loss·d)`), so a machine
  farther from the source loads the net **more** for the same `eut`. A run so long that the
  delivered voltage reaches 0 cannot be powered at that tier and is reported infeasible. Loss is a
  flat **1 EU/block for every tier** for now (a simplifying assumption; per-material cable loss is
  Phase 2 dataset work).
- **A machine is not one connection.** A multiblock takes power through **energy hatches**, and a
  standard GT energy hatch accepts **2 amps** per tick - `MTEHatchEnergy.maxAmperesIn()` returns 2,
  its in-game tooltip reads "Accepts up to 2 Amps", and `BaseMetaTileEntity.injectEnergyUnits`
  enforces it against a per-tick `mAcceptedAmperes` counter. A multiblock's intake is the **sum**
  over its hatches (`MTEMultiBlockBase.getMaxInputPower()` adds `voltage x amperage` per hatch), so
  a machine drawing more than one hatch can take is fed through several, on as many cable runs as
  the 16x cap needs. TecTech's 4A/16A/64A hatches exist from **EV up only**; below that the 2 A
  hatch is the only one there is. The ceiling is rarely felt in play because it is 2 amps *at the
  hatch's own tier* - one HV hatch already passes 1024 EU/t.
- **A single-block machine's intake is a different rule, on a different class.** A GT **basic
  machine** (`MTEBasicMachine` and its subclasses: the Macerator, Forge Hammer, Chemical Reactor
  and the rest of the single-block processing machines) has no hatches at all. It takes power
  through its own block, and `MTEBasicMachine.maxAmperesIn()` returns `(mEUt * 2) / V[tier] + 1`
  on integer division, which `BaseMetaTileEntity.injectEnergyUnits` enforces against the same
  per-tick accepted-amperes counter as a hatch. So its ceiling **scales with the recipe it is
  running**, where a hatch's is fixed. Two things bound where the rule may be used:
  - **Class.** `MTEMultiBlockBase extends MetaTileEntity`, so a multiblock neither is nor inherits
    `MTEBasicMachine` and never uses `maxAmperesIn` for intake - the 2 A hatch bullet above is its
    rule. The flat `1` of `MetaTileEntity.maxAmperesIn` is the bare-block default every basic
    machine overrides, so it is not a fallback for either.
  - **Domain: `mEUt <= V[tier] * mAmperage`.** Both overclock paths cap the consumption they
    compute at that (`MTEBasicMachine.calculateOverclockedNess`,
    `EUOverclockDescriber.createCalculator`), and `mAmperage` is 1 on a standard basic machine, so
    a real one never carries a larger `mEUt` and GT never evaluates the formula above it.
    Extrapolating past it turns an upstream EU/t figure the exporter got wrong into a confident
    amp count no GT block can have.

  Inside that domain the ceiling is *nearly* vacuous by construction: `floor(2e/V) + 1 > 2e/V`, so
  a basic machine can always take in the recipe it runs at the source voltage, and the shortfall
  check can only fire past `V/2` cable blocks - 17 at LV, 65 at MV, 257 at HV, or 22 / 86 / 342
  for a machine drawing its whole tier - with `POWER_VOLTAGE_DROP_EXCESSIVE` taking over past `V`.
  The rule is still worth stating, because the alternative models are both wrong in the expensive
  direction, but it is not a load-bearing gate on a short run.
- **The solver states an intake ceiling only where it knows which rule applies.** That needs the
  machine's class, which means a physical dataset that is a **complete census** of the pack's
  multiblock controllers: only then is absence from the dump evidence that a machine is a single
  block. With no dataset - or with the committed fixtures, which are a sample and say
  so (`_meta.json`'s `census: false`) - the class is unknown, `Port.max_amps` stays unset, and the
  connection reads as genuinely unmeasurable. The validator then *reports* that it did not measure
  the machine (`ValidationReport.unverified_power_intake`, surfaced by the CLI) instead of picking
  whichever of the two rules happens to be coded: a plausible wrong ceiling is worse than an
  honest gap, and a silent skip is worse than either.
- **Enough power must arrive, not just fit the cable.** Loss shrinks every packet, and a hatch
  passes a bounded number of them, so a machine's real intake is
  `sum(hatch_amps x delivered_volts)` over its hatches. A machine whose intake falls short of its
  `eut` cannot run its recipe even though every cable on the way is thick enough - the solver
  re-places the machine nearer its source, and reports it (`POWER_SUPPLY_INSUFFICIENT`) if that
  cannot close the gap, rather than certifying it. The reverse - a cable offering a hatch more
  amps than it can take - is deliberately **not** treated as an error: the hatch simply takes its
  2 and the under-supply check is what catches a genuine shortfall.
- **Thickness** (1x / 2x / 4x / 8x / 12x / 16x, **16x max**) is sized to the **summed load** through
  that segment, rounded up to whole amps.
- **The source's own cable block carries its whole output (#347).** Every amp the source puts out
  passes through the block it feeds, whichever leg it then takes. Any other block has a parent
  segment carrying everything beyond it, and a block is built at the thickest cable touching it
  ("Cables and pipes as blocks" below), so it is never thinner than what passes through it. The
  source's block has no parent segment: when two legs leave it, or a machine taps it, no one
  segment carries the whole output, and a block built at the thickest of them burns. So every
  segment leaving the source's block is sized for the source's **whole output** on that cable, the
  whole amps every connection on it sums to; a leg's second block comes out thicker than its own
  load, which never burns. That is the root's own figure, per route. For a source on one net, as
  every source the adapter synthesizes is, it is the amperage the run tells the builder to feed
  that source (`system_io.power_amps_by_source`); that report sums a hand-built source's several
  nets, while each of their roots carries only its own. An output over 16 A is refused at the
  source, never split over two legs that each look legal. The adapter still packs a tier's sources
  to 16 A at nominal load, with no headroom for cable loss, so a group packed close to 16 A can be
  refused here (platline's EV group: 16.00 A nominal, 17 A at the source); giving that partition
  headroom is a follow-up. The validator holds the block to that output on its own arithmetic, at
  the thickest cable touching it.
- A segment needing **> 16x** must split into **parallel runs** or move to a **higher voltage
  tier** (more power per amp).
- **Hatches ride casing cells, and casings are interchangeable.** A GT multiblock's shell asks
  only that each cell hold *something* legal, so almost any casing may be swapped for an input bus,
  an output hatch, a maintenance hatch **or an energy hatch** - they all compete for the same pool
  of cells. The Industrial Coke Oven, for instance, has 17 such cells and every one of them accepts
  any of those kinds. That pool is the ceiling on a machine's total connections, and the solver
  rejects a layout that wires more onto a machine than its structure can host
  (`HATCH_CELLS_EXCEEDED`).
- **TEMPORARY: a machine needing more than 3 energy hatches is supplied at a higher tier instead.**
  Some upstream gtnh-factory-flow exports compute EU/t against a wrong recipe model, so a node can
  arrive drawing far more than its stated tier plausibly delivers. Rather than ring it with a dozen
  hatches, the adapter raises the tier it is *supplied* at until 3 hatches suffice - what a player
  would do. This does **not** re-derive the recipe (a real tier change re-overclocks, moving both
  `eut` and the parallel count, which only the exporter can do), so the EU/t figure is still
  upstream's. Remove it once gtnh-factory-flow #44 and #45 land.
- **The synthesized source is fed from outside.** A plan export has no power node, so the
  adapter invents them: one per voltage tier, or several when a tier's summed draw needs more
  than one cable run can carry. *How* each is powered is left to the builder. The
  layout reserves **the source's front face as the external feed entry**: placement pins that
  face flush on the region boundary (validator-enforced), internal cables use the other five
  faces, and the builder runs power in through the wall the front touches.
- **An Energy Acceptor takes its source's whole output (#336).** An ME network powered by the
  line's own EU (below, "What an ME network draws") gets an AE2 Energy Acceptor, a machine on the
  shared-amperage tree like any other. But an acceptor has **no amp cap and no voltage check**: it
  accepts every whole amp its source offers until its network's storage is full (80,000 AE per
  acceptor), so a network that starts empty pulls the source's full output, not its steady draw
  (spike 6.3, `GTPowerSink`), and a GT cable carrying more than it is rated for burns. So the
  cable from the source to an acceptor is sized for **the source's full output**: the whole amps
  every connection on that source's net sums to at its delivered voltage, which is the amperage
  the run tells the builder to feed that source (`system_io.power_amps_by_source`). The power
  router loads the acceptor with that output in place of its own draw, and sizes no segment for
  more than the source puts out, since a cable carries no more than its source emits: every
  segment between the source and an acceptor is sized for the source's whole output, and every
  other segment as before. An acceptor may tap the source's own cable block like any machine: that
  block is built for the whole output anyway (above, #347). The validator re-derives all of it on
  its own arithmetic. That holds
  only while the source puts out what the run says, so the run tells the builder to feed it no
  more (a battery buffer with more batteries than that would push its extra amps down the
  acceptor's cable). The other way AE2 allows, a 1 A limit in front of the acceptor, is a block
  this solver does not place.
- **A crop card is an input on the edge too, through its Crop Manager (#282).** An arodoid crop
  card's `machineCount` is the number of crop sticks planted, never a machine count, and the field
  is not part of the line. So the field is not simulated: a card harvested by a Crop Manager (the
  `crop-manager` handler) becomes one block, its tier's Crop Manager (`cropManagerTier`, "1" LV
  to "8" UV as the fork reads it; CropsNH's `MTECropManager`, GT machine ids 28001 up), putting
  out the whole card's yield. Like the power source, its front faces outside the build
  (`Machine.outside_front`) and sits flush on the region boundary, where the builder lays the
  field. It takes no inputs (the seed is planted, not piped) and draws nothing (its upkeep is not
  simulated either). An Industrial Farm card is a multiblock and keeps its one-machine-per-crop
  mapping for now.

## Cables and pipes as blocks

The sections above say how much flows and how thick the cable must be. This one says **which block
that is** and what it looks like - what the previewer draws and what a bill of materials counts.
All of it is read from the GT source and confirmed against an extractor run of pack 2.8.4.

**A route is drawn as a tier-representative STAND-IN, never as a build spec.** GT gives one voltage
tier many cable materials - at LV alone tin, lead, cobalt, zinc, soldering alloy and redstone alloy
all carry 32 V, differing in amperage and loss, not in what they can power. The router sizes a cable
by **gauge** (summed amperage) and never by material, and no dataset names one, so "the LV cable" is
a choice rather than a fact. `dataset/pipes.py` makes that choice once - the community-standard
ladder, the material a player actually builds at each tier - and every `RouteMaterial` it produces
carries `stand_in=True`. The previewer's legend footnotes it, and a `.schematic` exporter must
refuse to lower it into a real block. Counts, gauges and thicknesses are
real; only the material is representative. A cable rendered in Tin when the build needs Aluminium is
plausible, confident and wrong - the failure `docs/dataset-extraction/texture-resolution.md` calls
unrecoverable.

The ladder stops at **UV**, which is a fact and not an omission: above it GT ships no insulated
cable at all. UHV and beyond are carried by superconductor **bare wire**, which is lossless and a
different block family, so there is nothing to be representative of and the route keeps a bare bar.

**A cell incident to two gauges is built at the thicker one.** A shared trunk that splits carries
different summed amperage on either side of the split, so one cell can be incident to a 2x segment
and a 1x segment at once (the sand line does this at cell `(2,0,1)`). A cell is one block, the
fattest incident cable is the one that physically meets it, and under-sizing is what burns - so the
cell is the maximum over its incident segments. As a coloured bar that was harmless smoothing; as a
real block it is a build instruction, which is why it is written down here rather than left implicit
in a render template. The one block whose load no incident segment carries is the power source's
own, which carries its whole output, so the router sizes the segments leaving it for that output
(Power above, #347).

**Texture layers.** GT composites an ordered stack of `(sprite, RGBA multiply)`; `<SET>` below is
the material's texture set under `materialicons/`, and bare names are under `iconsets/`. Sprites are
**greyscale** - measured, not assumed - so the material's identity lives entirely in the multiplier,
and a dumper that drops it renders the whole pack as grey noodles:

| block | face | layers, bottom to top |
|---|---|---|
| insulated cable | an open end | `<SET>/wire` x material RGBA, then `INSULATION_<size>` x tint |
| insulated cable | not connected | `INSULATION_FULL` x `CABLE_INSULATION`, alone |
| bare wire | any | `<SET>/wire` x `getModulation(colorIndex, material RGBA)` |
| fluid / item pipe | barrel | `pipeSide` from the material's texture set |
| fluid / item pipe | the bore | the size sprite (`pipeTiny`..`pipeHuge`) |

`Dyes.CABLE_INSULATION` is a flat **64/64/64** darkening that cannot be skipped (confirmed to hold
on a dedicated server, where it is read from a client-config default). Painting a cable recolours
only its insulation; painting a bare wire recolours the conductor.

**Geometry.** For a pipe of thickness `t`: `pipeMin = (1 - t) / 2`, `pipeMax = (1 + t) / 2`. The
shape is a uniform axis-aligned cross - a core cube plus one arm per **connected** side running to
the block edge, with the **same cross-section as the core**; there is no fatter node. A straight run
collapses to one full-length box, and `t >= 1` renders as a full cube. Thickness in blocks:

| family | thicknesses |
|---|---|
| insulated cable, 1x/2x/4x/8x/12x/16x | 0.25 / 0.375 / 0.5 / 0.625 / 0.75 / 0.875 |
| bare wire, 1x..16x | 0.125 / 0.25 / 0.375 / 0.5 / 0.625 / 0.75 |
| fluid pipe, tiny..huge | 0.25 / 0.375 / 0.5 / 0.75 / 0.875 (quadruple, nonuple: full cubes) |
| item pipe, tiny..large | 0.25 / 0.375 / 0.5 / 0.75 (huge: a full cube; restrictive huge 0.875) |

**Which sides connect is player state, not a block property.** `mConnections` is set by the player
with a wire cutter, soldering iron or wrench (the same reason a pipe never auto-connects, which is
why pipe adjacency is not a silent-tap hazard for routing). It cannot be observed outside a world
and should not be: the extractor **enumerates** the two looks GT's own inventory render uses, and
the previewer synthesises the cross from the connection mask it derives from the route itself.

**The mask is normalised before textures, but not before geometry** - the one asymmetry in the
render, and it is deliberate. `BaseMetaPipeEntity.getTextureUncovered` folds a lone connection onto
its whole axis (DOWN alone is textured DOWN|UP, and so on) and then calls a face an open end when
`connections == 0 || (connections & side) != 0`, while `MetaPipeEntity.renderInWorld` switches its
boxes on the **raw** mask. So:

- a **stub** (one connection) is a core plus one arm, yet shows its conductor at the *free* end as
  well - a cut cable hanging off a machine, not one capped in insulation;
- a cable with **no** connections is open on all six faces: the block you hold in your hand;
- a run of two opposite connections is one box through the block, open at both faces.

**Sprites are sampled by position, not stretched.** `RenderBlocks.renderFaceXPos` and its five
siblings take each face's UVs from the current render bounds - `getInterpolatedU(renderMinZ * 16)`,
`getInterpolatedV(16 - renderMaxY * 16)` - so a sub-block box shows the part of the sprite belonging
at its place inside the block, and a pipe's texture runs unbroken from core into arm. The axis pairs
are (u, v) = (x, z) for the horizontal faces and (x or z, y) for the vertical ones. A renderer that
maps 0..1 across every face instead draws the whole sprite squeezed onto each box, breaking the
pattern at every joint.

**The tint is a plain multiply.** `SBRContextBase.setupColor` converts the layer's RGBA to
`channel / 255` with the alpha slot dropped entirely, then scales by a per-face constant
`LIGHTNESS = {DOWN 0.5, UP 1.0, NORTH 0.8, SOUTH 0.8, WEST 0.6, EAST 0.6}` - vanilla's directional
shading. There is no normalisation or brightness compensation anywhere in the path, which is what
makes `CABLE_INSULATION`'s 64/64/64 the dark casing it looks like in game.

## Boundary storages (the adapter closes the line)

A plan export names recipes and flows but not the containers at the line's edge, so the adapter
synthesizes them - the same line-closing move as the per-tier power source above:

- **Inputs** map to a boundary **Super Chest** (items) / **Super Tank** (fluids) the builder
  fills; nothing in the line feeds it, so it only *sources*.
- **Outputs** are closed the same way: for every machine OUTPUT port that no net consumes, the
  adapter adds a **Super Chest/Tank plus a net** wired to it at the port's recorded rate, so the
  finished product is collected instead of exiting into thin air (the sand line auto-outputs its
  sand straight into this collection chest, still zero pipes). Such a storage only *sinks*.

These boundary storages are unpowered blocks that accept I/O covers on their faces (so covers ride
storage faces, never pipes); `system_io` surfaces the only-*source* ones as the line's inputs and
the only-*sink* ones as its outputs.

## ME networks (AE2)

**ME is chosen per net, by the user** (#332). `gtnh-solve plan.json --list-nets` prints every
item and fluid net (its ends, its rate, and the ME device each end would get); the user (or
gtnh-solver-site's picker) chooses which nets ride which ME network, attached to their main
network or a subnet of its own colour, and `--me-plan FILE` reads that back. `--me items` /
`--me fluids` is shorthand for every net of that kind on one attached network. Default is to build
everything physically.

A net on ME is built as AE2 instead of a pipe (#335). The adapter (`adapter/me_build.py`) gives
each machine port on it an ME device and each network the blocks it needs of its own (dense attach
stubs, links, a controller over 8 devices, an Energy Acceptor when the line's own EU powers it,
#336), and drops the boundary chests an attached or link network's storage replaces. Placement charges each device a face and a cell beside it, like a
pipe's terminal, and pulls each network's machines together, stub included. The solver lays each
network's cable after the pipes and before power (the router below); the CLI then says, per
network, what its storage must supply, what lands there, how many of the main network's channels
it spends, and what it draws (`system_io`; "What an ME network draws" below). The preview draws
every ME block (#338, below), and the schematic export writes them, its cable buses for a named
world only (#339, below).

A single block's outputs can be split: one product on ME and the rest piped. Its auto-output face
still ejects every item, so the piped ones are merged and sorted by Item Filters as before (#249)
and the one on ME stays out of the trunk: a GT pipe takes from a face only the items some filter on
it accepts, which leaves the rest in the machine for its ME device.

Power is not an ME network, but `--me power` leaves it to the builder: there is **no power
source**, since the adapter drops the per-tier sources and their nets, and a source no cable
reaches would be placed and exported connected to nothing (#225). Each powered machine keeps its
energy ports, so the preview still states the draw per tier for whatever delivers it.

Building the ME side for real is epic #329. Its rules are read from the pack's own AE2,
AE2FluidCraft and GT source and cited, file and line, in
[`spikes/329-me-ae2.md`](spikes/329-me-ae2.md); `dataset/me.py` holds them as data (#331): what
each cable carries (8 channels, 32 on dense cable), what each bus moves with its cards, GT's own ME
hatches by mID, AE2's power figures, and which device serves a machine's port. In short:

- **A port's ME device** is the first that keeps up, one and then two: on a multiblock, GT's ME
  hatch where the hatch policy and the line's tier allow it (an ME output bus flushes 39 items/t),
  else a normal hatch with an AE2 part on the cable in front of it (a normal output bus pushes
  `8 x (tier + 1)^2` items/t into an interface); on a single block, an export bus on an input face,
  an interface on its auto-output face, or an import bus.
- **An interface takes a GT push straight into the network**, so its rate is the pusher's. A single
  block pushes fluid 1000 mB at a time, on each completion and every 20 ticks; a faster fluid output
  is drained by a fluid import bus instead.
- **Cards**: the fewest Acceleration Cards that keep up, and Hyper-Acceleration Cards only on a line
  that has reached LuV, which their recipe needs.
- **A bus set to an item at any damage takes a Fuzzy Card** (#353). A plan names such an item with
  Forge's wildcard meta, `registry@32767` (a coke oven's `minecraft:log@32767`). AE2 matches a
  bus's filter or partition exactly unless a Fuzzy Card is fitted, and no real stack is at damage
  32767, so without the card the bus moves nothing. With it, set to ignore damage (`IGNORE_ALL`, a
  fresh bus's own setting), AE2 matches through the ore dictionary: every log the network holds,
  other mods' included, as a recipe taking any log does (spike 4.2). Every item import, export and
  storage bus whose config names such an item gets one, and keeps the wildcard as its filter. The
  card takes one of the bus's four slots, so its speed cards get three, and a port faster than
  three move is split across two buses, each with its own card. Fluid buses take none: a fluid has
  no damage.

### What a valid ME build is (#333)

The validator's ME gate (`validator/me.py`) holds a layout to these rules, each read from AE2's
source (spike sections in brackets). The router builds to them; the validator re-derives every one
from the blocks the layout places, never from the router's own bookkeeping.

**The blocks.** A layout lists, per ME network, its cable blocks (each a kind: smart, dense, ...)
and its devices (an AE2 part on one side of a cable block, or a GT ME hatch). Its controller,
acceptor, attach stub and link are machines (`Machine.me_role`); the stub and the link are cable
blocks too, a dense and a smart one, at their own cell.

1. **Every cable block stands on free ground**: inside the region, on no machine (but its own stub
   or link), no pipe or cable, no reserved cell, and no other network's cable.
2. **Every device is where its job is.** A part sits on a cable block that takes parts (not dense,
   spike 3), on a side no other part takes, facing the block it works on: a single block's face
   its port may use (never its front), or a multiblock's normal hatch whose front faces back at the
   part (the hatch kind the endpoint names). A GT ME hatch takes a hatch slot of its parent kind,
   front out (spike 5.2). A link's storage buses sit on its front, facing out. Each of a machine's
   ME endpoints is built exactly once, as the device it specifies (kind, mID, cards, config).
3. **A single block's interfaces share one face**, its auto-output face, and an item interface
   never serves a fluid: a single block with both on one network takes them through one Dual
   Interface (spike 4.6).
4. **Every device keeps up** with its share of its ports' rate: a bus by its cards (spike 4.2,
   4.3), a GT ME output hatch by its flush (spike 5.3), an interface in front of a normal output bus
   by that bus's push (spike 4.8); a bus takes four cards at most.

**Which blocks join which network** is AE's to decide, not the layout's (spike 3): a cable block
joins every compatible neighbour (Fluix joins every colour) on a side no part takes; a part joins
only its own cable; a controller and an acceptor join on all six sides; a GT ME hatch only through
its front. The validator builds that graph from the blocks, and:

5. **Each network is one piece, and only itself.** A connected group of AE blocks holds one
   network's blocks (two touching are merged in game), and a network's blocks are one group, except
   an attached network, whose groups may each reach the main network through a stub of their own.

**Channels**, by AE's own pathing (spike 2):

6. **A network gets its channels from a source**: an attached network from the main network through
   its stubs (a dense cable each, so 32 a stub), a subnet from a controller, whose every neighbour
   is a root carrying its own capacity. An attached network spends at most its channel budget.
7. **A subnet with no controller is ad hoc**: 8 channel devices at most, on any cable; a 9th takes
   every channel away.
8. **No block carries more channels than it can**: every channel device beyond a cable block (or a
   block device, or a stub) on AE's path to it counts against that block's capacity, 8 or 32
   (dense). On a tree that count is exact and does not depend on the order AE visits blocks in
   (spike 2.4); where blocks form a cycle, the validator counts every device AE could route through
   a block, which is sound for any order AE picks (spike 2.5).

**Power**, by what the laid network draws (spike 6; "What an ME network draws" below):

9. **A network on an Energy Acceptor gets what it draws.** Its acceptor is placed, reached by a
   power cable, and rated (its `eut`) for at least the network's EU/t, which the validator computes
   on its own from the blocks: the devices it places, and the channel term from its own pathing,
   never the router's `me_channels`. Under `--me power` neither the cable nor the rating is
   checked: the builder brings every machine's power, the acceptor's included. And its store must
   hold one flush of each GT ME output bus or hatch at once, 16,000 AE for an Output Bus (ME),
   where a grid with no acceptor, controller or cell holds only 1,000 (spike 5.3, 6.3); an
   acceptor or a controller holds 80,000. An externally powered network is not judged: it draws on
   the main network or through a quartz fiber, whose store the layout cannot see, and the run
   tells the builder what that store must hold (below). A rating under the laid figure names the
   acceptor: another placement, laying less cable, can meet it, so the solver ranks that layout as
   one that left the acceptor's power net unrouted rather than as a bug.

### How the router lays an ME network (#334)

`router/me.py` builds to those rules by laying each network as a **forest of trees**, one root per
place its channels enter: each attach stub of an attached network, each free cell beside a
subnet's controller, or, ad hoc, one tree seeded at the first device's dock (at its link, when it
has none). Each device is then grown on as a leaf, the way the power trunk grows: a **tap** puts
its part on a cable already laid beside the machine (two machines facing one cell share it, a part
on either side), else a shortest **leg** runs to a free dock cell from any cable with a channel to
spare. A GT ME hatch takes its casing cell with its front on a cable; a link joins as a leaf with
its storage bus on its front; an acceptor is touched by exactly one cable (or the controller). A
GT pipe connects only where the player wires it, but an AE cable joins every neighbour it can, so
every new cable keeps a **halo**: it touches no AE block of another network, and of its own only
the cell it grows from, so the graph AE builds is exactly the tree laid and rule 8 is exact. The
halo is kept between every two networks, whatever their colours. Channels are counted on the tree
as AE does: a cable holding a part carries at most 8 and is smart, any other carries up to 32 and
is dense when it carries more than 8, and growth refuses a tap or a leg that would overfill one; a
leaf that finds only full cable is retried with the cell that ran full kept bare. What no cable can
fix is refused before any is laid: an ad-hoc subnet over 8 devices (`me_adhoc`), an attached
network over its budget (`me_channel_budget`) or over 32 a stub (`me_channels`), and infrastructure
that contradicts its mode, touches another network's, or puts controllers where AE would not run
them as one cluster (`me_infrastructure`).

The solve lays every network after the pipes and before power (`solver/core.py`, #335), and the
hatch pass turns each multiblock endpoint's terminal into the hatch of the slot kind its endpoint
names, a GT ME hatch or the normal hatch an AE2 part faces. Those are kept apart by casing cell,
not by port, since a port too fast for one device has two. Rule 3 needs no pin in the solve: the
adapter gives a single block an interface only when every output of the block rides that one
network, so no pipe or auto-output competes for the face, and `output_faces` reads the face the
interface works on as the block's auto-output face. The router still takes a pin per endpoint
(`endpoint_faces`) for a caller that needs one.

### What an ME network draws (#336)

An AE2 network draws AE every tick, and the pack converts 1 EU into 2 AE (spike 6.1;
`dataset/me.py`):

    AE/t = (idle + channelsByBlocks / 128 + items moved + 1000 mB charges) x 10,   EU/t = AE/t / 2

- **idle**: 1 AE/t for every device (a bus, an interface, a storage bus, a GT ME hatch) and 3 for
  every controller block; cables and an acceptor draw none.
- **the channel term** is not `channels / 128`: AE sums the channels through every node and through
  every connection its pathing visits (spike 6.2). On a tree each node hangs from one connection,
  which carries the node's own count, so that is twice the channels through every node (88 on the
  spike 2.7 trace). Ad hoc it is every node times the channels in use.
- **moves**: one charge for every item a device inserts or extracts, so a net between two machines
  on one network pays an insert and an extract per item, one the network supplies an extract only,
  and one it stores an insert only. A fluid pays one charge for every started 1000 mB of each
  operation: a fluid bus operates every 5 ticks, an ME output hatch at each flush, anything else
  is charged as if it moved every tick; a Dual Interface takes a pushed fluid free (spike 4.6).

**How a network is powered** is the user's choice (`MENetworkSpec.power`). `external` leaves it to
the builder: an attached network adds its draw to the main network's, and a subnet is fed through
a quartz fiber from a network with power. `acceptor` gives a subnet an Energy Acceptor on the
line's own EU: the adapter adds one per such network, at the line's highest powered tier (an
acceptor takes any voltage, and the highest carries its draw in the fewest amps on the thinnest
cable; never below LV), before the power synthesis, so it joins that tier's shared-amperage tree
and is cabled for its source's whole output (above, "Power"). It is also a leaf of its network's
tree, touched by one cable. **An attached network is refused an acceptor** (`MEPlanError`, exit
2): it is part of the player's main network, which their base already powers, and an acceptor
there would power the whole base and keep filling its storage from this line's supply.

**The acceptor is rated before any cable is laid**, since the power synthesis sizes its draw then.
Its `eut` is an upper bound on what the network draws: its devices idle and moving what their
ports state, a controller's idle, and the channel term of a network in which every device's
channel crosses as many cable blocks as the line's region is wide, high and deep together (its
Manhattan diameter as the adapter first sizes it, never fewer than 16; `estimated_channel_load`).
The router lays each device's cable as a shortest path from the cable already laid, so a channel
goes further only where the halo forces a detour, and the validator holds the rating to what the
laid network really draws, so that case fails the layout rather than passing it. The rating is
not re-made from the laid network: the problem the caller holds would then disagree with the
layout it was solved for. A generous rating costs little, since the highest tier carries it in
few amps.

**What a run reports is the laid figure**, from the cable the layout lays (each cable's channels,
each device's one; `system_io.laid_me_ae_per_tick`). The CLI says it per network the way the
builder supplies it: an attached network "adds X AE/t (Y EU/t) to your main network's power
draw", an external subnet is to be fed it "through a quartz fiber", and an acceptor network's
note gives the acceptor's draw and rating and tells the builder to feed its source no more than
the amps its cable is sized for. An external subnet with no controller of its own, whose GT ME
output flushes more at once than AE's 1,000 AE default buffer, is told the network powering it
must store that much (`external_store_ae`); an attached network draws on the main network's
controller and a subnet with a controller on its own, each holding 80,000 AE. For an attached
network the figure is what it adds, short of the main network's own cable between its controller
and the stub, which the layout cannot see. The layout carries the same per network in
`LayoutMetrics.me` (docs/IR.md), since gtnh-solver-site reads only the layout: the draw, the
store, and for an acceptor its rating, its source and the most amps to feed that source.

### How the preview draws an ME network (#338)

**AE2 decides what a cable bus looks like from what touches it**, in its renderer, so
`previewer/me_blocks.py` makes the same decisions in Python from AE2's own numbers
(`data/ae2/<AE2 version>/render.json`, spike section 8), and the viewer only draws the result.
Per cable block: the sides AE2 joins (the connection rules of the validator's graph, restated
there, plus an attach stub's front, where the main network enters), what each side carries, and
the boxes. A cable with exactly two opposite connections is one straight bar when AE2's rule for
its kind allows it (no part on a smart cable, dense on both sides of a dense one); any other is a
core with an arm per connection, a plug where the arm meets a device block (a controller, an
acceptor, a GT ME hatch's front, which reports a smart cable), and per part the arm out to it and
the part's own boxes. AE2 itself overlaps a plug with its arm and runs a dense arm into the core;
those are cut back where AE2 hides them anyway, so no two boxes of a cell share a volume (a property
test). Nor is there a hole: a face AE2 leaves open (an arm's ends, a glass bar's) always meets a box
at least as wide beyond it, and a covered, smart or dense bar, wider than the arm it meets, draws
its ends, as AE2 does.

**A side's lights show what that connection carries.** A layout states one channel count per cable
(`MECableCell.me_channels`, the router's tree count); a connection between two cables carries the
count of the one farther from the root, the smaller on a tree, and a part or a GT ME hatch takes
one. AE2 shows at most 8 a side, and in fours where both nodes have its dense capacity: dense
cables, and a dense cable meeting a controller (`TileController.java:55`). The network is drawn
powered: what powers it is reported, not built (`--me power`, an acceptor). Render data that will
not load costs only the look: each ME block is drawn as a plain cube in a flat colour, with a
warning naming `render.json`, so the schematic export, which builds the same scene, never fails on
it.

**GT's ME hatches are GT blocks**, drawn by the texture pass like any hatch but found by mID
(`TextureManifest.me_hatch`), since two pairs share a class; an ME hatch the manifest lacks keeps
its casing rather than drawing as a normal hatch of its kind.

**AE2's art is CC BY-NC-SA 3.0** (spike 9.2), unlike GT's LGPL sprites, and AE2FluidCraft's is
treated the same. Both are read from the pinned jars at preview time and embedded only in the page,
and a page that embeds any of it, on an ME network, a GT block that wears AE2 art or an AE2 or FC
item's icon, credits what it embeds with the licence link on its HUD: it may be shared only for
non-commercial purposes (`NOTICE`). A failing AE2 or FC jar, or one whose members do not decode,
costs only the ME art, never the GT textures.

### How the export writes an ME network (#339)

`schematic/ae.py` writes every ME block as AE2 itself saves it, read off the maintainer's in-game
golden (`tests/golden/schematic/ae2-golden-*.schematic`, spike 7.5) and held to it tag for tag:

- **A controller or an acceptor** is its AE2 block (Data 0; a controller's 1 is AE's live
  "online"), with `orientation_forward` its placed front and `orientation_up` `UP`, as AE2 orients
  a block placed by hand; a controller is painted its network's colour (`paintedColor`), which is
  what joins it to that network alone. Neither needs an item id, so both are always written.
- **A GT ME hatch** is a GT block in its multiblock's casing, written like any hatch but by its own
  mID (the texture pass finds it with `TextureManifest.me_hatch`), facing its cable; on a coloured
  subnet it is painted that colour, GT's `mColor` being the dye plus one (AE colour `c` is
  `mColor` `16 - c`, spike 5.2). One the manifest cannot name is refused, since the casing left in
  its place would form a multiblock without it.
- **A cable cell** is a `BlockCableBus` whose tile entity holds the cable (`def:6`, `ItemMultiPart`
  at kind plus colour) and each part on its side (`def:N` / `extra:N`): the bus's default settings,
  its cards one a slot, and its filter or partition (an item by the world's id and damage, a fluid
  by name). A cable bus is written **only for a named world** (`--world`, the save folder or its
  `level.dat`, as for covers): every item in it is numbered by the world's FML table, which
  Schematica never remaps, so without one each cable cell is left out and counted. Either way
  Schematica's printer places none of it (Platform, above): the ghost carries the network, and the
  builder places it by hand. `--inspect-schematic FILE --world <save>` names the items in a file's
  cable buses by that world's table; without it only AE2's own cables and buses are named.
- **The ME Interface part and FC's Dual Interface part are left off their cable**, and listed by
  cell and side: the golden has neither, so their NBT is unverified (spike 11).
- **A filter slot for an item at any damage is left unset** (a resource `registry@32767`, such as a
  coke oven's `minecraft:log@32767`), and listed with its bus. AE2 matches 32767 as "any damage"
  only in its fuzzy lookup (`ItemList.findFuzzy`), which an export bus takes only with a Fuzzy Card
  (`PartBaseExportBus.java:120`); written as is, the bus would move nothing. The builder fits a
  Fuzzy Card or sets the slot to the one item they feed.
- A part on a cell the layout lays no cable on (the validator refuses that) is counted and listed
  rather than dropped.

One `SchematicWarning` says what was left out and why. `--inspect-schematic --world` checks the
world first: a table that puts `ItemMultiPart` at another id than the file's cables use is from
another world, so it is set aside with a warning and the items stay numbers.

## Multiblocks

Represented as a **bounding box + controller-face and hatch/bus-face metadata** (multiblocks
do I/O through hatches/buses on casing faces). Full internal StructureLib materialization is
deferred to the export milestone. Fallback if metadata is too costly to author early:
single-block-machines-only for the first solver, multiblocks added via round-trip import.

### A multiblock's tiered blocks (#312)

Some parts of a GT multiblock are named by a StructureLib **channel** (`GTStructureChannels`)
rather than by one block: the part is built from any block on a tier ladder, and the tier changes
what the machine does. The ExxonMobil Chemical Plant (`MTEChemicalPlant`, controller
`gregtech:gt.blockmachines@998`) has four, and each decides whether, or how fast, it runs (GT
5.09.54.133):

| Channel | Blocks GT accepts | What GT does with the tier |
|---|---|---|
| `casing` (solid casing) | 8 tiers: bronze, steel, aluminium, stainless steel, titanium, tungstensteel, laurenium, botmium (`GregtechAlgaeContent` lines 37-52) | a recipe runs only if its special value is at or below the tier (`validateRecipe`, line 584); the whole build is one tier, and the controller and every hatch take its texture (`updateHatchTexture`) |
| `pipe` (pipe casing) | `gt.blockcasings2` metas 12-15, bronze to tungstensteel | parallels = 2 x (meta - 11) (`getMaxParallelRecipes`, line 492) |
| `coil` | `gt.blockcasings5`, 14 coils | speed bonus 2 / (1 + coil tier), Cupronickel tier 0 (line 634) |
| `machine_casing` | `gt.blockcasings` metas 0-9, meta = tier ULV..UHV | refuses to form if the meta is below the highest hatch tier, unless UHV (`checkMachine`, lines 398-403) |

The adapter chooses each from the plan (`adapter/structure_blocks.py`) onto
`Machine.structure_blocks`:

- **Solid casing:** the cheapest whose tier meets the highest special value the node's recipes
  state. arodoid states it three ways (`specialValue`, `metadata.specialValue`, an NEI "Special
  value: 4" line); MrBruh's fork only in the NEI line. A casing the plan names
  (`machineConfigTiers.solidCasing`, written by gtnh-shadow-convert) is kept if it meets that, and
  raised with a warning if not. With nothing stated it is bronze, with a warning.
- **Pipe casing:** the node's `pipeCasing`, else the control's default, else bronze. gtnh-factory-flow
  also offers PTFE and PBI, which the plant's `check()` refuses; they build as tungstensteel, which
  the planner rates the same.
- **Coil, on every multiblock whose coil channel offers a choice:** `coilTier`, else
  `machineConfigTiers.heatingCoil`, else the control's default. A channel of one block is a fixed
  coil and is left alone. A plant with no coil stated warns: the dump's Cupronickel runs it at half
  speed.
- **Machine casing:** the casing of the tier the plant is supplied at, read after the power
  synthesis, which can raise it (`adapter.power._supply_tier`). Every hatch the export places is
  that tier or below, so the plant forms.

**Two tiers are valid in game and absent from every dump.** GT++'s `addTieredBlock`
(`GTPPMultiBlockBase` lines 699-748) places meta `minMeta + stack` for a stack of at least 1, but
`check()` accepts from `minMeta`, so the extractor, which records what `construct` places, never
sees the lowest tier: Bronze pipe casing (meta 12) and the ULV machine casing (meta 0).
`dataset.channel_blocks` adds them from that rule, for the plant only, and a dump listing a block
outside the rule's set wins with a warning (#315 will record what `check()` accepts instead).

**A cell belongs to a channel by membership.** The dump tags no cell with its channel, so a cell is
in a channel when its block is one the channel accepts. Never by a channel's first entry (each
Industrial Coke Oven form built from stack N carries coil N) and never by `channel_value` (the
hand-written EBF fixture numbers its coils from 0). The previewer swaps every cell of a chosen
channel in the form itself, all at once, and only when exactly one of the channel's blocks is in
the form; the hatches then wear the swapped casing and the controller is drawn over it, as GT draws
both. The `.schematic` export draws through the same expansion.

Not modelled yet: the plant's 70-casing minimum, which caps it at 22 hatches (#317); a validator
check of both casing rules (#316); an EBF-family coil's heat against the recipe's (#318).

## Cell↔block realizability (don't let the abstraction lie)

Placement/routing run on a coarse cell grid; block-accuracy is materialized only at export. A
cell boundary has finite physical room, so the router enforces a **margin → max-channels-per-
edge** cap and a **cell→block realizability** check fed back into search. Without it, the
solver can certify layouts that don't physically fit.
