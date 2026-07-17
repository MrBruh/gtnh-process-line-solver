# Spike: pipe and cable textures in the previewer (#4)

Branch `spike/4-pipe-cable-textures`. Goal: figure out how to render routed pipes and cables
with their in-game textures, and de-risk the parts we can before committing to the work.

## TL;DR

The previewer-side wrap is **easy and proven** (see the prototype below). The real work, and
the real uncertainty, is the **dataset side**: the texture manifest carries zero pipe/cable
sprites today, because the extractor only walks multiblock member blocks and pipes/cables are
not multiblock members. So #4 splits into three pieces, only one of which is hard.

## What is already there (reused, not rebuilt)

- **Bake pipeline** (`previewer/bake.py`): given an ordered layer stack `[{icon, rgba, glow}]`
  it fetches each sprite, applies the material RGBA tint (multiply, brightness-normalised), and
  composites to one flat 16x16 PNG. This is exactly how a GT cable renders: a wire sprite tinted
  by the material colour. It reuses for cables with no change.
- **Texture -> material path** (`previewer/html.py`): `loadTex(dataURI)` already builds a
  NearestFilter, sRGB three.js texture from an embedded PNG. The machine cubes use it; routes
  can use the same helper.
- **Scene already carries the tier**: a power route's `netId` is `"power:<TIER>"` (e.g.
  `power:LV`), and each segment carries its amperage `thickness` (1x/2x/4x/8x/12x/16x). So the
  data needed to pick a cable skin is present without any IR change.
- **Route rendering** (`html.py`, the `SCENE.routes` loop): draws a node cube per cell plus a
  uniform-cross-section arm per connection, flat-coloured by commodity, sized by thickness.

## The gap

- `data/textures/manifest.json` (schema 2) has 26 blocks / 84 icons, all `gt.blockcasings` and
  `gt.blockmachines` (multiblock members and machine hulls). **Zero cable/wire/pipe entries.**
- GT cables and pipes are `MetaPipeEntity` blocks, a different registry family the current
  extractor never enumerates. Their sprites (a material iconset wire/pipe icon, tinted by the
  material RGBA) are not in the manifest, so there is nothing to bake yet.

## The three workstreams

1. **Dataset (the blocker, needs the JDK/gtnh-extractor toolchain).** Extend the extractor to
   enumerate the `MetaPipeEntity` registry and emit schema-2 entries for cables (by material +
   wire gauge) and item/fluid pipes (by material + size), each as an icon + material-RGBA tint,
   the same shape casings already use. This is the bulk of the effort and the real unknown. It
   sits under the #35 extraction plan (a sibling of lane 6, the texture manifest).
2. **Modelling decision (cheap, but a real choice).** GT has many cable materials per tier
   (tin, lead, ... all carry LV), so "the LV cable" is not canonical. Pick a representative
   cable material per voltage tier and map amperage thickness to the wire-gauge sprite; pick one
   representative item-pipe and one fluid-pipe sprite for v1 (matches #4's single-channel
   non-goal). Document the table. The spike ships a starter `TIER_RGBA` table.
3. **Previewer wrap (easy, DONE in this spike).** Feed a per-route baked sprite into the route
   bars, degrade to the flat colour when absent. Small, previewer-internal, no contract change.

## What this spike proved (runnable)

`spike_pipe_textures.py` runs the **real** path end to end, standing in only the source PNG:

- `bake_layers([{icon: WIRE, rgba: <tier colour>, glow: false}], ...)` tints and composites a
  wire sprite exactly as a real cable would (proves workstream 1's bake step reuses unchanged).
- The baked PNG is attached to the scene as `routeTextures["power:LV"]` (proves the scene shape:
  a `netId -> dataURI` map, analogous to `scene["textures"]`).
- `previewer/html.py` was patched (about 8 lines): `bar()`/`node()` take an optional material,
  and the route loop builds one `MeshStandardMaterial({map: loadTex(uri)})` per route when the
  scene supplies a sprite, else the existing flat colour. Fallback is the same graceful-
  degradation contract the machine cubes use.
- Output `out/sand-pipes-spike.html`: the sand line's `power:LV` cable renders wrapped in the
  tinted wire sprite. All 21 `test_previewer.py` tests still pass (the wrap is opt-in).

The synthesized wire sprite is a stand-in; only the source PNG is fake. Everything downstream is
production code, so once workstream 1 lands a real cable icon in the manifest, workstreams 2 and
3 are a short hop.

## Recommended plan for #4

- **Phase A**: extend the extractor to dump cable + pipe sprites into the manifest (workstream 1).
  Needs the gtnh-extractor JDK toolchain; this is where the effort and risk live.
- **Phase B**: land the tier -> material / gauge mapping (workstream 2) and the previewer wrap
  (workstream 3, essentially this spike's patch, cleaned up with tests), reading real sprites.

The dependency ordering matters: the wrap cannot be meaningfully tested against real textures
until the extractor produces them, but it can be built and unit-tested against a fixture sprite
in parallel (this spike is that proof).

## Files touched (spike, not for merge as-is)

- `src/gtnh_solver/previewer/html.py`: optional material on `bar()`/`node()` + route-loop wrap.
- `spike_pipe_textures.py`: the standalone proof (synthesized sprite -> bake -> scene -> render).
- `out/sand-pipes-spike.html`: the rendered result.
