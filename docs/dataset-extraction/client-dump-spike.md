# Spike: dumping textures from a client JVM

**Question.** [`texture-resolution.md`](texture-resolution.md) opens by saying everything follows from
one fact: the texture pass runs in a headless dedicated server, where FML's `SideTransformer` deletes
every `@SideOnly(Side.CLIENT)` member. Every method on `net.minecraft.util.IIcon` is annotated that
way, `getIconName()` included, so the server cannot ask a sprite what it is called. Five routes, a
hand-transcribed casing table, an icon-injection pass and a bytecode matcher all exist to recover a
name that one deleted method would have returned.

Does running the same dump in a client JVM remove that whole class of problem?

**Answer: yes, decisively, at both packs.** Measured, not inferred.

## The four numbers

Each column is one real dump. Server and client ran the same binary at the same mod pins; the only
difference is which JVM hosted it.

| | 2.8.4 server | 2.8.4 client | 2.9 server | 2.9 client |
|---|---|---|---|---|
| **unresolved (block, meta) pairs** | 45 | **1** | 261 | **12** |
| **multiblocks with a gap** | 56 of 208 | **1 of 208** | 208 of 296 | **13 of 296** |
| manifest `gaps` records | 5492 | 413 | 25430 | 12902 |
| icon names resolved | 2370 | 3510 | 1845 | 3084 |
| block entries | 11208 | 15850 | 9998 | 17132 |
| `gregtech` icons with no PNG in the pinned jar | 5 | 3 \* | 147 | **2** |
| wall clock of the dump itself | 3 s | 13 s | 36 s | 3 s |

\* The 2.8.4 client dump predates the `minecraft`-domain fix below; 34 of its 3510 icon paths carry
the wrong domain. Its headline rows are unaffected. The same fix took 2.9's `gregtech` breakage from
147 to 2, so the correction is measured, just not at this pin.

**The plan's success bar was "client-side 2.9 lands at or below 2.8.4's current gap level" (45 pairs,
56 of 208).** It landed at 12 pairs and 13 of 296, which is below 2.8.4's *client* result in
proportion and an order of magnitude below the bar that was set.

## The four questions the spike was to answer

**1. Do 2.9's `gt.blockframes`, `gt.blockcasings2` and `gt.blockcasings4` resolve?** Yes, completely.
Those three families carried 807 gap records server-side (777 + 16 + 14) and carry **zero** on the
client. Spot-checked against the pinned jar, each resolves to a sprite that exists:

| pair | client layer | in jar |
|---|---|---|
| `gregtech:gt.blockframes\|0` | `gregtech:materialicons/NONE/frameGt` | yes |
| `gregtech:gt.blockframes\|316` | `gregtech:materialicons/NONE/frameGt` | yes |
| `gregtech:gt.blockcasings4\|1` | `gregtech:iconsets/MACHINE_CASING_CLEAN_STAINLESSSTEEL` | yes |
| `gregtech:gt.blockcasings2\|13` | `gregtech:iconsets/MACHINE_CASING_PIPE_STEEL` | yes |

**2. Does the 17364-strong `unresolvable IIconContainer` count collapse?** Partly, and the split is
the interesting part:

| container class | 2.9 server | 2.9 client |
|---|---|---|
| `GTTextureSetBlockIconContainer` | 14676 | **0** |
| `gregtech.api.enums.StoneType$1` | 2688 | 12480 |

The 2.9-specific failure goes to zero. That is the one the `blockIconFieldNames` map in #172 was
built for, and the client does not need the map. The residual is entirely `StoneType$1`, the
anonymous container behind ore-stone variants; it *rises* only because the client enumerates 17132
blocks where the server reached 9998, so it meets more ore blocks. No process line places an ore
block, and the multiblock-scoped number confirms that reading: 208 of 296 down to 13 of 296.

**3. Does the run exit cleanly and write a complete manifest?** Yes. Both client runs ended
`BUILD SUCCESSFUL`, the manifest was complete, and the graceful `System.exit` path worked, so the
shutdown watchdog added for this spike never fired. 2.9's dump took 1 m 48 s end to end from a warm
workspace.

**4. Does anything resolve worse?** No. Across both packs the client dump is a strict superset:
**4642 drawable keys gained and 0 lost at 2.8.4; 7134 gained and 0 lost at 2.9.** The jar check says
the same thing more sharply: `gregtech` icons naming a PNG the jar does not carry fell from 147 to 2
at 2.9. The client route repaired 146 paths the server route had been inventing: all 30
`materialicons/*/blockCasing` and `blockCasingAdvanced` entries, 84 `*_GLOW` overlays, and 32 others.
One name is newly broken, `gregtech:iconsets/TRANSPARENT`, which the jar carries no file for, and one
of the server's 147 is still missing.

