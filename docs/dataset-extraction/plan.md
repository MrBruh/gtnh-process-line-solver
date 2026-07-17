# Plan: Automated Multiblock & Texture Dataset Extraction

**Status:** Shipped, reconciled with the code (2026-07-17). Originally a forward-looking design
doc; the pipeline described below is now built (lanes 1-3 and 5-7; lane 4 dropped). This file is
kept as the **temporary** working roadmap and a cross-check against the durable docs. The stable
references are [requirements.md](requirements.md) (the *what*) and [implementation.md](implementation.md)
(the *how*); where this plan and those disagree, they win, and this plan gets corrected.
**Audience:** gtnh-process-line-solver contributors.
**Goal:** Replace the hand-pinned physical dataset with an automated pipeline that extracts
multiblock footprints, exact block configurations, hatch-position constraints, and texture
mappings directly from the GTNH mod source.

> **Amendment (2026-07-17): ALL generated data is now local-only and version-namespaced.** This
> supersedes the split below: the texture manifest is no longer committed either. The extractor
> regenerates both the structure dump and the texture manifest on demand into gitignored
> `data/<version>/{multiblocks,textures}/` folders, so a user can hold several pack versions side by
> side without overwriting. The repo ships only the two multiblock fixtures and a small (~120 KB)
> `data/textures/manifest.json` scoped to the example lines' machines (derived from a full manifest),
> so `gtnh-solve --preview examples/*.json` still skins out of the box. `resolve_dataset_path` picks
> the newest local `data/<version>/` per sub-path, else the fixtures; `--dataset-version` pins one.
> Both `update-dataset.yml` and `update-textures.yml`, and the `tools/dataset_ci` helper, are
> retired. Where sections 3, 5, 6, 8 below still say the manifest is committed or that
> `update-textures.yml` stays, read this amendment instead.

> **Amendment (2026-07-08): the structure dump stays local. It is never committed and has
> no CI.** The full multiblock structure dump (~190 controllers, ~17 MB of churny generated
> JSON) is produced on a developer machine with the extractor and written to a gitignored
> `data/multiblocks/`. The repo commits only the two curated fixtures (EBF, Vacuum Freezer)
> the tests depend on. There is **no** `update-dataset.yml`: the dump is not worth its repo
> weight or a weekly Forge CI run (the shipped example lines barely use multiblock docs).
> The **texture manifest is unaffected** and stays committed with its own
> `update-textures.yml` (it is the keystone that skins every machine). Consequence, accepted:
> a fresh clone can only place and render the two fixtures plus single-block machines; any
> other multiblock is a 1x1x1 placeholder until a developer runs the extractor locally.
> Sections 3, 6, 7 and lane 4 below are superseded where they describe committing the dump
> or a structure-dump CI PR.

---

## 1. Background & findings

### 1.1 Where the data lives

- **GT5-Unofficial** (`github.com/GTNewHorizons/GT5-Unofficial`, LGPL) is the single source of truth. Nearly every multiblock-providing mod has been **merged into this one repo** as packages: `gregtech`, `bartworks`, `gtPlusPlus`, `tectech`, `goodgenerator`, `kekztech`, `kubatech`, `gtnhlanth`, `gtnhintergalactic`, `ggfab`, and more. One dependency covers ~all multiblocks we care about.
- **StructureLib** (`github.com/GTNewHorizons/StructureLib`, LGPL) is the framework every multiblock structure is defined with.
- **Version pinning:** `GTNewHorizons/DreamAssemblerXXL` → `releases/manifests/<packversion>.json` lists the exact mod version per pack release (e.g. `2.9.0-beta-1.json` pins `GT5-Unofficial: 5.09.52.594`, `StructureLib: 1.4.38`). GitHub tags on the mod repos match these versions. The repo pins its own build via `gtnh.lock.json`, maintained by hand alongside a local dump refresh.
- **Binary artifacts:** versioned jars are served from the GTNH Maven:
  `https://nexus.gtnewhorizons.com/repository/public/com/github/GTNewHorizons/<Mod>/<version>/<Mod>-<version>.jar` (verified reachable). Jars contain the compiled code **and** the `assets/` texture trees.

### 1.2 Why we can't just parse files

