# examples/

Sample inputs for developing and testing the adapter and solver.

Drop a **gtnh-factory-flow exported plan JSON** here to run the adapter and solver against a
real plan. Two forks of that app emit plans the adapter reads, and they are **not** distinguishable
by `schemaVersion` (both spell it as a small integer, incompatibly), so the adapter identifies the
producer from structural markers and `--plan-schema` overrides it:

| Fixture | Fork | Identified by |
|---|---|---|
| `gtnh-sand.json`, `gtnh-nitrobenzene.json` | [MrBruh/gtnh-factory-flow](https://github.com/MrBruh/gtnh-factory-flow) | a `resolved` throughput block, `app`, `schemaVersion: 2` |
| `gtnh-parallel-sand.json` | [arodoid/gtnh-factory-flow](https://github.com/arodoid/gtnh-factory-flow) | `recipes[].machineHandlers`; no `resolved`, `schemaVersion: 1` |

The original upstream both forks descend from is
[Samiracle64/gtnh-factory-flow](https://github.com/Samiracle64/gtnh-factory-flow).

```bash
gtnh-solve examples/<your-plan>.json                 # print the build guide to stdout
gtnh-solve examples/<your-plan>.json -o guide.txt    # ...or write the guide to a file
gtnh-solve examples/<your-plan>.json --preview view.html  # ...or a double-clickable 3D preview
```

These are user-exported data files (the GTNH recipe/texture data inside them belongs to its
owners). Keep large or proprietary plans out of version control; small representative plans
used as test fixtures are welcome.
