# Documentation index

The `docs/` folder is the source of truth for *why* and *how* `gtnh_solver` is built. Read this
first, then the file for whatever you are touching. Code and docs are meant to agree; where they
diverge, the doc records the intent, so reconcile one to the other and say which.

## Map

| Doc | One line |
|-----|----------|
| [DESIGN.md](DESIGN.md) | Problem framing: why a physical place-and-route solver for GT:NH exists, what it is and is not, and the agreed premises. |
| [DOMAIN.md](DOMAIN.md) | The GT:NH rules the solver encodes (machine faces, covers, single-channel pipes, shared-amperage power, the 1.7.10 platform) that both the router and the validator are built against. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Source of truth for how the solver is built: the end-to-end data flow and the nine engineering-review decisions. |
| [IR.md](IR.md) | The two versioned data contracts everything couples to, the input IR (the problem) and the output layout schema (the solution). |
| [ROADMAP.md](ROADMAP.md) | The phased v1 plan (Phase 1 thin slice, Phase 2 optimizer core), the parallel lanes, and the deferred v1.1+ work. |
| [TESTING.md](TESTING.md) | The testing strategy given there is no headless GT simulator: independent validator, hypothesis property tests, golden corpus. |
| **[dataset-extraction/](dataset-extraction/)** | How the physical dataset, multiblock structures and textures, is extracted from GT5-Unofficial. Its files are below. |

### dataset-extraction/

| Doc | One line |
|-----|----------|
| [requirements.md](dataset-extraction/requirements.md) | What the extraction pipeline must achieve: its outputs, constraints, and acceptance criteria (the *what*). |
| [implementation.md](dataset-extraction/implementation.md) | How the code achieves it: the Java extractor and the Python consumer, mechanism by mechanism (the *how*). |
| [texture-resolution.md](dataset-extraction/texture-resolution.md) | Deep dive on the texture pass: the routes `TextureDumper` tries to turn a `(block, meta)` into a sprite name, why a headless dedicated server needs more than one, and what is still unreachable. |
| [plan.md](dataset-extraction/plan.md) | The working extraction plan. **Temporary**: a roadmap that requirements.md and implementation.md gradually absorb; delete it once they have. |

## Keeping this current

Each doc names its intent; when you change code near one, update that doc in the same commit
(this repo's convention, see [CONTRIBUTING.md](../CONTRIBUTING.md)). When you add or remove a doc,
add or remove its row above.

**TODO (not built yet): automate the doc-freshness reminder.** A pre-commit hook or an advisory
CI check should flag when a commit touches a watched code path but no owning doc. Cheapest first
cut: a small `{ code-glob -> doc }` table (e.g. `src/gtnh_solver/router/** -> docs/ARCHITECTURE.md`,
`tools/gtnh-extractor/** -> docs/dataset-extraction/implementation.md`) that a pre-commit script
diffs the staged paths against, printing the docs worth a second look. Keep it **advisory**, never
blocking, so it nudges rather than gates. This note is the placeholder until it exists.
