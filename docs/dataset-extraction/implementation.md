# Implementation: physical dataset extraction

How the pipeline meets [requirements.md](requirements.md). This doc is the *how*; the goals,
contracts, and policy are in requirements.md and are not restated here. The living roadmap is
[plan.md](plan.md) (temporary).

## Shape of the pipeline

```
tools/gtnh-extractor/   Forge 1.7.10 dedicated-server mod (JDK 25 daemon + JDK 8 toolchain)
   runServer, gated by -PdatasetOut and/or -PtextureOut
        |
        |-- structure dump  --> data/multiblocks/<controller>.json + _meta.json   (schema v1)
        '-- texture manifest --> data/textures/manifest.json                       (schema v2)
                                          |
                                          v
src/gtnh_solver/        pure Python; reads only the JSON
   dataset/schema.py        validate raw facts (Pydantic v2, extra="forbid")
   dataset/multiblocks.py   interpret facts -> MachinePhysical (footprint, io_faces, coil layers)
   previewer/textures.py    expand each machine into per-block textured cubes
   previewer/bake.py        composite one layer stack -> a flat PNG
   previewer/jar.py         fetch the pinned jar's PNG bytes (injected; never committed)
```

The tool runs as a **dedicated server** (headless, no OpenGL). `DumperMod` hooks
`FMLServerStartedEvent`, runs whichever passes are requested, and exits the JVM with a shell exit
code CI can gate on. Passing only `-PtextureOut` runs the texture pass alone and skips the
structure dump.

## The Java extractor (`tools/gtnh-extractor/src/.../extractor/`)

### DumperMod: boot, mode, exit
The `@Mod` entrypoint. Resolves the output directories and run metadata (pack version, mod
versions, git SHA) from system properties the Gradle build forwards, runs the texture pass and/or
the structure dump, and calls `exitJava(0)` on success or nonzero on any escaping `Throwable`, so
an empty or partial run fails loudly instead of producing a silent dataset.

### StructureDumper: the structure loop
Iterates `GregTechAPI.METATILEENTITIES`, keeps the ones that are `IConstructable`, and dumps each
into a scratch region high in the void world (origin `8, 210, 8`, controller facing NORTH). Per
controller:

- **Two-pass build, per variant.**
  1. *Hint pass*: `construct(trigger, hintsOnly=true)` to read the hologram's hatch dots. The
     hint walk is **client-only** (`if (!world.isRemote && hintsOnly) return false`), so the
     dumper reflectively (a) swaps a `RecordingProxy` into StructureLib's static `proxy` field to
     capture the hint particles and (b) flips `world.isRemote` true for the pass. Best-effort: it
     can abort early on client-only icon rendering, keeping whatever dots it captured so far.
  2. *Block pass*: `construct(trigger, hintsOnly=false)` with the `gt_no_hatch` channel set, so no
     real hatch tile entity is auto-placed and the scan sees the casing shell plus its hint slots.
- **Scan** the affected cube into `{ [dx,dy,dz], block, meta }` relative to the controller; a
  `fallbackBlocksFromHints` recovers the shell when the void-world build placed nothing.
- **Trigger-stack sweep (size variants).** Build for stack sizes `1..N` and collapse by an
  *occupied-cell signature* that ignores block identity: a stack size that changes the *shape*
  yields a new variant; one that only swaps a tiered block collapses. The sweep stops when the
  cell set stabilises.
- **Channel probe (tier substitutions).** StructureLib reads a channel via
  `ChannelDataAccessor.getChannelData`, which **falls back to the trigger's stack size** when the
  channel is unset, so the stack sweep already varies every channel at once. The probe then
  recovers the tier info the shape-collapse discarded: holding stack size 1, it sets one channel
  at a time to `2..N` and records the block that swaps at the controlled cells as a
  `{ channel_value, block, meta }` substitution.
- **Coil special case.** Classic furnaces (EBF, Multi Smelter) read the coil tier from the *stack
  size*, not the `coil` channel, so coils get their own stack-size sweep that identifies coil
  blocks by the `IHeatingCoil` interface the block itself implements (covering both the classic
  furnaces and the channel-bound mega furnaces).
- **Robustness.** `preloadRegion()` force-loads the scratch chunks up front to dodge a re-entrant
  `"Already decorating!!"` decorator cascade; hard caps bound the stack sweep (16), variant count
  (6), hinted cells (20000), scan dimension (80), and substitution entries (128) so a pathological
  controller lands on the failure list instead of running away.

### RecordingProxy: headless hint capture
A `CommonProxy` subclass whose `hintParticle*` overrides forward each hinted cell (coordinate,
block, meta) to a sink, so the hint dots that the server's no-op proxy would otherwise drop are
captured during the hint pass.

### TextureDumper: the layered manifest (schema v2)
Two mechanisms, composed:

- **MTE reflection** for machines, hatches, buses, and controller hulls. Basic single-block
  machines read their `ITexture[]` via the `getXxxFacingInactive/Active(byte)` accessors (no tile
  entity needed); hulls and hatches are placed once and read via
  `getTexture(base, side, facing, colour, active, redstone)`.
- **Block-icon reflection** for the plain structure blocks (casings, coils, glass): one un-tinted
  iconset layer per meta.