Multiblock definitions are **Java code, not data**. Each controller builds an
`IStructureDefinition` from string shape arrays whose characters map to *elements* -
sometimes a plain block, but frequently a lambda: tiered coils (`ofBlocksTiered`),
hatch-or-casing adders, channel-configurable blocks, and fully dynamic shapes
(distillation tower layer stacking, fusion rings, drills). Of ~250 classes implementing
`IConstructable`/`ISurvivalConstructable`, only ~74 use plain static
`transpose(String[][])` arrays. **Static source parsing covers roughly a third of
multiblocks and silently mis-describes the rest.** It is not the primary strategy.

### 1.3 The strategy that is guaranteed correct

**Runtime extraction.** The in-game NEI structure preview and the Multiblock Structure
Hologram Projector both work by calling each controller's `construct()` against a
world and reading back the placed blocks. We do the same thing headlessly: run the
mods on a dedicated server, build every multiblock into a void world, scan the
region, and dump JSON. Because we execute the same code the hologram projector
executes, the output matches in-game behavior by construction.

---

## 2. Design principles

1. **Quarantine the Java.** One standalone Gradle tool in `tools/`. The Python
   solver never imports it and never calls it at runtime - it only reads the JSON the
   tool emitted. If the tool vanished tomorrow, the solver still runs on the committed
   dataset (the two fixtures) and any locally generated dump.
2. **Never fork or patch GT5-Unofficial.** Depend on it as a library from the GTNH
   Nexus. An update is a version-number bump, not a merge.
3. **Extractor emits facts, Python interprets.** The Java dumps blocks, coordinates,
   hints, variants, and substitutions. Footprint bounding boxes, face constraints, tier
   semantics, and everything IR-shaped stays in Python, where our contracts and tests
   live. The moment the Java starts making solver-shaped decisions it becomes a second
   codebase - don't.
4. **Generated data is local, not committed.** Both the structure dump and the texture manifest
   are regenerated on demand into gitignored `data/<version>/` folders (2026-07-17 amendment);
   neither has a CI or PR flow. Only the two fixtures plus a small example-scoped manifest ship.
5. **Fail loud, per-controller.** One broken multiblock must never kill a run; it lands
   on the `_meta.json` failure list as a visible coverage number instead of a silent
   absence.

---

## 3. Repository layout

```
gtnh-process-line-solver/
├── src/gtnh_solver/                 # pure Python, reads only data/ (never invokes the tool)
│   ├── dataset/                     #   schema.py (contract) + multiblocks.py (facts -> physical)
│   └── previewer/                   #   textures.py + bake.py + jar.py (render from the manifest)
├── data/
│   ├── multiblocks/                 # LOCAL-ONLY generated dump (gitignored); only the two
│   │   │                            #   curated fixtures are committed, for the tests
│   │   ├── _meta.json               # schema version, pack version, mod versions,
│   │   │                            #   generation date, per-controller failures
│   │   └── <safe_registry_name>_<meta>.json
│   └── textures/
│       └── manifest.json            # committed schema-2 layered manifest
│                                    #   (PNGs are NOT committed; fetched at preview time)
├── tools/gtnh-extractor/            # the ONLY Java in the repo
│   ├── build.gradle                 # GTNH buildscript (ExampleMod1.7.10 template)
│   ├── gradle.properties
│   ├── dependencies.gradle          # GT5-Unofficial + StructureLib from GTNH Nexus
│   └── src/main/java/…              # seven classes (see §4)
├── gtnh.lock.json                   # pack version → pinned mod versions
│                                    #   (derived from DreamAssemblerXXL manifest; hand-maintained)
└── .github/workflows/
    └── update-textures.yml          # texture manifest (separate, may lag a version)
                                     #   (no update-dataset.yml: the structure dump is local-only)
```

Licensing note: extracted structure *facts* are fine, but do not vendor LGPL PNGs into
this Apache-2.0 repo. Fetch textures from the Nexus jar at preview time and keep the
`NOTICE` entries crediting GT5-Unofficial/StructureLib either way.

---

## 4. The extractor tool (`tools/gtnh-extractor/`)

Built on GTNH's **ExampleMod1.7.10** template - their standard buildscript handles the
Forge 1.7.10 dev workspace, deobf mappings, and pulls dependencies from the GTNH Nexus.
It carries **no game logic of its own** (principle 3), but it grew past the first "a few
hundred lines" estimate: the headless machinery (the client-only hint walk, the recursive
`ITexture` flatten) needed real scaffolding.

