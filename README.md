# gtnh_solver

**Physical place-and-route solver for GregTech: New Horizons process lines.**

> **Status: pre-alpha.** The design is complete and reviewed; the IR contracts (`ir/`) and
> the validator are implemented. The rest of the pipeline — adapter, dataset, placement,
> router, solver, previewer, build guide — is scaffolded stubs. This repo is the
> contributable foundation. See [`docs/ROADMAP.md`](docs/ROADMAP.md).

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
                        placement (SA/LNS) ◄─routing-aware cost─► router (A*, 2.5D,
                                  │            + feedback loop     per-commodity, power)
                                  └──────────────┬─────────────────┘
                                                 ▼
                                           validator (independent checks)
                                          ┌──────┴──────┐
                                          ▼             ▼
                                     previewer      build guide
                                     (three.js)     (BoM, layers)
```

## Quickstart (planned)

```bash
pip install -e ".[dev]"
gtnh-solve plan.json --out out/   # plan.json exported from gtnh-factory-flow
# opens the previewer; writes a build guide
```

(The CLI does not exist yet — this is the target interface. See the roadmap.)

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

New here? Read [`CONTRIBUTING.md`](CONTRIBUTING.md) — it maps the parallel workstreams
("lanes") so you can pick an independent piece and start. The architecture is designed so
the adapter, placement, router, validator, dataset, and previewer can be built in parallel
once the IR contract lands.

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Consumes plan/recipe JSON
exported by the MIT-licensed [`gtnh-factory-flow`](https://github.com/Samiracle64/gtnh-factory-flow);
no third-party code is vendored.
