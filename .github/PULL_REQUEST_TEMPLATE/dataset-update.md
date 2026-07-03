## Automated dataset update

This PR was opened by `.github/workflows/update-dataset.yml` after a GTNH pack version
bump. It refreshes `data/multiblocks/`, `gtnh.lock.json`, and the extractor's pinned mod
versions. The version and dataset diffs are appended below. **Never auto-merge:** a human
must review the diff first.

### Dataset-diff review checklist

- [ ] **Coverage delta looks sane.** The controller count moved by a plausible amount. A
      large drop ("40 controllers vanished") means the extractor broke, not that GTNH
      deleted machines - investigate before merging.
- [ ] **No unexplained variant explosion.** A controller that gained many variants should
      map to a real GTNH change (a new tier/size), not a runaway sweep.
- [ ] **Failure-list changes are understood.** New entries in `_meta.json` `failures` are
      accounted for (a genuinely dropped/broken multiblock), and the failure count has not
      crept above the accepted threshold.
- [ ] **Added / removed multiblocks are expected** for this pack version's changelog.
- [ ] **Golden tests still pass** (they ran in the build job) and none were silently
      loosened to accommodate a regression.
- [ ] **Version bump matches the manifest** - `gtnh.lock.json` and the extractor pins agree
      with the tracked mod versions in the pack manifest.
