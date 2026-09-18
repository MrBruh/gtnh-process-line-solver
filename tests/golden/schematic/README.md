# Schematic format references

Real `.schematic` files saved out of the GT:NH instance with Schematica, kept so the
`.schematic` exporter (#96) targets the real NBT tag layout instead of a guessed one.

These are **not** validator golden cases. The pairs described in `../README.md` are
`InputIR` + `LayoutResult` JSON for `validate()`; these are binary NBT and answer a different
question: what a file Schematica accepts actually contains.

## What is here

| file | status | what it is |
|------|--------|------------|
| `sand.schematic` | **golden** | Close enough to the line the solver builds to pin the format. The exporter's output for the sand plan should match its *shape*: same tag layout, same lowering of machines/cables to blocks + tile entities. |
| `nitrobenzene-reference.schematic` | reference only | A hand-built example of one way to arrange the blocks. **Not solver output**, and not the layout the solver produces. Kept because it is the only sample containing multiblock casings, a controller and hatches. Never assert our output against it cell for cell. |

Neither file is a byte-for-byte expectation for our exporter. Both were built by hand in
game, so they will differ from a solved layout in placement, and the nitrobenzene one also
contains blocks from other mods (EnderIO, StorageDrawers, ExtraUtilities) that the solver
never emits.

## What they establish

- **Root compound** `Schematic`: `Width` / `Height` / `Length`, `Materials = 'Alpha'`,
  `Blocks` and `Data` (one byte per cell), `AddBlocks` (nibble-packed high bits, needed
  because `gt.blockmachines` is block id 2417), `Entities`, `TileEntities`, `Icon`, and a
  `SchematicaMapping` compound of `registry_name -> numeric id` so the file remaps onto
  whatever ids the loading instance assigned.
- **Cell order** is MCEdit's `(y * Length + z) * Width + x`, which lines every `TileEntities`
  entry up with its cell.
- **The `Data` nibble selects the tile entity class, not the machine.** Per
  `gregtech/GTMod.java`, metas 0-3 and 12-15 construct a `BaseMetaTileEntity` and metas 4-11
  a `BaseMetaPipeEntity`. In these files machines sit at Data 0-3, cables at 9, fluid pipes
  at 7.
- **Machine identity lives in the tile entity** as `mID` (`BaseMetaTileEntity.writeToNBT`),
  which is why a block-only export cannot work: `mID` runs to five digits and `Data` is four
  bits.
- **Our dataset's `meta` is exactly that `mID`.** Every mID in both files resolves in the
  texture manifest: 135 `Super Chest I`, 611 `Basic Forge Hammer`, 1246/1247
  `cable.tin.01`/`cable.tin.02`, 998 `ExxonMobil Chemical Plant`, 41/73/90 the hatches.
- **Casings carry no tile entity**: they are plain `(block, meta)` with genuine block
  metadata, so only machines and routes need NBT.
- The `Power Source (LV)` the adapter synthesizes appears here as GT's **Debug Power
  Generator** (mID 15498), which is the substitution the exporter should make.

Three mIDs in the nitrobenzene reference (1115, 1116, 1117) resolve to nothing in the
manifest; see #96 for that gap.