Each `ITexture` is **recursively flattened** into an ordered layer list: a sided/multi wrapper is
unwrapped via its `mTextures` (length 6 means sided, pick this side; otherwise composite all), a
rendered leaf resolves to `{ icon, rgba, glow }` from `mIconContainer` + `getRGBA()`/`mRGBa` +
`glow`, and a copied-block leaf resolves via the block-icon path. Icon *names* come from the
`Textures.BlockIcons` enum `name()` (which maps 1:1 to the PNG under
`assets/<modid>/textures/blocks/`) because the client-only `getTextureFile()` throws on the
server; `populateIconNames()` injects a server-safe `NamedIcon` into each `BlockIcons.mIcon` field
so a block's own `getIcon` hands back a named icon. Unknown `ITexture` implementations are recorded
as `gaps`, never guessed.

### JsonWriter and ErrorCollector
`JsonWriter` serialises schema v1 with Gson, every list sorted and every key order fixed (blocks
and hints by `(dy, dz, dx)` then identity), so a regenerated dump is a minimal, reviewable diff;
field order mirrors `schema.py` exactly so it loads without a translation step. `ErrorCollector`
gathers per-controller failures into `_meta.json.failures`.

## The Python consumer (`src/gtnh_solver/`)

### dataset/schema.py: the contract
Pydantic v2 models with `extra="forbid"` that re-state the extractor's raw facts (controllers,
variants, blocks, hints, substitutions) and nothing more. This is the cross-language contract;
`multiblock_json_schema()` derives a JSON Schema from it for the Java tests, so the two cannot
drift. It holds **no interpretation**.

### dataset/multiblocks.py: facts to physical rules
`to_physical()` turns a validated `MultiblockDoc` into a `MachinePhysical`:

- **footprint** from the box the *primary variant* (the one placing the most blocks) actually
  spans, cross-checked against the reported bbox (a mismatch raises `DatasetError`, a scan or
  facing bug);
- **io_faces** from which bounding-box faces the hint positions touch (a hint on an axis min or
  max plane means a hatch there may face outward that way);
- **coil_layer_count** from the y-layers whose blocks are in the `coil` substitution table.

`load_physical_dataset()` keys every machine by display name into a `PhysicalDataset`. The
gtnh-factory-flow adapter (`adapter/core.py`) uses it **opt-in**: with no dataset it keeps the
crude 1x1x1 default, so the solver runs with or without a committed dump.

### previewer/textures.py: per-block textured cubes
`texturize_scene()` materialises block accuracy only at preview time. It expands each placed
machine into one textured cube per constituent block (from its multiblock doc, or a single cube
for a 1x1x1 machine), yaw-rotating the dump's NORTH-built blocks to the machine's placed front.
Each cube face resolves its layer stack from the manifest, which `bake.py` bakes to a flat PNG
embedded as a `data:` URI.

- **Single-block machines** have no multiblock doc (the whole machine *is* one block), so they
  resolve through the manifest's MTE index by display name. A plan names them generically ("Forge
  Hammer") but the manifest keys them by their tier-prefixed in-game name ("Basic Forge Hammer" at
  LV, "Advanced" at MV); resolution tries exact, then normalised, then the tier prefix plus a
  `Basic` fallback. Above MV the naming diverges per family, so those fall back to `Basic`, an
  honest stand-in since GT single-block skins are near-identical across tiers.
- **Graceful degradation is the contract:** a machine with no doc, an all-unresolved variant, no
  PNG bytes, or a Pillow-less install keeps its flat placeholder box; a single unresolved face
  falls back to flat colour there. Nothing here raises on a miss.

### previewer/bake.py and previewer/jar.py
`bake.py` composites a layer stack into one flat 16x16 PNG the way GT renders it: the base sprite
tinted by the RGBA multiply (without it every casing renders grey), overlays alpha-composited,
glow and animation-frame handled, behind an optional Pillow dependency. `jar.py` is the one
network-touching shim: it fetches the **pinned** GT5-Unofficial jar from the Nexus, caches it
outside the repo tree, and reads the requested `iconsets/*.png` bytes; it is injected as a
`png_provider` so the test suite never downloads. PNGs are embedded only in the emitted HTML,
never committed.

## Known gaps and limitations

Where the code stops short of [requirements.md](requirements.md) today, the raw material for
planning next steps:

- **No hatch-assignment stage.** The dump records hint *slots* and the loader turns them into face
  capabilities (`io_faces`), but nothing chooses a concrete hatch (input vs output, item vs fluid,
  tier) or emits it as a placeable block. A build guide cannot yet say "put an LV input hatch
  here."
- **Doc-less multiblocks stay placeholders.** A multiblock whose structure failed extraction (e.g.
  the dynamic-height Distillation Tower) correctly refuses to collapse to a single cube; it renders
  as its reserved-footprint placeholder until its doc exists.
- **Single-block tier naming above MV** falls back to `Basic`, so a high-tier machine may show the
  low-tier skin (near-identical in GT, but not exact).
- **Stale scaffold comments in `DumperMod`** still describe the lane-1 "empty dump" scaffold and a
  lane-4 CI workflow that was dropped when the structure dump went local-only. The runtime is
  correct; the comments are not.