Where the two dumps name the same layer, they disagree 4325 times out of 30419. Every disagreement
sampled had the client right, because the server was falling back to a generic `textureName` field
where the client reads the sprite actually bound:

| pair | server | client |
|---|---|---|
| `miscutils:blockBlockBlackTitanium\|2` | `miscutils:blockBlock` | `gregtech:materialicons/NONE/block5` |
| `minecraft:redstone_wire\|9` | `minecraft:redstone_dust` | `minecraft:redstone_dust_cross` |
| `minecraft:wool\|5` | `minecraft:wool_colored` | `minecraft:wool_colored_lime` |
| `appliedenergistics2:tile.BlockSecurity\|8` | `appliedenergistics2:BlockSecurity` | `appliedenergistics2:BlockSecuritySide` |

## What the client run costs

- **A GL window, but not a human.** `DumperMod` fires on `FMLServerStartedEvent`, which a client
  raises for its integrated server, so a world has to be loaded. `-PautoWorld=true` loads one:
  `ClientProxy` waits for the main menu and makes the same `Minecraft.launchIntegratedServer` call
  the Create New World button makes, on a superflat scratch world it owns and is allowed to delete.
  A 2.9 client dump then runs start to finish in **1 m 27 s with nobody at the keyboard**, and
  produces a manifest byte-for-byte identical to the clicked one.

  The GL window is not removable. 1.7.10 is LWJGL2, which needs a real OpenGL context, and the whole
  point of the client route is reading the atlas that context stitches. It can be minimised out of
  the way (measured: no effect on the dump, 1 m 27 s minimised against 1 m 29 s visible), but a
  headless client is not available on Windows. So this is scriptable, and still not a CI job.
- **Icon injection is off.** A client must not write `NamedIcon` stubs over live sprites: the render
  thread is reading those fields, and it would mask the very difference being measured. Both client
  runs above had `-PinjectIcons` defaulted off and still resolved everything, which means
  `populateIconNames` and `injectQueuedIconContainers` earn nothing there.
- **A hard exit mid-session.** Use a throwaway world.

## Two things the spike settled on the way

**Icons really are registered before the integrated server starts.** The plan flagged this as
unverified. The client log shows `GTMod: Starting Block Icon Load Phase` then
`Created: 4096x2048 textures/blocks-atlas` during resource load, well before the main menu, and both
dumps ran after that. That phase *is* `BlockMachines.registerBlockIcons` draining
`GregTechAPI.sGTBlockIconload`, the queue `injectQueuedIconContainers` exists to work around.

**A bare sprite name is a `minecraft` name.** Found by asking question 4 rather than by re-reading the
code. `TextureMap` keys a sprite by the exact string given to `registerIcon` and resolves it with
`new ResourceLocation(name)`, whose domain defaults to `minecraft`. A bartworks block whose `getIcon`
returns the vanilla stone sprite names it `"stone"`, and that sprite is `minecraft:stone`. Resolving
it against the block's own registry domain, which is correct for the un-annotated `textureNames`
fields and therefore a plausible guess, put 34 unfetchable paths in the first 2.8.4 client manifest.
Fixed, re-measured, zero remaining.

## The casing table yields to the live sprite

The first client dumps still took their casing sprites from `CASING_ICON_TABLE`, because
`dumpPlainBlocks` tried the table before `getIcon`. That ordering is right on a server, where the
call does not exist, and backwards on a client, where the table is a hand-transcribed copy of an
answer the block will give directly. So on a client the tabled families now resolve per side through
`getIcon` first, and fall back to the table only for a meta the live call cannot answer on all six
faces. Scoped to those families deliberately: this is not a licence to prefer `getIcon` generally,
because `BlockMachines.getIcon` is a vestigial stub that would skin every machine hull as an LV
casing side.

Measured by re-running the 2.9 client dump with the two orderings, which isolates the change exactly:

- **0 metas where the table and the live sprite named different sprites.** The table is not lying at
  2.9. The silent-wrong-sprite risk it carries is real but had not fired, and saying otherwise would
  overstate what was found.
- **3 metas gained their true top and bottom faces**, which the table had flattened to one sprite:

  | meta | table, all six faces | live sprite, UP and DOWN |
  |---|---|---|
  | `gt.blockcasings9\|2` | `PRIMITIVE_WOODEN_CASING_SIDE` | `PRIMITIVE_WOODEN_CASING_TOP` |
  | `gt.blockcasings10\|5` | `COMPRESSOR_PIPE_CASING` | `COMPRESSOR_PIPE_CASING_TOP` |
  | `gt.blockcasings12\|4` | `NANOCHIP_FIREWALL_PROJECTION_CASING` | `NANOCHIP_FIREWALL_PROJECTION_CASING_TOP` |

  Those three were being drawn with their side texture on the top and bottom, which is a visible
  error in the previewer rather than a bookkeeping one.
