"""Resolve GTNH pack versions from DreamAssemblerXXL manifests and diff the lock file.

This is the typed core of ``.github/workflows/update-dataset.yml`` (issue #47, lane 4). It
is deliberately network-free and stdlib-only: the workflow does the HTTP fetches (with
``gh`` / ``curl`` and the runner's ``GITHUB_TOKEN``) and hands local files to this module,
which keeps the logic trivially unit-testable and ``mypy --strict`` clean.

Pipeline (one subcommand per workflow phase)::

    manifest listing --latest-version--> pack version
    manifest.json + gtnh.lock.json --resolve--> changed?/pack_version + summary
    dependencies.gradle + manifest.json --bump-gradle--> pinned mod coordinates
    manifest.json --write-lock--> new gtnh.lock.json

Contract: the DreamAssemblerXXL manifest (``releases/manifests/<packversion>.json``)
--------------------------------------------------------------------------------------
Real format (verified against ``2.8.4.json``)::

    {
      "version": "2.8.4",
      "github_mods":   {"GT5-Unofficial": {"version": "5.09.51.482", "side": "BOTH"}, ...},
      "external_mods": {"Automagy": {"version": "0.28.2", "side": "BOTH"}, ...},
      ...
    }

The extractor's dependencies (GT5-Unofficial, StructureLib) are GTNH-hosted, so they live
under ``github_mods``. "Latest stable" = the highest manifest whose filename stem is purely
a dotted numeric version (``2.8.4``); prereleases (``-beta-``/``-rc-``) and the rolling
``daily``/``experimental`` manifests are excluded by construction.

Contract: ``gtnh.lock.json`` (repo root; owned by lane 1, read here at runtime)
-------------------------------------------------------------------------------
::

    {
        "schema": 1,
        "pack_version": "2.8.4",
        "mods": {"GT5-Unofficial": "5.09.51.482", "StructureLib": "1.4.23"},
    }

``mods`` pins exactly the *tracked* extractor dependencies (``--tracked``), sorted for a
stable, reviewable diff. Tests use a fixture lock under ``tests/fixtures/dataset_ci/`` and
never touch a real repo-root lock file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOCK_SCHEMA = 1
DEFAULT_TRACKED: tuple[str, ...] = ("GT5-Unofficial", "StructureLib")

# A stable pack manifest filename is purely a dotted numeric version plus ``.json``.
# This rejects ``2.9.0-beta-1.json``, ``daily.json``, ``experimental.json``, ``.keep`` etc.
_STABLE_STEM = re.compile(r"^\d+(?:\.\d+)*$")

# GTNH dependency coordinates look like ``com.github.GTNewHorizons:<Mod>:<version>[:dev]``.
# Lane 1 owns dependencies.gradle; we only rewrite the version segment of these coordinates.
_COORD_PREFIX = r"com\.github\.GTNewHorizons:"


class DatasetCIError(RuntimeError):
    """A resolution/parse failure that must fail the workflow loudly (never silently)."""


@dataclass(frozen=True)
class LockState:
    """The pinned pack version plus the tracked extractor mod versions."""

    pack_version: str
    mods: dict[str, str]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": LOCK_SCHEMA,
            "pack_version": self.pack_version,
            "mods": dict(sorted(self.mods.items())),
        }


@dataclass(frozen=True)
class LockDiff:
    """Whether the resolved state differs from the lock, plus a Markdown summary."""

    changed: bool
    summary: str


# --------------------------------------------------------------------------------------
# Pure logic (no I/O)
# --------------------------------------------------------------------------------------


def is_stable_manifest(filename: str) -> bool:
    """True for a stable ``<version>.json`` manifest (not prerelease/daily/experimental)."""
    if not filename.endswith(".json"):
        return False
    return bool(_STABLE_STEM.match(filename[: -len(".json")]))


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def latest_stable_version(filenames: Iterable[str]) -> str:
    """Pick the highest stable pack version from a manifest-directory listing."""
    stable = [name[: -len(".json")] for name in filenames if is_stable_manifest(name)]
    if not stable:
        raise DatasetCIError("no stable pack manifest found in the DreamAssemblerXXL listing")
    return max(stable, key=_version_key)


def parse_manifest(manifest: Mapping[str, Any], tracked: Sequence[str]) -> LockState:
    """Extract the pack version and the tracked mod versions from a manifest mapping."""
    version = manifest.get("version")
    if not isinstance(version, str):
        raise DatasetCIError("manifest is missing a string 'version' field")
    github_mods = manifest.get("github_mods")
    if not isinstance(github_mods, Mapping):
        raise DatasetCIError("manifest is missing a 'github_mods' mapping")

    mods: dict[str, str] = {}
    missing: list[str] = []
    for name in tracked:
        entry = github_mods.get(name)
        mod_version = entry.get("version") if isinstance(entry, Mapping) else None
        if isinstance(mod_version, str):
            mods[name] = mod_version
        else:
            missing.append(name)
    if missing:
        raise DatasetCIError(
            "tracked mods absent from manifest github_mods: " + ", ".join(sorted(missing))
        )
    return LockState(pack_version=version, mods=mods)


def diff_states(old: LockState | None, new: LockState) -> LockDiff:
    """Diff the resolved state against the current lock (``None`` = no lock yet)."""
    changed = old is None or old.pack_version != new.pack_version or old.mods != new.mods
    return LockDiff(changed=changed, summary=render_version_summary(old, new))


def render_version_summary(old: LockState | None, new: LockState) -> str:
    """Render the version-bump portion of the PR body as Markdown."""
    lines: list[str] = ["## Pack version", ""]
    if old is None:
        lines.append(f"Initial lock at pack `{new.pack_version}`.")
    elif old.pack_version != new.pack_version:
        lines.append(f"`{old.pack_version}` -> `{new.pack_version}`")
    else:
        lines.append(f"`{new.pack_version}` (unchanged)")

    lines += ["", "## Tracked mod versions", ""]
    old_mods = old.mods if old is not None else {}
    for name in sorted(new.mods):
        new_version = new.mods[name]
        old_version = old_mods.get(name)
        if old_version is None:
            lines.append(f"- `{name}`: (new) `{new_version}`")
        elif old_version != new_version:
            lines.append(f"- `{name}`: `{old_version}` -> `{new_version}`")
        else:
            lines.append(f"- `{name}`: `{new_version}` (unchanged)")
    for name in sorted(set(old_mods) - set(new.mods)):
        lines.append(f"- `{name}`: removed (was `{old_mods[name]}`)")
    return "\n".join(lines) + "\n"


def bump_gradle(text: str, mods: Mapping[str, str]) -> tuple[str, list[str]]:
    """Rewrite the pinned version of each tracked mod's GTNH coordinate.

    Returns the updated text and the sorted names of any tracked mod whose coordinate was
    not found (the caller should fail loudly rather than build a stale version).
    """
    result = text
    unmatched: list[str] = []
    for name, version in mods.items():
        pattern = re.compile("(" + _COORD_PREFIX + re.escape(name) + ":)([^\\s:'\"()]+)")
        # Escape the replacement so a version can never be read as a group reference.
        replacement = "\\g<1>" + version.replace("\\", "\\\\")
        result, count = pattern.subn(replacement, result)
        if count == 0:
            unmatched.append(name)
    return result, sorted(unmatched)


# --------------------------------------------------------------------------------------
# I/O helpers
# --------------------------------------------------------------------------------------


def _load_json_object(path: Path) -> Mapping[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise DatasetCIError(f"{path} is not a JSON object")
    return data


def load_lock(path: Path) -> LockState | None:
    """Read the lock file, or ``None`` when it does not exist yet (first ever run)."""
    if not path.exists():
        return None
    data = _load_json_object(path)
    version = data.get("pack_version")
    mods = data.get("mods")
    if not isinstance(version, str) or not isinstance(mods, Mapping):
        raise DatasetCIError(f"lock file {path} is missing 'pack_version'/'mods'")
    typed_mods: dict[str, str] = {}
    for key, value in mods.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise DatasetCIError(f"lock file {path} has a non-string mod entry")
        typed_mods[key] = value
    return LockState(pack_version=version, mods=typed_mods)


def write_lock(path: Path, state: LockState) -> None:
    """Write the lock file with stable key ordering and a trailing newline."""
    path.write_text(json.dumps(state.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def emit_github_output(key: str, value: str) -> None:
    """Append ``key=value`` to ``$GITHUB_OUTPUT`` (or stdout when running locally)."""
    line = f"{key}={value}\n"
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(line)
    else:
        sys.stdout.write(line)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _tracked(values: Sequence[Any]) -> list[str]:
    return [str(value) for value in values]


def _cmd_latest_version(listing: Path) -> int:
    names = [line.strip() for line in listing.read_text(encoding="utf-8").splitlines()]
    sys.stdout.write(latest_stable_version(name for name in names if name) + "\n")
    return 0


def _cmd_resolve(
    manifest: Path, lock: Path, tracked: Sequence[str], summary_out: Path | None
) -> int:
    new = parse_manifest(_load_json_object(manifest), tracked)
    old = load_lock(lock)
    diff = diff_states(old, new)
    emit_github_output("changed", "true" if diff.changed else "false")
    emit_github_output("pack_version", new.pack_version)
    if summary_out is not None:
        summary_out.write_text(diff.summary, encoding="utf-8")
    sys.stdout.write(diff.summary)
    return 0


def _cmd_bump_gradle(gradle: Path, manifest: Path, tracked: Sequence[str]) -> int:
    new = parse_manifest(_load_json_object(manifest), tracked)
    updated, unmatched = bump_gradle(gradle.read_text(encoding="utf-8"), new.mods)
    if unmatched:
        raise DatasetCIError("no GTNH coordinate found in gradle for: " + ", ".join(unmatched))
    gradle.write_text(updated, encoding="utf-8")
    sys.stdout.write(f"bumped {len(new.mods)} mod coordinate(s) in {gradle}\n")
    return 0


def _cmd_write_lock(manifest: Path, out: Path, tracked: Sequence[str]) -> int:
    new = parse_manifest(_load_json_object(manifest), tracked)
    write_lock(out, new)
    sys.stdout.write(f"wrote {out} pinned at pack {new.pack_version}\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GTNH dataset version resolver (CI helper).")
    sub = parser.add_subparsers(dest="command", required=True)

    latest = sub.add_parser("latest-version", help="print the latest stable pack version")
    latest.add_argument("--listing", required=True, type=Path, help="file of manifest filenames")

    resolve = sub.add_parser("resolve", help="diff a manifest against the lock file")
    resolve.add_argument("--manifest", required=True, type=Path)
    resolve.add_argument("--lock", required=True, type=Path)
    resolve.add_argument("--tracked", nargs="+", default=list(DEFAULT_TRACKED))
    resolve.add_argument("--summary-out", type=Path, help="write the Markdown summary here")

    bump = sub.add_parser("bump-gradle", help="rewrite the extractor's pinned mod versions")
    bump.add_argument("--gradle", required=True, type=Path)
    bump.add_argument("--manifest", required=True, type=Path)
    bump.add_argument("--tracked", nargs="+", default=list(DEFAULT_TRACKED))

    write = sub.add_parser("write-lock", help="write gtnh.lock.json from a manifest")
    write.add_argument("--manifest", required=True, type=Path)
    write.add_argument("--out", required=True, type=Path)
    write.add_argument("--tracked", nargs="+", default=list(DEFAULT_TRACKED))

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "latest-version":
            return _cmd_latest_version(Path(args.listing))
        if args.command == "resolve":
            summary_out = Path(args.summary_out) if args.summary_out is not None else None
            return _cmd_resolve(
                Path(args.manifest), Path(args.lock), _tracked(args.tracked), summary_out
            )
        if args.command == "bump-gradle":
            return _cmd_bump_gradle(Path(args.gradle), Path(args.manifest), _tracked(args.tracked))
        if args.command == "write-lock":
            return _cmd_write_lock(Path(args.manifest), Path(args.out), _tracked(args.tracked))
    except DatasetCIError as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    raise AssertionError(f"unhandled command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
