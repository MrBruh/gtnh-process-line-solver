# gtnh_solver

**Physical place-and-route solver for GregTech: New Horizons process lines.**

> **Status: Phase 1 complete - crude but end-to-end.** A real gtnh-factory-flow export now
> goes all the way to a validated, buildable layout: the IR contracts (`ir/`), adapter,
> dataset (voltage ladder + amp helpers, a schema-v1 physical-rules loader, and the first
> multiblock footprints), placement, router, solver, validator, previewer,
> and build guide are all implemented - each is an `Added` entry in
> [`CHANGELOG.md`](CHANGELOG.md). Some pieces stay deliberately crude (single-channel routing,
> size-or-reject power), so **Phase 2 is quality**: SA/LNS placement polish, the multi-channel
> realizability invariant, power optimization, the full physical dataset (the extractor is built,
> but only the first footprint entries ship today), and previewer polish. See
> [`docs/ROADMAP.md`](docs/ROADMAP.md).

Yes, this project is heavily vibe coded. If you see any areas in the code or documentation that can be
de-slopified, feel free to contribute and make issues or PR's!

**Input comes from [gtnh-factory-flow](https://github.com/Samiracle64/gtnh-factory-flow)** (MIT):
you design and balance a production line there, export it as plan JSON, and `gtnh_solver` turns
that into a physical, buildable layout.

## How it works (data flow)

```
   gtnh-factory-flow (exported plan JSON) ──adapter──► IR ◄── physical-rules dataset
                                             │      (footprints, faces, tiers, ME)
                                             ▼
                        placement (SA/LNS) ◄─routing-aware cost─► router (A*, 3D,
                                  │            + feedback loop     per-commodity, power)
                                  └──────────────┬─────────────────┘
                                                 ▼
                                           validator (independent checks)
                                          ┌──────┴──────┐
                                          ▼             ▼
                                     previewer      build guide
                                     (three.js)     (BoM, layers)
```

## Quickstart

Needs **Python 3.10+** (`pyproject.toml` sets `requires-python = ">=3.10"`; an older default
`python` makes `pip install` fail opaquely). Work inside a virtual env:

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: py -3.12 -m venv .venv; .venv\Scripts\activate
pip install -e ".[dev]"
gtnh-solve examples/gtnh-sand.json        # solve a gtnh-factory-flow export, print the build guide
gtnh-solve plan.json -o guide.txt         # ...or write the guide to a file
gtnh-solve plan.json --preview view.html  # ...or a double-clickable 3D preview (three.js)
gtnh-solve plan.json --fast               # skip optimization: a near-instant constructive layout
gtnh-solve plan.json --seed 3             # pick the solver seed (deterministic per seed)
gtnh-solve plan.json --objective volume   # what "compact" means: footprint|volume|balanced
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md#setup) for the full dev setup (hooks, tests, lint).

Exit code: 0 when the layout is fully valid, 1 when the solver can only return an explicit
infeasibility (the reason prints to stderr), 2 when the export can't be loaded. The `--preview`
three.js viewer is built; a congestion heatmap, multi-seed compare, and offline (vendored)
three.js are Phase 2 (see the roadmap).

## Documentation

| Doc | What's in it |
|-----|--------------|
| [`docs/DESIGN.md`](docs/DESIGN.md) | Problem, premises, chosen approach |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Components, data flow, engineering decisions |
| [`docs/IR.md`](docs/IR.md) | The IR + output-layout contracts |
| [`docs/DOMAIN.md`](docs/DOMAIN.md) | GT:NH rules the solver encodes |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | v1 scope, deferrals, milestones, parallel lanes |
| [`docs/TESTING.md`](docs/TESTING.md) | Test strategy and ground-truth approach |

## Contributing

New here? Read [`CONTRIBUTING.md`](CONTRIBUTING.md) - its **Build lanes** table maps the
Phase 2 workstreams with a status for each, so you can pick an actionable piece and start.
Phase 1 already built a crude end-to-end version of the whole pipeline; the lanes are the
quality upgrades on top of it (deeper phase context in [`docs/ROADMAP.md`](docs/ROADMAP.md)).

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Consumes plan/recipe JSON
exported by the MIT-licensed [`gtnh-factory-flow`](https://github.com/Samiracle64/gtnh-factory-flow);
no third-party code is vendored.
