# Golden corpus

Real-world ground truth for the validator, since there is no headless GT simulator
(docs/TESTING.md).

- **`good/`** — known-good layouts the validator MUST accept (hand-authored from real,
  working GT:NH builds to start; grown from the v1.1 round-trip importer later).
- **`bad/`** — known-bad layouts the validator MUST reject, each with a one-line note on
  *why* it's invalid (overlap, over-capacity pipe, burnt cable, blocked required face, ...).

Each case is a **pair** — an `InputIR` (the problem) and a `LayoutResult` (the candidate
solution), since the validator checks a solution *against* its problem (`validate(problem,
layout)`). Store one JSON per case of the form:

```json
{ "input": { ...InputIR... }, "layout": { ...LayoutResult... },
  "expect_ok": false, "expect_codes": ["machine_overlap"] }
```

`good/` cases set `expect_ok: true`; `bad/` cases set `expect_ok: false` and list the
`ViolationCode`s the validator must report (see `validator/report.py`). Keep cases small and
focused.

> Until this directory is populated from real GT:NH builds, the in-code corpus in
> `tests/test_validator.py` (one focused known-good/known-bad pair per violation) is the
> active gate.