Run mode: `./gradlew runServer` - a **dedicated server**, not a client. Headless, no
OpenGL. Passes are gated by `-PdatasetOut` (structure dump) and `-PtextureOut` (texture
manifest); a texture-only run (only `-PtextureOut`) skips the structure dump entirely.

### 4.1 Classes

Seven classes, not the 3-4 first sketched - the extra three are the data model, the hint
recorder, and the texture pass.

**`DumperMod`** - the `@Mod` entrypoint.
Hooks `FMLServerStartedEvent`, resolves the output dirs + run metadata (pack version, mod
versions, git SHA) from system properties the build forwards, runs the texture pass and/or
the structure dump, then calls `FMLCommonHandler.instance().exitJava(0)` (nonzero on any
escaping `Throwable`, so an empty/partial run fails loudly).

**`StructureDumper`** - the core structure loop.
- Iterate `GregTechAPI.METATILEENTITIES`; keep the `IConstructable` ones.
- Place each controller at a fixed scratch origin (`8, 210, 8`) facing NORTH, high in the
  void world so the region is empty air to build into and wipe. `preloadRegion()` force-loads
  the working chunks up front to dodge a re-entrant `"Already decorating!!"` decorator cascade.
- **Two-pass build, per variant.** A *hint pass* (`construct(trigger, hintsOnly=true)`) reads
  the hologram's hatch dots; because that walk is **client-only**
  (`if (!world.isRemote && hintsOnly) return false`), the dumper reflectively swaps a
  `RecordingProxy` into StructureLib's static `proxy` field and flips `world.isRemote` true for
  the pass. A *block pass* (`construct(trigger, hintsOnly=false)`) with `gt_no_hatch` set builds
  the casing shell without auto-placing real hatch tile entities. The region is wiped between
  passes; a `fallbackBlocksFromHints` recovers the shell if the void-world build placed nothing.
- **Trigger-stack sweep (size variants).** Build for stack sizes `1..N` and collapse by an
  occupied-cell *signature* that ignores block identity: a shape change is a new variant; an
  identity-only tier swap collapses. Stops when the cell set stabilises.
- **Channel probe (tier substitutions).** `ChannelDataAccessor.getChannelData` falls back to the
  trigger's *stack size* when a channel is unset, so the stack sweep already varies every channel
  at once. The probe recovers the tier info the shape-collapse discarded: at stack size 1 it sets
  one channel at a time to `2..N` and records the swapped block as a `{channel_value, block, meta}`
  substitution. Coils are a special case (classic furnaces read the coil tier from the stack size,
  not the `coil` channel), swept separately and identified by the block's own `IHeatingCoil`
  interface.
