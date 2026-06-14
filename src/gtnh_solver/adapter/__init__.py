"""adapter — extract gtnh-flow's balanced graph into the InputIR.

gtnh-flow renders graphviz, not a clean API. The decision (docs/ARCHITECTURE.md #3): vendor
a fork under vendor/gtnh-flow/ and add ONE serialization point that dumps the computed graph
as IR JSON, which this module loads. A CI job tracks upstream; breaks are patched manually.

TODO(adapter): load IR JSON from the forked gtnh-flow; map its machines/edges/throughputs to
InputIR; handle missing/changed fields and version mismatch explicitly (never silently drop).
"""
