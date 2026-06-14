# examples/

Sample inputs for developing and testing the adapter and solver.

Drop a **gtnh-factory-flow exported plan JSON** here (export it from
[gtnh-factory-flow](https://github.com/Samiracle64/gtnh-factory-flow) or the live site) to run
the adapter and solver against a real plan:

```bash
gtnh-solve examples/<your-plan>.json --out out/
```

These are user-exported data files (the GTNH recipe/texture data inside them belongs to its
owners). Keep large or proprietary plans out of version control; small representative plans
used as test fixtures are welcome.