- 0 drawable keys gained or lost, coverage unchanged at 12 unresolved pairs and 13 of 296
  multiblocks, and the server dump is byte-identical again (9998 / 1845 / 25430).

`gt.blockcasings9|2` is one of the 5 entries the table can no longer resolve at 2.9, so it had been
falling through to the single-face `getIcon` fallback. It now carries all six faces.

## What going client-side does NOT fix

Three of 2.9's 13 remaining problems are unrelated to which JVM ran the dump:

- **11 of the 12 unresolved pairs are `gt.sheetmetal|<material id>`.** That block is keyed by GT
  material id, like `gt.blockframes`, but `MATERIAL_INDEXED_BLOCKS` lists only `gt.blockframes`, so
  only metas 0 to 15 are scanned and the referenced ones are absent rather than gapped. A one-line
  allowlist addition.
- **`gregtech:gt.blockmachines|12698` (Windmill)** resolves no layer on any side, on both sides of
  the comparison.
- **The casing table has already drifted at 2.9.** Both 2.9 runs log `5 casing table entries no
  longer resolve: gt.blockcasings9|2 -> TEXTURE_METAL_PANEL_E_A, gt.blockcasings10|13 ->
  MACHINE_CASING_MS160, ...`. This is exactly the maintenance hazard `CASING_ICON_TABLE`'s own
  docstring predicts, arriving on schedule.

## Recommendation

**Build a client dumper, and put the server-side name-recovery machinery up for retirement.**

The evidence is not marginal. At the pack the project is moving to, server-side extraction leaves 208
of 296 multiblocks undrawable and a client leaves 13, with nothing lost and 145 wrong asset paths
repaired. The four routes that exist only to recover a deleted method's answer are all provably
unnecessary in a client JVM:

| machinery | size | status under a client dumper |
|---|---|---|
| `CASING_ICON_TABLE` + verify/emit | ~240 lines, 11 hand-transcribed families | unnecessary, and already drifting at 2.9 |
| `injectQueuedIconContainers` | ~295 lines | unnecessary; injection was off for both runs |
| `populateIconNames` | ~35 lines | unnecessary, same |
| `IconNameMatcher` + injection (#170, #172) | 331 + ~181 lines | unnecessary; its 2.9 target went to 0 |
| **#169, the tableswitch reader** | not built | **do not build it** |

**#169 should be closed rather than implemented.** It exists to give the hand-transcribed casing table
an oracle. A client dump does not need the table, so it does not need the oracle. Building a
bytecode `tableswitch` reader to recover a mapping that `getIcon()` will simply return is effort
spent on the wrong side of the problem.

Two caveats on the retirement, neither of which changes the recommendation:

1. **Keep the server path working until the client path is the default.** The client route is proven
   as a *superset*, not as a replacement. It is scriptable now that `-PautoWorld=true` loads its own
   world, but it still needs a GL window, so it cannot become a CI gate the way `runServer` could.
2. **Retire in a separate change, deliberately.** This spike added the client route and deleted
   nothing, which is what it was scoped to do.

## Reproducing this

Two worktrees, because the pack pins differ:

- `spike/client-texture-dump` at the 2.9 pins (GT5U 5.09.54.20, StructureLib 1.4.42)
- `spike/client-texture-dump-284` at the 2.8.4 pins (GT5U 5.09.51.482, StructureLib 1.4.23)

```sh
export JAVA_HOME=".../jdk-25.0.3+9"
cd tools/gtnh-extractor
./gradlew runClient \
  -PtextureOut=../../out/textures-client \
  -PpackVersion=<pack> \
  "-PmodVersions=GT5-Unofficial=<ver>,StructureLib=<ver>"
```

Add `-PautoWorld=true` and it loads its own scratch world; without it, click through to a
throwaway single-player world. Measure with
`gtnh-solve --dataset-coverage --dataset-version <label>` after staging the manifest under
`data/<label>/textures/manifest.json`.

`runServer` needs no stdin now: seed `run/server/eula.txt` with `eula=true`, because the documented
`printf 'n\ny\n' |` trick does not survive a detached shell.

## The server path is unchanged

`iconAt` and `iconRef` are on the shared path, so the one way this spike could break what already
works is a server-side regression. Four server dumps, before and after both code changes:

| run | block entries | icons | gaps |
|---|---|---|---|
| 2.8.4 baseline | 11208 | 2370 | 5492 |
| 2.8.4 after the client route | 11208 | 2370 | 5492 |
| 2.8.4 after the domain fix | 11208 | 2370 | 5492 |
| 2.9 baseline, and after both | 9998 | 1845 | 25430 |

Identical, with 0 drawable keys gained or lost on every comparison. The sprite read is reflective
precisely so this holds: a direct `invokeinterface IIcon.getIconName` would die with
`NoSuchMethodError` on the server that runs the same binary.
