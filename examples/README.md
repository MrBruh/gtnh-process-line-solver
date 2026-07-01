# examples/

Sample inputs for developing and testing the adapter and solver.

Drop a **gtnh-factory-flow exported plan JSON** here to run the adapter and solver against a
real plan. Export it from the **maintained fork the adapter consumes**,
[MrBruh/gtnh-factory-flow](https://github.com/MrBruh/gtnh-factory-flow) (its export carries the
`resolved` block the adapter reads); the original upstream is
[Samiracle64/gtnh-factory-flow](https://github.com/Samiracle64/gtnh-factory-flow).

```bash
gtnh-solve examples/<your-plan>.json                 # print the build guide to stdout
gtnh-solve examples/<your-plan>.json -o guide.txt    # ...or write the guide to a file
gtnh-solve examples/<your-plan>.json --preview view.html  # ...or a double-clickable 3D preview
```

These are user-exported data files (the GTNH recipe/texture data inside them belongs to its
owners). Keep large or proprietary plans out of version control; small representative plans
used as test fixtures are welcome.
