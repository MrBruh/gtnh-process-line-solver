"""Tests for the CI version resolver (``tools/dataset_ci/resolve_versions.py``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dataset_ci import resolve_versions as rv

FIXTURES = Path(__file__).parent / "fixtures" / "dataset_ci"

# The real DreamAssemblerXXL manifest listing (trimmed) drives the stable-pick tests.
LISTING = [
    ".keep",
    "2.5.1.json",
    "2.6.0-beta-1.json",
    "2.7.4.json",
    "2.8.0-rc-1.json",
    "2.8.4.json",
    "2.9.0-beta-1.json",
    "daily.json",
    "experimental.json",
    "previous_daily.json",
]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("2.8.4.json", True),
        ("2.5.1.json", True),
        ("2.10.json", True),
        ("2.9.0-beta-1.json", False),
        ("2.8.0-rc-1.json", False),
        ("daily.json", False),
        ("experimental.json", False),
        (".keep", False),
        ("2.8.4.txt", False),
    ],
)
def test_is_stable_manifest(name: str, expected: bool) -> None:
    assert rv.is_stable_manifest(name) is expected


def test_latest_stable_version_picks_highest() -> None:
    # 2.9.0-beta-1 is newer numerically but a prerelease, so 2.8.4 wins.
    assert rv.latest_stable_version(LISTING) == "2.8.4"


def test_latest_stable_version_orders_numerically_not_lexically() -> None:
    assert rv.latest_stable_version(["2.9.0.json", "2.10.0.json", "2.8.4.json"]) == "2.10.0"


def test_latest_stable_version_no_stable_raises() -> None:
    with pytest.raises(rv.DatasetCIError, match="no stable pack manifest"):
        rv.latest_stable_version(["daily.json", "2.9.0-beta-1.json"])


def test_parse_manifest_extracts_tracked_versions() -> None:
    manifest = json.loads((FIXTURES / "manifest_2.8.4.json").read_text(encoding="utf-8"))
    state = rv.parse_manifest(manifest, rv.DEFAULT_TRACKED)
    assert state.pack_version == "2.8.4"
    assert state.mods == {"GT5-Unofficial": "5.09.51.482", "StructureLib": "1.4.23"}


def test_parse_manifest_missing_tracked_mod_raises() -> None:
    manifest = {"version": "2.8.4", "github_mods": {"StructureLib": {"version": "1.4.23"}}}
    with pytest.raises(rv.DatasetCIError, match="GT5-Unofficial"):
        rv.parse_manifest(manifest, rv.DEFAULT_TRACKED)


@pytest.mark.parametrize(
    "manifest",
    [
        {"github_mods": {}},  # no version
        {"version": "2.8.4"},  # no github_mods
        {"version": 284, "github_mods": {}},  # non-string version
    ],
)
def test_parse_manifest_malformed_raises(manifest: dict[str, object]) -> None:
    with pytest.raises(rv.DatasetCIError):
        rv.parse_manifest(manifest, rv.DEFAULT_TRACKED)


def test_load_lock_reads_fixture() -> None:
    state = rv.load_lock(FIXTURES / "gtnh.lock.json")
    assert state is not None
    assert state.pack_version == "2.8.3"
    assert state.mods["GT5-Unofficial"] == "5.09.51.400"


def test_load_lock_missing_returns_none(tmp_path: Path) -> None:
    assert rv.load_lock(tmp_path / "absent.json") is None


@pytest.mark.parametrize(
    "payload",
    [
        '["not", "an", "object"]',
        '{"pack_version": "2.8.4"}',
        '{"pack_version": "x", "mods": {"a": 1}}',
    ],
)
def test_load_lock_malformed_raises(tmp_path: Path, payload: str) -> None:
    lock = tmp_path / "gtnh.lock.json"
    lock.write_text(payload, encoding="utf-8")
    with pytest.raises(rv.DatasetCIError):
        rv.load_lock(lock)


def test_write_lock_round_trips_sorted(tmp_path: Path) -> None:
    state = rv.LockState("2.8.4", {"StructureLib": "1.4.23", "GT5-Unofficial": "5.09.51.482"})
    out = tmp_path / "gtnh.lock.json"
    rv.write_lock(out, state)
    text = out.read_text(encoding="utf-8")
    assert text.endswith("\n")
    data = json.loads(text)
    assert data == {
        "schema": rv.LOCK_SCHEMA,
        "pack_version": "2.8.4",
        "mods": {"GT5-Unofficial": "5.09.51.482", "StructureLib": "1.4.23"},
    }
    # Keys are emitted sorted so the committed diff is stable.
    assert list(data["mods"]) == ["GT5-Unofficial", "StructureLib"]
    assert rv.load_lock(out) == state


def test_diff_states_unchanged() -> None:
    state = rv.LockState("2.8.4", {"GT5-Unofficial": "5.09.51.482"})
    diff = rv.diff_states(state, state)
    assert diff.changed is False
    assert "unchanged" in diff.summary


def test_diff_states_first_run_is_changed() -> None:
    new = rv.LockState("2.8.4", {"GT5-Unofficial": "5.09.51.482"})
    diff = rv.diff_states(None, new)
    assert diff.changed is True
    assert "Initial lock" in diff.summary


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (rv.LockState("2.8.3", {"m": "1"}), rv.LockState("2.8.4", {"m": "1"})),  # pack bumped
        (rv.LockState("2.8.4", {"m": "1"}), rv.LockState("2.8.4", {"m": "2"})),  # mod bumped
    ],
)
def test_diff_states_detects_changes(old: rv.LockState, new: rv.LockState) -> None:
    assert rv.diff_states(old, new).changed is True


def test_render_version_summary_covers_new_bump_removed() -> None:
    old = rv.LockState("2.8.3", {"GT5-Unofficial": "5.09.51.400", "GoneMod": "1.0"})
    new = rv.LockState("2.8.4", {"GT5-Unofficial": "5.09.51.482", "StructureLib": "1.4.23"})
    summary = rv.render_version_summary(old, new)
    assert "`2.8.3` -> `2.8.4`" in summary
    assert "`GT5-Unofficial`: `5.09.51.400` -> `5.09.51.482`" in summary
    assert "`StructureLib`: (new) `1.4.23`" in summary
    assert "`GoneMod`: removed (was `1.0`)" in summary


def test_bump_gradle_rewrites_versions() -> None:
    text = (FIXTURES / "dependencies.gradle").read_text(encoding="utf-8")
    updated, unmatched = rv.bump_gradle(
        text, {"GT5-Unofficial": "5.09.51.482", "StructureLib": "1.4.38"}
    )
    assert unmatched == []
    assert "GT5-Unofficial:5.09.51.482:dev" in updated
    assert "StructureLib:1.4.38:dev" in updated
    assert "5.09.51.400" not in updated


def test_bump_gradle_reports_unmatched() -> None:
    updated, unmatched = rv.bump_gradle("nothing here", {"GT5-Unofficial": "5.09.51.482"})
    assert updated == "nothing here"
    assert unmatched == ["GT5-Unofficial"]


# --- CLI -------------------------------------------------------------------------------


def test_cli_latest_version(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    listing = tmp_path / "listing.txt"
    listing.write_text("\n".join(LISTING) + "\n", encoding="utf-8")
    assert rv.main(["latest-version", "--listing", str(listing)]) == 0
    assert capsys.readouterr().out.strip() == "2.8.4"


def _github_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    return out


def test_cli_resolve_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh_output = _github_output(monkeypatch, tmp_path)
    summary = tmp_path / "summary.md"
    code = rv.main(
        [
            "resolve",
            "--manifest",
            str(FIXTURES / "manifest_2.8.4.json"),
            "--lock",
            str(FIXTURES / "gtnh.lock.json"),
            "--summary-out",
            str(summary),
        ]
    )
    assert code == 0
    outputs = gh_output.read_text(encoding="utf-8")
    assert "changed=true" in outputs
    assert "pack_version=2.8.4" in outputs
    assert summary.read_text(encoding="utf-8") == capsys.readouterr().out


def test_cli_resolve_unchanged_emits_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gh_output = _github_output(monkeypatch, tmp_path)
    # Lock that already matches the manifest -> no-op.
    lock = tmp_path / "gtnh.lock.json"
    rv.write_lock(
        lock, rv.LockState("2.8.4", {"GT5-Unofficial": "5.09.51.482", "StructureLib": "1.4.23"})
    )
    code = rv.main(
        ["resolve", "--manifest", str(FIXTURES / "manifest_2.8.4.json"), "--lock", str(lock)]
    )
    assert code == 0
    assert "changed=false" in gh_output.read_text(encoding="utf-8")


def test_cli_resolve_without_github_output_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    lock = tmp_path / "gtnh.lock.json"
    rv.write_lock(
        lock, rv.LockState("2.8.4", {"GT5-Unofficial": "5.09.51.482", "StructureLib": "1.4.23"})
    )
    assert (
        rv.main(
            ["resolve", "--manifest", str(FIXTURES / "manifest_2.8.4.json"), "--lock", str(lock)]
        )
        == 0
    )
    # With no GITHUB_OUTPUT, outputs fall back to stdout.
    assert "changed=false" in capsys.readouterr().out


def test_cli_bump_gradle_in_place(tmp_path: Path) -> None:
    gradle = tmp_path / "dependencies.gradle"
    gradle.write_text(
        (FIXTURES / "dependencies.gradle").read_text(encoding="utf-8"), encoding="utf-8"
    )
    code = rv.main(
        [
            "bump-gradle",
            "--gradle",
            str(gradle),
            "--manifest",
            str(FIXTURES / "manifest_2.8.4.json"),
        ]
    )
    assert code == 0
    assert "GT5-Unofficial:5.09.51.482:dev" in gradle.read_text(encoding="utf-8")


def test_cli_bump_gradle_unmatched_fails(tmp_path: Path) -> None:
    gradle = tmp_path / "dependencies.gradle"
    gradle.write_text("no coordinates here\n", encoding="utf-8")
    code = rv.main(
        [
            "bump-gradle",
            "--gradle",
            str(gradle),
            "--manifest",
            str(FIXTURES / "manifest_2.8.4.json"),
        ]
    )
    assert code == 1


def test_cli_write_lock(tmp_path: Path) -> None:
    out = tmp_path / "gtnh.lock.json"
    code = rv.main(
        ["write-lock", "--manifest", str(FIXTURES / "manifest_2.8.4.json"), "--out", str(out)]
    )
    assert code == 0
    assert rv.load_lock(out) == rv.LockState(
        "2.8.4", {"GT5-Unofficial": "5.09.51.482", "StructureLib": "1.4.23"}
    )
