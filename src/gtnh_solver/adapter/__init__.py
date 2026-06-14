"""adapter — parse gtnh-factory-flow's exported plan JSON into the InputIR.

Input is a gtnh-factory-flow exported plan (Zod-validated JSON: graph nodes/edges, fuel
profiles, targets, and the exact recipes placed) plus its versioned recipe dataset. Decision
(docs/ARCHITECTURE.md #3): consume this documented export directly; validate against a pinned
plan-schema version and pin a recipe-dataset version. No code is vendored.

TODO(adapter): load + validate the exported plan JSON; derive per-net typed throughput from
the embedded recipes + balance; map machines/edges to InputIR; fail clearly on schema/dataset
version mismatch (never silently drop fields).
"""
