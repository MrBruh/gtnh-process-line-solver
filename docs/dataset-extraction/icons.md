# Item and fluid icons: the icon index

How the previewer gets a picture of each fluid and item a line moves (#297), and the one file it reads
to do it. Since #296 every hover and panel names a resource (`Toluene (liquid_toluene)`); this is the
picture beside the name.

## Why a separate export

A plan carries no image data. The arodoid fork's `iconPath` is a URL into its own web app, and the
MrBruh fork has none. Our extractor's dev environment cannot render one either, because it loads GT
and its direct dependencies but not the mods most items on an edge come from (dreamcraft, Forestry,
HarvestCraft). Only a full pack instance has all of them, and NESQL exporter renders every NEI item
and every fluid from inside one. ShadowTheAge's fork of it is the one that runs on 2.9 and pins the
same GT5-Unofficial as `gtnh.lock.json` (5.09.54.20). Its export is reduced to an **icon index**, one
per pack, local only like every other generated dataset (`data/<version>/` is gitignored).

## What a preview does with it

- **An index for the plan's pack** (`data/<pack>/icons/`): each resource the line moves gets its
  icon, the 64 px PNG embedded as a `data:` URI and drawn at 16 px, beside its name on a route's
  and a cover's hover, a storage's contents, an Item Filter's slots, a hatch's hover, the nets
  panel and the system i/o panel. Only the resources the line carries are embedded.
- **No index for that pack, but one for another**: the newest pack's index is used, and an INFO
  line says so. Icons are display only, so a picture from another pack is at worst a stale picture
  beside the right id. Block ids never cross packs this way.
- **No index at all** (a fresh clone, CI): each resource gets a dot in the plan's own colour for it
  instead (`InputIR.resource_colors`, from factory-flow's `dominantColor`), and an INFO line points
  here. Every resource of the four shipped examples has a colour.
- **An index that cannot be read**, or an image in it that is not a small PNG: the icon pass is
  skipped with a warning and the page draws the plan's colours. The page is always written.

Names come along too: where the plan names a resource nothing, the index's name is shown. The
plan's name always wins, then the index's, then the bare id.

## The index format

```
data/<version>/icons/
    index.json
    images.zip
```

`index.json`, with `generated_at` first so `roots.generated_at` finds it in its 64 KB window:

```json
{
  "generated_at": "2026-10-02T00:00:00Z",
  "schema": 1,
  "source": {"exporter": "...", "commit": "...", "export": "...", "pack_version": "2.9.0-beta-2",
             "icon_px": 64},
  "items":  {"minecraft:gravel": {"name": "Gravel", "png": "item/minecraft/gravel~0.png"}},
  "fluids": {"liquid_toluene": {"name": "Toluene", "png": "fluid/gregtech/toluene.png"}}
}
```

- An **item** is keyed the way a plan spells it: `mod:internal`, then `@damage` unless the damage
  is 0, lowercased (`gregtech:gt.metaitem.01@2032`). A plan's `@32767` (any damage) takes the index's
  own `@32767` row if it has one, else the item's lowest damage.
- A **fluid** is keyed by its registry name (`liquid_toluene`).
- `name` is the English display name with formatting codes removed. `png` names a member of
  `images.zip`, or is `null` when the export rendered no image.
- A member must be a PNG of at most 256 KiB. Anything else refuses the whole pass, as above.

The reader is `src/gtnh_solver/dataset/icons.py`; the previewer side is
`src/gtnh_solver/previewer/icons.py`.

## Making an index

Three steps, the middle one in a game client: build the exporter once, export from a full pack
instance (about an hour, unattended once it starts), and derive the index here. The exporter is
pinned in `gtnh.lock.json` under `tools.nesql-exporter`, at the commit named below; a test holds
this page and the lock to the same commit.

### 1. Build the exporter

NESQL Exporter's upstream (D-Cysteine) targets GT5-Unofficial 5.09.45, pack 2.6, and does not load
on 2.9. ShadowTheAge's fork pins 5.09.54.20, the same as our lock, but publishes no jar, and its
build no longer resolves: GTNH's maven has pruned RetroFuturaGradle 1.3.35 and Galacticraft
3.2.5-GTNH, both of which it asks for. `MrBruh/nesql-exporter` is that fork plus one build-file
commit (RFG 1.4.9, and Galacticraft left to the newer one GT5-Unofficial already brings), with no
source change:

| | Repository | Commit |
|---|---|---|
| Pinned | https://github.com/MrBruh/nesql-exporter | `9b650ed2c1fd9c020c1b418fa8a1a6ee10e16287` |
| Its upstream | https://github.com/ShadowTheAge/nesql-exporter | `b5b896ebd9bcf4cfe1f4fad19ccdc7de8414ee57` |

Both are LGPL-3.0; none of it is vendored here. Clone it to a short path beside the repo (Gradle's
paths under a long one pass Windows' 260-character limit) and build it on a JDK 8:

```sh
git clone https://github.com/MrBruh/nesql-exporter ../gtnh-worktrees/nesql-exporter
cd ../gtnh-worktrees/nesql-exporter
git checkout 9b650ed2c1fd9c020c1b418fa8a1a6ee10e16287
export JAVA_HOME="/c/Users/<you>/AppData/Local/Programs/Eclipse Adoptium/jdk8u492-b09"
nice -n 10 ./gradlew build --no-daemon
```

It writes `build/libs/NESQL-Exporter-0.5.7-ShadowTheAge.jar` and
`build/libs/NESQL-Exporter-0.5.7-ShadowTheAge-deps.jar` (Hibernate, HSQLDB and the rest of what
the mod needs); both go in the instance's `mods/`. On the maintainer's machine a ready hand-over of
both jars and the config below sits in `../gtnh-worktrees/nesql-handover/{mods,config}`.

### 2. Export from a pack instance

In a GT:NH **2.9.0-beta-2** instance, the pack `gtnh.lock.json` pins (step 3 says why it must be):

1. Copy both jars to `mods/`, and this as `config/NESQL-Exporter.cfg`. It keeps the config file,
   runs only the three plugins an index needs (`base`, `nei` for the item list, `forge` for the
   fluids), and skips the mob renders; `icon_dimension` stays at its default, 64 px:

   ```
   options {
       B:enable_config_file=true
       S:enabled_plugins <
           base
           nei
           forge
        >
       B:render_mobs=false
   }
   ```

2. Set the game's language to English (US). Names are exported as the client draws them.
3. Check that BugTorch is not in `mods/`: with it, enchanted items render blank.
4. Start the client. 2.9 runs on Java 17 or newer through lwjgl3ify, and the exporter's Hibernate
   and ByteBuddy stack has only been run on Java 8. If the log says ByteBuddy does not support
   `Java N`, add `-Dnet.bytebuddy.experimental=true` to the instance's JVM arguments; if it still
   fails, export from a copy of the instance on Java 8.
5. Open a single-player world and open NEI once (the inventory), so its item list loads. Without it
   the export stops at once with "NEI item list is empty! Please load it, and retry."
6. Run `/nesql gtnh-2.9.0-beta-2` and leave it. Chat reports each stage; the export is done at
   **"Export complete!"**, which follows "Rendering complete!". A red "Something went wrong during
   export! Please check your logs." means it failed. `/nesql` will not overwrite an earlier export
   of the same name ("Cannot create repository ... it already exists!"); `/nesqlf` does.

It leaves `nesql/gtnh-2.9.0-beta-2/` in the instance's game folder (`.minecraft/` or `minecraft/`),
holding `nesql-db.script` and `image.zip` (and a `nesql-db.properties` nothing here reads).

### 3. Derive the index

```sh
nice -n 10 .venv/Scripts/python tools/derive_icons.py "<game folder>/nesql/gtnh-2.9.0-beta-2" --pack-version 2.9.0-beta-2
```

It reads the script and the archive's member names (no image is unpacked), copies `image.zip` to
`data/2.9.0-beta-2/icons/images.zip`, writes `index.json` beside it through a temporary file (so an
interrupted run leaves the last index whole), and prints how many rows became how many entries,
with how many have no image. It refuses, and writes nothing, when:

- `nesql-db.script` or `image.zip` is missing, or the script has no ITEM, FLUID or METADATA table;
- the export has fewer than 10,000 items. A whole 2.9 instance has about 50,000; fewer means NEI's
  list was empty when it ran;
- `--pack-version` is not `gtnh.lock.json`'s `pack_version`. The export records nothing about the
  pack it ran in (its METADATA table holds the exporter's version and a timestamp), so it cannot be
  checked against the pack. The tool holds it to the lock instead, which pins this exporter for one
  pack, and stamps the index with the lock's exporter commit. To index another pack, bump the lock
  first;
- the script holds anything the reader does not understand (`src/gtnh_solver/dataset/nesql.py`),
  such as a CACHED table, whose rows would not be in the script at all.

How rows become entries: an item is keyed as above from `MOD_ID`, `INTERNAL_NAME` and
`ITEM_DAMAGE`, a fluid by `INTERNAL_NAME`, never by NESQL's own `ID`. Rows that share a key are NBT
variants of one item, and the NBT-free one is used; a key with only NBT variants takes the lowest
`ID` of them, and the run counts those. Formatting codes are stripped from names and GT:NH's
private-use font glyphs become the symbols they are drawn as (ShadowTheAge's table, see `NOTICE`);
the run counts any glyph it does not know. An item whose name threw in the exporter ("ERROR") gets
no name, and the plan's or the bare id is shown.

Then check it against the shipped lines, and look at a preview:

```sh
nice -n 10 .venv/Scripts/python -m pytest --no-cov tests/test_acceptance_icons.py
nice -n 10 .venv/Scripts/python -m gtnh_solver.cli examples/gtnh-nitrobenzene.json --effort minimal --preview out/nitrobenzene.html
```

The acceptance test looks up every fluid and item the shipped example lines move and wants a name
and a picture for each. A resource the export has no picture of goes in its `KNOWN_GAPS` with the
reason, so a gap is listed rather than hidden. It skips where there is no index (CI, a fresh
clone).