- **Scan** the affected region into `{[dx,dy,dz], block, meta}` relative to the controller, plus
  the facing convention and hint-dot positions (legal hatch slots → the solver's face constraints).
- Hard caps bound the sweep (stack 16, variants 6, cells 20000, scan dim 80, substitution entries
  128) so a pathological controller lands on the failure list rather than running away.

**`RecordingProxy`** - a `CommonProxy` whose `hintParticle*` overrides forward each hinted cell
to a sink, capturing headlessly the dots the server's no-op proxy would otherwise drop.

**`TextureDumper`** - the layered texture manifest (schema 2); see §5.

**`DumpModel`** - plain data holders mirroring the schema-v1 shape (controller, variant, block,
hint, substitution, failure); raw facts only, field names matching `schema.py`.

**`JsonWriter`** - plain Gson (already on the 1.7.10 classpath). Every list sorted and key order
fixed (blocks/hints by `(dy, dz, dx)` then identity) so dataset diffs are human-reviewable and
load straight through the Pydantic loader. One file per controller plus `_meta.json`.

**`ErrorCollector`** - wraps every controller so a `Throwable`, a runaway sweep, or an empty scan
becomes a `_meta.json` failure `{registry_name, reason}` instead of aborting the run.

### 4.2 Output schema (v1) - realized in `src/gtnh_solver/dataset/schema.py`

The contract is no longer a sketch; it is the Pydantic models in `dataset/schema.py`
(`extra="forbid"`), and a JSON Schema is derived from them for the Java tests so the two cannot
drift. Illustrative shape:

```jsonc
// data/multiblocks/gregtech_gt_blockmachines_1000.json  (illustrative)
{
  "schema": 1,
  "controller": {
    "registry_name": "gregtech:gt.blockmachines",
    "meta": 1000,
    "display_name": "Electric Blast Furnace",
    "source_class": "gregtech.common.tileentities.machines.multi.MTEElectricBlastFurnace",
    "facing_convention": "controller front = NORTH (-Z), ExtendedFacing …; d = [dx,dy,dz] deltas"
  },
  "variants": [
    {
      "trigger_stack_size": 1,
      "channels": { "gt_no_hatch": 1 },
      "blocks": [ { "d": [0, 0, 0], "block": "…", "meta": 0 }, … ],
      "hints": [ { "d": [1, 0, 0], "hint": 1 }, … ],
      "bbox": [3, 4, 3]                    // derived, cross-checked by the adapter
    }
  ],
  "substitutions": {                        // identity-only channel effects (coil/glass/…)
    "coil": [ { "channel_value": 1, "block": "…", "meta": 0 }, … ]
  },
  "failures": []
}
```

`_meta.json` records: schema version, pack version, `{mod: version}` map, generation
timestamp, extractor git SHA, controller count, and the failure list.

### 4.3 What the Java explicitly does NOT do

No footprint/rotation math beyond raw coordinates, no tier semantics, no IR mapping,
no filtering of "which multiblocks the solver supports." All of that is the Python
side's job (`dataset/multiblocks.py`), covered by the existing test strategy.

---

## 5. Texture pipeline (built as `TextureDumper` + `previewer/`)

Covers **casings (multiblock shells), single-block machines, hatches/buses, and multiblock
controller hulls**; pipe/cable material-tint fidelity is the related follow-up (issue #4). Two
parts with very different difficulty:

**5.1 The PNGs - trivial, no Java.** The previewer's `jar.py` fetches the pinned GT5-Unofficial
jar from the Nexus once, caches it **outside** the repo tree, and reads the requested
`iconsets/*.png` entries straight out of the zip (the `gregtech` iconsets folder alone has ~2,250
PNGs including all casings; per-machine overlays live in `basicmachines/<machine>/` folders;
merged mods have parallel `assets/<modid>/…` trees). Fetched at preview time, injected as a
`png_provider` so the test suite never downloads; **never committed**.

**5.2 How GT actually textures blocks - layer stacks, not single images.**
Verified against the rendering code:

- **Casings** are plain per-meta icons from `iconsets/` (enum constant name == PNG filename).
  One layer, no tint.
- **Single-block machines** are composites assembled in code, per side and per active-state:
  - *Base layer:* per-tier casing PNGs **tinted with an RGBA multiplier** (`Dyes.MACHINE_METAL`
    by default). The PNGs on disk are neutral; the tier colour only exists after tinting, so a
    previewer that skips the multiply renders every machine grey (`bake.py` applies it, pinned by
    a golden test).
  - *Overlay layer(s):* `OVERLAY_{FRONT,TOP,SIDE,BOTTOM}[_ACTIVE][_GLOW]`, optional per face,
    `_GLOW` emissive, some animated. Hatches, buses, and older machines use the same mechanism.
- **Cables & pipes** (the solver routes these): texture = the material's `TextureSet` icon tinted
  with the material's `mRGBa`. Pipe/cable MTEs flow through the same MTE reflection path below, but
  dedicated material-tint handling for the router's cables is tracked as issue #4.

**5.3 Extraction - server-side ITexture reflection, no client needed.** MTE constructors run
during registration on dedicated servers too, so every texture object exists in the headless run;
`ITexture` implementations store `mIconContainer`, `mRGBa`, and `glow` as plain fields; enum icon
containers' `name()` matches the PNG filename. The client-only `getTextureFile()` is
`@SideOnly(CLIENT)` and throws on the server, so `TextureDumper` takes icon *names* instead:
`populateIconNames()` injects a server-safe `NamedIcon` into every `Textures.BlockIcons.mIcon`
field so a block's own `getIcon` hands back a named icon. Two composed mechanisms:

1. **MTE reflection** (machines, hatches, buses, controller hulls): basic single-block machines via
   the `getXxxFacingInactive/Active(byte)` accessors (no tile entity needed); hulls/hatches placed
   once and read via `getTexture(base, side, facing, colour, active, redstone)`.
2. **Block-icon reflection** (plain structure blocks - casings, coils, glass): one un-tinted
   iconset layer per real meta.

Each `ITexture` is recursively flattened into an ordered layer list (sided/multi wrappers unwrapped
via `mTextures`, rendered leaves → `{icon, rgba, glow}`, copied-block leaves via the block-icon
path). Unknown `ITexture` implementations → the `gaps` list, never guessed. **Option B (xvfb
client-mode dump)** stays documented as a fallback but has not been needed.

**5.4 Manifest schema (schema 2).** `data/textures/manifest.json` is:
`{ schema, method, provenance{pack_version, mod_versions, generated_at, extractor_sha, coverage{blocks, mte, icons, gaps}}, asset_root, blocks{}, icons{}, gaps[] }`.
Each `blocks` entry is keyed `"<registry_name>|<meta>"` (MTEs keyed by id) and holds
`{ kind: "mte"|"block", display_name?, source_class?, sides{ SIDE → { "inactive"|"active" → [ {icon, rgba, glow}, … ] } } }`
(layers ordered bottom→top). `icons` maps an icon name to its jar path; `gaps` lists unresolved
`(block, meta, side, reason)`. There is no separate `materials` table (an earlier idea) - cable
tint fidelity rides the block/MTE entries and issue #4.

**5.5 Compositing (`previewer/bake.py`).** A Pillow post-step **pre-bakes** flat PNGs per
`(block, meta, side, state)` - base × RGBA multiply, alpha-composite overlays, glow, animated
textures use frame 0 - so the three.js previewer only ever loads flat images, embedded as `data:`
URIs. Pillow is the optional `preview` extra; without it the previewer degrades to placeholder
boxes rather than failing. `previewer/textures.py` expands each machine into per-block textured
cubes and drives the bake.

Either way, **textures are a separate workflow** from the structure dump. Structures are
correctness-critical for the solver; the texture manifest only feeds the previewer and may lag a
pack version behind without hurting anyone.

---

## 6. Regenerating the structure dump (local only)

There is **no** `update-dataset.yml` and no CI for structures (2026-07-08 amendment): the
dump is a developer convenience, produced on demand and never committed. Textures keep
their own committed workflow (`update-textures.yml`, §5), which is unaffected.

To refresh multiblocks locally (needs the JDK-25 Forge toolchain; see the extractor README
and HANDOFF). Track the latest stable pack, not dailies:

```bash
cd tools/gtnh-extractor
export JAVA_HOME=".../jdk-25"
printf 'n\ny\n' | ./gradlew --no-daemon runServer "-PdatasetOut=<dir>"
cp -r <dir>/multiblocks/* data/multiblocks/   # gitignored except the two fixtures
```

`data/multiblocks/` is gitignored apart from the two curated fixtures the tests pin
(`gregtech_machine_1000.json` = EBF, `_1001.json` = Vacuum Freezer). A contributor who
wants the solver to place arbitrary multiblocks runs the extractor once; the committed tree
stays lean. `gtnh.lock.json` stays as the local record of what pack the dump came from.

---

## 7. Testing & acceptance

- **Golden tests (Python):** hard-code a handful of ground truths that only change
  when GTNH actually changes them, e.g. "EBF main piece is 3x3x4", "EBF has exactly
  2 coil layers", "hint blocks exist on the EBF hatch layer", "Vacuum Freezer is 3x3x3".
  These catch extractor regressions (bad sweep, bad scan bounds, facing bugs) against the two
  committed fixtures.
- **Schema validation:** every file in `data/multiblocks/` validates against the
  Pydantic loader; `_meta.json` failure list is asserted below a threshold (start lenient,
  ratchet down).
- **Acceptance for the whole plan:** for textures, a pack-version bump requires editing
  nothing by hand (the texture workflow opens the PR) and reviewing one PR. For structures there
  is nothing to review because nothing is committed; a developer who needs the updated
  multiblocks reruns the local extractor (§6).

---

## 8. Work lanes

All shipped except the dropped lane 4. Kept for provenance and to anchor the follow-ups in §9.

| # | Lane | Deliverable | Status |
|---|------|-------------|--------|
| 1 | Extractor scaffold | `tools/gtnh-extractor/` builds against pinned GT5U/StructureLib; `DumperMod` boots a dedicated server and exits cleanly | **done** (#44) |
| 2 | Core dump | `StructureDumper` + `DumpModel` + `JsonWriter` + `ErrorCollector`; two-pass hint/block build; `gt_no_hatch`; hints captured; schema v1 | **done** (#45) |
| 3 | Channel handling | Stack-size sweep + identity-substitution probe + coil sweep; variant/entry caps | **done** (#46) |
| 4 | ~~Structure-dump CI~~ | Dropped 2026-07-08: structures are local-only, no `update-dataset.yml`; `gtnh.lock.json` is the hand-maintained pin | **dropped** |
| 5 | Python adapter | `data/multiblocks/` → `MachinePhysical` (footprint/faces/coil tiers); opt-in in the flow adapter; golden tests | **done** (#48) |
| 6 | Texture manifest | `TextureDumper` server-side layered reflection (schema 2) for casings, single-block machines, hatches, hulls (manifest now local-only, see the 2026-07-17 amendment) | **done** (#79/#81) |
| 7 | Texture bake + previewer hookup | `previewer/bake.py` (tint + composite → flat PNGs) + `previewer/textures.py` per-block cubes; three.js consumes baked `data:` URIs | **done** (#82/#83) |

**Not yet built (see §9 and requirements.md "Known gaps"):** a hatch-assignment stage (choose and
emit a concrete input/output hatch per hint slot); doc-less dynamic multiblocks (e.g. Distillation
Tower) still render as placeholders; single-block tier naming above MV falls back to `Basic`; the
stale scaffold/lane-4 comments in `DumperMod` need a cleanup pass.

---

## 9. Risks & mitigations

- **GT5U API churn** (e.g. `METATILEENTITIES` or `IConstructable` moves). These are
  ancient, stable interfaces and we never reference the ~250 controller classes by
  name - but if it happens the extractor fails to compile. *Mitigation:* keep the tool's GT5U
  API surface to a handful of symbols documented in the tool README; the texture workflow's build
  fails loudly, and a local structure run fails at the developer's machine.
- **Non-terminating or explosive variant sweeps** (dynamic structures with many
  sizes). *Mitigation:* the hard caps in §4.1; overflow goes to the failure list for a human
  decision.
- **Multiblocks that need world context** (e.g. checks that touch biomes/dimensions)
  may fail in a void world. *Mitigation:* that's what the failure list is for; the
  `fallbackBlocksFromHints` path recovers some, and a small tail may stay hand-curated
  (the Distillation Tower is a known doc-less case).
- **1.7.10 toolchain on modern CI/dev** (Java version matrix, Gradle daemon quirks). *Mitigation:*
  the daemon is pinned to JDK 25 with a JDK 8 mod toolchain; `runServer` takes the EULA on stdin.
- **Exotic renderers escape the layer-stack model.** The base+overlay reflection covers casings,
  basic machines, hatches, hulls, and coils, but a tail of custom `ITexture`/ISBRH renderers may
  not decompose. *Mitigation:* unknown texture classes go to the `gaps` list for case-by-case
  handling; Option B (xvfb client dump) remains a documented fallback; textures are previewer-only,
  so gaps never block the solver.
- **License hygiene.** LGPL assets stay out of the repo; extracted structure facts are fine;
  `NOTICE` credits GT5-Unofficial and StructureLib.

## 10. Non-goals (v1)

- Parsing Java source statically (kept only as a possible one-off bootstrap, never as
  the pipeline).
- Supporting non-GT multiblocks that don't use StructureLib (e.g. Thaumcraft altars).
- Tracking daily/experimental pack builds.
- Extracting recipes or any non-physical data (that's gtnh-factory-flow's domain).
- Committing the structure dump or any PNG.

## 11. Reference links

- GT5-Unofficial: https://github.com/GTNewHorizons/GT5-Unofficial
- StructureLib: https://github.com/GTNewHorizons/StructureLib
- Pack manifests: https://github.com/GTNewHorizons/DreamAssemblerXXL (`releases/manifests/`)
- GTNH Maven: https://nexus.gtnewhorizons.com/repository/public/
- Dev-environment docs: https://wiki.gtnewhorizons.com/wiki/Development
- Mod template: https://github.com/GTNewHorizons/ExampleMod1.7.10
- MSHP / hints background: https://wiki.gtnewhorizons.com/wiki/Multiblock_Structure_Hologram_Projector
