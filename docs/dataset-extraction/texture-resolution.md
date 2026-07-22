# Texture resolution: how the extractor names a block's sprite

How `TextureDumper` turns a `(block, meta)` into an iconset name, why it needs five routes to do it,
and what is still unreachable. Written from a live dedicated-server dump at pack 2.8.4 /
GT5-Unofficial 5.09.51.482; every claim is either verified against a run or flagged as inference.

## The one fact everything follows from

The texture pass runs inside a headless **dedicated server**. FML's `SideTransformer` deletes every
member annotated `@SideOnly(Side.CLIENT)` at class-load time. Block textures are a client concern, so
most of the obvious API is simply not there at runtime.

That single fact produces **five unrelated failure modes**. They look identical from the outside (the
previewer draws a grey cube) and need five different fixes. Recognising which one you are looking at
is most of the work.

| Mode | What happens | Symptom in `gaps` | Route that fixes it |
|---|---|---|---|
| **domain** | The icon resolves, but its jar path is built with the wrong mod domain | icon path 404s at bake time | read `mModID` / embedded `:` |
| **A** | The block's own `getIcon(int,int)` is `@SideOnly`, so the method is *deleted* | `no server-side getIcon override` | casing table, or name fields |
| **B** | `getIcon` survives but reaches the sprite via `invokeinterface IIconContainer.getIcon()`, which is `@SideOnly` | `NoSuchMethodError` | ITexture accessor |
| **C** | `getIcon` runs and returns null: the icon holder is client-only state never populated | `getIcon returned null` | inject the queued containers |
| **D** | `getTexture` answers, but every layer holds a null icon container | `unknown ITexture class` / `resolved no layer` | unresolved, see below |

## The five routes, in the order they are tried

1. **ITexture accessor.** `getTextures(int)` / `getTexture(int)` carry no `@SideOnly` and never
   dereference the icon; they only pass containers into `TextureFactory`. Tried first because it
   cannot hit the stripping cliff and it preserves per-layer tint and glow that the single-icon path
   discards. This is what makes coils render with a real active/inactive pair, and frames carry their
   per-material tint.
2. **Casing table.** For families whose `getIcon` is deleted outright, the meta-to-constant mapping is
   transcribed from GT source. Eleven families. See the warnings below.
3. **`getIcon`.** The original route, still correct wherever it survives.
4. **Werkstoff formula.** bartworks material casings store neither icon nor name; the sprite is
   recomputed from the werkstoff registry plus its texture set.
5. **Texture-name fields.** The cheapest and widest route: see below.

Plus one pass that runs once up front: **container injection**.

### Container injection (fixes mode C generically)

Every custom `IIconContainer` self-registers into `GregTechAPI.sGTBlockIconload` from its own
constructor, and the only thing that ever drains that queue is
`BlockMachines.registerBlockIcons`, which is `@SideOnly`. So on a server the queue is fully populated
and never run, leaving every `mIcon` null.

That public list is therefore a **complete server-side registry of every custom icon container**.
Injecting a named icon into all 11,766 of them fixes GT++, kekztech and others at once, reaches
instances held in private statics that walking any single holder class would miss, and needs no
per-mod knowledge. Prefer this shape of fix to a per-mod table whenever one exists.

### Texture-name fields (fixes most of mode A, without a table)

A great many blocks put the `@SideOnly` on the **resolved** icon array while the strings that NAME
those icons are un-annotated and survive untouched:

```java
@SideOnly(Side.CLIENT) protected IIcon[] texture;   // stripped
String[] textureNames;                             // survives, set from the constructor
```

That is bartworks' and GoodGenerator's shape. Vanilla does the equivalent one level up:
`setBlockTextureName` and the `textureName` field it writes are both un-annotated, which is how
gtnhlanth and kekztech name themselves. A per-side variant (`textureSide` / `textureTopAndDown`)
exists too and must be checked *first*, because a block that has it also inherits an unset
`textureNames`.

## Traps

Ordered by how much time each one cost.

1. **Do not diagnose from `javap` or from source alone; instrument the runtime.** Two confident
   source-level diagnoses of the mode-D bug were both wrong. One `instanceFields` probe printing
   runtime *values* settled it in a single run: `mIconContainer=null`. The gap reasons now carry that
   state for exactly this reason.
