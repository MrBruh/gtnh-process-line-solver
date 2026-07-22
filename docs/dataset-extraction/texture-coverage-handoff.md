# Texture coverage: handoff

**Status as of 2026-07-22.** Written for whoever picks up the remaining texture gaps after
[#98](https://github.com/MrBruh/gtnh-process-line-solver/issues/98). Everything below the "Root
cause" heading was read out of GT source at the pinned version, not inferred from bytecode. Where
something is *not* verified it says so explicitly, and those are the places to be careful.

## The problem in one line

57% of the `(block, meta)` pairs that dumped multiblocks reference have no entry in the texture
manifest, so those blocks render as flat grey cubes inside otherwise-correct structures.

Concretely, on the nitrobenzene example: the Large Chemical Reactor is 25 of its 27 blocks grey, and
the coil families are grey in 20 of the 208 dumped multiblocks.

## Why it happens

`TextureDumper` resolves a block's sprite by reflectively calling `block.getIcon(side, meta)` on a
headless **dedicated server**, after injecting a `NamedIcon` into every `Textures.BlockIcons` enum
constant's `mIcon` field. FML's `SideTransformer` strips `@SideOnly(Side.CLIENT)` members on that
side, and that single fact produces **three distinct failure modes**. They need three different
fixes; there is no one general repair.

| Mode | What happens | Symptom in `gaps` | Affected |
|---|---|---|---|
| **A** | The block's own `getIcon(int,int)` is `@SideOnly(CLIENT)`, so the method is deleted. `findGetIcon` returns null and `iconAt` is never reached. | `meta: -1`, `no server-side getIcon override (...)` | `BlockCasings6/8/9/10/11/12/13/NH`, `BlockGlass1` |
| **B** | `getIcon` survives, but reaches the icon via `invokeinterface IIconContainer.getIcon()`, and *that* interface method is `@SideOnly`. | per-meta, `no icon for meta (NoSuchMethodError)` | `BlockCasings5` (all metas), `BlockCasings1` metas 0-9 |
| **C** | `getIcon` runs fine and returns `null`, because the icon holder is client-only state never populated on a server. | per-meta, `no icon for meta (getIcon returned null)` | GT++ `GregtechMetaCasingBlocks` (C1), tectech `BlockGTCasingsTT` (C2) |

Mode B for `BlockCasings1` metas 0-9 is **already fixed** (`ARRAY_BACKED_CASINGS` in
`TextureDumper.java`), which is why the tiered machine casings render. That fix does not generalise:
it works only because those icons happen to live in static `IIconContainer[]` arrays of enum
constants that can be named without calling the stripped method.

### The Mode B root cause is worth understanding

`BlockCasings5.getIcon` (reference checkout, line 107) is:

```java
IIconContainer background = switch (meta % ACTIVE_OFFSET) {   // local typed as the INTERFACE
    case 1 -> Textures.BlockIcons.MACHINE_COIL_KANTHAL_BACKGROUND;
    ...
};
return background.getIcon();                                   // -> invokeinterface
```

Every arm assigns an *enum constant*, but the local's declared type is `IIconContainer`, and javac
emits the call site from the receiver's static type. Sibling casings inline
`Textures.BlockIcons.FOO.getIcon()` and compile to `invokevirtual`, which survives stripping because
`BlockIcons`' own `getIcon()` override is un-annotated. One local variable's declared type is the
whole difference.

## Work items, in the order they should be done

### 1. Fix `ICON_DOMAIN` (prerequisite, and a live bug)

`TextureDumper.iconName()` hardcodes `ICON_DOMAIN = "gregtech"`. The **currently shipped manifest**
already contains 46 broken icon paths because of it:

- **17** named `gregtech:TileEntities/...` resolving to
  `assets/gregtech/textures/blocks/TileEntities/*.png`. That directory does not exist in GT5U (which
  has only `basicmachines`, `fluids`, `icons`, `iconsets`, `materialicons`). These are GT++ textures
  and really live under `assets/miscutils/`.
- **29** double-prefixed as `gregtech:gregtech:icons/...`, producing a path with a literal colon.

Make `iconName()` read the container's `mModID` when it has one, and detect an already-embedded `:`.
**Do this first** - item 4 injects more GT++ icons and would otherwise add 13 more broken paths.

Verify by grepping the regenerated manifest for `gregtech:gregtech:` and
`gregtech:TileEntities/` - both should be empty, and every `icons` value should point at a path that
exists in the corresponding mod jar.

### 2. Coils via `getTextures`, not `getIcon` (highest value: 20 of 208 multiblocks)

`BlockCasings5` implements `IBlockWithTextures` (line 50) and overrides
`getTextures(int metadata)` returning `ITexture[6][layers]`. That route avoids Mode B entirely:

- `IBlockWithTextures.getTextures` carries **no** `@SideOnly`.
- Its body never calls the stripped `getIcon()`; it only passes containers into `TextureFactory`.
- The leaves are `GTRenderedTexture`, whose `mIconContainer` and `glow` fields are **exactly what
  `TextureDumper.describe()` already reads**. No new naming logic needed.
- `getTextures(meta + 16)` returns the **active** stack (background plus a glowing `_FOREGROUND`
  layer), so the real inactive/active pair the schema already models comes for free. The `+16` is a
  client-only render meta, not stored block metadata, so `realMetas()` correctly yields 0..13 and you
  synthesize the active state yourself.

**Trap:** the meta-to-icon mapping is direct (`meta % 16` to a specific constant) and must **not** be
derived from the coil tier or the block's display name. `getCoilHeatFromDamage` maps meta 8 to UIV
and 9 to LuV (non-monotonic), and meta 3 is named "TPV-Alloy Coil Block" while using the
TUNGSTENSTEEL sprite. Deriving it either other way yields wrong sprites that still look plausible.

**Caveat, and the main thing to prove:** the side-safety argument above is *static* reasoning from
annotations plus the fact that GT itself calls `TextureFactory` server-side during mod init
(`BlockCasingsAbstract` constructor). **It has not been executed against a live dedicated server.**
Confirm with a real dump run before trusting it. `BlockCasings5` is also the only class in the
monorepo implementing `IBlockWithTextures`, so this is not a general escape hatch.

### 3. Mode A families via source-derived tables (unblocks the LCR)

These have no callable method at all, so the only options are a per-family table or reading the
unstripped class bytes.

`BlockCasings8.getIcon` ignores `ordinalSide` entirely - one icon per meta on all six faces - so it
needs *less* machinery than `ARRAY_BACKED_CASINGS`: a `registryName -> meta -> BlockIcons constant
name` table, resolved by `Field` lookup on `Textures.BlockIcons` and named exactly the way
`populateIconNames` does, emitted through the existing single-`"all"`-side path.

The LCR uses metas 0 and 1, which are the first two entries:

| meta | constant |
|---|---|
| 0 | `MACHINE_CASING_CHEMICALLY_INERT` |
| 1 | `MACHINE_CASING_PIPE_POLYTETRAFLUOROETHYLENE` |

Transcribe the rest from `BlockCasings8.java`. Casings 9, 11, 12, 13 share the flat shape and can
reuse the mechanism; casings 6 and 10 special-case sides 0/1 and fall back to the
`MACHINECASINGS_BOTTOM/TOP/SIDE` arrays that `arrayCasingIcon` already reads, so they need the
per-side variant.

**Unproven alternative worth evaluating first.** `SideTransformer` rewrites bytes at `defineClass`
only, so `classLoader.getResourceAsStream("gregtech/common/blocks/BlockCasings8.class")` should still
yield the *unstripped* bytes, and Forge already has `org.objectweb.asm` on the classpath. Parsing the
`tableswitch -> GETSTATIC Textures$BlockIcons.<NAME>` pairs would fix all nine Mode A families plus
`BlockMachines` and any third-party casing with the same annotation, with no hand-maintained table.
It assumes GT's own enum constant names survive reobfuscation unchanged (they should, since only MC
members are remapped) - **verify that before committing to it**. The table is the lower-risk way to
unblock the LCR now; the ASM route is the better end state if it holds up.

