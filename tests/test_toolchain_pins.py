"""The dev toolchain is pinned in two files that must agree, so something has to check.

``constraints-dev.txt`` pins the tools every clone and CI run installs. ``.pre-commit-config.yaml``
pins the ruff the git hook runs, and pre-commit builds that hook in its own isolated environment
from a git rev, which no constraints file can reach. So ruff is named twice, and the two can drift
apart silently: that is how the hook came to be 14 patch releases behind the ruff the dev extra
resolved, with the hook's formatter and a plain ``ruff format .`` free to disagree about the same
file. Dependabot now proposes both halves, which makes the drift *more* likely to happen one side
at a time, not less.

The comments in both files say "bump them together". This is what makes that true.

Parsed with regexes rather than a YAML/TOML reader on purpose: the pins are what is under test, so
the test should not itself depend on a parser whose version is one of the things being pinned.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_CONSTRAINTS = _ROOT / "constraints-dev.txt"
_PRE_COMMIT = _ROOT / ".pre-commit-config.yaml"

#: Tools that decide whether CI is red or green without any code changing, so a floating version
#: is an unexplained failure waiting to happen. Kept as a list here rather than derived from the
#: file, so *dropping* a pin is a test failure too - the silent direction of this drift.
_MUST_BE_PINNED = (
    "hypothesis",
    "mypy",
    "pre-commit",
    "pydantic",
    "pytest",
    "pytest-cov",
    "pytest-xdist",
    "ruff",
)


def _pins() -> dict[str, str]:
    """Every ``name==version`` in the constraints file, keyed by the normalized package name."""
    out: dict[str, str] = {}
    for line in _CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"\s*([A-Za-z0-9._-]+)\s*==\s*([^\s#]+)\s*", line)
        if match:
            out[match.group(1).lower().replace("_", "-")] = match.group(2)
    return out


def test_every_tool_that_decides_the_build_is_pinned() -> None:
    pins = _pins()
    assert not set(_MUST_BE_PINNED) - set(pins), "unpinned dev tool(s) in constraints-dev.txt"


@pytest.mark.parametrize("package", _MUST_BE_PINNED)
def test_pins_are_exact_versions(package: str) -> None:
    # A range would defeat the point: the file exists so two runs install the same thing.
    assert re.fullmatch(r"[0-9][0-9A-Za-z.\-+!]*", _pins()[package])


def test_the_ruff_hook_rev_matches_the_pinned_ruff() -> None:
    """The one pin that lives in two places, because pre-commit cannot read a constraints file."""
    config = _PRE_COMMIT.read_text(encoding="utf-8")
    match = re.search(
        r"repo:\s*https://github\.com/astral-sh/ruff-pre-commit\s*\n(?:\s*#.*\n)*\s*rev:\s*v?([^\s#]+)",
        config,
    )
    assert match is not None, "could not find the ruff hook rev in .pre-commit-config.yaml"
    assert match.group(1) == _pins()["ruff"], (
        f"the ruff pre-commit hook is v{match.group(1)} but constraints-dev.txt pins "
        f"ruff=={_pins()['ruff']}. The hook's formatter and `ruff format .` can now disagree "
        f"about the same file; bump both in one commit."
    )
