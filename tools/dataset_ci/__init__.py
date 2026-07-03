"""CI-only helpers for the automated GTNH dataset update loop (issue #47, lane 4).

This package is *tooling*, never imported by ``src/gtnh_solver`` at runtime. It backs
``.github/workflows/update-dataset.yml``: resolve the latest stable pack version from the
DreamAssemblerXXL manifests, diff it against the committed lock file, bump the extractor's
Gradle dependency pins, and render a reviewable Markdown summary for the update PR.
"""
