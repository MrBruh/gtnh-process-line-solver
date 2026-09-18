# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **A display name is no longer assumed unique across the multiblock dump, because in GTNH 2.9 it is
  not.** 2.9 shares 52 display names between two controllers each: GT migrated the GT++ machines into
  `gregtech.*` and kept every original registered as `...Legacy`, so "Industrial Centrifuge" is both
  `MTEIndustrialCentrifuge` (meta 15512, 5x5x5) and `MTEIndustrialCentrifugeLegacy` (meta 790,
  3x3x3). `load_physical_dataset` raised on the first collision, which made the whole pack
  unloadable.

  A collision is now a fact about the pack rather than a corrupt dump. When exactly one of the
  colliding records is not `...Legacy` it takes the name, since a plan naming that machine means the
  one the name refers to now; that is read off `source_class`, a declared fact of the dump rather than
  a hand-maintained list, and it settles **51 of the 52**. The one that genuinely cannot be decided
  (2.9's "Drone Centre" is two controllers of the same class) has its name withheld and recorded in
  `PhysicalDataset.ambiguous`, so a lookup abstains instead of returning the wrong one of two real
  machines. Every record stays in the new `PhysicalDataset.records` and addressable by `block_key`.

  Two files claiming the same **block_key** is still an error: that is one controller dumped twice.
### Added
- **A node standing for several parallel machines is mapped instead of rejected (#76, adapter half).**
  `machineCount > 1` used to fail the load outright, which excluded most throughput-scaled
  factories. It now expands into one `Machine` per physical machine, with `#1`-suffixed ids; a
  single-machine node keeps its **bare** id, so every existing layout, golden file and preview is
  unchanged.

  **No IR change was needed**, contrary to what the issue anticipated: `Net.endpoints` is already an
  unbounded list, so N producers and M consumers share one net, the router chains them and the
  validator already permits several producers. The shared bus is also what a real GT line is.

  What the adapter had to get right is which figures are per machine and which are per group, a
  distinction that was invisible while `machineCount` was forced to 1. `Port.rate` and `Machine.eut`
  stay **per machine** (a port belongs to one block; the power synthesis gives each machine its own
  hatches and sums them). `Net.throughput` is **per group**, so a pipe is sized for what all N
  machines move. A `resolved.totalEut`, being a group total, is divided back down before use, or each
  of N machines would have been handed the whole group's draw. An unconsumed output collects into
  **one** buffer per node rather than one per machine.

  `examples/gtnh-parallel-sand.json` now solves to a **VALID** layout, which is the rest of #76
  and is described under Fixed below.

### Fixed
- **A parallel line lays out: nine machines at three instances each reach a VALID layout (#76).**
  `examples/gtnh-parallel-sand.json` used to stop at `partial_invalid`, reporting
  `face_reachability` on whichever net lost the race for a machine's last free face. The line was
  short of room in **two** independent ways, and fixing either alone still left it partial, which
  is why three earlier diagnoses each looked right and were not.

  **The placement could not host its own connections.** The placer packed the nine machines into a
  solid row, so every interior machine had its east and west faces against a neighbour and its
  north and down faces against the region wall: **two free cells for three connections** (item in,
  item out, power in). No routing order can rescue that. It is priced now by a face-shortfall term
  in the placement cost, and decided exactly - before any routing - by a new
  `placement.feasibility` gate that answers whether every connection can be given a cell of its
  own. That is a bipartite matching, too slow per annealing step and cheap once per candidate
  layout, so the cheap term steers the anneal and the gate rules on the result, raising the term's
  weight when it keeps rejecting. The gate is **advisory by construction**: if it turns down every
  attempt the solver routes anyway, so it can never turn a line the routers would have solved into
  a hard failure.

  **And the router stranded nets it had room for.** Docking is greedy and net by net, so an early
  net could take the one cell a later net needed while having somewhere else to go itself. The
  assignment existed; the order missed it. A net that fails to dock now asks the holders of the
  cells it wanted to move aside, recursively (an augmenting path), and fails only if none can.
  This runs **only after a net has already failed**, so a line that docks cleanly today docks
  exactly as it did before.

  Power also no longer loses a face by accident: one dock cell per power endpoint is held back
  before the pipes are laid, because power routes last against every pipe cell as an obstacle.

  Two rules the gate has to respect, both learned by getting them wrong first: a net covered by a
  free **auto-output** needs no dock cell at all (charging the sand line's zero-pipe chain for six
  of them declared its hand-built 3x2x2 crowded and drove the search to a box half again as
  large), and a **power terminal may share** its cell with another machine's, because GT feeds
  every wired face next to a cable block and `route_power` taps rather than laying a new leg.

  The shipped lines are unaffected: `gtnh-sand.json` and `gtnh-nitrobenzene.json` still solve to
  VALID layouts within the same hand-built compactness and cable targets.
- **Multiblocks resolve to their real footprint instead of silently becoming one block.** The
  exporter names a machine by its localized **recipe map** ("Blast Furnace", "Macerator"); the
  structure dump is keyed by the **controller's** own display name ("Electric Blast Furnace",
  "Industrial Maceration Stack"). For a GT++ machine the two differ, so joining on the recipe-map
  name resolved only 5 of 9 nodes on one real plan and 34 of 53 on another, dropping the rest to the
  1x1x1 default: a real multiblock modelled as a single block, which is the "coarse-cell abstraction
  can lie" failure arriving quietly.

  The join now tries, in order, the controller-block id (`registry@meta`, exact and unbeatable), then
  the node's effective `machineHandlers` entry's `label`, which *is* the controller name, then the
  recipe-map name, each also through a three-entry alias table for the machines whose handler list is
  empty. That takes those two plans to **9 of 9** and **51 of 53**. The footprint and the hatch
  ceiling now come from the one resolved record rather than two separate lookups, so they cannot
  describe different built forms of the same machine.

  Additive for the committed fixtures: they carry `machineBlock`, so the exact identity already
  resolved every one of their machines and the name ladder adds nothing.

- **A census miss is read according to what the machine is.** A census dump enumerates every
  multiblock controller, so a miss is a positive fact, but the fact depends on `handler.kind` and the
  two readings are opposites. `kind: "single"` (or no handler, which is every MrBruh-fork plan) means
  the machine is basic, so GT's `maxAmperesIn` ceiling applies and the under-supply check can run.
  `kind: "multiblock"` means the **dump** is incomplete, not that the machine is basic: claiming it
  would state the wrong intake formula and reserve 1x1x1 for a real structure, so it is reported and
  left unclaimed. Conflating the two is how an alias table swallows a genuine extraction gap.

  The alias table is a stopgap and says so: every entry is a wrong answer waiting for a pack release
  to move a display name. The durable fix is the controller-block id.
- **Power and throughput follow the figures a machine actually runs at, not the recipe's base
  values.** `recipe.eut` and `recipe.durationTicks` are the values at the recipe's *minimum* tier; a
  machine run above it draws 4x and runs 2x faster per step. The adapter read the base values, so a
  whole plan came out **6.1x** under-powered and a single machine up to **256x** (an EV machine on an
  LV recipe is 4³), with every pipe sized for a matching fraction of its flow. Under-sized cable is
  the one failure a builder cannot see in the preview.

  Both forks already ship the real per-tier figures in `recipes[].runtimeCalculation`, computed
  against GT's own `OverclockCalculator`, so consuming them needs **no producer branch**. The draw
  now resolves best-source-first: a `resolved` block, then the matched runtime variant, then the base
  value. `resolved` stays on top because it is the exporter's own balancer and accounts for machine
  count and parallelism a variant cannot see; the two disagree by 24.5x on one real node with nothing
  in the export to arbitrate, which is why this is a ladder and not a single source.

  Selection matches on the variant's **fields**, never on its id: real ids carry suffixes beyond the
  tier and coil (`tier-ev-perfect-oc`), so composing an id silently misses about half the nodes of a
  real plan. A coil-bearing machine is narrowed by the node's coil, and a node that leaves the coil
  unstated against coil-keyed variants stays **unmatched** rather than guessing, since every coil is a
  different heat bonus and so a different EU/t.

  Verified additive: all three committed fixtures run at their recipe's own tier, so their EU/t,
  durations and layouts are untouched.

- **`_supply_tier` no longer re-tiers an arodoid plan.** That workaround absorbs an implausible
  draw from the MrBruh fork's recipe model; pointed at figures from GT's own calculator it would move
  machines that were already right. Disabled only for the producer positively known not to need it,
  so an undetermined plan keeps the defensive behaviour (it changes only the voltage supplied, never
  the stated draw).

- **A machine's unmodelled parallelism is reported.** A GT++ multiblock can run parallel batches set
  by a machine-configuration control, and that multiplier appears in neither `node.parallel` (always
  1 on every plan seen) nor any runtime variant (all of which report `parallel: 1`). It is **not**
  composed into the draw: doing so would re-derive the exporter's machine model here, and these
  multipliers are fractional in practice (1.5, 2.5, 3.5), so they are throughput factors rather than
  batch counts and several GT machines carry a parallel EU discount. An `AdapterWarning` names the
  machine and the factor, keeping the error visible and one-directional.

  The provenance warning added above is correspondingly narrowed: a node covered by a matched variant
  is no longer a fallback, so a plan whose figures are now right stays silent.
- **`nodes[].recipeInputOverrides` is applied, so a wildcard recipe input no longer fails the
  load.** A GT recipe can accept any metadata of an item (`minecraft:log@32767`, Forge's
  `OreDictionary.WILDCARD_VALUE`), and the exporter records which one the player actually feeds it.
  The adapter built ports from `recipe.inputs[].id` only, so the edge named `minecraft:log@1`, the
  machine had no such port, and the load died on `references unknown port`. Both shipped MrBruh
  fixtures carry overrides and only escaped this because theirs resolve to the id the recipe already
  names.

  **Only a narrowing is applied.** Real plans also carry overrides that name an entirely different
  resource at that index (`oxygen` to `water`, `ammonia` to `hydrochloricacid_gt5u`), always one the
  recipe already lists at the *next* index. Applying those drops a required input and duplicates
  another, silently shrinking the port set, and nothing in the export distinguishes a stale plan
  from a deliberate swap. Such an override is refused, the recipe's own input kept, and an
  `AdapterWarning` names both resources.

  Overrides resolve **per node and never onto the recipe**, which one node's siblings share, and the
  throughput lookup reads the same resolved list: matching rates against the recipe's own `@32767`
  entry would find nothing and rate the net at zero.

### Added
- **The texture dump can run in a client JVM, where nothing is `@SideOnly`-stripped
  (`tools/gtnh-extractor/`).** Every method on `net.minecraft.util.IIcon` is `@SideOnly(CLIENT)`,
  `getIconName()` included, so a dedicated server cannot ask a sprite what it is called. The five
  resolution routes, the hand-transcribed casing table, the icon injection and the bytecode matcher
  all exist to recover that one deleted answer. A client runs `registerBlockIcons`, stitches the
  atlas, and hands back a real sprite that names itself.

  `iconAt` and `iconRef` read that name first and fall back to every existing route, and
  `build.gradle.kts` forwards the `-P` run properties to `runClient` instead of only to an exact
  `runServer` match. The read is reflective on purpose: a direct `invokeinterface
  IIcon.getIconName` would die with `NoSuchMethodError` on the server running the same binary. Icon
  injection defaults **off** on a client, because writing `NamedIcon` stubs over live sprites would
  both mask the measurement and race the render thread; `-PinjectIcons` overrides it either way.
  The manifest now records which mechanism produced it, the physical side, and whether injection
  ran, so two dumps can be compared without guessing.

  **A bare sprite name belongs to the `minecraft` domain**, whatever registered it: `TextureMap`
  keys a sprite by the exact string given to `registerIcon` and resolves it with `new
  ResourceLocation(name)`. Resolving it against the block's own registry domain instead, which *is*
  right for the un-annotated `textureNames` fields, put 34 unfetchable paths like
  `assets/bartworks/textures/blocks/stone.png` in the first client manifest.

  **On a client the transcribed casing table yields to the live sprite.** `dumpPlainBlocks` tried
  `CASING_ICON_TABLE` before `getIcon`, which is right on a server (the call does not exist) and
  backwards on a client (the table is a hand copy of what the block will answer directly). The
  tabled families now resolve per side through `getIcon` first and keep the table as the fallback,
  scoped to those families because `BlockMachines.getIcon` is a vestigial stub that would otherwise
  skin every machine hull as an LV casing side. Re-running 2.9 with both orderings found **0 metas
  where the two named different sprites**, so the table is not lying at 2.9, and **3 metas that the
  table had flattened to a single face** now carry their real top and bottom:
  `gt.blockcasings9|2`, `gt.blockcasings10|5` and `gt.blockcasings12|4` were each drawn with their
  side texture on top and bottom.

  **A client run needs no human.** `-PautoWorld=true` has `ClientProxy` wait for the main menu and
  make the same `Minecraft.launchIntegratedServer` call the Create New World button makes, on a
  superflat scratch world it owns and is the only thing it will delete. A 2.9 dump then runs in
  1 m 27 s unattended and produces a manifest byte-for-byte identical to the clicked one. Off by
  default, because `runClient` is also how a person plays the dev environment. The GL window stays:
  1.7.10 is LWJGL2 and needs a real OpenGL context, which is what stitches the atlas this route
  reads, so the run is scriptable but not headless.

  Measured across both packs, same binary and same pins, differing only in the host JVM: **2.9 goes
  from 261 unresolved pairs and 208 of 296 gapped multiblocks to 12 and 13 of 296; 2.8.4 from 45 and
  56 of 208 to 1 and 1 of 208.** Nothing resolves worse: 4642 drawable keys gained and 0 lost at
  2.8.4, 7134 and 0 at 2.9, and `gregtech` icons naming a PNG the jar does not carry fall from 147
  to 2. A server dump is unchanged, verified at both packs. It needs a GL window and a human to load
  a world, so it does not replace the server path yet; `docs/dataset-extraction/client-dump-spike.md`
  has the numbers and the recommendation that follows from them. (#169)
- **The icon-name matcher works on GT 2.9 as well as 2.8.4 (`dataset/`, extractor).** 2.9 refactored
  `Textures.BlockIcons` from an enum into a class, and the icon holder went with it:
  `new CustomIcon(name)` became `Textures.BlockIcons.custom(name)`, a static factory returning the
  `IIconContainer` **interface**. The matcher keyed on an `INVOKESPECIAL` constructor, so on 2.9 it
  would have matched nothing, and matching nothing is silent: every tectech controller overlay would
  have regressed on the newer pack while the run still reported success. Shape B' matches the
  factory, and the injector asks GT for the container through that same factory, since an
  interface-typed field has no constructor to call.

  **The `BlockIcons` statics are named again.** Up to 2.8.4 the enum constant named itself, so
  `iconsets/<NAME>` came free. On 2.9 the containers behind the class's static fields do not all
  carry an `mIconName` (`GTTextureSetBlockIconContainer` has no such field), so 2939 came back
  unnameable. The field name still is the sprite, verified against the jar, so the dump records
  field-name to container once up front and `iconRef` consults that first: 1942 names recovered.

  **Both exits of `populateIconNames` now run both passes.** The 2.9 branch returned early, which
  skipped the bytecode matcher entirely on exactly the pack version it was added for.

  **A class the matcher cannot parse is now loud.** It used to collapse into an empty result,
  making "could not read this class" indistinguishable from "read it, found no names" - the way a
  whole mod goes missing from a run that reports success. Unreadable throws and is logged as an
  error; an allowlisted class that yields nothing is a warning, because that is what a pack bump
  moving a shape looks like.

  The matcher's real-bytes tests now read the **base** entry of the jar rather than going through
  the classloader. GT5U 2.9 ships a multi-release jar whose `META-INF/versions/17/` copies are Java
  17 bytecode; a modern test JVM prefers those, and ASM 5.0.3 refuses them, while the dump itself
  runs on Java 8 and reads the base Java 8 entry. The test was asserting on bytes no dump will ever
  see. (#98)
- **`gtnh-solve --dataset-coverage` reports what the local dataset cannot draw (`dataset/`, `cli`).**
  #98's scope asked for this and it existed only as prose: three questions, each failing
  differently, each previously answered by a throwaway script. Which controllers never dumped
  (`_meta.json`), which `(block, meta)` a multiblock places has no sprite **name** (the dump's
  `gaps`), and which resolved name has no sprite **bytes** in the jar the previewer fetches.

  **Gaps rank by multiblocks touched, not by raw count.** The manifest's `gaps` list runs to
  thousands and is dominated by fluid and ore blocks nothing ever places; sorting that way is how
  the wrong lane gets worked on (`texture-resolution.md`, trap 6).

  **Placed and substitutable blocks are counted apart**, which the first run showed is not a
  stylistic choice: `IC2:blockAlloyGlass` is in 4 controllers' block lists and is a `glass` channel
  alternative in 33 more. Every earlier count of it, this project's own notes included, said 4,
  because nothing was looking at substitutions. Reporting only that reads as a rounding error;
  reporting only the sum of 37 reads as 37 broken builds. Both numbers now ship, separately.

  The sprite-bytes half needs the GT jar and is checked **only if one is already cached**: a
  coverage report is not worth a 135 MB download nobody asked for, and the report says it skipped
  the question rather than implying it passed. `previewer.jar.cached_jar()` is the accessor for
  that, and it never downloads. (#98)
- **Icon names are recovered from unstripped bytecode, closing 73 of the dataset's 118 texture gaps
  (`dataset/`, extractor).** FML's `SideTransformer` deletes every `@SideOnly(CLIENT)` member as a
  class loads, so a name that exists only as a string literal inside a client-only method is
  unreachable by reflection. The `.class` in the jar is untouched, though, and
  `LaunchClassLoader.getClassBytes` returns the pre-transform copy, so an ASM matcher can read what
  the server cannot call. A stub `IIconRegister` cannot substitute: that interface is itself
  client-only, so a class implementing it will not load (measured: found on 806 blocks, threw on
  all 806).

  **Recovering the name is the whole fix.** The holders these names belong in (`BlockGTCasingsTT`'s
  `eM0..eM14`, `TTMultiblockBase`'s `ScreenON`/`ScreenOFF`) are *not* stripped, merely null, because
  the `new CustomIcon("...")` that would have filled them sits in a deleted method. Filling them
  lets every existing route answer unchanged: `BlockGTCasingsTT.getIcon` is a plain switch over its
  own fields and survives, per-side variants and all. No new resolution path, and no table. This is
  the sibling of `injectQueuedIconContainers`, which names containers GT *built*; these were never
  built at all.

  Three instruction shapes, each read out of real bytes before being written: `registerIcon` into a
  scalar field, a `CustomIcon` constructed into a holder, and `registerIcon` into an `IIcon[]` at a
  **constant** index. A computed index is refused, which is the line between this and the shared
  tier arrays the notes warn against. Anything unrecognised contributes nothing and keeps its gap.

  Measured against the 2.8.4 dump: **unresolved pairs 118 -> 45, multiblocks with a gap 70 -> 56**.
  All six tectech casing families and 25 of the 29 controller hulls now resolve. Of the 56 that
  remain, **34 are held only by `IC2:blockAlloyGlass`**, a declared non-goal, leaving 22 with
  anything still fixable. The four remaining hulls fail for unrelated reasons (a `NoSuchMethodError`
  and a `getTexture` NPE), not for want of a name.

  Two things the notes had wrong, both found by disassembling rather than reasoning: the monorepo
  jar is **not uniformly deobfuscated** (tectech calls `registerIcon`, gtnhintergalactic calls the
  SRG `func_94245_a`, and knowing only one silently skips the other's whole mod), and the array
  shape exists at all. The allowlist is explicit, per class, and never widens to "anything that
  matches". (#98)
- **`.schematic` files can be read back, not just written (`schematic/`, `cli`).**
  `read_schematic()` is the inverse of the exporter's lowering, and
  `gtnh-solve --inspect-schematic FILE` prints what a file holds: dimensions, a block histogram by
  registry name, and every tile entity with its `mID` resolved to a machine name.

  It exists because the format punishes re-deriving it. Three conventions decode to plausible
  nonsense rather than to an error, so each is now settled against the goldens and pinned by a
  test: cell order is MCEdit's `(y * Length + z) * Width + x`; `AddBlocks` gives an **even** cell
  index the **high** nibble (the other way round leaves 224 of the reference build's 1386 cells
  unmapped and 47 tile entities floating in air); and an id absent from `SchematicaMapping` reads
  back as `<unmapped:2417>` rather than being given an invented registry name. Round-tripping our
  own export covers the no-`AddBlocks` path, which the goldens cannot, since the exporter numbers
  ids compactly from 1.

  An unresolved `mID` **names the manifest that was asked**. The committed manifest is
  example-scoped, so against it most of a real build resolves to nothing, and a bare "not found"
  reads like a corrupt file when it only means the small manifest was consulted.
  `TextureManifest.display_name()` is the accessor this needed, alongside the existing
  `source_class` / `kind` / `te_base_type` family. (#96)
- **`gtnh-solve <plan> --schematic FILE` exports a solved layout as a Schematica build ghost
  (`schematic/`, `cli`).** Minecraft 1.7.10 has no Litematica; the consumer is Schematica, which
  loads a classic MCEdit `.schematic` as a build overlay. The lowering is three-way, because GT
  blocks are not one thing: a casing is a plain `(block, meta)` with no tile entity, while a
  machine or a cable is a `gt.blockmachines` cell whose identity is the `mID` in its tile entity
  and whose `Data` nibble selects the tile entity class (`te_base_type`, #158). Machines carry
  `mFacing` from the solver's placement, cables and pipes `mConnections` from the sides their
  route connects on, and the synthesized `Power Source` becomes GT's Debug Power Generator.
  Registry names ride a `SchematicaMapping` compound so the file remaps onto whatever block ids
  the loading instance assigned.

  A block the dataset cannot type is **refused with its name and the reason**, never guessed: a
  wrong nibble rebuilds a cable as a machine, and a schematic that is wrong looks buildable in a
  way an absent one does not. Machine configuration (covers, I/O sides, recipe locks) is
  deliberately out: that is the paste-fidelity half of #96, gated on the import corpus.

  NBT is hand-rolled (`schematic/nbt.py`), keeping the runtime at pydantic alone, and is proven
  by round-tripping the real files in `tests/golden/schematic/`: values, tag widths and bytes all
  survive. **Not yet verified in game.** (#96)
- **The dataset records the block metadata GT places each MTE at (`dataset/`, `previewer/`,
  `tools/`).** A `.schematic` (#96) gives each cell a block id and a 4-bit `Data` nibble, and for
  a GT block that nibble is *not* the machine: it selects the tile entity class, so writing the
  wrong one rebuilds a cable as a machine. GT derives it per MTE via `getTileEntityBaseType()` in
  four different ways (insulation for cables, the material's tool quality for pipes, the voltage
  tier for tiered machines, a constant for multiblocks), and only the first was reconstructible
  from what the manifest stored, so the extractor now asks the MTE directly and emits
  `te_base_type`. `TextureManifest.te_base_type()` reads it, answering `None` for a plain block
  and for any manifest predating the field, so an older dump still loads and simply cannot be
  exported. The previewer does not use the field. The manifest schema stays at **2**: the field
  is optional and additive, every reader treats its absence as "not stated", and bumping would
  claim an incompatibility that does not exist. (#158)
- **The committed manifest ships the block that stands in for a synthesized power source.**
  `Power Source (LV)` is an adapter invention, so nothing in the pack is named after it and
  `derive_small_manifest`'s machine-name rule could never keep one; GT's Debug Power Generator now
  ships by a dedicated rule, the way cables and hatches already did. (#158)
- **The extractor builds and dumps against GTNH 2.9.** Pins bumped to GT5-Unofficial `5.09.54.20` /
  StructureLib `1.4.42` from the DreamAssemblerXXL 2.9.0-beta-2 manifest, and `TextureDumper` ported
  to GT's refactor of `Textures.BlockIcons` from an `enum` into a `final class` of static
  `IIconContainer` fields, which had broken compilation outright.

  The port made the code **more** general, not less: the new `GTBlockIconContainer`s already carry
  `mIconName` and self-register into `GregTechAPI.sGTBlockIconload`, which is exactly the population
  `injectQueuedIconContainers` walks and `iconRef` reads, so on 2.9 the enum-era name injection simply
  has nothing to do. Both shapes are detected reflectively (`getEnumConstants()` returns `null` for a
  non-enum; a missing `mIcon` is a shape difference, not a breakage), so one code path serves either
  pack version with no per-version branch.

  Yields a 296-controller census dump against 2.8.4's 208. Local-only per the dataset policy, so the
  dump itself is not committed; only the pins and the port are.
- **The adapter knows which gtnh-factory-flow fork exported a plan, and says so when it cannot
  tell (`adapter/producer.py`, `--plan-schema`).** Two live forks emit plans this solver loads, and
  `schemaVersion` cannot tell them apart: MrBruh's bumped to 2 when it added the `resolved` block,
  arodoid's kept 1 through a thousand diverging commits. An arodoid plan therefore used
  to load with **zero warnings** and then size its power network from `recipe.eut`, the
  *pre-overclock* figure - 6.1x low across a whole plan and 256x low on a single machine, because
  an EV machine running an LV recipe draws 4^3 times its base value. Under-sized cable is the one
  failure a builder cannot see in the preview.

  Detection reads structural markers instead of the version integer: `resolved`/`app` (or
  `schemaVersion >= 2`) mark MrBruh's fork, `recipes[].machineHandlers` marks arodoid's.
  `--plan-schema {auto,mrbruh-v2,arodoid-v1}` pins it, `auto` is the default, and an
  undetermined plan is reported on stderr with the evidence, since naming the fork on the command
  line is advice only the CLI can give.

  The new `AdapterWarning` fires **only where it can matter**: a plan with no resolved figures
  whose own `machineHandlers` declare a machine `multiblock`. Base EU/t is exact for a single block
  at its recipe's tier, so `examples/gtnh-parallel-sand.json` stays correctly silent. Detection
  itself never warns - an undetermined result is normal for any hand-built plan, and warning there
  would fire across the suite and teach readers to filter `AdapterWarning` out, costing us the one
  warning that matters.

- **`examples/gtnh-parallel-sand.json`**, the first committed export from the arodoid fork:
  3 nodes, and the only fixture that exercises the single-block path (its Forge Hammers declare
  `kind: "single"` and are correctly absent from the multiblock census).

- **The physical dataset follows the pack the plan was balanced against.** `data/<version>/` dumps
  have coexisted for a while, but which one loaded was decided by *modification time*, so a 2.9 plan
  silently resolved its footprints against a 2.8.4 dump merely because that was the newest one on
  the machine. The join from a plan's machine to its physical record is by display name, and names
  move between pack releases, so a mismatch does not fail: it resolves some machines to the wrong
  footprint and drops others to the 1x1x1 default.

  Both forks state the pack per recipe (`source.datasetVersionId`, as `stable-2.8.4` or
  `local-2.9.0-beta-2`), so that is now read, channel-stripped to name a `data/<version>/` folder,
  and used as the default for `--dataset-version`. A plan whose recipes disagree, or that states
  nothing, keeps the previous resolution. A **derived** version is a preference rather than a pin:
  if no local dump provides it, resolution falls back instead of pinning a folder that does not
  exist and losing every real footprint. An explicit `--dataset-version` is never second-guessed.

  A remaining mismatch warns, naming both packs. The check abstains for a **non-census** dump: the
  committed `data/multiblocks/` fixtures are a two-machine sample whose `pack_version` is nominal,
  and they are what a fresh clone resolves to, so trusting it would greet every new contributor with
  a spurious mismatch against the shipped examples.

- **Hatches render as real GT hatch blocks, at their own facing, vertical ones included
  (`previewer/`, `tools/`).** A hatch was previously invisible: the previewer drew the casing block
  it displaced. It now resolves to the actual `(block, meta)` GT would place - an `Input Bus (HV)`,
  an `LV Energy Hatch`, a `Muffler Hatch` - **replacing** that casing cube rather than adding one,
  which is what makes a hatch cost a casing cell. The join is on the MTE's `source_class`, exactly
  what `HatchElement.mteClasses()` names, because the display names come in two shapes
  ("Input Bus (LV)" against "LV Energy Hatch") and a subclass keeps its parent's kind the way GT's
  own adders do. Nitrobenzene places 45 of them across 14 distinct blocks, 7 of them facing up or
  down.

  **A vertical facing could not previously be expressed at all.** `_rotate_side` permutes the four
  horizontal sides and returns UP and DOWN unchanged, and the extractor pins `aFacing` to NORTH for
  every MTE it walks, so the front overlays exist on that one side only. A hatch face is therefore
  **spliced**: the target side's own background, plus NORTH's overlays where the side is the one
  the hatch faces. That is exact rather than approximate - `MTEHatch.getTexture` computes its
  background without consulting either `side` or `aFacing`, so a six-facing re-dump would write
  byte-identical stacks. Taking the background from the target side is essential: UP and DOWN carry
  `MACHINE_<TIER>_TOP`/`_BOTTOM` against the horizontals' `_SIDE` in every hatch in the pack.

  A hatch is not turned by its machine's yaw, only by its own facing, and the texture pool key
  carries that facing - without it an UP-facing and a NORTH-facing bus of one type collide and one
  silently gets the other's bake.

- **The maintenance hatch is no longer drawn duct-taped.** Its dumped states are inverted: GT flips
  it to `active` the moment it joins a formed multiblock, so the `inactive` stack is
  `OVERLAY_MAINTENANCE + OVERLAY_DUCTTAPE`, the *broken* look. Read straight, every machine in a
  line would show as needing repair - the one skin a builder is meant to react to.

- **The committed manifest carries hatches.** `tools/derive_small_manifest.py` kept an MTE only if
  its display name contained an example machine type, which no hatch can match, so a preview run
  without a local dump skinned none of them. It now also keeps every hatch kind at every tier the
  examples use, resolved through the previewer's *own* `TextureManifest.hatch_block` so the two
  cannot drift. 30 blocks to 52, 204 KB to 310 KB.

- **The scene carries `port` on every terminal and the hatch list on every machine**, so a viewer
  can tie a terminal to the hatch it docks against instead of inferring it from geometry.

- **A layout now says which casing cell every hatch is, and which way it faces
  (`router/hatches.py`).** `LayoutResult.hatches` has existed since the output contract went to
  v1 and has been empty ever since; it is filled now, from three sources. A routed terminal
  becomes a hatch on the casing cell behind it, facing the way it docked. A **free auto-output**
  connection places two - GT still ejects through an output bus's own front face into an input
  bus, so two touching casing blocks are spent even though no pipe is laid, and nothing recorded
  them before. And every dumped machine gets the **upkeep hatches its structure records**: a
  maintenance hatch, plus a muffler wherever GT offered the muffler element, which it only does
  for a controller that pollutes.

  A machine with no dumped structure gets none at all: a single-block machine *is* its own I/O,
  and emitting a bus at its cell would describe replacing the machine with a bus. So sand, which
  is single-block machines and boundary storage throughout, still places zero hatches.

- **The muffler's vent is a routing keep-out, a constraint class the solver had no instance of.**
  `MTEHatchMuffler.polluteEnvironment` calls `getAirAtSide` on its own front facing, so a cable, a
  pipe, a casing or a neighbouring machine in that cell makes it return false and the controller
  stops with `POLLUTION_FAIL`. Hatch placement picks a facing whose outward cell is empty, and the
  validator proves it (`muffler_blocked`), along with a polluting structure that was given no
  muffler at all (`muffler_missing`).

- **Running out of casing cells is an explicit infeasibility, never a retry** (`hatch_budget`).
  The budget is a per-machine total, so no nearby cell and no re-placement can create one; it
  names the machine and what it ran out of room for.

- **Routed cables and pipes render as real GT blocks, and the build guide counts them** (`#4`).
  Routes were the last untextured geometry in the preview - flat coloured bars, sized
  `0.09 * sqrt(thickness)`, which drew a 1x cable at roughly a third of its in-game size with the
  wrong ladder shape besides. They now draw at GT's own cross-section, skinned with the real cable
  and pipe sprites. Nitrobenzene's MV trunk and both bronze fluid pipes resolve; sand's LV trunk
  does too.

  **The extractor was dumping none of them.** `data/2.8.4/textures/manifest.json` carried exactly
  **1185 gaps** reading `place threw NullPointerException`, and zero pipe entries, from two bugs
  that had to be fixed together: the dump path placed each block in a world (a `MetaPipeEntity` is a
  *sibling* of `MetaTileEntity` and NPEs there), and the `getTexture` overload it called returns
  `ERROR_RENDERING` for every pipe. Fixing placement alone would have turned 1185 honest gaps into
  1185 blocks of confident garbage. Pipes now dump from the registered prototype through the
  `int connections` overload, which every pipe's body answers as a pure function of its own fields.
  A re-dump: **1185 -> 0**, 1179 pipe entries, 0 isotropy failures, 0 key collisions.

  **The material a route is drawn in is a labelled STAND-IN, not a build spec.** GT gives one
  voltage tier many cable materials - at LV alone tin, lead, cobalt, zinc, soldering alloy and
  redstone alloy all carry 32 V - and this solver sizes a cable by *gauge*, never by material, so
  "the LV cable" is a choice rather than a fact. `dataset/pipes.py` makes that choice once (the
  community-standard ladder, stopping at UV because above it GT ships only superconductor bare
  wire), and every `RouteMaterial` it produces carries `stand_in=True`. The build guide prints the
  caveat, the previewer's legend footnotes it, and a `.schematic` exporter (#96) must refuse to
  lower one into a real block: a cable rendered in Tin when the build needs Aluminium is plausible,
  confident and wrong, the one failure nothing downstream can detect.

  **A cell incident to two gauges is built at the thicker one.** A shared trunk that splits carries
  different summed amperage either side of the split, so one cell can meet a 2x hop and a 1x hop at
  once (sand's cell `(2,0,1)` does). As a coloured bar that was harmless smoothing; as a real block
  it is a build instruction, and under-sizing is what burns. The rule now lives in `docs/DOMAIN.md`
  and is pinned by a test, where it used to live only in the viewer template's JavaScript.

  **The bill of materials is shoppable.** `5 x power cable` becomes `1 x 1x tin cable
  (cable.tin.01)` and `4 x 2x tin cable (cable.tin.02)` - split by gauge, because a trunk that
  thickens where the load sums is two different blocks, with GT's own unlocalized id so the name is
  matchable rather than merely readable. Counting moved from cells-per-commodity to blocks-per-cell,
  so a terminal cell no segment touches is now charged the block it needs.

  New `route_blocks.py` does the per-cell derivation for both surfaces, beside `system_io.py` and
  for the same reason: the guide and the preview must not be able to disagree about what one layout
  is made of. The validator re-derives each route's tier **independently**, from the machines the
  route terminates at rather than from the router's answer (`route_material_tier_mismatch`,
  `route_material_unknown`), which catches a route whose terminals moved without its material
  following - something neither the contract nor the router can see.

  Two things about the render are deliberate. Cable stacks bake with the tint applied **raw** rather
  than peak-normalised: those sprites are greyscale and the multiply *is* the material, so the
  casing normalisation both washed cables out and collapsed the dark insulated face into the bright
  open end until the two looked alike. And an unresolvable route keeps its **flat coloured bar**,
  never a checkerboard - a checkerboarded casing reads as "no sprite" beside the casings that have
  one, but a checkerboarded noodle threaded through a layout reads as damage. Because the bar is a
  correct render, the gap is reported in the texture summary instead.

### Changed
- **The placement fit test indexes a byte grid instead of building a cell list (`placement/`).**
  `_best_insertion` asked `reserved.isdisjoint(cells) and occupied.isdisjoint(cells)` of a freshly
  materialised list, so every candidate built one tuple per cell of the body. That is nothing when
  a machine is 1x1x1 and ruinous once the real dataset gives the Distillation Tower a 7x7x7 body:
  line profiling put it at **90% of every `occupied_cells` yield in a solve** (22.8M of 25.3M) and
  made it the single hottest line in the solver at 36% of `_best_insertion`.

  `_occupancy_grid` restates `occupied | reserved` as one byte per region cell, and `_box_offsets`
  expresses a rotated body once as flat offsets from its origin. The test becomes an indexed walk
  that breaks on the first hit, with no allocation and no hashing per candidate: 29.2s to 6.9s of
  instrumented time. `_ruin_and_recreate` builds the grid once and sets bits as each machine lands
  rather than rebuilding per insertion, which took the build itself from 3.1s to 0.4s.

  The grid is **unpadded** on purpose. `box_in_region` already gates every test with six
  comparisons on the rotated box's corners, so an index built from a passing origin is always in
  range and a blocked border would buy a bounds check that has already been paid for.

  Nitrobenzene solves in 10.76s against 15.19s, a 29% cut, with both shipped lines byte-identical
  (sand `591509cf937c621d`, nitrobenzene `eb408e8beef528a7`). **The win is invisible on the
  committed fixtures** (4.07s against 4.03s), because they collapse every machine to 1x1x1 and the
  old list was one tuple; it only appears against a real structure dump, which is the trap
  docs/TESTING.md names.

- **The previewer's arrow toggle is labelled `auto-output arrows` (`previewer/`).** It read just
  `arrows: on`, in a HUD whose own hint line uses "arrows" for the arrow *keys* that pan the camera,
  so the button did not say which arrows it hid. The label is unchanged in meaning and the title
  attribute already said it; only the button text is longer.

- **Insertion ranking stops scoring candidates that cannot win (`placement/`).**
  `_marginal_insertion_cost` now takes the incumbent's cost as a `bound` and returns `inf` above
  it, skipping the whole auto term for candidates already out of the running: the `Placement` it
  would have to build to ask with, and an `auto_output_possible` call per pair. That rule got
  dearer when it started asking what the router actually answers (#107), so the early-out is worth
  more than it would have been. `_best_insertion` keeps a candidate only on a strict `<`, so an
  admissible bound cannot change the argmin: both shipped lines still hash exactly as before.

  **The bound has to account for the auto reward being subtracted.** The running `wire + cable`
  total is an *upper* bound on the result, so testing it against the incumbent directly would
  discard candidates the reward would have pulled under - a wrong answer, not a slow one.
  Subtracting the largest reward still available makes it admissible.
  `test_the_pruning_bound_never_changes_which_cost_is_reported` fails with `inf == 6.0` against
  the naive form.

  Nitrobenzene 6.32s -> 4.36s, and 7.95s -> 4.36s (-45%) across both placement changes; the test
  suite 191s -> 76s. Solve figures are medians of 5 runs taken back to back against `main` in one
  session, the suite one run each the same way.

- **Insertion ranking stops re-deriving what every candidate shares (`placement/`).** A
  nitrobenzene solve spent 63% of itself in `_best_insertion`, which evaluates ~50
  (origin, orientation) candidates per insertion and rebuilt the same already-placed quantities
  for each one. Output is byte-identical - both shipped lines hash the same before and after -
  because nothing about the cost changed, only how many times its invariant parts are computed.

  - A net's half-perimeter is the bounding box of its members' centroids. Every member but the
    candidate is fixed during an insertion, so `_placed_invariants` precomputes that box once per
    net (`_NetBox`) and each candidate only widens it. Same two operands subtracted, so the span
    is identical; what goes away is ~1.9M `max()`, ~2.1M `min()` and ~1.9M list appends per solve.
    The penalized-power term cannot collapse to a box (it is a nearest-member distance, not a
    span) but its centroids are hoisted the same way (`_PowerAttach`).
  - `Machine.is_power_source` is a Pydantic property that rescans `faces.ports` on every read and
    depends on the machine alone, yet `_feed_ok` was asking it once per candidate - ~647k times a
    solve, ~4% of it. `_feed_ok_for` takes the answer as an argument so the loop hoists it, and
    `_feed_ok` delegates, keeping the feed rule in one place.

  Nitrobenzene solves in 4.27s against 5.81s, a 26% cut; the test suite drops 104s to 73s.
  `_cost` is deliberately untouched - it is a global recompute over all placements where
  everything may have moved, so it has no equivalent cross-call invariant.

- **The shipped example lines are solved once per session, not once per test (`tests/`).** A probe
  over a serial run put 53.0s of 75s inside `solve()`, and the same two lines were being re-solved
  from scratch by several modules that only needed *a* real layout to render or validate. A
  nitrobenzene solve is ~5.6s.

  `solved_sand` and `solved_nitrobenzene` in `tests/conftest.py` do the real `adapt_file` + `solve`
  once and hand each test a private deep copy. The copy is load-bearing rather than defensive:
  `InputIR` and `LayoutResult` are `StrictModel`, so a session-scoped object one test edits is a
  failure the *next* test reports, and a deep copy is ~0.4ms against a ~570ms solve. Tests whose
  subject is the act of solving keep their own call - determinism needs two independent solves to
  compare, and a different `physical` dataset, `objective`, `seed` or `optimize` is a different
  problem that cannot be served the cached one.

  Property-test budgets now come from `property_examples()` (`tests/_helpers.py`): the full
  200/50/300 whenever `CI` is set, a quarter of it locally, tunable with
  `GTNH_TEST_HYPOTHESIS_FRACTION`. Every PR is still held to the full generated space; only local
  iteration is cheaper. Scaled rather than re-set per test, so the ratio between the three budgets
  survives - the largest one fuzzes `validate`, not `solve`.

  Local `pytest` goes 175s to 93s; a CI-equivalent run (full budget, all cores) goes 175s to 141s,
  with coverage unchanged at 98%.

- **A local test run leaves the machine usable (`tests/conftest.py`).** `addopts` carries
  `-n auto`, which means *every* core, so `pytest` pinned the box at 100% for its whole run and
  nothing else stayed responsive. Two dials now bound it, both off when `CI` is set so GitHub
  Actions still gets the whole runner:

  - `pytest_xdist_auto_num_workers` scales `-n auto` to `floor(GTNH_TEST_CPU_FRACTION * cores)`,
    floor 1. It **defaults to `1.0`** - a run takes the whole machine, as `-n auto` always did -
    so the dial hands cores back on demand rather than withholding them. An explicit `-n 4` still
    wins, and xdist's own `PYTEST_XDIST_AUTO_NUM_WORKERS` escape hatch is left untouched.
  - every process, controller and each xdist worker, drops to a below-normal scheduler priority
    (`GTNH_TEST_NICE=0` opts out), so the cores it does hold yield to the foreground.

  **Handing a core back costs no wall clock.** Measured on a 4-core box at `--no-cov`: `-n 4` 56s,
  `-n 3` 54s, `-n 2` 58s. The fourth worker oversubscribes the cores the controller also needs, so
  `GTNH_TEST_CPU_FRACTION=0.75` there is if anything faster. The priority drop is done per-process
  rather than once in the controller because that does not depend on Windows priority-class
  inheritance through `execnet`'s popen.

- **Placement asks its geometry questions of the boxes, not of every cell (`ir/`, `placement/`).**
  Two predicates in the hot loop walked cell sets whose size is machine *volume*, so a solve got
  slower as the physical dataset gave machines their real footprints - exactly backwards, since
  that dataset is what makes a layout buildable.

  `auto_output_faces` materialized both machines' complete cell sets and scanned for a touching
  pair on each candidate face. The placement loop asks it about 1.5M times per solve, and the
  largest machine in nitrobenzene is 7x7x7: issue #110 profiled it at **70% of a solve**, driving
  88% of every `occupied_cells` step in the run. Two solid axis-aligned boxes touch across a face
  iff the source box stepped one cell that way overlaps the target on all three axes - six integer
  comparisons, independent of volume.

  The fit test beside it (`_best_insertion`, `_relocate`, `_swap`, `_turn_fits`) walked a
  candidate's cells to ask whether the body lies inside the bounding region: 21.7M `in_region`
  calls, about a quarter of what was left. A solid box is in-region iff its two corners are, so the
  new `ir.geometry.box_in_region` answers in six comparisons and the cells are expanded only for a
  candidate that has already cleared it - the overlap tests against `reserved`/`occupied` still
  need them, the region test never did.

  Nitrobenzene (seed 0, physical dataset): **86.5 s -> 22.3 s, 3.9x**; sand 1.0 s -> 0.8 s. Both
  shipped examples produce byte-identical VALID layouts - nitrobenzene over two seeds x all three
  objectives, sand over four - so this is a pure speed change: the search makes the same decisions
  in the same order, and no layout moves. Property tests pin both box formulations against the
  cell-set ones they replace, alongside the existing rotation-equivalence test, plus an exhaustive
  sweep of one body's whole neighbourhood at every facing pair.

- **Two touching machines are no longer enough for a free auto-output.** A multiblock ejects
  through an output hatch's own front face and receives through an input bus's, so the connection
  needs a touching pair of casing cells that can *host* those two hatches - not merely two bodies
  in contact. `ir.geometry.auto_output_faces` still models the loose rule and is still right for
  the placement cost that rewards adjacency and for a single-block machine, whose one cell is its
  own hatch; the router and the validator both apply the tighter one now.

  The **one-auto-output-per-machine** limit is scoped to match. It is a real GT limit on a
  single-block machine (one auto-output face, items XOR fluids) and simply wrong for a multiblock,
  where each output hatch ejects on its own front face independently. A multiblock now spends
  casing *cells* instead, and those claims reach the pipe router, so a pipe cannot dock onto a
  block an auto-output hatch is already standing on.

  Nitrobenzene: 45 hatches placed, floor area 154 against 144, but under the `volume` and
  `balanced` objectives the line comes out at 882 against a 1360 baseline, with 34 power cable
  cells against 60 and 79 route segments against 114. Sand is unchanged.

- **A pipe or cable may only attach where GT would actually let a hatch be built
  (`ir/`, `router/`, `validator/`, `solver/`).** Docking walked every cell of a machine's bounding
  box, so a route could dock against a casing block no hatch can replace, and the layout described
  a structure that will not form. It now walks the machine's recorded hatch slots
  (`Machine.hatch_slots`, turned with the placement), filtered to the cells that accept that
  port's own `HatchElement` kind, and only on faces that are actually **exposed** - which is what
  keeps a hatch off an interior slot, 29% of every slot in the dump, where it would be walled
  inside the structure and reach nothing.

  Kinds stay a **lower bound, never a whitelist**, at three levels: a machine recording no slots
  constrains nothing; a kind some slot names restricts to those slots; a kind *no* slot names is
  allowed anywhere, because a GT hatch adder built from a bare method reference records the cell
  without the kind rather than as refusing it. That last one is load-bearing, not theoretical: the
  Chemical Plant records zero `Energy`-capable cells and nitrobenzene must still power it. Power
  input accepts `Energy`, `ExoticEnergy` and `MultiAmpEnergy`, since 34 of 208 controllers record
  only the TecTech spelling.

  A machine's hatch cells are also now **one shared pool**. A casing cell is one block, so a cell
  holding an input bus cannot also hold an energy hatch - and a claim on the *dock* cell could
  never see that, because one casing cell has up to five free faces. The item/fluid router hands
  its claims to the power router, so the two compete for one budget instead of quietly stacking
  two hatches on one block, which they did before. On a machine with no recorded slots the claim
  falls back to the face, since a single-block GT machine genuinely does take input on one face
  and output on another of the same block.

  The validator proves all of it from its own rotation (`_geometry.hatch_cells`, written from the
  dump's stated convention, not from `ir.geometry.rotated_slot`): `terminal_not_on_hatch_cell` and
  `terminal_hatch_contention`. A property test holds the two derivations to the same answer, and
  the oracle checks every recorded slot of every controller at all four facings lands inside its
  own machine.

  Nitrobenzene's floor area comes back from 152 to 144 as a side effect: the shared pool is what
  the previous release gave up when route-aware docking started starving multi-hatch machines.

- **Which face a pipe docks on is now decided by the route, not by a tuple ordering
  (`router/`).** `dock()` walked `FACE_ORDER` and committed to the first free face before it knew
  where the route had to go, which is why nitrobenzene's item and fluid terminals piled onto
  SOUTH 16 times out of 18 while the route-aware power router spread over four faces. Docking now
  takes *every* free cell outside a usable face and lets multi-goal A* pick: the first leg runs
  multi-source and multi-goal, so the opening pair of faces is chosen together rather than the
  first endpoint guessing before it knows the second, and each later leg starts from the cell
  already chosen. Terminals stay fixed for the whole negotiation exactly as before, so the
  docks-are-not-tradeable invariant is untouched, and an endpoint the chain cannot reach falls
  back to the old first-fit so the failure taxonomy is unchanged. `dock()` itself is gone; its
  only caller was this one.

  Nitrobenzene's terminals now read `west 6, east 6, south 4, down 3, up 3`, its build lays 86
  route segments against 114, and its power cable drops from 60 cells to 43. Its floor area rises
  from 136 to 152: shorter pipes hug machine surfaces, and one machine with three HV energy
  hatches is left with two free dock cells, so the power net cannot dock and the attempt that used
  to win is lost. Per-machine cell claiming is the next lane's job (`docs/hatch-placement/`), and
  the regression is recorded there rather than absorbed into a loosened assertion. Sand is
  unchanged.

### Fixed
- **The Heat Proof Machine Casing is no longer dumped as a UIV machine casing (`dataset/`).**
  `BlockCasings1.getIcon` answers `gt.blockcasings` metas 10-15 from its own switch (bronze plated
  bricks, heat proof, the three dimensional casings, the superconducting coil) and only falls
  through to the tiered `MACHINECASINGS_*` arrays below that, but the extractor's casing table
  claimed 0-14 and runs ahead of the `getIcon` route, so those six metas were skinned as the tier
  casing sitting at their index. Meta 11 shipped as `MACHINE_UIV_SIDE` and meta 12 as
  `MACHINE_UMV_SIDE`.

  This is the one failure mode nothing downstream can catch: a checkerboard is recoverable, a
  confident wrong sprite is not. It reached every clean clone rather than only local dumps, because
  the Electric Blast Furnace is one of the two committed multiblock fixtures and Heat Proof is the
  casing it is built from; and since #128 made the controller's casing the background for every
  formed hatch, one wrong casing propagated across every hatch of any multiblock using it. The
  bound is now the switch's rather than the block's 16 metas, and a golden guard over the committed
  manifest pins meta 11 to its own sprite. Metas 0-9 are unchanged. (#130)

- **Auto-output arrows are drawn for single-block sources only (`previewer/`).** The decal is
  positioned off the source machine's bounding box, which is where the ejection happens only when
  the machine is one cell, because then it is its own hatch. A multiblock controller has no
  auto-output at all in GT (`doesAutoOutput` lives on `MTEBasicMachine`, and `MTEMultiBlockBase`
  never mentions it); its output hatch or bus pushes to that *hatch's* own front face. So an arrow
  on the controller's box marked a casing face that moves nothing: on the nitrobenzene example, 10
  of 16 auto-connections have a multiblock source, and the 7x7x7 Chemical Plant carried four arrows
  about 3.5 blocks off the nearest real hatch. The connection itself is unchanged in the scene, the
  build guide and the validator; only the misplaced decal is gone. Drawing it at the hatch cell that
  `LayoutResult.hatches` already records is the follow-up (#153).

- **The under-supply check now says when it did not run, and states a ceiling only where GT's own
  rule is known to apply (`validator/`, `adapter/`, `dataset/`, `cli/`).**
  `POWER_SUPPLY_INSUFFICIENT` only ever ran on a connection declaring a `Port.max_amps` ceiling,
  and a machine whose connections declared none never entered the supply sum at all - so it was
  skipped silently, indistinguishable from "checked and fine". That is the hole #114 was filed
  about, and it also corrects a premise of #106, whose acceptance rested on the validator checking
  intake independently: that held only where `max_amps` happened to be set.

  **Abstaining is now reported, not silent.** `ValidationReport.unverified_power_intake` carries
  every powered machine the check could not measure - an unknown ceiling on some connection, an
  unverifiable route, or a machine that never reached a power route - and `gtnh-solve` prints the
  count on stderr ("power intake unmeasured for 3 of 3 powered machine(s)"). It is deliberately
  **not** a violation: `report.ok` is `not violations` and the solver downgrades any layout
  carrying one, so shipping an abstention as a `Violation` would fail every layout whose machines
  cannot be classified. A check that could not run proves nothing either way; it just must not
  read as a pass.

  **GT has two intake rules, and the solver states only the one it can prove applies.** A
  multiblock takes power through energy hatches at a fixed 2 A each
  (`MTEMultiBlockBase.getMaxInputPower()`); a **basic machine** - `MTEBasicMachine` and its
  subclasses, the single-block processing machines - takes it through its own block, at
  `maxAmperesIn() = (mEUt * 2) / V[tier] + 1`, which scales with the recipe it runs. The two are
  not interchangeable, and `Machine.hatch_cells is None` does not pick between them: it says only
  "no structural record", a population **dominated by multiblocks** whenever the dump does not
  cover them. Under the committed two-machine fixtures it is every machine in both shipped
  examples, the Large Chemical Reactor included. So `dataset.machine_amps_in` is applied only to a
  machine a **census** dataset positively failed to find, which is what makes it a single block;
  the committed fixtures declare themselves a sample (`_meta.json`'s new `census: false`), and
  without a dataset nothing is known at all. Elsewhere the connection reads as genuinely
  unmeasurable rather than being measured against a fabricated number.

  **The formula is also bounded by GT's own input domain.** Both overclock paths cap the
  consumption they compute at `V[tier] * mAmperage`
  (`MTEBasicMachine.calculateOverclockedNess`, `EUOverclockDescriber.createCalculator`), so with
  the standard amperage of 1 a real basic machine never carries `mEUt > V[tier]`.
  `machine_amps_in` returns "unverifiable" above that instead of extrapolating: the #114 repro's
  LV machine drawing 480 EU/t would otherwise be answered with "GT accepts 31 amps", a confident
  number no GT block can hold, about an EU/t figure the PR itself identifies as an upstream export
  bug. A plausible wrong value is worse than an honest gap.

  **Where it does apply, it is a ceiling with teeth only on a long run** - stated plainly rather
  than buried. `floor(2e/V) + 1 > 2e/V`, so a basic machine can always take in the recipe it runs
  at the source voltage and the check cannot fire within half the tier voltage in cable blocks. It
  first bites at `V/2 + 1` (17 blocks at LV, 65 at MV, 257 at HV), or at 22 / 86 / 342 for a
  machine drawing its whole tier, with `POWER_VOLTAGE_DROP_EXCESSIVE` taking over past `V`.

  **An unknown ceiling now marks its machine unverifiable** instead of quietly contributing
  nothing, which closes a latent false positive: a machine with one rated and one unrated
  connection was judged on the rated one alone, and could be reported starved on part of its
  intake.

  **`Port.max_amps = null` is settled as "unknown", never "unlimited"** (no version bump; both
  readings produced the same verdict, so nothing was live). `docs/IR.md` said unlimited while the
  validator had always implemented unknown, and a versioned contract should not carry two
  readings. Every GT connection has a ceiling: the producer either names the rule or abstains, so
  a consumer must treat null as unmeasurable, not satisfied. Recorded in `ir/__init__.py`.

  Neither shipped example changes verdict (sand valid, nitrobenzene `partial_invalid` on the
  pre-existing face-reachability failure); on the committed fixtures both now report every powered
  machine as unmeasured for intake, which is the honest state of the gate there.

- **A hatch on a formed multiblock now wears that machine's casing, not the standalone skin
  (`previewer/`).** GT re-skins a hatch when it joins a *formed* multiblock:
  `MTEHatch.getTexture` reads its background from `casingTexturePages[page][index]`, an id the
  controller hands over through `updateTexture` in `add***ToMachineList`, and falls back to
  `MACHINE_CASINGS[mTier]` only while the hatch stands alone. The extractor dumps hatches standing
  alone, so the manifest holds that fallback - which drew an input bus on the Industrial Coke Oven
  in HV machine casing rather than the oven's own Structural Coke Oven Casing, on every hatch of
  every multiblock (GitHub #109 part 2).

  **It is fixed in the splice, with no extractor change and no re-dump.** The casing id is not in
  the dump, but the block it names is: a GT hatch element is written
  `buildHatchAdder(..).casingIndex(CASING_INDEX).buildAndChain(ofBlock(CASING, META))`, and that
  `casingIndex` is the `TextureFactory.of(CASING, META)` the same casing registered - so the blocks
  at a machine's hatch-capable cells name the casing GT re-skins its hatches to. Checked against GT
  source for the Distillation Tower (`CASING_INDEX = 49`, page 0 index 49, `gt.blockcasings4|1`),
  the Large Chemical Reactor (`176`, page 1 index 48, `gt.blockcasings8|0`), the Industrial Coke
  Oven (`TAE.GTPP_INDEX(1)`, `miscutils.blockcasings|1`) and the ExxonMobil Chemical Plant
  (`getCasingTextureID()`, its own solid casing).

  **One casing per machine, taken as the mode over its hatch cells, not the block under each
  hatch.** GT declares a single `CASING_INDEX` per controller and hands it to every hatch, and
  reading the cell instead is wrong on a shipped example: the Large Chemical Reactor's `x` element
  chains `activeCoils(..)` ahead of its casing, so 1 of its 25 hatch-capable cells holds a
  cupronickel coil and a hatch landing there came out coil-skinned. The population is the dump's
  `hatch_slots` where it recorded them, else the cells the machine's own hatches occupy.
  Nitrobenzene's 45 hatch cubes now resolve to exactly four casings, one per machine type, and a
  run reports the count, because every one of these looks is a complete and plausible hatch and
  nothing else would say which of them a page got.

  **A mode that lands on a non-casing is drawn, but reported as a guess.** GT's real answer is the
  controller's `casingIndex` integer, which the dump does not carry, so the mode is an estimate
  that can never be confirmed - only caught out. A few controllers accept their hatches in a glass
  ring rather than in the block their `casingIndex` names: measured over the 208 locally dumped
  multiblocks, 6 land on a non-casing (the T.F.F.T. on `gt.blockglass1|0` and the five Compact
  Fusion Computers on `BW_GlasBlocks`). The dump's own `source_class` provenance says which: a
  casing resolves through a casing class, glass does not. Those faces are now counted apart
  (`TextureSummary.hatches_recased_uncertain`), the blocks they wear are named
  (`uncertain_hatch_casings`), and the run warns, so a plausible wrong sprite cannot pass for a
  resolved one. The sprite still goes down, because it is what the cells around the hatch hold and
  a builder can read that; what changed is that it is no longer indistinguishable from a fact.

  Where the casing is undeterminable - no hatch cell resolves, or the casing is one the manifest
  cannot skin - the face keeps the hatch's own background rather than losing its texture, taken per
  side so an UP-facing hatch still gets `MACHINE_<TIER>_TOP`. The texture pool key gained the
  casing: one bus kind serves several machines in a line and each wears its own, so without it the
  dedupe would paint them all in whichever machine baked first.

  The committed fixture manifest is load-bearing for hatch backgrounds now as well as for casing
  cubes, which makes the `gt.blockcasings` metas 10-15 mis-skin (issue #130) reach one more surface.
  That needs a `TextureDumper` bound change and a re-dump, so it is tracked separately.
- **The validator now checks the hatches a layout *needs*, not only the ones it records
  (`validator/`).** Two holes in one gate, both of them the safety net certifying the producer
  instead of checking it (docs/ARCHITECTURE.md #4).

  **A missing maintenance hatch was invisible (#116).** `router/hatches.py` treats maintenance and
  muffler as equally required on the way in (either one unplaceable is an explicit infeasibility),
  but only the muffler was re-checked on the way out. Stripping all 7 `Maintenance` hatches from a
  solved nitrobenzene layout added no violation at all, while stripping its single muffler was
  caught: the codebase checked the *optional* upkeep hatch and not the mandatory one.
  `mMaintenanceHatches.size() == 1` is asserted in a dozen-odd `checkMachine` implementations, so
  a structure without one does not form. `MAINTENANCE_MISSING` now mirrors `MUFFLER_MISSING`,
  derived from the machine's own recorded slots, so a dump silent about that kind still reads as
  "unknown" rather than "forbidden" (35 of 208 controllers record no `Maintenance`-capable cell).

  **Exactly one, counted rather than looked for.** GT reads the *count*: 57 of the 64 controllers
  that touch `mMaintenanceHatches` assert `size() == 1` and the remaining 7 demand `<= 1`, so none
  of them forms with two. A machine carrying three of them on three distinct casing cells satisfies
  every other hatch check there is (real body cells, outward facings, one hatch per cell, so
  `HATCH_CELL_COLLISION` sees nothing wrong), which is why presence could not catch it. A surplus is
  `MAINTENANCE_DUPLICATE`, its own code rather than a widened `MAINTENANCE_MISSING`, because "place
  one" and "remove two" are different fixes and a code named `_missing` would be a lie about a count
  of three. The muffler deliberately keeps the weaker presence-only rule:
  `MTEMultiBlockBase.polluteEnvironment` divides the vent batch across however many mufflers a
  controller has, and controllers assert 2 of them (Nuclear Salt Processing Plant) or 4 (Nuclear
  Reactor, the larger turbines), so demanding exactly one there would reject structures GT requires.

  **A port whose hatch went missing was invisible too (#119).** The gate validated the hatches
  that were *present* (a body cell of its own machine, an outward facing, no two on one casing
  cell, agreement with its terminal) and never asked whether a connection that needs a hatch has
  one, so the property held only because the producer happened to be correct. On a multiblock the
  connection IS a block, so a pipe docked against plain casing describes a structure that forms
  and then moves nothing. `PORT_HATCH_MISSING` re-derives the requirement from the problem's own
  nets: a net the layout physically realizes, by pipe or by free auto-output, needs a hatch at
  each machine it attaches to. Three cases genuinely need none and are deliberately left alone,
  because a false infeasibility is the worse failure of the two: an ME-toggled commodity is not
  physically routed at all; a net with neither a route nor an auto-connection is
  `MISSING_CONNECTION`'s to report, as is a port no net names (closed by a boundary storage, or a
  feed the plan never drew); and a machine that records no hatch slots is its own I/O, or is
  simply unknown, which 23 of 208 dumped controllers are.

  Neither check calls into `router/hatches.py` or restates its logic. Both are computed from the
  `LayoutResult` and the `InputIR`, which is what lets them catch the producer dropping a hatch
  rather than agreeing with it. Both shipped examples still solve VALID and validate clean.

  **`PORT_HATCH_MISSING` fires on real producer output today, so some layouts change verdict.**
  `router/hatches.py` can drop both hatches of a free auto-output connection when a power hatch has
  already taken the casing cell the connection reserved, and the result is a line with a silently
  dead connection: `main` calls such a layout VALID, this gate calls it `partial_invalid` and asks
  the user to report a solver bug. That is the honest verdict, not a new defect, but it IS a change
  for multiblock-dense inputs with power. Measured at 1 of 200 randomly generated multiblock
  problems, which is 1 of the 8 among them that had a free auto-output connection at all, and at 0
  of 28 solves of the two shipped examples. The producer bug is #131 and is fixed there, not here.

- **A power source is now placed by the cable it actually costs (`solver/`).** A source's position
  exists purely to serve a trunk, and it was the one machine the annealer had no gradient on: an
  un-penalized power net never entered the placement cost, so a 1x1x1 source anywhere inside the
  build's bounding box was cost-neutral to move. Ranking eight fully-routed attempts on real cable
  cells is not the same as searching - no move inside an attempt was ever aimed at shortening a
  trunk, so the grid picked the luckiest of eight accidents.

  **The obvious fix was tried again and lost again.** PR #62 removed power's base wirelength term
  after measuring that center-distance proxies steer AWAY from low-cable layouts, and re-measuring
  that here confirmed it: giving power nets an MST pull at weights 1.0, 0.5, 0.25 and 0.1 made the
  shipped sand line's cable go **up** at every one (3 cells to 4-6) and its floor area with it
  (5 to 8-12). The proxy cannot see that sand's source sits *on top* of the machine row with the
  hammers tapping its dock cell through their top faces - it scores a nearer, worse position
  higher, and 0.1 breaks that layout as completely as 1.0, because the term flips a near-tie
  rather than applying a gradual pull.

  So the source is positioned where cable IS knowable, on a routed layout - the same decision as
  #62, carried through rather than reversed. A new **repair pass** (`solver/repair.py`) runs after
  the pipes are laid: each source **aims at the load it serves** (its trunk's first branch, or its
  only connection), takes the nearest legal poses on the region walls its feed face must sit flush
  against, and every one of those is **really routed** with `route_power` and ranked on the
  feedback loop's own quality key (`solver/_structure.py`, now shared with the loop so the two
  cannot pull against each other). A candidate is adopted only if it is strictly better on that
  key, so the pass either improves a layout or leaves it exactly alone.

  **Aim first, then project to the wall** - the order matters. Picking candidates by adjacency to
  a sink instead intersects two constraints that often miss each other entirely: nitrobenzene's MV
  consumer spans z3..5 while the nearest wall a horizontal feed face can use is z=0, so a radius-1
  adjacency shell offered that source **no pose at all** and it sat 10 cells along the wall from
  its load behind a 14-cell trunk. The nearest wall pose to a point, by contrast, always exists.
  The aim only shortlists; `route_power` still decides, which is what keeps #62's finding from
  creeping back in - a ranking proxy can miss a good candidate but cannot promote a bad one past a
  real routing. Twenty candidates per source was measured, not guessed: it matches an exhaustive
  scan of every legal pose on both examples and all three objectives, for about 60 routings per
  attempt instead of 5200.

  Two rules keep it from doing harm. It declines a net carrying a **pinned** I/O, whose route is
  constrained to ground the power router does not model. And it leaves a placement whose power
  will not route **untouched**: such an attempt is on its way to the feedback loop as a diagnosis
  ("these nets failed, penalize them and re-place"), and shuffling sources first changed which
  nets failed each attempt - the loop gives up early when a failed-net set repeats, and that churn
  alone cost nitrobenzene/balanced its valid layout, breaking off after three attempts instead of
  eight. The fast (`optimize=False`) path is unchanged: it stays a single constructive placement.

  Measured at seed 0, structure floor area / layers / power cable cells:

  | example | objective | before | after |
  |---|---|---|---|
  | sand | footprint / volume / balanced | 5 / 2 / **3** | 5 / 2 / **3** |
  | nitrobenzene | footprint | 154 / 10 / **60** | 136 / 10 / **33** |
  | nitrobenzene | volume | 126 / 7 / **34** | 126 / 7 / **25** |
  | nitrobenzene | balanced | 126 / 7 / **34** | 126 / 7 / **25** |

  Sand holds its hand-built 3-cable target exactly; nitrobenzene drops 45% of its cable on the
  default objective and 26% on the other two, and its floor area comes down 154 to 136 as well.
  Every one of those matches what an exhaustive scan of all ~5200 legal source poses finds, so
  there is no further win available from moving sources alone.
  Refs #123.

- **A machine starved of power is now something the feedback loop can fix, not a lost attempt
  (`solver/`, `validator/`).** A machine takes packets through hatches capped at `Port.max_amps`
  and cable loss shrinks every packet, so its real intake is `sum(max_amps * delivered_volts)`.
  The hatch allowance is designed against a nominal 16-block run, and the validator re-checks it
  at the distance actually routed (`POWER_SUPPLY_INSUFFICIENT`) - deliberately, since only a
  routed layout knows that distance.

  But the solver returned that rejection as `partial_invalid` with an **empty** `failed_nets`, on
  the rule that a layout which routed everything yet failed validation is a solver bug and
  re-placing cannot help. For this one violation that rule is inverted: the shortfall is driven by
  cable distance, so re-placing the machine nearer its source is precisely the fix. With no failed
  net nothing was penalized and the loop wrote the whole attempt off. It now names the starved
  machine's power net, so the placement cost gains its MST trunk-length pull and the next attempt
  pulls the machine in. The empty-`failed_nets` short circuit stays exactly as it was for every
  other violation, including a report that proves a starve *alongside* a real geometric bug.

  A layout rejected only for this reports a `power_supply` infeasibility rather than the generic
  `validation` one, so the advice names the real fix (shorten the run) instead of asking for a bug
  report. `Violation` gained an optional `machine_id` so the machine it names travels structurally
  rather than only in the message prose. Refs #106.

### Security
- **A machine type or resource id from someone else's plan can no longer run script in the
  preview page (`previewer/`).** The legend and the system-i/o rows were built by concatenating
  those plan strings into HTML and assigning the result to `innerHTML`, so opening a `--preview`
  page generated from a shared plan executed whatever its author put in a machine name, at a
  `file://` origin, on a page with no policy of its own. Sharing exported plans is the normal
  workflow here, so "a plan you did not write" is the expected case.

  The panel is now assembled from DOM nodes: `createElement` plus `textContent`, with the swatch
  colour set through the CSSOM (`style.background`) rather than interpolated into a `style`
  attribute. Markup in a name renders as the text it is. That is the runtime half of the hole the
  `</` payload escape closed at parse time; the escape stays, because it fixes a different one.

  The page also ships a **Content-Security-Policy** as the layer behind both: `default-src 'none'`,
  scripts only from the sha256 hashes of the page's own two inline blocks plus the three.js CDN
  origin, styles only from the stylesheet's hash, images only from `data:` (the baked GT
  textures). Hashes rather than `'unsafe-inline'`, so an injected inline handler does not run even
  if a sink is ever reintroduced, and no `connect-src` at all, so nothing can carry a layout off
  the machine. The policy is computed from the emitted blocks, and a test recomputes it from the
  finished page, so it cannot drift into silently blocking three.js. Refs #111.

### Added
- **A multiblock's hatch cells now say WHERE they are, and a layout says where each hatch went
  (`ir/` LayoutResult v1, `dataset/`, `adapter/`, `validator/`).** `Machine.hatch_slots` carries
  each hatch-capable casing cell as an offset from the machine's unrotated minimum corner plus the
  kinds it accepts, taken from the same built form as the footprint and the ceiling so all three
  describe one building. The dump measures those offsets from the *controller block*, which is not
  generally the minimum corner (the Coke Oven records a slot at `[-1, 0, 0]`), so the translation
  happens once in the dataset. `InputIR` stays at v3: the field is additive and an empty tuple
  reads exactly as the old behaviour, which is also what a single-block machine, a plan adapted
  without the dataset, and the 23 of 208 controllers that record no slots all get.

  `LayoutResult` gains `hatches`, one `PlacedHatch` per hatch or bus the build needs, at the body
  cell it occupies and the way it faces. **This bumps the output contract to v1, the first bump it
  has had**, because the omission is the breakage: a maintenance hatch and a muffler belong to no
  net and had nowhere to live, so a v0 layout described a machine that will not run, and a consumer
  that ignores the new field keeps describing one. A routed hatch is deliberately recorded twice,
  here and by its `Terminal`, and the validator re-derives the agreement rather than trusting it.

  The validator gains the structural half of the hatch rules: a hatch sits on a body cell of its
  own machine, faces *out* of it, shares its cell with nothing, and agrees with its port's terminal
  (`HATCH_NOT_ON_MACHINE`, `HATCH_FACES_INWARD`, `HATCH_CELL_COLLISION`, `HATCH_TERMINAL_MISMATCH`,
  `HATCH_UNKNOWN_PORT`). Facing outward is the one GT will not catch for us: `IStructureElement`
  takes no facing and every hatch returns `isFacingValid = true`, so a multiblock forms happily
  with a hatch pointing into itself and then moves nothing. `kinds` is documented at every level as
  a **lower bound, never a whitelist** - 61 of 185 controllers record no `Energy`-capable cell, so
  treating an absent kind as a prohibition would manufacture false infeasibilities across a third
  of the dataset. Nothing emits hatches yet; the assignment stage is the next lane.
- **A machine takes power through as many energy hatches as its draw needs, not one connection
  (`ir/` v3, `dataset/`, `adapter/`, `router/`, `validator/`, `system_io`).** A standard GT energy
  hatch accepts 2 amps (`MTEHatchEnergy.maxAmperesIn()`; its tooltip says so, and
  `BaseMetaTileEntity.injectEnergyUnits` enforces it per tick), and a multiblock's intake is the
  sum over its hatches. The solver modelled one connection per machine, so it could certify a
  layout feeding 16 amps into a single MV hatch that can take 2 - unbuildable. The adapter now
  gives each machine the hatches its draw needs, each carrying its share of the EU/t, and the
  partitioner works in hatches rather than machines: a heavy machine's hatches spread over as many
  cable runs as the 16x cap requires, which is what makes it powerable at all. A machine needing
  one hatch keeps the plain `power:in` port id; several suffix it (`power:in#1`, ...).

  The structural budget comes from the dump the extractor already records: `hatch_slots` says, per
  cell, which hatch kinds it accepts, and a multiblock's casing cells take I/O of any kind - the
  Industrial Coke Oven has 17 cells and every one accepts `Energy`, `InputBus`, `InputHatch`,
  `Maintenance`, `Muffler`, `OutputBus` and `OutputHatch` alike. `MachinePhysical` now derives
  those counts and the validator rejects a layout wiring more connections onto a machine than its
  structure can host (`HATCH_CELLS_EXCEEDED`). A machine with no structural record - a single-block
  machine, or any plan adapted without the dataset - keeps exactly one connection, as before.

- **The validator checks that enough power *arrives*, not just that the cable is thick enough
  (`validator/`).** Cable loss shrinks every packet and a hatch passes a bounded number per tick,
  so a machine far from its source can sit on cables that are all correctly sized and still be
  starved. Its real intake is `sum(hatch_amps x delivered_volts)` over its hatches, accumulated
  across routes because a machine's hatches can sit on different nets at different distances; a
  shortfall against its `eut` is reported as `POWER_SUPPLY_INSUFFICIENT`. The opposite case - a
  cable offering a hatch more amps than it accepts - is deliberately not an error: the hatch takes
  its 2 and the under-supply check catches any genuine shortfall.

- **TEMPORARY: a machine needing more than 3 energy hatches is supplied at a higher voltage tier
  (`adapter/`).** Upstream gtnh-factory-flow computes some machines' EU/t against a wrong recipe
  model (#44 and #45: the Industrial Coke Oven has no heating coils, and its parallel caps are
  18/30 rather than 16/32), so a node can arrive drawing far more than its stated tier plausibly
  delivers - the nitrobenzene Coke Oven wants 2355 EU/t at MV, which is 11 hatches. Rather than
  ring it with a dozen hatches, the adapter raises the tier it is *supplied* at until 3 suffice
  (MV -> HV here, which needs 3), the same thing a player would do. This does **not** re-derive the
  recipe: a real tier change also re-overclocks, moving both `eut` and the parallel count, which
  only the exporter can do. **Remove it once those upstream fixes land.** It applies only to
  machines with a structural record, so a plan adapted without the dataset is untouched.

  With these three, **the nitrobenzene example now solves `valid`** - the first time it has. It
  supersedes the "needs parallel runs or a higher tier, still Phase 2" caveat below: parallel runs
  for one machine are what the hatch model delivers.

- **A voltage tier drawing past the 16x cable cap is now split across several power sources
  (`adapter/`, `system_io`).** A shared-amperage trunk sums every machine hanging off it, so one
  source per tier meant the segment at the source carried the whole tier: the nitrobenzene line's
  MV tier wanted 21 amps against a cap of 16 and was rejected outright, no matter how it was
  placed. The synthesis now bin-packs each tier's machines by nominal amp load (first-fit
  decreasing) and gives every group its own source and net, so a tier that does not fit one run
  gets as many runs as it needs. A tier that fits keeps its old ids (`power-source:MV`,
  `power:MV`); one that splits suffixes them (`power-source:MV#1`, `power:MV#2`, ...), so the
  common case reads unchanged in the build guide and previewer.

  Partitioning uses the *nominal* (at-source) amp load, because cable distances do not exist until
  placement has run. That deliberately reserves no headroom for voltage loss: a group that loss
  later pushes over the cap is still reported by the router, never silently accepted. A machine
  whose own draw exceeds the cap is put in a group by itself rather than folded in with a
  neighbour - no partition can help one machine's single feed, and burying it in a shared trunk
  would hide the real problem behind a misleading number. That case needs parallel runs or a
  higher tier, which is still Phase 2 (`docs/ROADMAP.md`).

- **`system_io` now reports feed amperage per source as well as per tier.** The build guide states
  a wiring spec per source block, and it was reading the per-*tier* total, which was correct only
  while a tier had exactly one source. With the split above it charged every source of a tier the
  whole tier's amps: the nitrobenzene MV source feeding a lone 120 EU/t Distillation Tower asked
  the builder for 20 A instead of 2. `SystemIO.power_amps_by_source` attributes each machine's load
  to the source on its own net, and the guide renders that; `power_amps_by_tier` stays as the
  tier-wide summary the previewer shows. A power net without exactly one source is skipped rather
  than guessed at, since the validator rejects that layout anyway.

- **Every block the shipped example lines place now renders with its real GT sprite
  (`tools/gtnh-extractor/`, `previewer/`, GitHub #98).** Four mechanisms, because the single symptom
  ("the block is grey") had four unrelated causes, each needing its own fix:
  - *Icon domain.* `iconName()` hardcoded the `gregtech` domain, which put 46 unfetchable paths in
    the shipped manifest: 17 GT++ icons pointed at `assets/gregtech/.../TileEntities/` (a directory
    GT5U does not have - they live under `assets/miscutils/`), and 29 came out double-prefixed as
    `gregtech:gregtech:icons/...`, a path with a literal colon in it. The domain now comes from the
    container's own `mModID`, or from an already-qualified `mIconName`. One jar still serves all of
    them: GT5-Unofficial is a monorepo and ships all 20 asset domains.
  - *Custom icon containers.* GT's client-only icon-load hook is what populates every custom
    `IIconContainer`'s `mIcon`, so on a server they answer `getIcon` with null. Since each one
    self-registers into `GregTechAPI.sGTBlockIconload`, that public list is a complete server-side
    registry of them, and injecting a named icon into all 11,766 fixes GT++, kekztech and the rest
    generically - no per-mod table, and it reaches instances held in private statics that walking any
    one holder class would miss.
  - *ITexture accessors.* Some blocks expose a `getTextures(int)` / `getTexture(int)` that carries no
    `@SideOnly` and never dereferences the icon. Preferred over `getIcon` wherever present: it cannot
    hit the side-stripping cliff, and it carries the per-layer tint and glow that the single-icon path
    discards. This is what makes coils render, with their real active/inactive pair, and frames carry
    their per-material tint.
  - *Casing table.* The rest declare `getIcon` `@SideOnly(CLIENT)`, so the method is deleted outright
    and no reflection can reach the mapping. Ten families are transcribed from GT source, verified at
    startup against the live constants (a GT bump that moves one is now a loud log line rather than a
    silently wrong sprite), and kept an explicit allowlist: a generic "any block with a stripped
    getIcon" rule would have skinned every GT machine hull as an LV casing side, since
    `BlockMachines.getIcon` is a vestigial stub returning one constant for every meta.

  Frames additionally needed the meta scan widened: they are keyed by GT material id (up to 1000),
  not by world block metadata, so `gt.blockframes|316` was never probed at all - which is why the
  single most-referenced family in the dump stayed grey. That widening is allowlisted too, after a
  blanket version emitted 876 metas for the coil block, whose accessor answers any meta through its
  `default` arm.

  A fifth mechanism covers the third-party tail: many blocks put the `@SideOnly` on the *resolved*
  `IIcon[]` while the strings that NAME those icons are un-annotated and survive untouched
  (bartworks' and GoodGenerator's `textureNames`, vanilla's `textureName` behind
  `setBlockTextureName`). Reading those recovers bartworks glass, the whole GoodGenerator casing
  family, gtnhlanth and kekztech without any table at all, carrying the per-meta glass tints with
  them. Two traps worth naming, since both look callable and are not: `Block.getTextureName()` *is*
  `@SideOnly` while the `textureName` field behind it is not, and bartworks' un-annotated
  `getColor(int)` dereferences the stripped `IIcon[]` and dies with `NoSuchFieldError` - so both must
  be read as fields, never through their accessors.

  A sixth route covers bartworks' werkstoff material casings, which store neither an icon nor a name:
  their sprite is recomputed from the werkstoff registry plus its texture set. Their metas are
  werkstoff ids running to five digits, so they are enumerated from that registry rather than scanned.
  Deliberately not bug-compatible with GT here: upstream derives the texture-set name in a way that
  yields a nonexistent directory for the nine custom sets, so GT itself renders those as a missing
  texture; reading `mSetName` gives a path that exists.

  Net on the local 208-multiblock dump: unresolved `(block, meta)` pairs 330 to 91, multiblocks
  carrying at least one grey block 177 to 40. Both shipped example lines (sand, nitrobenzene) now
  resolve every constituent block, so the unresolved-block warning is silent on each.
- **Texture gaps name the block that has one (`tools/gtnh-extractor/`, GitHub #98).** A multiblock
  controller whose layer stack resolved empty was dropped from the manifest with *nothing recorded
  under its name*: the flattener files its complaint under the offending `ITexture` class instead, so
  29 controller hulls (Eye of Harmony, Forge of the Gods, the Space Modules) went missing with no
  discoverable reason. They now record a gap keyed by the block. The unresolved-shape gap also
  carries the runtime field state that produced it and is deduped per class, which turned a
  212-entry dead end into two lines that named the root cause directly: `mIconContainer=null`,
  because those icon holders are assigned only inside client-only `registerIcons` methods.
- **An unresolved block face draws Minecraft's missing-texture checkerboard (`previewer/html.py`,
  GitHub #98).** It used to fall back to a neutral casing grey, which was actively misleading: a
  great many GT casings genuinely are plain grey, so a missing sprite was indistinguishable from a
  correctly rendered one and the gap stayed invisible in the very view meant to reveal it. Magenta
  and black, matching the convention every Minecraft player already reads as "no texture here".
- **Multiblock casings: tiered machine casings render, and missing sprites are reported
  (`tools/gtnh-extractor/`, `previewer/`, GitHub #98).** `IIconContainer.getIcon()` is
  `@SideOnly(CLIENT)`, so FML strips it from the interface on a dedicated server: a casing reaching
  its icon via `invokevirtual BlockIcons.getIcon()` resolves, but one going through
  `invokeinterface IIconContainer.getIcon()` dies with `NoSuchMethodError`. That is the whole reason
  `gt.blockcasings` metas 10-15 always rendered while metas 0-9 - the tiered machine casings that
  are most of an ExxonMobil Chemical Plant - were always grey. Those metas are now read straight
  from the `MACHINECASINGS_BOTTOM/TOP/SIDE` arrays that hold them, per side rather than flattened to
  one face. The block scan also no longer pre-filters on `IHasIndexedTexture`, which silently
  dropped 75 registry names a dumped multiblock actually uses. (The families still unreachable at
  that point - `gt.blockcasings8`, the coils, the GT++ casings - are closed by the entry below,
  without needing the client-side route of #78.)
- **Untextured blocks are now loud (`previewer/textures.py`, GitHub #98).** A block with no manifest
  entry renders neutral grey inside an otherwise-expanded multiblock, where it is indistinguishable
  from a deliberately plain casing - nothing surfaced it, since the machine keeps no placeholder
  label. `TextureSummary` gains `unskinned_blocks` and the pass warns with the exact
  `<block>|<meta>` list. The extractor likewise records the two skips that previously `continue`d in
  silence, taking the manifest's own recorded gaps from 1846 to 5370: the shortfall was never
  measured before because most of it was invisible.
- **Distillation Towers are sized to their recipe, not to the maximum
  (`dataset/multiblocks.py`, `adapter/`, `previewer/`, GitHub #98).** A GT Distillation Tower routes
  the recipe's fluid output `i` to structure layer `i` and nowhere else, so a tower shorter than the
  recipe's fluid-output count is a *legal* build that silently voids the remainder. `MachinePhysical`
  now carries every built form with its routable-output capacity and picks the smallest that fits:
  on the nitrobenzene line the Distilled Water tower (1 fluid out) reserves 3x3x3 and the Creosote
  Oil tower (5 out) reserves 3x6x3, where both previously reserved 3x12x3. Selection applies only to
  a family that adds exactly one layer and one routable output per step; anything else (a Mega
  Distillation Tower, whose output layer is a 5-block band, or a pre-v2 dump) falls back to the
  largest form, which over-reserves but can never lose product.
- **Multiblocks resolve by controller block, not just by name (`adapter/`, `dataset/`, `previewer/`,
  `ir/`, GitHub #98).** gtnh-factory-flow names a machine by its localized `RecipeMap`, which for a
  GT++ machine is not the controller block's own name the structure dump is keyed by (`Chemical
  Plant` vs `ExxonMobil Chemical Plant`). Those machines therefore missed the structure lookup
  entirely and silently fell back to a 1x1x1 footprint and a lone cube, even though the dump had
  them. A plan exported after gtnh-factory-flow #25 now carries `recipe.source.machineBlock.id`
  (`"<registry_name>@<meta>"`), which `InputIR.Machine.block_key` plumbs to both consumers:
  `PhysicalDataset.get` and the previewer's doc lookup try the block key first and fall back to the
  name, so the join is exact where the export supports it and every pre-#25 plan behaves exactly as
  before. The Chemical Plant now resolves its real 7x7x7 footprint instead of 1x1x1.
- **Dataset schema v2: `variants[].hatch_slots` (`dataset/schema.py`, `tools/gtnh-extractor/`,
  GitHub #98).** Geometry alone cannot say which cells are I/O slots or what they accept, and for a
  layer-indexed machine that is load-bearing. The extractor now records, per cell, the
  `gregtech.api.enums.HatchElement` kinds it accepts, using StructureLib's element-visit
  instrumentation (`StructureLibAPI.enableInstrument` + `StructureElementVisitedEvent`) during the
  block pass. Two things this replaces: hint metadata is NOT hatch data (it is `dot - 1`, with
  13/14/15 reserved as StructureLib's `AIR`/`NOT_AIR`/`ERROR` - a meta-13 cell is a hollow interior,
  which is why the EBF's coil layers looked like I/O slots), and re-running the block pass without
  `gt_no_hatch` recovers nothing, since GT's hatch element returns an unconditional `false` from
  `placeBlock` and the channel is read only by the survival autobuild path.
- **Previewer: hover a block to see its machine name (`previewer/html.py`).** A textured cube carries
  no readable label (the front-face name plate is drawn only on the flat placeholder box), so once
  every machine is skinned it was hard to tell which is which. Moving the pointer over any block now
  floats that block's machine name above it (a raycast pick, reprojected each frame so it stays glued
  to the block while the camera orbits) and clears when the pointer leaves. Hovering any sub-block of
  an expanded multiblock shows its parent machine's name.
- **Previewer: toggle the auto-output arrows (`previewer/`).** The controls bar gains an
  `arrows: on/off` button that shows or hides the cyan auto-output direction arrows, so a builder can
  declutter the view. When on, the arrows still follow the layer slider; the button disables itself
  for a layout with no auto-output connections.
- **Previewer resolves more single-block machines by name (`previewer/`, GitHub #3).** Building on
  the voltage-tier prefix match, `TextureManifest.mte_block` now also resolves two naming shapes the
  plan's generic names previously missed: tiered-storage families keyed by numeral in the manifest
  (`Super Tank` -> the lowest `Super Tank I`, likewise `Super Chest`), and flavor-prefixed in-game
  names where the generic name is a whole-word suffix (`Chemical Plant` -> `ExxonMobil Chemical
  Plant`, `Coke Oven` -> `Industrial Coke Oven`). On the two example lines this takes single-block
  texture resolution from 8 to 25 machines; only the solver-synthesized Power Sources stay
  placeholders. The new strategies run after the exact/normalized/tier-prefix ones, so nothing that
  resolved before changes, and a genuinely unknown machine still resolves to nothing (kept on its
  placeholder box, never mis-mapped).
- **Real multiblock footprints wired into the solve path (`cli`, `adapter`, GAP A / the overlap
  fix).** `gtnh-solve` now loads the committed physical dataset (`data/multiblocks/`) and passes it
  to the adapter, so a machine whose type the dataset knows (Electric Blast Furnace, Vacuum Freezer)
  reserves its real multi-cell footprint instead of the crude 1x1x1 default that made multiblock
  structures overlap and the previewer render them sparse. The lookup stays a graceful enhancement:
  a missing, unreadable, or empty dataset warns to stderr and falls back to 1x1x1 footprints, so the
  documented 0/1/2 exit-code contract is untouched (a type the dataset lacks, e.g. Forge Hammer or
  the nitrobenzene multiblocks, still gets the single-block default, so those examples are
  unchanged). `_bounding_region` is now footprint-aware: the region height clears the tallest machine
  (a hardcoded 4 made a 10-tall Distillation Tower infeasible before placement even ran) and the
  floor holds the summed footprint areas with routing slack, reproducing the old `side x 4 x side`
  sizing exactly for an all-1x1x1 line. A non-square-base multiblock is pinned to a single
  orientation until `occupied_cells` becomes rotation-aware (the placer, router, and validator share
  that primitive, so a rotated non-cubic footprint would be a shared blind spot); every current
  dataset machine is square-base and keeps all four orientations.
- **Previewer active/idle machine skin toggle (`previewer/`).** The viewer now carries a `state`
  control that swaps every machine between its idle (at-rest) and running skin, so a builder can see
  which faces light up when the line runs (e.g. the Distillation Tower and Large Chemical Reactor
  front overlays in the nitrobenzene preview). The texture pass bakes both states but emits a second
  `data:` URI (`scene.texturesActive`) only for faces whose running bake actually differs from idle
  (an `_ACTIVE` overlay); a plain casing, identical in both states, carries one texture, so the
  embedded page never bloats for faces that look the same. State selection and the byte-level dedup
  live in Python (`texturize_scene`); the viewer only swaps between the two maps the scene hands it,
  and disables the control for a layout where no machine has a distinct running skin. Default display
  stays idle (no behavior change until toggled).
- **Previewer textures generically named single-block machines by voltage tier (`previewer/`,
  GitHub #3).** A plan export names a single-block machine generically ("Forge Hammer"), but the
  schema-2 texture manifest keys every single-block machine by its in-game tier-prefixed name
  ("Basic Forge Hammer" at LV, "Advanced Forge Hammer" at MV), so a generic name never matched and
  the machine (e.g. the sand line's Forge Hammer) rendered as a flat placeholder box even though its
  texture was in the manifest. `TextureManifest.mte_block` now resolves a generic name plus the
  machine's voltage tier: it tries the exact name, then a case/punctuation/whitespace-normalized
  match, then the tier's GT prefix (LV "Basic", MV "Advanced") with a "Basic" fallback for the higher
  tiers whose naming diverges per family ("Advanced X II/III/IV", "Universal", "Elite"). Single-block
  skins are near identical across tiers, so the Basic texture is an honest preview stand-in when the
  exact tier key is absent; a genuinely unknown machine resolves to nothing and keeps its placeholder
  box (never mis-mapped). The scene dict now carries each machine's `voltage_tier` for the texture
  pass to read. The sand preview's three Forge Hammers now render with their real GT texture.
- **Commit the schema-2 layered texture manifest (`data/textures/manifest.json`, ~5.8 MB).** The
  lane 7 v2 previewer reads this manifest to skin machines with real GT textures, but the repo still
  shipped only the old schema-1 (icon-only, 9 casing blocks) manifest, so a fresh clone rendered every
  machine as a placeholder. This is the extractor's `server-itexture-reflection` output (pack 2.8.4,
  GT5-Unofficial 5.09.51.482): 1470 blocks (1395 machine-tile-entities carrying their `display_name`,
  75 plain blocks), 816 icons. The pre-commit large-file guard is scoped to exclude `data/` so the
  dataset can live in the repo (rather than a repo-wide `maxkb` bump). PNGs are still never committed
  (LGPL); the previewer fetches them from the pinned GT5U jar at render time.
- **Previewer real GT textures via per-block cubes and a Pillow bake (`previewer/`, lane 7 v2,
  GitHub #50).** Supersedes the v1 that skinned one stretched box per machine with a single
  representative casing - a defect that erased the coils, glass, and hatch faces that make a layout
  readable. The previewer now materialises each placed machine into ONE textured cube per constituent
  block (principle 6): it looks up the machine's extracted multiblock doc, selects the representative
  variant, expands its `blocks` list at each block's `[dx, dy, dz]` offset (yaw-oriented to the placed
  front, so the controller's front overlay points the way the solver oriented it), and textures every
  cube face independently. Cubes are clamped to the machine's reserved footprint (and a yaw that
  would spill a non-cubic machine past it falls back to native orientation), so one machine's blocks
  can never overlap a neighbour - wall-sharing is a GTNH feature left to a later change. Only a
  genuine 1x1x1 machine takes the single-block path (via the manifest's display-name index); a
  doc-less MULTIblock, such as the dynamic-height Distillation Tower whose extraction overflowed the
  variant cap, keeps its placeholder box rather than collapsing to a lone controller cube. A new Pillow
  pre-bake (`previewer/bake.py`) composites each face's layer stack - base times its RGBA multiply,
  then alpha-composited overlays, animated sprites reduced to frame 0 - into one flat 16x16 PNG per
  `(block, meta, side, state)`, so the three.js viewer only ever loads flat images and never
  composites at runtime; a face with no baked texture falls back to a neutral casing grey, an
  un-baked machine keeps its placeholder box. Pillow is the optional `preview` extra and its absence
  degrades the whole pass to placeholders. Golden tests pin the tint multiply (a machine base is never
  neutral grey), the per-block expansion (a multiblock is many distinct cubes, not one box; an
  interior coil textures distinct from the casing), and icon-name stability. PNGs stay LGPL and
  uncommitted, fetched from the pinned jar into an out-of-repo cache (`previewer/jar.py`, injected so
  the test suite never fetches).
- **`LayoutMetrics` footprint/layers are now populated (`solver`, GitHub #13).** `solve()` fills
  `LayoutResult.metrics.footprint` (floor-area bounding box of machines plus routes) and `.layers`
  (vertical extent) on every assembled layout, computed from the same occupied-cell basis the
  feedback loop ranks on. These are consumed as data (the seed-compare workflow, and the previewer
  embeds them in its scene JSON); previously they were always `null`. `buildability` and
  `congestion` stay `None` until a scoring model is defined; an infeasible (nothing-placed) result
  leaves all metrics `None`.
- **Adapter consumes the plan-schema-v2 `resolved` block (`adapter/`, GitHub #2).** A
  gtnh-factory-flow v2 export (`schemaVersion: 2`) additively carries `app`,
  `datasetVersionId`, and a `resolved` throughput block (per-machine EU/t, per-edge rates,
  external I/O, a power total); `adapter/plan.py` now parses all of it typed (unknown
  subfields stay tolerated). When `resolved` covers a node, its `totalEut` is trusted for
  `Machine.eut` - the exporter's balancer models overclocking, which `recipe.eut * parallel`
  cannot - and cross-checked against that synthesis: a divergence beyond float tolerance
  emits an `AdapterWarning` (new, exported) but the resolved figure wins, so power amperage
  is sized for the real draw. `resolved.power.totalEut` is likewise cross-checked against the
  synthesized per-tier power nets. v1 plans (no `resolved`) adapt exactly as before.
  `examples/gtnh-sand.json` is refreshed to the v2 export (adapter output unchanged - its
  resolved figures match the synthesis); the v2 nitrobenzene export ships as
  `tests/fixtures/gtnh-nitrobenzene-v2.json` instead of replacing the example, because its
  resolved EU/t legitimately diverges (overclocked LCR: 2880 vs 480 EU/t) and would shift the
  example-pinned power numbers.
- **Extractor channel handling and identity-substitution tables (`tools/gtnh-extractor/`, lane 3,
  GitHub #46).** `StructureDumper` now fills the per-controller `substitutions` object. After the
  trigger-stack sweep it probes each GT channel (`GTStructureChannels.values()`, skipping the
  always-applied `gt_no_hatch`) against the default build: holding the stack size at 1 it sets one
  channel at a time and diffs the placed blocks. Because an unset StructureLib channel reads the
  trigger's stack size, the existing stack sweep already varies every channel, so shape-changing
  channels (a distillation tower's `height`, a structure's `length`) are already recorded as size
  variants and the probe skips them; a channel that only swaps a tiered block (coil, glass, pipe
  casing) keeps the same shape and is recorded once as `substitutions[channel]` = the default tier
  plus every distinct higher tier `{channel_value, block, meta}`, rather than exploding into one
  variant per tier. The default-placed block is always included, which is what lets the Python
  adapter match the tiered blocks in the primary variant. Heating coils are a special case: the
  classic furnaces (Electric Blast Furnace, Multi Smelter, ...) place a bare `ofCoil` whose tier is
  read from the trigger's stack size rather than the `coil` channel, so the coil table is built by a
  separate stack-size sweep that identifies coil blocks by the GT `IHeatingCoil` interface (which
  also covers the channel-bound mega furnaces). New hard caps bound the per-channel value sweep and
  the total substitution entries; a controller that overflows them lands on the `_meta.json` failure
  list instead of emitting a runaway table. The Electric Blast Furnace stays one 3x3x4 shape variant
  and now carries a populated `coil` substitution table (14 tiers), so the adapter counts 2 coil
  layers.
- **Layered server-side `ITexture` texture manifest (`tools/gtnh-extractor/`, lane 6 v2, GitHub #79;
  spike #78).** Supersedes v1's flat single-icon Option A, which could only name casing shells and
  gapped every single-block machine and controller hull. A one-day spike (#78) first proved, against a
  booted GT5-Unofficial server, that the 6-arg `getTexture(...)` is not `@SideOnly(CLIENT)` and the
  `ITexture` layer objects store plain server-safe fields (`mIconContainer`, `mRGBa` via `getRGBA()`,
  `glow`, wrapper `mTextures`). The rewritten `TextureDumper` then emits schema-2 layered manifest
  entries: for every MetaTileEntity - via the `getXxxFacing{Inactive,Active}(byte)` accessors for
  basic single-block machines (reliable with no tile entity; `getTexture` NPEs on a bare placement for
  some) and via `getTexture(base, side, facing, colour, active, redstone)` for hulls and hatches
  (placed like `StructureDumper` does) - it walks the layer stack per side and active state, resolving
  each `GTRenderedTexture` to `{icon, rgba, glow}`, recursing multi/sided wrappers via `mTextures`, and
  resolving a hull's copied casing base through the block-icon path. The plain structure blocks a
  multiblock places (casings, coils) keep the v1 block-icon mechanism as un-tinted single layers.
  Icon names come from the `Textures.BlockIcons` enum `name()` (the client-only `getTextureFile()`
  throws server-side, as the spike confirmed), mapping 1:1 to the PNGs under
  `assets/<modid>/textures/blocks/`. Each MTE entry carries its `display_name` so the previewer can
  render single-block machines. PNGs are never committed; unresolved units (exotic ISBRH renderers,
  a tail of newer casing families) land on the manifest `gaps` for a follow-up. Wired additively via
  `-PtextureOut`: set alone the run is texture-only and skips the structure dump.
- **Extractor core dump loop (`tools/gtnh-extractor/`, lane 2, GitHub #45).** The Java tool now
  fills its `DumperMod.dump()` seam with `StructureDumper` + `JsonWriter` + `ErrorCollector` and
  emits the schema-v1 dataset. It iterates `GregTechAPI.METATILEENTITIES`, keeps the
  `IConstructable` controllers, and for each places it at a fixed origin in the server overworld,
  sweeps the trigger stack (size 1..N, stopping when the placed cell set stops changing so
  identity-only tier swaps collapse into one variant), and per size runs a hint pass
  (`construct(_, hintsOnly=true)` with a `RecordingProxy` swapped into `StructureLib.proxy`, and the
  world's `isRemote` flag briefly flipped since the hint walk is client-only) plus a block pass
  (`construct(_, hintsOnly=false)` with the `gt_no_hatch` channel, then scan). It writes one
  `<datasetOut>/multiblocks/<name>.json` per controller plus a `_meta.json` run summary (schema,
  pack version, mod versions, timestamp, extractor SHA, controller count, failures), with stable
  key + variant ordering. Every controller is wrapped so an exception, a non-terminating/explosive
  sweep, or an empty scan lands in `_meta.json.failures` rather than aborting the run; hint capture
  is best-effort so a controller with client-only icon hints still dumps its geometry. The output
  directory and run metadata come from `-PdatasetOut`/`-PpackVersion`/`-PextractorSha`. A verified
  headless `runServer` boot dumps 191 of 209 constructable controllers (Electric Blast Furnace
  3x3x4, Vacuum Freezer 3x3x3), all validating against `dataset/schema.py`. Channel handling /
  identity-substitution tables (`substitutions` stays empty) are lane 3; textures are lane 6.
- **Multiblock physical dataset - schema v1 + Python adapter (`dataset/`, GitHub #48).** The
  first slice of the automated dataset-extraction pipeline (`DATASET_EXTRACTION_PLAN.md`): the
  path from an extractor's raw JSON to the solver's physical rules. `dataset/schema.py` is a typed,
  `extra="forbid"` Pydantic loader for schema v1 (`MultiblockDoc` + `_meta.json` `DatasetMeta`,
  per plan section 4.2: `schema`, `controller`, `variants[blocks/hints/bbox]`, `substitutions`,
  `failures`), the cross-language contract for the future Java extractor (issue #45), with a
  derived JSON Schema (`multiblock_json_schema()`) for non-Python consumers so it cannot drift.
  `dataset/multiblocks.py` is the adapter that does **all interpretation in Python** (plan design
  principle 3): it derives each machine's footprint bounding box, hint-derived I/O faces, and
  coil-tier count from the raw facts into an IR-shaped `MachinePhysical`, and `load_physical_dataset`
  keys a whole dump by display name. Because the real extractor is not built yet, illustrative
  hand-authored fixtures ship under `data/multiblocks/` (Electric Blast Furnace, Vacuum Freezer)
  marked as such in a README, so the adapter and golden tests run today. Golden tests pin the
  ground truths (EBF is 3x3x4 with two coil layers and hatch-layer hints; Vacuum Freezer is 3x3x3)
  plus schema validation (every file validates, `_meta.json` failure list under a lenient
  threshold). Wired **opt-in** into the gtnh-factory-flow adapter: `to_input_ir(plan, physical=...)`
  stamps a known machine's real footprint on the `InputIR`, while the default path stays single-block
  so the solver runs with or without a dump. No IR contract change (additive keyword-only argument).
- **Automated dataset-update CI (`.github/workflows/update-dataset.yml`, lane 4)** - a
  weekly + manual workflow that tracks the latest *stable* GTNH pack: it resolves the pack
  version from the DreamAssemblerXXL manifests, diffs the pinned mod versions against
  `gtnh.lock.json` (exiting green with no PR when unchanged), and on a change bumps the
  extractor pins, runs the headless Forge dump, installs the dataset, re-locks, runs the
  full test suite, and opens a reviewable PR whose summary surfaces the controller-count
  delta, added/removed/changed multiblocks, and the extractor failure list. Never
  auto-merges. Backed by a typed, tested CI helper (`tools/dataset_ci/`) and a dataset-diff
  review checklist (`.github/PULL_REQUEST_TEMPLATE/dataset-update.md`).
- **Dataset extractor scaffold (`tools/gtnh-extractor/`, the repo's only Java)** - lane 1 of
  the automated multiblock-dataset pipeline (GitHub #44). A standalone GTNH
  `ExampleMod1.7.10`-based Gradle tool whose `DumperMod` (`@Mod` entrypoint) hooks
  `FMLServerStartedEvent`, runs an empty dump body, and calls `FMLCommonHandler.exitJava`
  (0 on success, nonzero on failure) so `./gradlew runServer` boots a headless dedicated
  1.7.10 server with GT5-Unofficial + StructureLib and exits as a pass/fail gate. GT5U and
  StructureLib are pinned in `dependencies.gradle` from the current stable pack manifest
  (2.8.4) and mirrored in the new repo-root `gtnh.lock.json`; the rest of GT5U's hard deps
  resolve transitively from its Nexus POM. The Python solver gains no dependency on the tool
  (it will read only the JSON the tool emits; the dump loop itself is lane 2). `NOTICE` now
  credits the two LGPL mods.
- Project scaffold: docs, package skeleton, CI, license.
- Design and architecture documentation ported from the office-hours design doc
  and the engineering review (see `docs/`).
- **IR contracts (`ir/`)** - the two versioned Pydantic v2 schemas everything couples
  to: `InputIR` (the problem) and `LayoutResult` (the solution), with shared cell-grid
  geometry and enums. The input IR enforces referential integrity; geometric/rule checks
  are left to the validator. Full test suite (example + hypothesis). `docs/IR.md` updated
  to match the implemented shape.
- **Validator (`validator/`)** - the automated correctness gate: `validate(problem, layout)`
  independently checks a layout's geometry + structure (machines in-bounds / non-overlapping /
  off reserved cells / legally oriented / fully placed; nets routed once, contiguous,
  in-bounds, ME-toggles honored; pinned I/O on-route; power thickness well-formed) and
  returns a `ValidationReport` of every proven violation - never raises, never passes a
  silently-invalid layout. Rule-data checks (tier caps, summed amperage, face reachability)
  are stubbed for the dataset lane. In-code golden corpus (one known-bad case per violation).
- **Placement (`placement/`) - Phase 1 crude placer** - `place(problem)` does deterministic
  first-fit constructive placement on the cell grid (floor layer first, honoring reserved
  cells and never overlapping; orientation = first legal option), returning a
  `PlacementResult` that is either every machine placed or an explicit `Infeasibility` naming
  what did not fit (never raises). The validator independently certifies the output. Shared
  cell-grid helpers (`occupied_cells`, `in_region`) lifted into `ir/geometry.py`. Property
  test proves the core promise: any input yields a valid placement or an explicit
  infeasibility. SA/LNS placement is Phase 2 (see `docs/ROADMAP.md`).
- **Adapter (`adapter/`)** - `adapt_file(path)` / `to_input_ir(plan)` map a gtnh-factory-flow
  exported plan JSON to `InputIR`: nodes -> machines (recipe I/O -> item/fluid ports, computed
  typed throughput), storages -> boundary **Super Chest** (items) / **Super Tank** (fluids)
  machines (blocks that take I/O covers, so covers ride machine/storage faces, never pipes),
  edges -> nets. Typed view of the consumed export shape (`plan.py`, tolerant of extra fields).
  Two real exports committed as fixtures in `examples/` (sand, nitrobenzene). Crude for Phase 1:
  single-block footprints, default orientations, power nets not synthesized yet.
- **Router (`router/`) - Phase 1 crude router** - `route(problem, placements)` resolves a
  `Terminal` per net endpoint on a usable (non-front) machine face, then A* between terminals
  over the free cell grid, returning routes or an explicit `Infeasibility`. The sand demo line
  now goes **export -> place -> route -> validator.ok**, the whole Phase 1 slice end to end.
  Crude: one channel, no capacity, item/fluid only. Added `Route.terminals: list[Terminal]` to
  the output schema (additive); the validator gained the route<->endpoint **reachability check**
  (terminal on a non-front face adjacent to its machine and on the route). Machine `orientation`
  is now constrained to horizontal facings (GT machines never face up/down).
- **Build guide (`buildguide/`)** - `build_guide(problem, layout)` renders a `LayoutResult` as
  a human-readable text guide: header, bill of materials (machines by type, pipe/cable cells
  per commodity, I/O cover count), per-net connections (resource + machine faces), and a
  per-layer ASCII map with a key. The cheap, visible Phase 1 payoff - a player can read and
  build the sand line from it - ahead of the three.js previewer.
- **Solver (`solver/`) + auto-output** - `solve(problem)` composes the pipeline: place (now in
  **flow order** - a topological sort so producers land next to consumers) -> assign
  **auto-output connections** (a source machine ejecting straight into an adjacent target's
  input face: no pipe, no cover, GT's free connection - one auto-output per machine) -> route
  pipes only for what auto-output can't cover -> assemble. The sand line now solves to a flat
  row of 4 machines auto-feeding each other: **zero pipes, zero covers**. Added
  `LayoutResult.auto_connections: list[AutoConnection]` (additive); a net is satisfied by a
  `Route` XOR an `AutoConnection`, and the validator checks auto-connection adjacency / faces /
  single-auto-output-per-machine. Power nets are still not synthesized (the export has no power
  source) - a shared-amperage power model with optimized source count/placement is next.

- **Contributor standards & tooling** - documented coding + Conventional-Commits
  conventions in `CONTRIBUTING.md`; added a `.pre-commit-config.yaml` (ruff lint + format,
  `mypy --strict`, file hygiene, commit-msg lint), a PR template, and bug/feature issue
  templates.

- **Power (shared-amperage net) - synthesis + routing.** The export carries each machine's
  `eut` + voltage tier but no power source, so the adapter now synthesizes the power network:
  one synthetic source machine + one shared-amperage power net per voltage tier feeding the
  powered machines (`adapter/power.py`). The new power router (`router/power.py`) routes each
  per-tier net as a cable trunk and sizes every segment to the **summed amperage of the machines
  downstream of it** (1x/2x/4x/8x/16x), rejecting a load over the 16x cap as an explicit
  infeasibility - correctness-first single-source-per-tier (multi-source / voltage-loss
  optimization is Phase 2). `solve()` runs it alongside the item/fluid router, and placement no
  longer lets a power source split an auto-feeding material chain. The build guide gains a
  **Power** section telling the builder where to feed external power (synthetic sources are not
  self-powered). Backing it: a new `dataset` voltage ladder + `amperage` helper, `Machine.eut`
  (additive, InputIR v1), and shared router grid/dock/A* primitives lifted into `router/_grid.py`
  (the generic router no longer touches power). See `docs/DOMAIN.md`, `docs/ARCHITECTURE.md` #8.

- **CLI (`gtnh-solve`)** - the first real Phase 1 entry point: `gtnh-solve <export.json>` loads +
  adapts the export, solves (place -> auto-output -> item/fluid + power route -> self-validate),
  and prints the build guide (`-o FILE` to write it, `--seed` to pick the seed). Exit code 0 when
  the layout is fully VALID, 1 when the solver returns an explicit infeasibility (printed to
  stderr), 2 when the export can't be loaded. Replaces the planning-stub entry point.

- **Placement optimizer (`placement/search.py`) - Phase 2 simulated annealing.**
  `optimize_placement(problem, *, seed)` seeds from the constructive first-fit placer and
  improves a **routing-aware cost** (per-net half-perimeter wirelength + compactness + flat-build
  bias) with relocate / swap / **reorient** moves (orientation is a search variable), Metropolis
  acceptance, geometric cooling, best-valid-so-far. `solve()` now uses it (the crude placer stays
  as the SA seed + a fallback). Every accepted state stays validator-clean; deterministic per
  seed. Connected machines cluster - a hub+4-spoke star drops from HPWL 10 (first-fit row) to 5
  (annealed cluster), and sand stays all-auto-output. LNS + the place<->route feedback loop are
  next (docs/ROADMAP.md lane C).

- **Placement LNS (`placement/search.py`) - large-neighbourhood ruin-and-recreate.** The optimizer
  gains a large move alongside relocate / swap / reorient: rip out a *related* (net-connected)
  cluster of machines and greedily re-insert each at the position + orientation that minimises the
  cost, biased toward cells beside its already-placed net-neighbours. One step reshapes a whole
  cluster, escaping local optima the single-cell moves plateau in. It is probability-gated inside
  the same annealing loop, so Metropolis acceptance / cooling / best-so-far and per-seed
  determinism are unchanged, and every candidate stays validator-clean (recreate validity-checks
  each insertion and abandons the move if a machine cannot be re-placed). Insertions are ranked by a
  cheap marginal cost (the machine's own nets + auto pairs + a flat-build bias), not a full recompute,
  so LNS fits the same budget as the small moves. Finishes the SA + LNS half of ROADMAP lane C.
  Because the cost is still HPWL-driven, tighter clustering can push a route onto a second layer (the
  sand demo's power cable now rises one layer, still valid) - the future congestion-aware cost
  (lane C) is what removes that. (`placement/`.)

- **Solver "optimize or not" toggle (`solve(..., optimize=...)`, `gtnh-solve --fast`).** `solve`
  gains an `optimize` flag. The default (`True`) runs the annealed placer (SA + LNS) inside the
  place<->route feedback loop; `False` takes a near-instant single constructive placement, with no
  annealing and no re-placement. Both validate their output (VALID / explicit partial /
  infeasibility, never silently invalid) - fast just trades the optimizer's clustering and
  unrouted-net recovery for speed. `--fast` exposes it on the CLI. This is the user-facing control
  the planned unified site is built around, and the home for LNS (opt-in behind the optimized
  path). (`solver/`, `cli/`.)

- **Previewer (`previewer/`) + `gtnh-solve --preview`** - a self-contained, double-clickable 3D
  view of a solved layout. `build_scene(problem, layout)` flattens the layout into a render-ready
  scene (machine boxes coloured by type with the machine name on the front face, rectangular
  cables/pipes sized by cable thickness with a lead to each machine face, auto-output arrows,
  legend, and a tight `bounds` of the built extent) - a pure, fully-tested mapping; `render_html`
  inlines it into a static three.js viewer (CDN, no npm build) with an **orbit + pan camera**
  (right-drag / arrow keys) and a **layer-by-layer slider**, framed on the built extent rather
  than the solver's oversized search region. `gtnh-solve plan.json --preview view.html` writes it.
  Build-assist scope; the congestion heatmap, multi-seed compare, real block textures, and offline
  (vendored three.js) are follow-ups.

- **Routing capacity invariant (lane D, first slice).** Routes are now laid **capacity-aware**:
  each laid route's cells become obstacles for the routes after it - across item/fluid (`route`)
  and power (`route_power` gains an `extra_obstacles` arg the solver feeds the item cells into) -
  so no cell ever carries two routes (the crude single-channel cap: one route per cell). The
  validator independently enforces it (`route_cell_collision`), closing the gap where item pipes
  and power cables could share a cell and the abstraction would certify an unbuildable layout
  (docs/ARCHITECTURE.md #7). Crude for now: one channel per cell; the per-edge multi-channel cap
  (a routing margin hosting several parallel channels) is a later lane-D slice.

- **Rip-up/reroute (lane D, second slice).** Capacity makes routing order-dependent - a net that
  grabs a scarce cell can wedge a later net out (a *false* infeasibility, not a real one). The
  item/fluid router now routes a pass, and if any net failed, rips everything up and retries with
  the failed nets moved to the front (most-constrained-first), stopping when a pass is clean or a
  failed-net set repeats (a genuine infeasibility, not an ordering accident). So a bad net order is
  no longer mistaken for unroutable. Crude failed-first reordering; negotiated-congestion routing
  (the gold-standard, order-independent approach) is tracked as a follow-up (GitHub #7).

- **Build guide is buildable from alone.** The text guide was a sketch; it now carries the detail a
  player needs to build the line without guessing: a **Placement** table (each machine's exact
  `(x, y, z)` cell, front face, and footprint), per-pipe-terminal **covers** (conveyor for items,
  pump for fluids, in input/output mode - docs/DOMAIN.md), the exact **cells** each pipe/cable runs
  along, and **per-segment cable thickness** for power. The Power note now states the amperage to
  feed each source (its trunk-root thickness) instead of pointing at the ASCII map that never
  showed it.

- **Place↔route feedback loop in `solve()`** (docs/ARCHITECTURE.md #1, #6). `solve()` no longer
  takes a single placement on faith: it assembles an attempt (place → auto-output → route →
  validate), and if the router leaves nets unrouted it **penalizes exactly those nets** so the next
  placement pulls their machines tighter (shorter routes, or adjacency that auto-outputs) and
  re-places with the next seed. It keeps the best layout seen and returns the first fully-VALID one
  (anytime: best-so-far), stopping early when re-placing cannot help - a non-routing defect, or the
  same nets failing again. A layout a single attempt leaves `partial_invalid` (one net it could not
  pipe in a congested placement) now solves VALID. Deterministic (bounded attempts keyed off `seed`
  + the accumulated penalties, no wall-clock). The routers gained `failed_nets` (which nets stalled)
  and `optimize_placement` a `net_penalties` weight to carry the signal. Crude feedback (penalize +
  re-seed); a richer incremental routing estimate inside the SA move is future work.

- **Build guide states the boundary + a real power-feed spec (GitHub #15).** Two gaps that made
  the "buildable from alone" guide actually need guesswork are closed, both from data already in the
  IR. A new **System inputs / outputs** section names what to load each boundary input storage with
  (resource + typed rate, e.g. `load Super Chest at (0, 0, 0) with minecraft:stone (~0.1 items/t)`)
  and where each finished product exits with nothing collecting it (`minecraft:sand exits Forge
  Hammer at (3, 0, 0) - place a Super Chest/Tank to collect it`) - boundary storages that only
  source, and machine output ports no net consumes. And the **Power** note now reads as a wiring
  spec - `feed LV (32 V), >=4 A -> up to 128 EU/t` (tier voltage from the `dataset` ladder × the
  trunk-root amperage) - instead of the bare cable thickness (`4x amperage`) it printed before.

- **Previewer shows the system's inputs, outputs, and power (GitHub #5).** The 3D preview now
  surfaces the same boundary the text guide does: a **System I/O** panel in the HUD lists the
  inputs to load (resource + rate), the products to collect, the total EU/t draw, and the summed
  **amperage per voltage tier** (the tier already implies the volts, so amps is the useful number,
  e.g. `LV 3A`). A toggle switches every rate between **per tick and per second**. Both surfaces
  read from one new shared, fully-tested helper - `system_io(problem, layout)`
  (`gtnh_solver/system_io.py`) - so the guide and previewer can never disagree on what crosses the
  line's edge; `build_scene` emits it as `scene.io` and the build guide was refactored onto the
  same helper (its text output is unchanged).

- **Boundary output rates in the previewer (GitHub #16).** A finished product exits a machine
  output port that no net consumes, so its rate lived nowhere - the previewer showed the product
  with no throughput. The adapter now records each port's rate from the recipe on a new additive
  `Port.rate` (items/t or mB/t, InputIR v2, no version bump), and `system_io` reads it, so the HUD
  shows e.g. `out: minecraft:sand (0.1 items/t)`. Input rates are unchanged (still the net's typed
  throughput).

- **The adapter closes the line: output buffers (GitHub #16).** A system output used to exit a
  machine into thin air (only inputs got a boundary storage), so a line was never fully collectible
  without hand-editing. The adapter now synthesizes a **Super Chest/Tank + net per unconsumed
  output** (a machine OUTPUT port no net sources), placed and wired at the port's recorded rate, so
  the product is gathered automatically - the sand line now auto-outputs its sand into a collection
  chest (still zero pipes). `system_io` reports a boundary storage that only *sinks* as a system
  output (mirroring the only-*sources* input), and the build guide reads `minecraft:sand collected
  by Super Chest at (x, y, z) (~0.1 items/t)`. Sand grows from 5 machines to 6, nitrobenzene from
  21 to 23.

- **Power sources reserve a boundary feed face.** A synthesized power source is fed by the builder
  from outside the structure, but nothing said *where* - it was placed like any machine, so the
  optimizer could bury it mid-region with no face left for the external feed. Its **front face is
  now the reserved feed entry**: constructive and SA/LNS placement pin that face flush on the
  region boundary (every move preserves it; a problem with no such slot is an explicit
  `power_feed` infeasibility), and the validator enforces the same rule independently (new
  `POWER_FEED_NOT_ON_BOUNDARY`). Internal cables keep using the other five faces - the existing
  front-face rule already keeps them off the feed face. New shared helpers:
  `Machine.is_power_source` (the buildguide's private predicate, promoted) and
  `ir.geometry.front_on_boundary`.

- **The optimizer now finds compact, low-wire layouts (the hand-built sand target).** Two
  coordinated changes (docs/ROADMAP.md lane C). The placement cost is **footprint-first**: the
  compactness driver is now the floor area (x-span times z-span, weight 1.0), so stacking a layer
  is free while sprawling costs, with the bounding-box volume kept as a mild tiebreak; power nets
  lost their base wirelength term entirely (center-distance proxies cannot see dock faces or
  shared cable taps and measurably steered AWAY from low-cable layouts) and instead gain an MST
  trunk-length pull only when feedback-penalized, to rescue a power net the router failed. The
  real cable cost is judged where it is knowable: the solver's **feedback loop is now
  quality-driven** - every bounded attempt is fully routed + validated and the best VALID layout
  by (structure footprint, power cable cells, structure volume) wins, instead of returning the
  first valid one. Optimized sand now solves to a 5x1x2 stack - the machine row with the source
  on top and a **3-cell cable trunk** tapped through the hammers' top faces - matching the
  maintainer's hand-built 3-cable solution with a smaller footprint (5 vs 6) and volume (10 vs
  12). Acceptance is pinned by a solver test.

- **Selectable compactness objective** - `solve(..., objective="footprint" | "volume" |
  "balanced")` and `gtnh-solve --objective`. "Compact" is ambiguous and the two metrics pull
  opposite ways (stacking a layer shrinks the floor but can grow the enclosing box), so the
  builder picks: `footprint` (default, the maintainer's target) minimizes floor area and stacks
  tall, `volume` minimizes the enclosing box and stays flat/cubic, `balanced` weighs both. The
  objective drives both the placement cost's compactness weights and the feedback loop's quality
  ranking of routed layouts; the fast path ignores it (constructive placement is floor-first by
  construction). This is the future unified site's second user control, next to optimize-or-not.
  Sand passes the hand-built compactness + <= 3-cable budget under every objective.

### Changed
- **Cell geometry is rotation-aware, and the validator no longer shares it (`ir/`, `validator/`,
  `placement/`, `router/`, `previewer/`, `adapter/`).** `occupied_cells` turns a footprint about the
  vertical axis, so a non-square-base multiblock finally reserves the cells it actually covers, and
  the adapter's pin holding those machines to a single orientation is gone (81 of the 208 dumped
  controllers were affected). `orientation` is a **required** argument rather than a defaulted one:
  the primitive is shared by placement, the router and the validator, so a caller that forgot to
  rotate would be wrong identically on both sides, and requiring it makes every such caller a type
  error instead of a silent one.

  The validator now expands cells **independently** (`validator/_geometry.body_cells`), written from
  the dump's stated facing convention rather than derived from the solver's code; it used to
  re-export `ir.geometry.occupied_cells`, which was harmless only while that function could not
  rotate (docs/ARCHITECTURE.md #4). What stays shared is data, not derivation: `FACE_DELTAS` and
  `OPPOSITE_FACE` are six unit vectors and six pairs, on the same reasoning the amperage check
  already shares `tier_voltage` and `CABLE_LOSS_PER_BLOCK` with the router. A property test asserts
  the two expansions agree everywhere, and an oracle test re-derives every controller's cell set
  from the raw dump offsets at all four facings (208 of 208 pass locally; the committed fixtures
  cover two).

  Four sites needed fixing that no type error would have found. `_reorient` performed no geometry
  check at all, so a turn could overlap a neighbour or leave the region and still be accepted;
  `_apply_occupied_delta` keyed its diff on the cell, which a reorient does not change even though
  it moves every cell a non-cubic machine covers; `_rand_origin` bounded the random origin with the
  unrotated extents, both rejecting origins that fit and offering origins that do not; and
  `_free_origins` decided whether an origin was free without knowing the orientation. The reserved
  box the previewer clamps against is now rotated too, which retires the yaw-spill fall-back that
  used to draw a turned machine unturned.

  Neither shipped example moved: both are entirely square-base, and the orientation-before-cells
  reordering was done so the RNG draw sequence is unchanged, so every pinned layout, metric and
  cable count is exactly as it was. A sand solve is 0.77s against 0.87s before the lane.
- **The committed dataset is now just small fixtures; full datasets are local and version-namespaced
  (`dataset`, `previewer`).** The extractor's outputs are regenerated on demand into gitignored
  per-version folders (`data/<version>/{multiblocks,textures}/`), so several pack versions coexist
  without overwriting. The repo ships only the two multiblock fixtures and a ~120 KB texture manifest
  scoped to the example lines' machines (down from ~6 MB), so `gtnh-solve --preview examples/*.json`
  still skins out of the box; the full manifest is now local. The loader resolves the newest local
  `data/<version>/` that provides each of multiblocks/textures, else the committed fixtures, with
  `gtnh-solve --dataset-version <v>` to pin one and `--list-dataset-versions` to list them; the jar
  for texture PNGs is fetched at the GT5-Unofficial version the resolved manifest records, so its
  icons match. Reverses the earlier "texture manifest is committed" policy.
- **Previewer wire->machine leads take the connecting cable's thickness (GitHub #6).** Each route
  terminal in the scene now carries the thickness of the fattest route segment incident to its
  cell (a mid-trunk tap touches several; the fattest is what visually meets the block), and the
  viewer sizes the short lead from the cable into the docked machine face with the same
  thickness->cross-section ramp as the trunk segments - so a 4x run meets its machine visibly fat
  and a 1x tap thin. Item/fluid terminals carry `null` and keep their fixed-size pipe leads.
  Previewer-internal (scene + viewer template): an additive scene field the template reads with a
  fallback, so no scene-version bump. (`previewer/`.)
- **The item/fluid router negotiates congestion instead of retrying orders (GitHub #7).** Laying
  nets sequentially (each net's cells hard-blocking the next) made the result hostage to net
  order; the failed-first reorder retry only reduced that. The router now runs the FPGA
  PathFinder scheme: every net routes independently with priced A* (a contested cell costs a
  present-sharing penalty per other user plus a history penalty that grows every round it stays
  contested), and all nets re-route round by round until no cell is shared - so an
  ordering-induced false infeasibility cannot happen, and what remains contested after the round
  budget is reported per net as an explicit `congestion` infeasibility (a maximal collision-free
  subset is still emitted for the feedback loop). Once the contested set stops changing, a
  geometric proof (a bottleneck cell that two nets both cannot route around) ends the negotiation
  early instead of grinding the whole round budget, so a genuine single-bottleneck congestion is
  rejected in a few rounds rather than 32; the proof only ever bails on a demonstrated collision,
  so a resolvable contention is never misreported. Power trunks keep the
  failed-first rip-up/reroute (trees grown by multi-goal A* do not decompose into per-cell
  pricing). (`router/core.py`, `router/_grid.py`.)
- **The test gate runs in a quarter of the time (GitHub #74).** Profiling showed ~3/4 of every
  CI test leg was coverage tracer overhead, not test work (the solver's hot loops execute
  millions of traced line events). The suite now runs parallel by default (`pytest-xdist`,
  `-n auto` in addopts - the local gate drops ~155s to ~60s), CI gates coverage on ONE matrix
  leg instead of every leg, and that leg uses coverage's `sys.monitoring` core
  (`COVERAGE_CORE=sysmon`, branch-capable on 3.14+). No test dropped; the 90% gate and the
  required `test` status check are unchanged.
- **CI tests Python 3.14; packaging metadata reflects real support.** The test matrix now runs
  the floor and the latest release only (`3.10` + `3.14`; a floor break or a new-release break
  is what a leg catches, and the 3.11-3.13 intermediates cannot fail while both ends pass), and
  the package gains per-version trove classifiers
  (`Programming Language :: Python :: 3.10` through `3.14`) and moves from
  `Development Status :: 1 - Planning` to `3 - Alpha`. Internal CI/build polish along with it:
  pip caching, least-privilege `permissions`, cancel-superseded-runs `concurrency`, a
  `hatchling>=1.26` build pin, and a Dependabot config (GitHub Actions + pip, weekly).
- **The router now owns the auto-output vs pipe decision.** `route()` decides itself, from the
  final placements + orientations, which nets GT's free auto-output connection covers (the logic
  moved from `solver/core.py` to `router/auto.py`, public `assign_auto_outputs`) and lays pipes
  only for the rest; `RouteResult` gains `auto_connections` so the decision rides the router's
  output, and the solver's assemble step just composes it (its `skip_nets` plumbing is gone).
  Behavior is unchanged - same greedy net order, one auto-output per source machine, only
  1-source-1-sink item/fluid nets are eligible, power/ME never auto-feed - and the validator's
  independent auto-output checks stay the gate. This advances lane D (docs/ROADMAP.md): the
  router is the geometry authority, so the optimizer's job shrinks to moving blocks and choosing
  front faces. (`router/`, `solver/`.)
- **Power trunks grow as trees with shared taps.** The power router chained every net
  source -> m0 -> m1 -> ... as a path and docked each terminal on its own distinct cell, so a
  source + N sinks always cost at least N+1 cable cells - geometrically unable to reach the
  hand-built 3-cable sand trunk. In GT one cable block feeds every adjacent wired machine face,
  so the trunk is now a tree: a sink whose dock candidate is already a trunk cell of its net
  taps it (terminal on that cell, no new cable; the cell nearest the source wins), and any other
  sink extends the tree with a multi-goal A* leg from every trunk cell laid so far. Sizing
  follows the tree - each machine's cable distance is its terminal's depth, and every segment
  carries the summed amperage of the sink terminals on its far-from-root side (replacing the
  per-leg suffix sum, which overcharged one side of a branch) - and the validator already
  re-derives branched trees and shared terminal cells independently. A source + three clustered
  sinks now trunk with two cable cells, within the sand target's three. (`router/power.py`.)
- **Power cables dock route-aware, on whichever face is nearest the trunk.** The power router
  (`router/power.py`) used to commit each terminal to the first free non-front face in a fixed
  order (south first), blind to where the cable then had to run, so a source behind a machine row
  made the trunk snake around it. It now considers every usable (non-front) face and docks via a
  multi-goal A* leg on the one that gives the shortest cable (new `_grid.dock_candidates` +
  `astar_multi`), the source docking toward its first sink. On the sand demo this drops the
  optimized power run from nine cables to five (matching the constructive baseline); every terminal
  is still validated (non-front, adjacent, on-route) and the trunk stays a single tree.
- **Optimized placement minimises total volume, with no separate per-layer penalty.** The
  routing-aware cost (`placement/search.py`) dropped its `layer count` term (and the matching
  flat-build bias in the LNS recreate ranking): the bounding-box **volume** term already accounts
  for height, so the optimizer now trades layers against footprint purely by which yields the
  smaller box. Only the optimized (SA/LNS) path uses this cost; the fast constructive path is
  unaffected.
- **Power sizing now models cable voltage loss over distance.** GT cables lose voltage per block,
  so a machine `d` blocks from the source receives `tier_voltage - loss·d`, not the full tier. The
  source stays at the machine's tier and the cable is thickened to compensate: each machine's
  amperage is sized at its *delivered* voltage (`ceil(eut / (tier_voltage - loss·d))`), so a
  machine farther out draws more amps, and a run whose voltage drops to 0 is reported infeasible
  (`voltage_drop`). Loss is a flat 1 EU/block for every tier for now (per-material loss is Phase 2).
  The power router (`router/power.py`) accumulates each machine's cable distance while building the
  trunk and sizes from it; the validator independently re-derives the distance from the cable tree
  and re-checks (new `power_voltage_drop_excessive` violation); the boundary summary
  (`system_io.py`, feeding the previewer and build guide) reports the loss-inclusive amperage the
  builder must supply. Backing it: `dataset` gains `CABLE_LOSS_PER_BLOCK`, `delivered_voltage`, an
  `UnpowerableError`, and a `distance=` argument on `amperage`. This makes the emitted line
  actually buildable: a too-long low-voltage cable is no longer certified as valid. See
  `docs/DOMAIN.md`, `docs/ARCHITECTURE.md` #8.
- **Previewer power HUD shows the feed spec with correct values.** The system-i/o panel showed
  power as `48 EU/t (LV 3A)`, where the 48 is the machines' sub-tier draw (16 x 3) and the tier
  breakdown omitted the voltage - easy to mis-supply in game. It now shows the input the way a GT
  source is fed: a total EU/t supplied plus the per-tier **full tier voltage x amps**
  (`power: 96 EU/t (LV 32V x 3A)`, where 96 = 32 V x 3 A, so the total matches the breakdown). The
  scene's `io.power.byTier` entries gain a per-tier `volts` and `total` is the summed feed (scene
  version 1). (`previewer/`.)
- **InputIR bumped to v2 (breaking): dropped `Port.is_auto_output`.** It was a dead, contradictory
  field - the adapter never set it and the solver auto-connects any adjacent output regardless of
  it. Whether a port is satisfied by auto-output is a **solver decision**, not a problem input: it
  is recorded in the output's `AutoConnection`, and the "one auto-output per machine, items-xor-
  fluids, never power" rule is enforced there by the validator (`duplicate_auto_output` /
  `auto_output_illegal_commodity`), not on the input contract. `FaceSpec`'s now-moot auto-output
  validation is removed with it. (`ir/`.)
- **InputIR bumped to v1 (breaking): dropped `Machine.count`.** Multi-instance machine groups
  are not modelled until routing is instance-aware (Phase 2): the placer expanded `count` into
  N placements sharing one machine id, but a `MachineFaceRef` cannot address a specific
  instance, so the router/solver/validator collapsed the copies via `setdefault` and left the
  extras silently unwired. Each `Machine` is now exactly one instance; the adapter rejects an
  export `machineCount > 1` with an explicit `AdapterError` instead of emitting an under-wired
  layout. (`ir/`, `adapter/`, `placement/`, `validator/`.)
- **CI expanded** to a single static-checks job (via pre-commit), a Python 3.10-3.13 test
  matrix with a coverage gate (`--cov-fail-under=90`), and an advisory (non-blocking)
  Conventional-Commits check on PRs. Ruff now runs a curated lint rule set plus
  `ruff format`; the Pydantic mypy plugin is enabled. (`pyproject.toml`,
  `.github/workflows/ci.yml`.)
- Input foundation switched from a forked gtnh-flow (Python) to consuming
  gtnh-factory-flow's MIT, Zod-validated exported plan JSON. The adapter now parses
  that documented export (no vendoring); recipes/throughput/machine-IDs come from its
  dataset, so the hand-authored physical dataset shrinks. Removed the `vendor/`
  placeholder in favor of `examples/` for sample exported plans.
- Depend on a maintained fork of gtnh-factory-flow (fix only the consumed
  export/throughput/dataset path) and snapshot a known-good dataset + sample exports
  as fixtures so the solver is decoupled from the fork's health.

### Removed
- **All generated datasets are now local-only: retired the `update-textures.yml` CI workflow and
  removed the `tools/dataset_ci` helper package.** With the full texture manifest no longer committed
  (only the small example-scoped one is, see Changed), the workflow that regenerated and committed the
  ~6 MB manifest has no job; the manifest is regenerated locally like the structure dump.
  `tools/dataset_ci` (`resolve_versions`, `dataset_summary`) existed only to drive the already-removed
  `update-dataset.yml`, so it and its tests are gone, along with the `mypy`/`pytest` `tools/` path
  config that only served it.
- **The multiblock structure dump is now local-only: retired the `update-dataset.yml` CI workflow**
  (and its `dataset-update` PR template). The full ~190-controller dump (~17 MB of generated JSON)
  is not worth its repo weight or a weekly Forge run, so it is no longer built or committed by CI; a
  developer regenerates it on demand with the extractor. `data/multiblocks/` is gitignored apart from
  the two curated fixtures (Electric Blast Furnace, Vacuum Freezer) the adapter/footprint tests pin.
  Consequence: a fresh clone places and renders only the two fixtures plus single-block machines
  until the extractor is run locally.
- Dropped the unused `networkx` and `numpy` core runtime dependencies - neither was
  imported anywhere in the implementation. They will be re-added if and when the Phase 2
  optimizer/graph work actually needs them (see `docs/ROADMAP.md`).

### Fixed
- **A machine's hatch ceiling now describes the form it actually reserves (`dataset/`, `adapter/`).**
  `footprint_for` sizes a parametric machine to its recipe (a Distillation Tower with one fluid
  output reserves 3x3x3, not 3x12x3), but the hatch counts were always taken from the largest
  variant, so the two described different buildings. The Creosote Oil tower on the nitrobenzene line
  reserves 3x6x3 and was charged the 3x12x3 form's **97** hatch cells against its own **49**, a
  ceiling twice the shape the builder is told to raise. Today that only loosens a validator bound;
  once hatch slots carry geometry it would place hatches outside the reserved box, so it is fixed
  ahead of that work. `MachinePhysical.variant_for(fluid_outputs)` is now the single selection
  point, `footprint_for` and `energy_hatch_budget` both read through it, and the counts ride on
  `VariantShape` per built form. The record-level counts stay as the form `footprint` describes, for
  a fixed-shape machine and a pre-v2 dump with no variants. No shipped example loses headroom: the
  nitrobenzene tower needs 7 connections against its own 49.
- **The extractor no longer discards legitimately parametric multiblocks
  (`tools/gtnh-extractor/`, GitHub #98).** `MAX_VARIANTS = 6` rejected 16 of 191 controllers
  outright, including the Distillation Tower, Assembly Line, Cleanroom and Lapotronic
  Supercapacitor. It was the wrong instrument: the sweep cannot produce more forms than
  `MAX_STACK_SWEEP`, and per-variant blowup is already bounded by `MAX_CELLS`/`MAX_SCAN_DIM`, so a
  low variant cap only discarded real machines. Pinned to `MAX_STACK_SWEEP`, taking a local dump from
  191 controllers / 18 failures to 208 / 1 with no change to any previously extracted machine. A
  family still growing at the sweep ceiling (the Lapotronic Supercapacitor spans heights 4..50) now
  records that truncation in its own `failures` list rather than presenting a prefix as complete.
- **Super Tank / Super Chest output glyph faced the wrong way (`previewer/`).** A boundary-storage
  block auto-outputs from its front face, but the previewer oriented its output glyph (OVERLAY_STANK /
  OVERLAY_SCHEST) to the placer's `front`, which defaults every machine to north and does not track
  the eject face, so the glyph pointed away from where the block actually outputs (glyph north while
  the auto-output ejected east). A storage block with a horizontal auto-output now orients its glyph
  to that direction, so the glyph and the cyan auto-output arrow agree. Machines whose front overlay
  is a GUI/identity face rather than an output, and a storage block with a vertical eject (which a
  side glyph cannot point at), keep their placed front.
- **Basic single-block machines rendered without their front-face overlay (`tools/gtnh-extractor`,
  `previewer`, GitHub #3).** A basic machine (Forge Hammer, Macerator, Alloy Smelter, ...) drew as a
  plain steel box because its per-machine glyph never reached the manifest: the textured `mTextures`
  stack that carries the overlay is built `@SideOnly(CLIENT)` and is null on the dedicated server the
  extractor runs, and the `getXxxFacing…(byte)` accessors return the base casing layer only. The
  extractor now reconstructs the glyph from its deterministic asset path,
  `basicmachines/<folder>/OVERLAY_<FACE>[_ACTIVE][_GLOW]`, deriving `<folder>` from the machine's
  server-side `mName` (`basicmachine.<token>.tier.NN`) matched against the real folder set it
  enumerates from the GT5U jar, and appends it (plus a separate `_GLOW` emissive layer where a sibling
  PNG exists) above the casing, only for faces whose PNG actually exists so nothing is invented. On the
  committed example manifest this drops casing-only single-block fronts from 677 to 197, and the Forge
  Hammers in the sand preview now show their hammer glyph. The steel casing tint (`[210,220,255]`) and
  the multiblock hull overlays are unchanged.
- **Basic single-block machines were extracted painted black (`tools/gtnh-extractor`, `previewer`).**
  `TextureDumper.basicMachineLayers` read each machine's texture through the `getXxxFacing…(byte)`
  accessors with colour index `0`, which is the black dye, so the base casing came out tinted
  `[32,32,32]` (near-black gray) instead of the default `MACHINE_METAL` steel. Passing `-1`
  (unpainted) fixes it, and the committed example manifest was regenerated from a fresh extractor
  run, so the Forge Hammers (and every other basic machine) now render steel-blue rather than gray.
  (Their front-face overlay icon is captured separately; see the entry above.)
- **Auto-output arrow draws on top of the machine in the previewer (`previewer/html.py`, GitHub
  #30).** The per-face auto-output arrows (#20) render on every source face perpendicular to the
  ejecting direction, but the arrow sat a hair off the 0.92-scaled placeholder box, so it was buried
  under whatever drew in front of it: the opaque front-face name plate, and, on a machine that bakes
  real textures, the full-size (1.0) block cubes of its expanded render (the sand line's Forge
  Hammers hid the arrow entirely). The arrow is now lifted just outside the machine's actual rendered
  surface, expansion-aware (1.0 for the textured cubes, 0.92 for the placeholder box, plus a hair to
  clear the name plate), so it draws on top of both the casing texture and the label while normal
  depth testing still hides it behind any machine genuinely in front of it. The name plate keeps its
  opaque, high-contrast backing, so label readability is unchanged. Rendering-only, no scene or
  contract change.
- **Dark casing tints no longer bake to near-black in the previewer (`previewer/bake.py`).** The
  Pillow bake turned a GT layer tint into per-channel multipliers with a raw `value / 255`, so a
  dark-neutral casing tint like bronze's `[32, 32, 32]` collapsed to `~0.125` and multiplied the
  already-full-colour tier sprite down to mean RGB around 20 (a Basic Forge Hammer baked
  effectively black). The tint is now normalised by its brightest channel instead: identical to
  `/ 255` for any tint whose peak channel is 255 (the electric `[210, 220, 255]` majority and plain
  whites are byte-unchanged), but a dark-neutral tint becomes identity, so the sprite shows through
  at full brightness with its hue shift preserved. A regression test pins that a `[32, 32, 32]` tint
  keeps a bright sprite bright, and the existing golden tint guards move to the new hue-shifted
  values. This is a readability-first approximation; GT-pixel-accurate casing colour stays a
  deferred cosmetic item.
- **The cable-thickness ladder gains GT's 12x rung** (maintainer-reported). GT ships six cable
  sizes (1x/2x/4x/8x/12x/16x) but the dataset only knew five, so any segment or feed summing to
  9 through 12 amps was sized a whole rung thick (16x). The router now picks 12x for that band,
  the output contract and validator accept it, and the docs spell the full ladder.
- **Power router does failed-first rip-up/reroute, like the item router (GitHub #40).** The power
  router laid each tier's trunk in problem order and stopped at the first net that could not route,
  reporting only that one - but capacity accretes obstacles, so a trunk laid for one tier can wedge
  a later tier's trunk out of a chokepoint: a *false* infeasibility from net order alone, and a
  weaker feedback-loop signal than the item router already gave for pipes. It now routes a pass
  and, if any net failed, rips every trunk up and retries with the failed nets first (most-
  constrained-first), stopping only when a pass is clean or a failed-net set repeats (a genuine
  infeasibility, not an ordering accident). When routing does stall it reports ALL still-failing
  nets, not just the first, so the place↔route feedback loop can penalize them all. The bounded-
  retry loop is now shared with the item router (`core._rip_up_reroute`). (`router/power.py`,
  `router/core.py`.)
- **Validator derives power amperage independently of the router (GitHub #36).** The validator is
  meant to be a second, differently-written implementation so a bug in the router's power math is
  caught, not certified (docs/ARCHITECTURE.md #4) - but its amperage re-check still called the same
  `dataset.amp_load` / `whole_amps` helpers the router sizes cables with, so a bug in the loss
  formula or the ceil-with-epsilon rounding would have been blessed by both sides. It now inlines
  its own arithmetic (`eut / (tier_voltage - loss * distance)` per machine, summed per segment,
  `ceil` with the shared epsilon), importing only the rule DATA (the voltage ladder,
  `CABLE_LOSS_PER_BLOCK`, `_AMP_EPSILON`) so the rounding policy stays identical and the two still
  agree on every valid layout, while a sizing bug is now caught on a separate code path. Separately,
  an unknown/off-ladder voltage tier was reported as `power_thickness_insufficient` (whose meaning
  is "cable thinner than the summed amps") - a wrong signal for a route that is merely unverifiable;
  it now gets its own additive `power_tier_unknown` violation code. (`validator/`.)
- **User-facing output surfaces are hardened against bad input and bad paths (GitHub #39).** The
  previewer inlined the scene JSON into its `<script>` block unescaped, so a machine type or
  resource id containing `</script>` (plan JSON is external input) could close the tag and break or
  inject into the page; the inline JSON now escapes `</` to `<\/` (JSON-transparent, the scene still
  round-trips). The CLI's `-o`/`--preview` writes raised an uncaught `OSError` on an unwritable path,
  dumping a raw traceback instead of honoring the documented 0/1/2 exit-code contract; both writes
  now report `error: could not write <path>: <reason>` to stderr and exit 2. (`previewer/`, `cli`.)
- **Amperage is sized from fractional machine loads, rounded up per aggregate - not per machine**
  (maintainer-verified in game). GT machines pull whole packets (1 amp = one packet of up to tier
  voltage) into an internal buffer only when it has room, so a 16 EU/t LV machine *averages* 0.5
  amps - but `dataset.amperage` ceiled every machine to whole amps and the callers summed the
  ceilings, overstating every aggregate: the optimized sand line's feed spec read 3 A / 96 EU/t
  when 2 A / 64 EU/t runs it in game, and cables could come out a tier thicker than needed.
  `amperage` is replaced by `amp_load` (the un-rounded `eut / delivered_voltage`, same
  unknown-tier / unpowerable errors) plus `whole_amps` (the ceil, with epsilon slack for float
  dust), and the rounding moves to where packets are actually quantized: per cable segment in the
  router and validator, per tier in `system_io` (so the guide and previewer both now say
  2 A / 64 EU/t for sand; this supersedes the interim 3 A number from the drift fix below). Cable
  loss still raises far machines' loads; the 16x cap and unpowerable checks are unchanged.
  (`dataset/`, `router/power.py`, `validator/`, `system_io`.)
- **Build guide power note agrees with the previewer (and reality).** The note read the feed
  amperage off the trunk's thickest cable segment, which both understates a trunk whose sink taps
  the source's own dock cell (its amps flow through no segment - on the optimized sand stack the
  guide said `>=2 A -> up to 64 EU/t` while the previewer said 3 A / 96 EU/t) and overstates when
  amps round up to a cable tier (the fast sand row printed 4 A for a 3 A draw). Both surfaces now
  read the same shared `system_io` numbers: the tier's machine draws summed at each machine's
  delivered voltage. Per-segment cable thickness still lives under Connections. (`buildguide/`.)
- **Validator requires a consumer on routed nets (GitHub #8).** The gate enforced the
  OUTPUT->INPUT port direction on the auto-connection path but not on the routed-pipe path, so a
  routed net with no consumer (every endpoint an OUTPUT producer) passed - the golden "valid"
  fixture even normalized one. A routed net now needs at least one INPUT endpoint
  (`route_net_no_consumer`), while still allowing multiple same-commodity producers feeding one
  pipe (GT lets several machines eject into one line). The routed path also independently checks
  every endpoint carries the net's own commodity (`route_net_mixed_commodity`), so a mixed-
  commodity net is caught even if a producer bypasses the input IR's own check. The base test
  fixture now wires a real consumer.
- **Previewer floor grid aligns to cell boundaries (GitHub #19).** The grid lines landed on
  integer boundaries on one axis but cut through the middle of the blocks on the other (a
  `GridHelper` centering artifact: integer line offsets need an even division count, half-integer
  offsets an odd one, so parity decided it per axis). The grid now uses an even span snapped to an
  integer center, so every line sits on a cell edge and the blocks read as sitting in their cells.
- **Previewer draws auto-output direction on the machine faces (GitHub #20).** The cyan auto-output
  arrow ran center-to-center between the two adjacent machines, so it was buried inside their opaque
  boxes and you could not tell which machine fed which. It is now a small flat arrow on each source
  face perpendicular to the ejecting direction (the two side faces plus top and bottom), each
  pointing the way the machine ejects, so at least one stays visible from any angle however tightly
  the machines are packed.
- **Previewer renders routes GT-style (GitHub #31).** Cables and pipes were flat bars spanning
  cell-center to cell-center plus a separate fixed lead to each machine face, which did not read like
  an in-game pipe/cable. Every route (item, fluid, power) is now a small cube at each cell centre
  with a uniform cross-section arm out to the block edge for each connection (an adjacent route cell,
  or a docked machine face), power sized by cable thickness. One node per cell keeps a run readable
  however tightly the routes are packed.
- **Validator route + auto-connection soundness holes** - the only automated correctness gate
  was certifying some geometrically-impossible layouts. Routes are now checked for unit-step
  segments (a single segment can no longer "teleport" two cells across a machine - connectivity
  alone missed it), and no route cell may sit inside a machine body or on a reserved cell.
  Auto-connections are now checked against the net they claim to satisfy: the connection must
  join that net's real OUTPUT->INPUT endpoint machines (resolved by port direction), `net_id`
  must resolve, and power/ME-routed commodities cannot be auto-output. New violation codes
  (`route_segment_not_unit`, `route_through_machine`, `route_on_reserved`,
  `auto_output_wrong_endpoints`, `auto_output_illegal_commodity`) with one negative test each.
- **`solve()` now validates its own output.** It previously returned `valid` whenever
  placement and routing each reported success, without ever running the independent validator -
  so the "never returns a silently-invalid layout" promise was not enforced end to end. `solve`
  now runs `validate()` on the assembled layout and downgrades a `valid` result to
  `partial_invalid` (carrying the violation) if anything is proven wrong.
- **Validator enforces summed-amperage power sizing** (previously deferred). It independently
  re-derives each power cable's load - rooting the cable tree at its source terminal and summing
  the draw of the machines downstream of every segment - and flags a segment whose cable is
  thinner than its load (`power_thickness_insufficient`), which also catches a load over the 16x
  cap. So a power-sizing bug in the router is caught, not certified.
- **Validator no longer blesses an uncertifiable power route.** The amperage check used to
  *skip* (certify by silence) a power route it could not verify - one with zero or multiple
  source terminals, or whose cables form a cycle/tangle instead of a single tree - the exact
  silently-invalid case the independent gate exists to catch. It now rejects both with explicit
  violations (`power_net_no_single_source`, `power_route_not_a_tree`), each with a negative test.
- **Power router always builds a tree.** `router/power` A*'d each leg of a trunk against
  obstacles that excluded the cable already laid, so legs could overlap into a non-tree whose
  per-segment amperage is undefined. Each laid leg's cells are now obstacles for the legs that
  follow, so the trunk is always a single non-overlapping path the validator can verify.
- **Placement optimizer keeps auto-output (orientation-aware cost).** The SA cost was
  orientation-independent, so `reorient` moves were a free random walk that could finalize an
  orientation putting a machine's front (no-I/O) face on a connecting side and **blocking**
  auto-output. The cost now rewards orientations that enable auto-output (a shared
  `ir.geometry.auto_output_faces` helper, reused by the solver), so the optimizer preserves -
  and recovers - the free connections instead of degrading them.
- **Adapter sizes power for `parallel`.** A node's `eut` is now `recipe.eut * parallel`: a node
  running N recipes in parallel draws N times the power, matching how throughput already scales,
  so the synthesized power cable is sized correctly for `parallel > 1` (was under-sized).
- **Validator checks terminals belong to their net.** `_check_terminals` only verified that every
  net endpoint *had* a terminal; a route could still carry a **foreign** terminal (a machine/port
  that is not one of the net's endpoints) or two terminals for one endpoint and pass. It now flags
  both (`terminal_not_an_endpoint`, `duplicate_terminal`), closing the structural half of
  required-I/O-face reachability so the gate cannot certify a route with bogus docks.

[Unreleased]: https://github.com/MrBruh/gtnh-process-line-solver/commits/main
