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

This is the **lane 1 scaffold** (issue #44). What is here:

- The GTNH `ExampleMod1.7.10` buildscript wiring (Gradle wrapper, `gtnhconvention`
  convention plugin, GTNH Nexus repositories), configured for this tool in
  `gradle.properties`.
- `dependencies.gradle` pinning GT5-Unofficial and StructureLib from the GTNH Nexus.
- `DumperMod`, the `@Mod` entrypoint: it hooks `FMLServerStartedEvent`, runs an **empty**
  dump body, and exits the JVM (0 on success, nonzero on failure) so a `runServer` boot
  is a pass/fail gate.

What is **not** here yet: the actual dump loop (`StructureDumper` + `JsonWriter` +
`ErrorCollector`) lands in lane 2 (issue #45). No game logic lives in this tool by design;
the whole extractor targets a few hundred lines across 3-4 classes.

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
just works or fails to compile loudly and locally). The lane 1 scaffold touches **none** of
these yet (only Forge/FML, below). This is the intended surface for the lane 2 dump loop:

| Symbol | Package | Used for |
| ------ | ------- | -------- |
| `GregTechAPI.METATILEENTITIES` | `gregtech.api` | The array of registered meta tile entities to iterate. |
| `IMetaTileEntity` | `gregtech.api.interfaces.metatileentity` | Element type of that array; the thing we filter and place. |
| `IConstructable` | `com.gtnewhorizons.structurelib.alignment.constructable` | Filter: controllers that can build themselves; exposes `construct(ItemStack, boolean hintsOnly)`. |
| `ISurvivalConstructable` | `com.gtnewhorizons.structurelib.alignment.constructable` | Filter: the survival-buildable variant, swept for size/tier variants. |
| `StructureLibAPI` | `com.gtnewhorizons.structurelib` | Hint-block / channel plumbing (e.g. `gt_no_hatch`) read during the scan. |

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

# Boot a dedicated server with GT5U + StructureLib + hard deps, run the (empty) dump,
# and exit. Exit code 0 == success. runServer transitively triggers the setup above.
./gradlew runServer
```

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