### 4. GT++ casings by extending the icon injection (do after item 1)

GT++ `TexturesGtBlock.CustomIcon` statics carry `mIconName` and `mModID`, so Mode C1 is fixable
without a table: walk `TexturesGtBlock.class.getDeclaredFields()` for static fields assignable to
`CustomIcon`, read those two fields, and set
`mIcon = new NamedIcon(mModID + ":" + mIconName, "assets/" + mModID + "/textures/blocks/" + mIconName + ".png")`.

`mIconName` **already includes its subfolder** (e.g. `TileEntities/MACHINE_CASING_STABLE_POTIN`), so
do not prepend `iconsets/`. All 13 use the single-arg constructor, so `mModID` is `"miscutils"`.

Once injected, the existing `getIcon` path returns the right `NamedIcon` on its own - no table
needed. Metas 2, 3, 4 already work today (they use `BlockIcons` enum constants), so a correct fix
takes that block from 3 resolved / 13 gapped to 16 / 0.

### Not worth attempting

Mode C2 - tectech's `BlockGTCasingsTT` uses raw static `IIcon` fields with no container, assigned
only inside a client-only `registerBlockIcons`. The icon *names* exist nowhere but that stripped
method body, so injection cannot reach them. That is ~29 gaps that will remain; do not expect the
GT++ fix to close them. They would need the ASM route from item 3, or a hand-written table.