2. **An un-annotated accessor over stripped state is still fatal.** `Block.getTextureName()` *is*
   `@SideOnly` while the field behind it is not. bartworks' `getColor(int)` is un-annotated but
   dereferences the stripped `IIcon[]` and dies with `NoSuchFieldError`. **Read the field, never the
   getter** - this pattern recurs across the codebase.
3. **Never generalise a route to "any block that fails".** `BlockMachines.getIcon` is a vestigial stub
   returning `MACHINE_LV_SIDE` for *every* meta. A generic mode-A rule matches it perfectly and would
   skin every GT machine hull as an LV casing side: hundreds of confident, wrong, unflagged sprites.
   Both the casing table and the wide-meta-scan are **explicit allowlists** for this reason.
4. **A wide meta scan over-emits.** Scanning every block to 1000 metas produced **876 entries for the
   coil block**, whose accessor answers any meta through its `default` arm. Widen only where the
   block's own indexing calls for it: frames are keyed by GT material id, werkstoff casings by
   werkstoff id (five digits, so taken from the registry rather than scanned).
5. **A grey block is recoverable; a plausible wrong sprite is not.** Nothing downstream can detect the
   latter. When a route cannot resolve something, it must record a gap, never guess.
6. **Measure against what multiblocks reference, not the raw gap count.** The `gaps` list is dominated
   by fluid and ore blocks nothing ever places. Ranking families by how many dumped multiblocks touch
   them is what surfaced frames and bartworks glass as the two biggest wins.
7. **`-PmodVersions` is required.** Without it the manifest records GT5U's self-reported `"MC1710"`,
   `jar.py` tries to download a jar that does not exist, and the previewer silently falls back to
   placeholders for *everything*. It looks like a catastrophic regression and is a one-flag mistake.
8. **Check gradle's real exit status.** `./gradlew ... | tail` reports `tail`'s status. The same
   applies to a backgrounded `cd X && ./gradlew ...`: if the `cd` fails, the whole thing reports
   success having run nothing.
9. **`Map.of` and friends do not exist here.** The extractor targets Java 8 bytecode; Jabel gives
   modern syntax, not the modern stdlib.

## Maintaining the casing table

`CASING_ICON_TABLE` is the only hand-transcribed data in the pass, and it has one real failure mode: a
GT version bump that **inserts a meta** shifts every later entry in that family, and the result still
validates, still renders, and is simply wrong.

`verifyCasingTable()` runs at startup and resolves every constant the table names, so a **renamed or
removed** constant is a loud log line. It cannot catch a **reordering**. That asymmetry is the
standing argument for replacing the table with the ASM route below.

## What is still unreachable, and why

91 unresolved pairs at the time of writing, 40 of 208 multiblocks carrying at least one. None affect
the shipped example lines, which resolve completely.

| Group | Multiblocks | Why |
|---|---|---|
| `gt.blockmachines` controller hulls | 29 | mode D |
| tectech casings (`blockcasingsTT`, `godforgecasing`, `blockcasingsBA0`) | 15 | mode D, same root cause |
| `gt.blockcasingsSE` | 11 | names exist only as literals in `registerBlockIcons` |
| `IC2:blockAlloyGlass` | 4 | external mod, not in the GT jar or the reference checkout |

**Mode D in detail.** The affected controllers take their overlay from a `CustomIcon` static declared
under a comment reading `// region Client side variables` and assigned *only* inside
`@SideOnly registerIcons`. It stays null, `GTTextureBuilder.addIcon` has no null check, and
`build()` constructs a `GTRenderedTexture` wrapping null. GT itself knows this state exists and
guards rendering with `isValidTexture()`.

The tempting fix - emit the layers that do resolve and gap only the null one - **does not work**. The
base casing layer is a copied-block texture pointing at `BlockGTCasingsTT`, whose own icons are also
client-only, so all six faces resolve to zero layers. There is nothing to partially emit.

### Dead end: a stub `IIconRegister` (verified, do not retry)

The obvious idea is to call `registerBlockIcons` yourself with a fake registry that records the names
it is asked for. `BlockGTCasingsTT.registerBlockIcons` is genuinely **not** `@SideOnly`, its body is
nothing but `registerIcon("gregtech:iconsets/EM_POWER")`-style literals, and its `super` call is
commented out - so it looks perfect.

It cannot work. Instrumented over a full run: the method was **found on 806 blocks and threw on all
806**, priming zero. `IIconRegister` is itself a client-only interface, absent server-side, so a class
implementing it cannot even load. You cannot implement an interface that does not exist on that side.

