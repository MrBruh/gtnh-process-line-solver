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

Not automated yet. Deriving the index from a NESQL export, and the steps to run that export in a
pack instance, are the second half of #297. Until then a preview draws the plan's colours.
