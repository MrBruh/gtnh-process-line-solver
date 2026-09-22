# examples/

Sample inputs for developing and testing the adapter and solver.

Drop a **gtnh-factory-flow exported plan JSON** here to run the adapter and solver against a
real plan. Two forks of that app emit plans the adapter reads, and they are **not** distinguishable
by `schemaVersion` (both spell it as a small integer, incompatibly), so the adapter identifies the
producer from structural markers and `--plan-schema` overrides it:

| Fixture | Fork | Identified by |
|---|---|---|
| `gtnh-sand.json`, `gtnh-nitrobenzene.json` | [MrBruh/gtnh-factory-flow](https://github.com/MrBruh/gtnh-factory-flow) | a `resolved` throughput block, `app`, `schemaVersion: 2` |
| `gtnh-parallel-sand.json` | the arodoid fork | `recipes[].machineHandlers`; no `resolved`, `schemaVersion: 1` |
| `ev-nitrobenzene.json` | the arodoid fork | as above; a real GTNH 2.9 line (see below) |

The original upstream both forks descend from is
[Samiracle64/gtnh-factory-flow](https://github.com/Samiracle64/gtnh-factory-flow).

```bash
gtnh-solve examples/<your-plan>.json                 # print the solved layout as JSON to stdout
gtnh-solve examples/<your-plan>.json > layout.json   # ...which is how it goes to a file
gtnh-solve examples/<your-plan>.json --preview view.html  # ...or a double-clickable 3D preview
gtnh-solve examples/<your-plan>.json --schematic line.schematic  # ...or a Schematica build ghost
```

These are user-exported data files (the GTNH recipe/texture data inside them belongs to its
owners). Keep large or proprietary plans out of version control; small representative plans
used as test fixtures are welcome.

**`ev-nitrobenzene.json` is the one deliberate exception.** At about 1.1 MB it is exactly the kind
of plan that note keeps out, and it is committed anyway because it is the only real GTNH 2.9 line in
the repo: nine multiblock nodes that `machineCount` expands to 14 machines, a Dangote Distillus at
12x parallel, and shared multiblock ports. `gtnh-parallel-sand.json` is a 9-hammer toy by
comparison. It is the acceptance fixture for the 2.9 preview and export work (#205 to #208), so it
is kept byte-identical to the export rather than trimmed. `.pre-commit-config.yaml` exempts this one
file from the large-file check, and `tools/derive_small_manifest.py` skips it, so the committed
texture manifest does not grow to cover its EV tier and 2.9 machines.

On a fresh clone its multiblocks resolve to 1x1x1 boxes: the committed dump in `data/multiblocks/`
is a two-machine sample (an Electric Blast Furnace and a Vacuum Freezer), not a census, so none of
this line's machines is in it. Real footprints need a local 2.9 dump from the extractor (see
`docs/dataset-extraction/`). The default test suite only adapts this plan; it never solves it.
