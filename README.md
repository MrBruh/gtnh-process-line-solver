# gtnh_solver

**Physical place-and-route solver for GregTech: New Horizons process lines.**

> **Status: planning / pre-alpha.** The design is complete and reviewed; implementation
> has not started. This repo is the contributable foundation. See [`docs/ROADMAP.md`](docs/ROADMAP.md).

## The problem

GregTech: New Horizons (GT:NH) has mature tooling for the *logical* side of factory
design — [`gtnh-flow`](https://github.com/OrderedSet86/gtnh-flow) and its forks take a
target output and balance machine ratios, counts, power, and I/O into a flow graph. They
stop there. Nothing solves the *physical* side: given that balanced graph, **where does
every machine go in the world, and how do the pipes and wires route between them so the
line actually works?**

`gtnh_solver` is that missing layer. It takes a balanced process line, plus a dataset of
GT physical rules (machine footprints, pipe/wire tiers, face rules), and produces a
concrete, buildable layout — optimized for compactness under fixed constraints (pinned
input/output chests, a bounding region) — with an interactive 3D previewer so you can
inspect and compare candidate layouts.

In CS terms: **place-and-route** (the VLSI/PCB problem family) plus the **facility layout
problem**, retargeted to GregTech with full physical fidelity.

## How it works (data flow)

```
   gtnh-flow (logical balance) ──adapter──► IR ◄── physical-rules dataset
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
gtnh-solve path/to/line.yaml --out out/
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

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Vendors a fork of the
MIT-licensed `gtnh-flow`.
