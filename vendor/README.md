# vendor/

Third-party code vendored into this project.

## gtnh-flow (planned)

A pinned **fork** of [gtnh-flow](https://github.com/OrderedSet86/gtnh-flow) (MIT) will live
at `vendor/gtnh-flow/`, added as a git submodule:

```bash
git submodule add <your-fork-url> vendor/gtnh-flow
git submodule update --init
```

The fork adds **one serialization point** that dumps gtnh-flow's computed graph as IR JSON,
which `src/gtnh_solver/adapter/` consumes (decision #3 in docs/ARCHITECTURE.md). We do not
rebuild gtnh-flow; we consume its output across a JSON boundary.

**License:** keep gtnh-flow's MIT `LICENSE.txt` and copyright notice intact in the submodule,
and mark any files you change (per its MIT terms and Apache-2.0 §4(b)). See the repo-root
`NOTICE`.

A CI job (planned) rebases upstream and runs the adapter tests; if an upstream change breaks
the fork, the job alerts and the patch is fixed manually.
