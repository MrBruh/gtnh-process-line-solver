# Golden corpus

Real-world ground truth for the validator, since there is no headless GT simulator
(docs/TESTING.md).

- **`good/`** — known-good layouts the validator MUST accept (hand-authored from real,
  working GT:NH builds to start; grown from the v1.1 round-trip importer later).
- **`bad/`** — known-bad layouts the validator MUST reject, each with a one-line note on
  *why* it's invalid (overlap, over-capacity pipe, burnt cable, blocked required face, ...).

Each case is a `LayoutResult` JSON (see docs/IR.md) plus a sidecar `.expected` stating the
expected verdict and, for bad cases, the specific violation. Keep cases small and focused.
