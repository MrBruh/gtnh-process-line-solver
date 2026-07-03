# gtnh-extractor

A standalone Forge 1.7.10 dev-only tool that extracts the physical multiblock dataset the
Python solver consumes. It is the **only Java in this repo**, quarantined under `tools/`
per design principle 1 of the dataset-extraction plan: the Python solver never imports or
runs it, it only reads the JSON the tool emits.

The tool boots a **headless dedicated server** with GT5-Unofficial + StructureLib loaded,
builds every multiblock controller into a void world, scans the result, and dumps JSON.
Because it executes the same `construct(...)` code the in-game hologram projector runs, the
output matches in-game behaviour by construction. See `DATASET_EXTRACTION_PLAN.md` (section
1.3 and 4) for the full rationale.

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
schema-v1 dataset:

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
  them); `JsonWriter` serialises the raw facts to schema-v1 JSON (Gson, stable key +
  variant ordering); `ErrorCollector` sends any exception, non-terminating/explosive sweep,
  or empty scan to `_meta.json.failures` so one broken multiblock never kills the run.

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
`gtnh.lock.json` from a newer manifest. Lane 4 (issue #47) automates this in CI.

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
| `StructureLibAPI.getBlockHint()` | `com.gtnewhorizon.structurelib` | Identify hint-block dots while scanning (a hatch/DOF slot vs. a solid casing cell). |
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

`TextureDumper` emits `data/textures/manifest.json`: a block-to-icon map so the previewer can skin a
multiblock shell. It is a **separate pass** gated by `-PtextureOut`; PNGs are never committed (LGPL),
only the icon **name** and the asset **path inside the mod jar**, which the previewer fetches from
the GTNH Nexus jar at preview time.

**Option A (implemented): server-side icon reflection.** In 1.7.10 `Block.getIcon(side, meta)`
returns an `IIcon` that is only populated by client-side texture registration, so on a dedicated
server it is normally `null` - and the register itself
(`net.minecraft.client.renderer.texture.IIconRegister`) is a `@SideOnly(CLIENT)` class FML's
`SideTransformer` **refuses to load on the server** (`invalid side SERVER`), even in the dev
workspace, so we cannot register icons the client way at all. But GT wires every casing texture
through a `Textures.BlockIcons` enum constant whose name **is** the PNG filename: every constant's
`run()` registers `"gregtech:iconsets/" + name()`, with the PNG at
`assets/gregtech/textures/blocks/iconsets/<NAME>.png` (the constant's own `getTextureFile()` is
unusable here - it dereferences the client-only `TextureMap` class the `SideTransformer` blocks). So
the pass skips the register entirely: it **reflectively sets each constant's
package-private `mIcon` field** (of the server-safe `net.minecraft.util.IIcon` type) to a `NamedIcon`
carrying the name, then invokes each block's own `getIcon(side, meta)` **reflectively**
(via `MethodHandles.findVirtual`, no compile-time reference to the possibly client-only method). When
the block's per-meta wiring references the icon constant directly, `getIcon` hands back our
`NamedIcon` and we read the name; no client, no icon register, and no reimplementing the per-family
switch.

**What Option A can and cannot name (the documented gap).** A meta resolves when its `getIcon`
references the constant **directly** - `invokevirtual` on the concrete `Textures$BlockIcons` enum,
whose `getIcon()` is *not* side-stripped - e.g. the Electric Blast Furnace's heat-proof machine
casing (`gt.blockcasings` meta 11 -> `MACHINE_HEATPROOFCASING`). A meta does **not** resolve when
`getIcon` reaches its sprite through the `IIconContainer` *interface* (a shared `MACHINECASINGS_*`
array, or a switch result typed as `IIconContainer`): `IIconContainer.getIcon()` is itself
`@SideOnly(CLIENT)`, so FML strips it from the interface on the server and the `invokeinterface`
throws `NoSuchMethodError`. The heating coils (`gt.blockcasings5`) hit exactly this. Those metas, plus
families with no server-side `getIcon` override (GT's newer client-only `ITexture` render path) and
the composite tile-entity `gt.blockmachines` controller hulls, are recorded in the manifest's `gaps`
with the reason - never invented - pointing at **Option B (fallback, not implemented): a client-mode
`runClient` dump under `xvfb-run`** whose main-menu tick handler reads
`getIcon(side, meta).getIconName()` for the gap blocks. Textures only feed the previewer, so this lane
can slip a pack version without blocking the solver; hence the separate `update-textures.yml`
workflow. (In the verified 2.8.4 boot: 9 casing blocks / 150 `(block,meta,side)` assignments resolved,
28 families/metas recorded as Option-B gaps.)

Extra GT5U / Minecraft API surface this pass touches (all server-safe: `IIconRegister` and the
client-only render path are deliberately avoided):

| Symbol | Package | Used for |
| ------ | ------- | -------- |
| `Textures.BlockIcons` (enum + `mIcon` field, reflected) | `gregtech.api.enums` | Enumerate every casing icon constant; inject a `NamedIcon` into its `mIcon` so `getIcon` returns a named icon on the server. The constant's `name()` gives the iconset name (`gregtech:iconsets/NAME`) + jar path. |
| `IHasIndexedTexture` | `gregtech.api.interfaces` | Filter the block registry to the indexed-texture (casing/coil) families. |
| `IIcon` | `net.minecraft.util` | The name-carrying `NamedIcon` injected as `mIcon` (a server-safe type, unlike the client-only `IIconRegister`). |
| `Block.getIcon(int, int)` (reflected) | `net.minecraft.block` | The block's own per-meta/side icon lookup, invoked reflectively so there is no compile-time client dependency. |
| `GameData.getBlockRegistry()` | `cpw.mods.fml.common.registry` | Iterate every registered block and get its `modid:name`. |

Forge/FML symbols the scaffold uses today:

| Symbol | Package | Used for |
| ------ | ------- | -------- |
| `FMLServerStartedEvent` | `cpw.mods.fml.common.event` | Entrypoint hook: fires after the dedicated server has fully started. |
| `FMLCommonHandler.exitJava(int, boolean)` | `cpw.mods.fml.common` | Terminate the JVM with a shell exit code once the dump finishes. |

## Build & run

Prerequisites:

- **A full JDK 25 installed locally.** The Gradle daemon runs on Java 25 (pinned by
  `gradle/gradle-daemon-jvm.properties`). Its auto-provision is unreliable: on Windows the
  template's pinned foojay URL resolves to a *JRE* (no `javac`/`javadoc`/`jar`), which
  Gradle rejects with "doesn't satisfy the specification", so the daemon fails to start.
  Install a real JDK 25 (e.g. Temurin) into a standard location and Gradle auto-detects it
  and skips the broken download. Verified working: Temurin `jdk-25` under the user Adoptium
  install dir.
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
# Headless CI (what jitpack.yml / the lane-4 workflow use):
./gradlew setupCIWorkspace

# Boot a dedicated server with GT5U + StructureLib + hard deps, run the dump, and exit.
# Exit code 0 == success. runServer transitively triggers the setup above.
./gradlew runServer -PdatasetOut=../../_dump_out
```

Run properties (`build.gradle.kts` forwards them into the server JVM as system properties):

- `-PdatasetOut=<dir>` where to emit `<dir>/multiblocks/` (resolved against the project dir;
  defaults to `<cwd>/dataset-out` if unset). The lane 4 workflow copies `<dir>/multiblocks/`
  into `data/`.
- `-PpackVersion=<ver>` / `-PextractorSha=<sha>` recorded in `_meta.json` (default `unknown-dev` /
  the `GITHUB_SHA` env var / `unknown`).
- `-PdebugMeta=<id>` diagnostics: log what the hint pass captured for one controller meta id.

Headless notes for CI (lane 4 wires these up; they are not committed here):

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
`FMLServerStartedEvent`, logs its scaffold-OK line, and calls `exitJava(0)`, yielding
`BUILD SUCCESSFUL`. On a fresh machine the wall-clock is dominated by the one-time Minecraft
decompile and the multi-GB dependency/toolchain download; once cached, a boot is about a
minute. From lane 4 onward CI runs this as the gating check.

## Licensing

Extracted structure *facts* are fine to commit to this Apache-2.0 repo, but LGPL assets
(textures) are not vendored here; they are fetched from the Nexus jar at build/preview
time. `NOTICE` credits GT5-Unofficial and StructureLib.
