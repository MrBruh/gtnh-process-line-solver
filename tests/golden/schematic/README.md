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
| `sand-parallel-reference.schematic` | reference only | The maintainer's own build of `examples/gtnh-parallel-sand.json`, saved from the instance: the same 9 Forge Hammers, 2 Super Chests and power source the solver places, in a 3x3x4 box. **Not solver output**, and well beyond what the solver can currently express. It is the quality target for that line, and the evidence behind the routing limits it is filed under. Never assert our output against it cell for cell. |
| `sand-parallel-exported.schematic` | **proven in game, in part** | The same build as `sand-parallel-reference.schematic`, written by **our own exporter** rather than by Schematica, and built in game by the maintainer. It carries the pipe wiring the Schematica copy loses. Proven for geometry, wiring, facings and power; **not** for item throughput, which it gets wrong (see below). |
| `28-sfb.schematic`, `29-sfb.schematic` | **golden, format only** | The maintainer's saves from a 2.8.4 and a 2.9 instance of three frame boxes in a row along +X: a plain Steel frame, a plain Black Steel frame, and a Steel frame with a cover. They settle how Schematica stores a frame, whose world metadata is a material id rather than a nibble (see below). |

None of these is a byte-for-byte expectation for our exporter. All were built by hand in
game, so they will differ from a solved layout in placement, and the nitrobenzene one also
contains blocks from other mods (EnderIO, StorageDrawers, ExtraUtilities) that the solver
never emits.

## What `sand-parallel-reference.schematic` measures

It is the only file here that is a **quality** reference rather than a format one, so the
numbers are the point. Decoded with `gtnh-solve --inspect-schematic` against the same plan the
solver solves:

| | reference | solver |
|---|---|---|
| bounding box | 3x3x4 = 36 cells | 11x4x5 = 220 cells |
| solid blocks | 27 | 75 |
| item pipes | 12 (6 huge, 6 large) | 40 (all `gt_pipe_tin_huge` since #165; all plain `gt_pipe_tin` before) |
| cables | 3 (2x `cable.tin.08`, 1x `cable.tin.12`) | 23 (3x 1x, 18x 2x, 2x 4x) |
| machines | 9 hammers, 2 chests, 1 source | the same |

What it does that we cannot is put **many terminals of one net on one pipe block**: 20 item
terminals on 12 cells, against our 20 on 40. Its 12 pipes are four disjoint 3-cell runs, one
per net, and no pipe block is shared between nets. An earlier version of this section said the
opposite, that one pipe column carried several material flows at once; that was read off a
layer dump without checking connectivity and is wrong. The router forbids the sharing it does
do, in three places, and #164 tracks them.

**Its pipe wiring is not evidence; everything else in it is.** All 15 of its pipe and cable
blocks carry `mConnections = 0`. The cause is the GT version it was saved on, not the build and
not Schematica. The file is stamped GT 5.09.51.476 (the 2.8 pack), and Schematica's GUI save reads
the *client* world. That GT synced a pipe's connections to the client only on its
`BaseMetaPipeEntity`, and an item pipe's or cable's own `saveNBTData` then writes its own copy,
never synced and so 0, over them. GT 5.09.54 syncs that copy too
(`MetaPipeEntity.receiveClientEvent`).

Measured on 2026-10-04 in the 2.9.0-beta-3 instance (GT 5.09.54.133): a GUI save and a
`/schematicaSave` of the same 3x3x4 parallel sand build have all 15 pipe and cable blocks wired,
with identical connection masks, facings and auto-output settings. (The build has since been
rebuilt with huge brass pipes, so those saves are not this file, and they are not committed.) The
two differ only in live state, which the GUI save lacks or has stale: items in transit, stored
energy, progress.

Our own exporter writes the wiring, on 63 of 63 pipes for the same line. So the box size, the block
counts and the run shapes above can be trusted, while which net each run carries cannot be read off
this file at all. `sand-parallel-exported.schematic` is this build written by our own exporter and
built in game, and it is the file to read for topology; a fresh 2.9 save of a build would serve too.

**Its facings are faithful**, because GT does sync those. An earlier version of this section
called them unreliable too. That came from reading `mFacing` as the front, before #249 settled that
on a GT basic machine `mFacing` is the OUTPUT face and `mMainFacing` the working one. Read that way
(2026-10-03), all nine hammers agree with the build: each `mFacing` points at the run that carries
its product on (stage 1 east, stage 2 up, stage 3 west into the run to the output chest), each has
auto-output on (`mItemTransfer = 1`), and no `mMainFacing` touches a pipe or cable (each faces a
neighbouring hammer, air or the edge of the box). The power source fronts away from its cable, the
one face a Debug Power Generator does not output on, and the input chest's cover sits on the face
its pipe touches.

Never assert our output against it cell for cell either way: it is hand built, so it differs
from any solved layout in placement.

## What `sand-parallel-exported.schematic` proves

It was produced by authoring the build as a `LayoutResult` and exporting it, not by saving a world.
The positions come from the Schematica reference, which carries geometry faithfully. The net each
pipe run carries, and every facing, were **chosen** rather than read: that file cannot supply the
wiring, and at the time its facings were wrongly thought unreliable too (see above). Each run is assigned to the one net whose producers and consumers it touches, every hammer
fronts north, and the chests and power source front south, on faces that carry no I/O.

Before export it passed `validate()` with no violations, and `route_power`, given only that
placement, independently chose the same three-cell cable column the maintainer built. It then
decodes to the same 3x3x4 box of 27 solid blocks, with all 15 pipe and cable blocks wired and the
facings as authored.

Built in game by the maintainer, it established:

| | result |
|---|---|
| geometry, wiring, facings | as authored |
| power | **works**: `cable.tin.04` and `.02` run all nine hammers. The `.08` and `.12` in the Schematica copy were over provisioned |
| item throughput | **fails**: every run is a plain `gt_pipe_tin`, and only one of the three stone hammers is fed at a time, so the line runs at a third of its designed rate |

The throughput failure is the useful part. It is why this file is kept even though the build it
describes does not fully work: it is the reference that #165 (choosing a gauge) and #190 (the
validator refusing a pipe too thin for its net) are measured against. `validate()` certified it at
the time, which made it the concrete case of the validator passing a layout that fails in game.
Since #190 it is refused, on the five pipe blocks of the stone and sand runs that carry more streams
than a plain pipe's one insertion per 40 ticks, and `tests/test_golden_sand_parallel.py` pins that.

