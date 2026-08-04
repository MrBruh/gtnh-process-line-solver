# gtnh-extractor

A standalone Forge 1.7.10 dev-only tool that extracts the physical multiblock dataset the
Python solver consumes. It is the **only Java in this repo**, quarantined under `tools/`
per design principle 1 of the dataset-extraction plan: the Python solver never imports or
runs it, it only reads the JSON the tool emits.

The tool boots a **headless dedicated server** with GT5-Unofficial + StructureLib loaded,
builds every multiblock controller into a void world, scans the result, and dumps JSON.
Because it executes the same `construct(...)` code the in-game hologram projector runs, the
output matches in-game behaviour by construction. See `docs/dataset-extraction/` (requirements.md,
implementation.md, plan.md, texture-resolution.md) for the full rationale.

## Status

**Lane 3 (issue #46): channel handling and identity-substitution tables are implemented.** On
top of lane 2's core dump loop, `StructureDumper` now fills the per-controller `substitutions`
object. The crux is that StructureLib reads a channel with
`ChannelDataAccessor.getChannelData(trigger, channel)`, which falls back to the trigger's **stack
size** when the channel is unset. So the lane 2 trigger-stack sweep already varies every channel at
once: a channel that changes the **shape** (a distillation tower's `height`, a `length`) yields
distinct occupied-cell sets and is recorded as separate size variants; a channel that only swaps a
tiered **block** (coil, glass, pipe casing) keeps the same shape and collapsed into one variant,
throwing its tier information away. Lane 3 recovers exactly that discarded tier information: holding
the stack size at 1, it sets each GT channel (`GTStructureChannels.values()`, minus the
always-applied `gt_no_hatch`) to values `2..N` and diffs the placed blocks against the default
build. Shape-changing channels (occupied cells move) are left to the stack sweep; identity-only
channels are recorded once as `substitutions[channel]` = the default tier plus every distinct higher
tier `{channel_value, block, meta}`. The default-placed block is always included, so the Python
adapter can match the tiered blocks in the primary variant. Hard caps bound the per-channel value
sweep and the total substitution entries; overflow lands on the `_meta.json` failure list.

**Heating coils are a special case.** Only the mega furnaces bind their coil element to the `coil`
channel (`HEATING_COIL.use(...)`); the classic ones (Electric Blast Furnace, Multi Smelter, ...)
place a bare `ofCoil` whose tier is read from the trigger's **stack size**, so an explicit `coil`
channel does nothing there. The coil table is built by a separate stack-size sweep that identifies
coil blocks by the GT `IHeatingCoil` interface the block implements (a declared fact, not a
hard-coded name); it covers both kinds. The Electric Blast Furnace stays one 3x3x4 shape variant
with a populated `coil` table (14 tiers), so the adapter counts its 2 coil layers.

**Lane 2 (issue #45): the core dump loop.** On top of the lane 1 scaffold
(the `ExampleMod1.7.10` buildscript wiring, `dependencies.gradle` pins, and the
`DumperMod` boot/exit plumbing), the tool builds every multiblock and emits the
schema-v2 dataset:

- `DumperMod` hooks `FMLServerStartedEvent`, resolves the run config, runs the dump, and
  exits the JVM (0 on success, nonzero on failure) so a `runServer` boot is a pass/fail gate.
- `StructureDumper` iterates `GregTechAPI.METATILEENTITIES`, keeps the `IConstructable`
  controllers, and for each one places it at a fixed origin in the server overworld and
  sweeps the trigger stack (size 1..N, stopping when the placed block set stops changing).
  Per stack size it runs a **hint pass** (`construct(trigger, hintsOnly=true)` with a
  recording proxy that captures the hologram's hint dots) and a **block pass**
  (`construct(trigger, hintsOnly=false)` into a wiped void region, then scan), applying the
  `gt_no_hatch` channel so real hatches stay out and the casing shell plus hint positions
  are what get recorded.
- `RecordingProxy` captures hint particles headlessly (the server's normal proxy no-ops
  them); `ElementRecorder` + `HatchProbe` ask each visited `IStructureElement` which hatch
  kinds it accepts, so a slot carries its `HatchElement` names (this is what schema v2 added:
  `variants[].hatch_slots`); `JsonWriter` serialises the raw facts to schema-v2 JSON (Gson,
  stable key + variant ordering); `ErrorCollector` sends any exception,
  non-terminating/explosive sweep, or empty scan to `_meta.json.failures` so one broken
  multiblock never kills the run.

Output: one `<datasetOut>/multiblocks/<name>.json` per controller plus a `_meta.json` run
summary, both validating against `src/gtnh_solver/dataset/schema.py`. Channel handling and
identity-substitution tables (lane 3, in the Status notes above) and texture mapping (lane 6, see
the Texture manifest section below) run as their own passes, not as part of this core loop. No
game logic beyond raw coordinate collection lives in this tool by design; all interpretation is
the Python adapter's.

## Pinned versions

Versions come from the DreamAssemblerXXL manifest for the current **stable** pack release
(not dailies/experimental) and are mirrored in `gtnh.lock.json` at the repo root. GitHub
tags on the two mod repos match these versions.

| Pack (manifest) | GT5-Unofficial | StructureLib |
| --------------- | -------------- | ------------ |
| 2.8.4           | 5.09.51.482    | 1.4.23       |

Only these two mods are pinned by hand. Every other hard dependency (IndustrialCraft2,
NotEnoughItems, NotEnoughIds, GTNHLib, ModularUI, waila, AE2, ...) is a runtime dependency
of GT5-Unofficial and resolves transitively from its Nexus POM (each entry is published
with `classifier=dev` and `compile` scope), so pulling GT5U populates the whole dev server
without listing them here. See the comments in `dependencies.gradle`.

One transitive branch is excluded: GT5U's POM lists `ThaumicTinkerer` (a Thaumcraft
integration addon) as a `compile` dependency *without* excluding its own transitives, and
that subtree resolves to `com.github.GTNewHorizons:CodeChickenLib:1.3.0`, which is not
published on the Nexus (the whole coordinate 404s). A plain transitive pull of GT5U
therefore fails at `:compileJava`. Thaumcraft integration is not needed to enumerate or
build multiblocks, so `dependencies.gradle` drops that one optional subtree; every other
GT5U hard dependency still resolves and loads on the dev server.

To bump: rewrite the two coordinates in `dependencies.gradle` and the entry in
`gtnh.lock.json` from a newer manifest. The pin is hand-maintained: lane 4 (a structure-dump
CI) was dropped, because the dump is local-only (see `docs/dataset-extraction/plan.md`).

## GT5U / StructureLib API surface

Kept deliberately tiny (plan risk 9.1: never reference the ~250 controller classes by
name; keep the API surface to a handful of stable, ancient symbols so a GT5U bump either
just works or fails to compile loudly and locally). This is the surface the lane 2 dump loop
actually touches:

| Symbol | Package | Used for |
| ------ | ------- | -------- |
| `GregTechAPI.METATILEENTITIES` | `gregtech.api` | The array of registered meta tile entities to iterate (index = meta id). |
| `IMetaTileEntity` | `gregtech.api.interfaces.metatileentity` | Element type of that array; `getStackForm`, `newMetaEntity`, `setBaseMetaTileEntity`, `getLocalName`/`getMetaName` filter, place, and name the controller. |
| `BaseMetaTileEntity` | `gregtech.api.metatileentity` | The tile entity the controller is placed into: `setMetaTileID`, `setMetaTileEntity`, `setFrontFacing`. |
| `IConstructable` | `com.gtnewhorizon.structurelib.alignment.constructable` | Filter + the build call `construct(ItemStack trigger, boolean hintsOnly)` (hint pass and block pass). |
| `ChannelDataAccessor` | `com.gtnewhorizon.structurelib.alignment.constructable` | `setChannelData(trigger, channel, value)` to apply `gt_no_hatch` and to probe each tier channel (lane 3). |
| `GTStructureChannels` | `gregtech.common.misc` | The enum of GT's structure channels; `values()` + `get()` give the channel names the lane 3 probe sweeps (coil, glass, pipe, ...) so the tool never hard-codes them and a GT5U bump that adds a channel is picked up automatically. |
| `IHeatingCoil` | `gregtech.api.interfaces` | Identifies a placed block as a heating coil (`block instanceof IHeatingCoil`) so the coil substitution table can be built from a stack-size sweep, since the classic coil element is stack-size driven rather than coil-channel driven. |
| `IAlignment` / `ExtendedFacing` | `com.gtnewhorizon.structurelib.alignment[.enumerable]` | Point the controller front at a fixed direction so the offset frame is deterministic. |
| `StructureLibAPI.getBlockHint()`, `enableInstrument()` / `disableInstrument()` | `com.gtnewhorizon.structurelib` | Identify hint-block dots while scanning (a hatch/DOF slot vs. a solid casing cell); the instrument brackets a build so `StructureEvent` reports which element visited each cell. |
| `IStructureElement` / `IStructureElementChain` (+ `StructureEvent`) | `com.gtnewhorizon.structurelib[.structure]` | `ElementRecorder` maps cell -> visiting element; `HatchProbe` flattens a chain and asks `getBlocksToPlace` what each leaf accepts, which is where a slot's hatch kinds come from. |
| `HatchElement` (+ `mteClasses()`) | `gregtech.api.enums` | The GT hatch-kind enum whose names a slot's `kinds` list holds (`InputBus`, `OutputHatch`, ...). One probe stack per kind (the first registered MTE of the classes the kind declares) is tested against the element's predicate, so a GT bump that renumbers hatches is picked up automatically. |
| `StructureLib.proxy` (reflected) + `CommonProxy` (subclassed) | `com.gtnewhorizon.structurelib` | Temporarily swap in `RecordingProxy` to capture hint particles headlessly (the server's proxy no-ops them). The one reflective touch of a StructureLib internal; a bump that moves it fails loudly and locally. |

One extra reflective touch, on the Minecraft side: StructureLib's hint walk is client-only
(`iterate()` opens with `if (!world.isRemote && hintsOnly) return false;`), so the dump briefly
flips the scratch world's `net.minecraft.world.World.isRemote` to `true` around each hint pass, then
restores it. This is safe because the whole dump runs synchronously in the server-started handler,
with nothing else touching the world; hint capture is best-effort, so a controller whose hint pass
reaches a client-only icon path (`IIconContainer.getIcon()` throws on a dedicated server) still
dumps its geometry from the block pass.

Note the framework package is `com.gtnewhorizon.structurelib` (singular). `ISurvivalConstructable`
is deliberately **not** used: the dump drives the creative `construct(...)` path, not survival
autoplace. Texture reflection (`Textures.BlockIcons`) is lane 6, in `TextureDumper` (see below).

## Texture manifest (lane 6, issue #49)

`TextureDumper` emits the texture manifest (schema 2, `TextureDumper.SCHEMA_VERSION`) so the previewer
can skin machines and casings. It is a **separate pass** gated by `-PtextureOut`, which writes
`<textureOut>/manifest.json`; the maintainer installs it locally at the version-namespaced
`data/<version>/textures/manifest.json` (gitignored; only a small example-scoped manifest is
committed, see `docs/dataset-extraction/`). PNGs are never committed (LGPL), only the icon **name**
and the asset **path inside the mod jar**, which the previewer fetches from the GTNH Nexus jar at
preview time. The manifest is regenerated locally on demand (no CI: `update-textures.yml` was retired
2026-07-17, see `docs/dataset-extraction/plan.md`).

**What it emits.** Per MetaTileEntity and per `(block, meta)`: the ordered bottom-to-top `ITexture`
layer stack for each side and active state, every layer resolved to an iconset name + RGBA tint +
glow flag. An `ITexture` is flattened recursively (`describe`): a sided or multi wrapper is unwrapped
via its `mTextures` (length 6 means sided, so pick this side), a rendered leaf resolves to
`{icon, rgba, glow}` from `mIconContainer` + `getRGBA()`, and a copied-block leaf resolves through the
block-icon path. Shapes the flattener does not know are recorded in the manifest's `gaps` (with the
offending instance's runtime field values), never guessed.

**How a sprite gets named.** Everything here is server-safe reflection: the icon register
(`net.minecraft.client.renderer.texture.IIconRegister`) is a `@SideOnly(CLIENT)` class FML's
`SideTransformer` refuses to load on a server, and `getTextureFile()` throws for the same reason, so
the pass never registers icons and never stubs the register. (Feeding the blocks a fake
`IIconRegister` that records the names they ask for is the obvious idea and is **impossible**: a
class cannot implement an interface that does not exist on this side. It was measured and removed;
texture-resolution.md records it as a dead end so it is not retried.) Icon *names* are taken instead
from the `Textures.BlockIcons` enum constants' `name()` (or a custom container's `mIconName` plus
`mModID`), which map 1:1 to the PNGs under `assets/<modid>/textures/blocks/`. Up front,
`populateIconNames()` injects a name-carrying `NamedIcon` into every `BlockIcons.mIcon` field **and**
into every custom icon container queued in `GregTechAPI.sGTBlockIconload` (the queue GT drains
client-side and never runs on a server, which is why those blocks answer `getIcon` with null), so any
block that answers `getIcon` at all hands back a named icon. Then:

- **MetaTileEntities** (machines, hatches, buses, controller hulls) are placed once at a scratch
  origin and read via `getTexture(base, side, facing, colour, active, redstone)`. Basic single-block
  machines need no tile entity and use the `getXxxFacing{Inactive,Active}(byte)` accessors instead;
  those return the casing layer only (the machine's own `mTextures` stack is built
  `@SideOnly(CLIENT)` and is null here), so the per-machine glyph is rebuilt from the deterministic
  `basicmachines/<folder>/OVERLAY_<FACE>[_ACTIVE][_GLOW]` asset path, enumerated from the GT5U jar.
  Only faces whose PNG actually exists get an overlay; nothing is invented.
- **Plain blocks** (casings, coils, glass, frames) try five routes per meta, in this order:
  a server-safe `getTextures(int)` / `getTexture(int)` `ITexture` accessor; the transcribed
  `CASING_ICON_TABLE` for the families whose `getIcon` the `SideTransformer` deleted outright; the
  block's own (now injected) `getIcon`; a formula over the bartworks werkstoff registry for material
  casings, which store neither icon nor name; and the block's un-annotated texture-**name** fields
  (`textureNames`, `textureName`, or `textureSide` + `textureTopAndDown`). A meta that survives all
  five records a gap carrying the reason.

**Why five routes are needed, and what is still unreachable:** see
[`docs/dataset-extraction/texture-resolution.md`](../../docs/dataset-extraction/texture-resolution.md).
It holds the deep treatment: the distinct `@SideOnly` failure modes each route answers, the traps,
the maintenance hazard in the hand-transcribed casing table, and the current unresolved set. No
coverage count is quoted here, because it moves with every run and every pack bump. Textures only
feed the previewer, so gaps never block the solver.

Extra GT5U / Minecraft API surface this pass touches (all server-safe: `IIconRegister` and the
client-only render path are deliberately avoided):

| Symbol | Package | Used for |
| ------ | ------- | -------- |
| `Textures.BlockIcons` (enum + `mIcon` field + the tiered `MACHINECASINGS_*` array fields, reflected) | `gregtech.api.enums` | Enumerate every casing icon constant; inject a `NamedIcon` into its `mIcon` so `getIcon` returns a named icon on the server. The constant's `name()` gives the iconset name (`gregtech:iconsets/NAME`) + jar path. The array fields back the casing table's indexed entries, read at runtime rather than transcribed. |
| `GregTechAPI.sGTBlockIconload` | `gregtech.api` | The queue every custom `IIconContainer` self-registers into from its constructor, and so the complete server-side registry of them; walked once to inject a `NamedIcon` into each one's `mIcon`. |
| `GregTechAPI.sGeneratedMaterials` | `gregtech.api` | Meta bound for the material-indexed blocks (`gt.blockframes`), whose sub-blocks are keyed by GT material id rather than by 0..15 world metadata. |
| `ITexture` / `IIconContainer` | `gregtech.api.interfaces` | The layer objects themselves: flattened via their `mTextures` / `mIconContainer` / `mRGBa` / `glow` fields, and named via the container's enum `name()` or `mIconName` + `mModID`. |
| `IMetaTileEntity.getTexture(...)`, `getStackForm`, `newMetaEntity`, `getLocalName` | `gregtech.api.interfaces.metatileentity` | The MTE layer stack, plus the block form, placement, and display name each manifest entry is keyed and labelled by. |
| `BaseMetaTileEntity` / `IGregTechTileEntity` | `gregtech.api.metatileentity` / `.interfaces.tileentity` | Place a hull or hatch at the scratch origin (`setMetaTileID`, `setMetaTileEntity`, `setFrontFacing`) so its `getTexture` can be queried live. |
| `MTEBasicMachine.getXxxFacing{Inactive,Active}(byte)` (reflected) | `gregtech.api.metatileentity.implementations` | A basic single-block machine's casing layers without placing it; also the `mName` field the overlay folder is derived from. |
| `getTextures(int)` / `getTexture(int)` (resolved by name, not compiled against) | declared by `IBlockWithTextures` (`gregtech.api.interfaces`) and `BlockFrameBox` | The preferred plain-block route: no `@SideOnly`, never dereferences the icon, and carries per-layer tint and glow that `getIcon` drops. |
| `Block.getIcon(int, int)` (reflected) | `net.minecraft.block` | The block's own per-meta/side icon lookup, invoked reflectively so there is no compile-time client dependency. |
| `IIcon` | `net.minecraft.util` | The name-carrying `NamedIcon` injected as `mIcon` (a server-safe type, unlike the client-only `IIconRegister`). |
| `GameData.getBlockRegistry()` / `FMLControlledNamespacedRegistry` | `cpw.mods.fml.common.registry` | Iterate every registered block and get its `modid:name`. |
| `bartworks.system.material.Werkstoff` (`Class.forName`, never linked) | `bartworks.system.material` | The werkstoff registry, texture set, and RGBA behind the material-casing formula. Reflective, so a pack without bartworks degrades to a recorded gap instead of failing the pass. |

Forge/FML symbols the scaffold uses today:

| Symbol | Package | Used for |
| ------ | ------- | -------- |
| `FMLServerStartedEvent` | `cpw.mods.fml.common.event` | Entrypoint hook: fires after the dedicated server has fully started. |
| `FMLCommonHandler.exitJava(int, boolean)` | `cpw.mods.fml.common` | Terminate the JVM with a shell exit code once the dump finishes. |
| `Loader.instance().getIndexedModList()` / `ModContainer.getVersion()` | `cpw.mods.fml.common` | Dev fallback for the tracked-mod versions in `_meta.json` / the manifest provenance, when `-PmodVersions` is not passed (GT5U's own container reports the uninformative `MC1710`). |

## Build & run

Prerequisites:

- **A full JDK 25 installed locally, and `JAVA_HOME` set to it.** The Gradle daemon runs on
  Java 25 (pinned by `gradle/gradle-daemon-jvm.properties`). **`JAVA_HOME` is required for
  every `./gradlew` invocation in this tool**: Gradle does *not* auto-detect the locally
  installed JDK 25. Measured on this machine, with Temurin `jdk-25.0.3+9` installed under the
  user Adoptium dir and `JAVA_HOME` unset, `./gradlew compileJava` fails (exit 1); the same
  command with `JAVA_HOME` exported succeeds.

  ```sh
  export JAVA_HOME="/c/Users/<you>/AppData/Local/Programs/Eclipse Adoptium/jdk-25.0.3+9"
  ```

  Worth documenting because the failure does not say "no JDK". Auto-detection misses the
  install, the build falls straight through to foojay auto-provision, and on Windows the
  template's pinned foojay URL resolves to a *JRE* (no `javac`/`javadoc`/`jar`), so what you
  actually get is a confusing provisioning error: `Unable to download toolchain matching the
  requirements ... Toolchain provisioned ... doesn't satisfy the specification ... must have
  the executable 'javac'`.
- **Leave toolchain auto-download enabled** (the default). The `gtnhconvention` plugin
  compiles its injected interfaces with an **Azul Zulu JDK 17** and the 1.7.10 mod with a
  **Java 8** toolchain; both download from foojay on first build. Do *not* pass
  `-Dorg.gradle.java.installations.auto-download=false`, or those toolchains fail to
  resolve. (Pre-installing a Temurin JDK 8 locally also satisfies the mod toolchain.)
- Network access to the GTNH Nexus (`https://nexus.gtnewhorizons.com/repository/public/`).
  The first run downloads Gradle, the toolchains, Forge, and the mod jars (multi-GB, slow);
  cached runs take minutes.

Commands (run from `tools/gtnh-extractor/`):

```sh
# One-time: decompile Minecraft + fetch deps into the dev workspace.
# Interactive/IDE use:
./gradlew setupDecompWorkspace
# Headless:
./gradlew setupCIWorkspace

# Boot a dedicated server with GT5U + StructureLib + hard deps, run the dump, and exit.
# Exit code 0 == success. runServer transitively triggers the setup above.
./gradlew runServer -PdatasetOut=../../_dump_out

# Texture-only run (skips the structure dump entirely). -PmodVersions is not optional here:
# see below.
printf 'n\ny\n' | ./gradlew runServer \
  -PtextureOut=../../out/textures-run \
  -PpackVersion=2.8.4 \
  "-PmodVersions=GT5-Unofficial=5.09.51.482,StructureLib=1.4.23"
```

Run properties (`build.gradle.kts` forwards them into the server JVM as `gtnhextractor.*` system
properties, which `DumperMod` reads):

- `-PdatasetOut=<dir>` where to emit `<dir>/multiblocks/` + `_meta.json` (resolved against the
  project dir; defaults to `<cwd>/dataset-out` when the structure dump runs without it).
- `-PtextureOut=<dir>` where to emit `<dir>/manifest.json` (the texture pass). Setting it *without*
  `-PdatasetOut` is a texture-only run, the only way to skip the structure dump; with neither
  property set, the structure dump still runs, into the default directory.
- `-PpackVersion=<ver>` / `-PextractorSha=<sha>` recorded in `_meta.json` and in the manifest
  provenance (default `unknown-dev` / the `GITHUB_SHA` env var / `unknown`).
- `-PmodVersions=<label>=<ver>,...` the pinned tracked-mod versions from the repo-root
  `gtnh.lock.json`, recorded as provenance. **Required for a usable texture manifest.** Without it
  the manifest records GT5U's self-reported `"MC1710"`, `previewer/jar.py` then tries to download a
  jar version that does not exist, and the previewer silently falls back to placeholder boxes for
  *everything*. It looks like a catastrophic regression and is a one-flag mistake.
- `-PdebugMeta=<id>` diagnostics: log what the hint pass captured for one controller meta id.

Headless notes:

- `runServer` prompts on **stdin** for online-mode and Minecraft EULA acceptance. With no
  stdin attached (a background or CI run) the prompts read EOF and the task fails with
  "Minecraft EULA not accepted". Feed the answers in:

  ```sh
  printf 'n\ny\n' | ./gradlew runServer
  ```

  The first `n` keeps the server offline (no Mojang auth); the `y` accepts the EULA. RFG
  writes `run/server/eula.txt` and the server `server.properties` from those answers.
- No `nogui` arg is needed: the GTNH server run config is already headless (it does not open
  the AWT server GUI).

The `runServer` boot-and-exit has been **verified end to end**: the dedicated server starts
with GT5U + StructureLib + their hard dependencies loaded, `DumperMod` fires on
`FMLServerStartedEvent`, runs the requested pass(es), and calls `exitJava(0)`, yielding
`BUILD SUCCESSFUL`. On a fresh machine the wall-clock is dominated by the one-time Minecraft
decompile and the multi-GB dependency/toolchain download; once cached, a boot is about a
minute. Nothing in CI runs it: both passes are local-only, so `BUILD SUCCESSFUL` (the real
exit status, not a piped `tail`'s) is the gate.

## Licensing

Extracted structure *facts* are fine to commit to this Apache-2.0 repo, but LGPL assets
(textures) are not vendored here; they are fetched from the Nexus jar at build/preview
time. `NOTICE` credits GT5-Unofficial and StructureLib.