This is why bytecode is not merely the *preferred* route for these families but the *only* one: the
names are right there as string literals in a callable method, and no runtime mechanism can reach
them.

## The ASM route (verified feasible, not yet built)

Recovering names from the **unstripped class bytes**. `SideTransformer` rewrites bytes on their way
into memory; the `.class` in the jar is never touched. So reflection sees a stripped class while
reading the file sees the original mapping intact.

Verified against the artifacts on this machine, not reasoned about:

- **`org.objectweb.asm` 5.0.3 is already on the compile and runtime classpath**, pulled by
  `net.minecraft:launchwrapper:1.12`. No dependency change needed.
- **`LaunchClassLoader` never intercepts resource reads.** It extends `URLClassLoader` and declares no
  `getResource*` override. Its public `getClassBytes(String)` returns **pre-transform** bytes (its
  `resourceCache` is pre-transform; `findClass` transforms *after* calling it) and is already warm for
  any loaded class. **Use `Launch.classLoader.getClassBytes(...)`, not `getResourceAsStream`.**
- **Deobfuscation is a non-question.** Both mods are pinned with the `:dev` classifier and `runServer`
  runs from `build/classes`, never a reobfuscated jar. `javap -c` on the shipped `BlockCasings8.class`
  emits `GETSTATIC ...BlockIcons.MACHINE_CASING_CHEMICALLY_INERT` verbatim.
- **The code shapes are uniform.** All nine mode-A classes use a single `tableswitch`, zero
  `lookupswitch`, zero if/else chains, four instruction shapes, no nesting beyond one level.
  Estimated ~250-300 lines including tests.

It also reaches the tectech group, whose names sit in the same unstripped bytes as
`LDC "gregtech:iconsets/EM_..."; INVOKEINTERFACE registerIcon; PUTSTATIC eM0`. Note those literals
already carry the `gregtech:` prefix and must not be re-prefixed.

**Three non-negotiable properties if it is built:**

1. **An explicit allowlist of registry names**, not "any block with a stripped `getIcon`". The
   generality is precisely the unsafe part - see trap 3.
2. **An unrecognised instruction shape records a gap, never a guess.**
3. **Array arms delegate to the existing runtime field read** (`tableIcon`'s `ARRAY_MARKER` path),
   never to inferred contents: `BlockCasingsNH` indexes shared tier arrays whose elements are not
   inferable from the call site.

The reason to prefer it over extending the table is not initial cost, which is comparable. It is that
a hand table's failure mode on a pack bump is a silently wrong sprite, and the matcher's is a recorded
gap.

## Running and measuring

```sh
# JAVA_HOME is required; Gradle does NOT auto-detect the JDK 25 toolchain
export JAVA_HOME="/c/Users/mdnss/AppData/Local/Programs/Eclipse Adoptium/jdk-25.0.3+9"
cd tools/gtnh-extractor

./gradlew compileJava            # check the real exit code, not a piped tail's

printf 'n\ny\n' | ./gradlew runServer \
  -PtextureOut=../../out/textures-run \
  -PpackVersion=2.8.4 \
  "-PmodVersions=GT5-Unofficial=5.09.51.482,StructureLib=1.4.23"
```

Then install and measure:

```sh
cp out/textures-run/manifest.json data/2.8.4/textures/manifest.json
python tools/derive_small_manifest.py     # refresh the committed example-scoped manifest

# silence is success
python -m gtnh_solver.cli examples/gtnh-nitrobenzene.json \
  --preview out/nitrobenzene.html 2>&1 | grep "render grey"
```

For the wider local dump, diff the manifest's `blocks` against the `(block, meta)` pairs that
`data/<version>/multiblocks/*.json` reference, and rank the misses by how many multiblocks touch each
- not by raw gap count (trap 6).

An unresolved face renders as Minecraft's magenta/black missing-texture checkerboard rather than
casing grey, so a gap is visible in the preview instead of passing for a plain casing.

## Related

- [#98](https://github.com/MrBruh/gtnh-process-line-solver/issues/98) - the coverage issue this work
  belongs to.
- [#102](https://github.com/MrBruh/gtnh-process-line-solver/issues/102) - the manifest key namespace
  shared between MTE ids and block metas (latent, no live collisions).
- [#4](https://github.com/MrBruh/gtnh-process-line-solver/issues/4) - pipe and cable textures for
  routed nets, covering different blocks.