The Schematica copy stays because it is the only record of the gauges that **do** work: huge on the
two runs to and from a chest, large on the two between hammer stages. Its twelve pipes sit on
exactly the export's twelve cells, so the same test reads those gauges from it block by block onto
the export's wiring, and pins that the validator accepts the result.

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

## What the frame saves (`28-sfb`, `29-sfb`) establish

In GT 5.09.54 a frame box (`gregtech:gt.blockframes`) stores its **material id** as its world
metadata (`BlockFrameBox.MATERIAL_MASK`, 0xFFF; Steel is 305, Black Steel 334), plus `MTE_BIT`
(0x1000) once it has a tile entity. A `.schematic` has four bits of `Data` per cell. Decoded with
`read_schematic`, both saves say the same thing (#212):

| cell | placed | `Data` | tile entity |
|---|---|---|---|
| x=0 | plain Steel frame (305) | 1 | none |
| x=1 | plain Black Steel frame (334) | 14 | none |
| x=2 | Steel frame with a cover | 1 | `BaseMetaPipeEntity`, `mID` 4401 = 4096 + 305, with the cover |

So Schematica keeps the **low nibble** of the material and nothing else, and the material survives
only in a frame that has a tile entity. It cannot survive a paste either: GT creates a frame's tile
entity only when `MTE_BIT` is in the metadata (`BlockFrameBox.hasTileEntity`), and the nibble cannot
carry it, so a pasted frame becomes material 1 or 14 (Hydrogen, Fluorine). The exporter therefore
writes every frame in the covered-frame shape, so the file at least records each frame's material,
and warns that a paste will get it wrong (`schematic.core._frame_cell`, `SchematicWarning`).

The two saves differ only in what each instance assigns: the block's numeric id (2433 against 3027 in
`SchematicaMapping`), GT's `nbtVersion` stamp (5.09.51.476 against 5.09.54.133), the cover's encoded
id, and a zero `mRedstone` tag present only in the 2.8.4 one.