## How to run and verify

Reference source for all of the above is checked out at `C:\Users\mdnss\Dev\gtnh-reference`, pinned
to the versions in `gtnh.lock.json`. Read that source rather than disassembling - see "Traps" below.

```sh
# JAVA_HOME is required; Gradle does NOT auto-detect the JDK 25 toolchain
export JAVA_HOME="/c/Users/mdnss/AppData/Local/Programs/Eclipse Adoptium/jdk-25.0.3+9"
cd tools/gtnh-extractor

./gradlew compileJava            # check the real exit code, not a piped tail's

# texture pass only; -PmodVersions is REQUIRED (see traps)
printf 'n\ny\n' | ./gradlew runServer \
  -PtextureOut=../../out/textures-run \
  -PpackVersion=2.8.4 \
  "-PmodVersions=GT5-Unofficial=5.09.51.482,StructureLib=1.4.23"
```

Then measure, rather than eyeballing the preview:

```sh
# what still renders grey on the example line
.venv/Scripts/python -m gtnh_solver.cli examples/gtnh-nitrobenzene.json \
  --preview out/nitrobenzene.html 2>&1 | grep "render grey"
```

`TextureSummary.unskinned_blocks` names every constituent block that resolved no face at all, and the
pass warns with the exact `<block>|<meta>` list on every build. That warning is the fastest feedback
loop available; use it as the success metric.

## Traps that have already cost time

Each of these produced a wrong conclusion or a wasted build during the #98 work.

1. **Do not diagnose from `javap` alone.** Two confident diagnoses derived from disassembly were both
   wrong. `StructureDumper.java` also contains raw NUL bytes, so grep treats it as binary and
   silently finds nothing - read it with a tool, or strip nulls first.
2. **`-PmodVersions` is required.** Without it the manifest records GT5U's self-reported `"MC1710"`
   instead of `5.09.51.482`, and `jar.py` then tries to download a jar that does not exist. The
   texture pass fails wholesale and the previewer silently falls back to placeholder boxes for
   *everything* - it looks like a catastrophic regression and is a one-flag mistake.
3. **`Map.of` and friends do not exist here.** The extractor targets Java 8 bytecode; Jabel provides
   modern *syntax*, not the modern stdlib. Use a static block with a `LinkedHashMap`.
4. **A Forge event handler class must be `public`.** `EventBus` generates an ASM wrapper in its own
   package, so a package-private handler dies with `IllegalAccessError` on every dispatch. This is
   why `ElementRecorder` breaks the package convention.
5. **Check gradle's real exit status.** `./gradlew ... | tail` reports `tail`'s status, so a failed
   build can look like a success.
6. **Read the whole `gaps` list before generalising.** The original misdiagnosis of `gt.blockcasings8`
   came from a truncated grep: the observed reason belonged to a different block, and the pattern was
   assumed to continue.

## Related

- [#98](https://github.com/MrBruh/gtnh-process-line-solver/issues/98) - the issue this work belongs
  to. Its "no silent grey placeholder" goal is **not** met, which is why PR #99 carries no closing
  keyword.
- [#78](https://github.com/MrBruh/gtnh-process-line-solver/issues/78) - the server-side ITexture
  reflection spike. If items 2 and 3 both prove unworkable, that is where this goes next.
- [#4](https://github.com/MrBruh/gtnh-process-line-solver/issues/4) - pipe and cable textures for
  routed nets, a sibling concern covering different blocks.
