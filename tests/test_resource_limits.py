"""The run's resource dials in ``tests/conftest.py``.

These hold a promise about the *machine*, not the code under test: a local ``pytest`` must leave
the box usable. That promise is invisible to every other test here and easy to break by accident
(the Windows priority call fails silently when its ``argtypes`` are dropped - that bug is exactly
what ``test_lower_priority_actually_lowers_it`` would have caught), so it gets its own file. The
hypothesis profile is the other side of the same promise: a busy box must not fail the suite either.
"""

from __future__ import annotations

import os
import sys

import pytest
from hypothesis import settings

from gtnh_solver.adapter import adapt_file
from gtnh_solver.ir import CellCoord, InputIR, LayoutResult, LayoutStatus

from ._helpers import property_examples
from .conftest import (
    _DEFAULT_CPU_FRACTION,
    _SAND,
    HYPOTHESIS_PROFILE,
    _cpu_fraction,
    _env_flag,
    _lower_priority,
    pytest_xdist_auto_num_workers,
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run with none of the dials set, so a value on the developer's machine cannot leak in."""
    for name in ("CI", "GTNH_TEST_CPU_FRACTION", "GTNH_TEST_NICE", "PYTEST_XDIST_AUTO_NUM_WORKERS"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.5", 0.5),
        ("1", 1.0),
        ("0.01", 0.01),
        # Every rejected form falls back to the default rather than to 1.0: a typo must not be
        # read as "take the whole machine", which is the failure this file exists to prevent.
        ("bogus", _DEFAULT_CPU_FRACTION),
        ("", _DEFAULT_CPU_FRACTION),
        ("0", _DEFAULT_CPU_FRACTION),
        ("-0.5", _DEFAULT_CPU_FRACTION),
        ("2", _DEFAULT_CPU_FRACTION),
        ("nan", _DEFAULT_CPU_FRACTION),
        ("0.75", 0.75),
    ],
)
def test_cpu_fraction_parses_or_falls_back(
    monkeypatch: pytest.MonkeyPatch, clean_env: None, raw: str, expected: float
) -> None:
    monkeypatch.setenv("GTNH_TEST_CPU_FRACTION", raw)
    assert _cpu_fraction() == pytest.approx(expected)


def test_cpu_fraction_unset_is_the_default(clean_env: None) -> None:
    assert _cpu_fraction() == pytest.approx(_DEFAULT_CPU_FRACTION)


def test_auto_workers_takes_every_core_by_default(
    monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    """The default is the whole machine, matching plain ``-n auto``."""
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    assert pytest_xdist_auto_num_workers(None) == 4  # type: ignore[arg-type]


def test_auto_workers_hands_cores_back_on_request(
    monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    monkeypatch.setenv("GTNH_TEST_CPU_FRACTION", "0.75")
    assert pytest_xdist_auto_num_workers(None) == 3  # type: ignore[arg-type]
    monkeypatch.setenv("GTNH_TEST_CPU_FRACTION", "0.5")
    assert pytest_xdist_auto_num_workers(None) == 2  # type: ignore[arg-type]


def test_auto_workers_never_returns_zero(monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
    """A single-core box, or a tiny fraction, must still get one worker - 0 would run nothing."""
    monkeypatch.setattr(os, "cpu_count", lambda: 1)
    assert pytest_xdist_auto_num_workers(None) == 1  # type: ignore[arg-type]
    monkeypatch.setenv("GTNH_TEST_CPU_FRACTION", "0.01")  # 8 cores -> 0 without the floor
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    assert pytest_xdist_auto_num_workers(None) == 1  # type: ignore[arg-type]


def test_auto_workers_handles_an_unknown_core_count(
    monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert pytest_xdist_auto_num_workers(None) == 1  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["CI", "PYTEST_XDIST_AUTO_NUM_WORKERS"])
def test_auto_workers_defers_to_xdist(
    monkeypatch: pytest.MonkeyPatch, clean_env: None, name: str
) -> None:
    """CI wants the whole runner, and an explicit env override is the user saying so.

    ``None`` is the signal that declines the hook, so xdist's own implementation runs.
    """
    monkeypatch.setenv(name, "1")
    assert pytest_xdist_auto_num_workers(None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("", False),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("anything", True),
        (" 0 ", False),
    ],
)
def test_env_flag_reads_the_usual_spellings_of_off(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    monkeypatch.setenv("GTNH_TEST_PROBE", raw)
    assert _env_flag("GTNH_TEST_PROBE", default=True) is expected
    assert _env_flag("GTNH_TEST_PROBE", default=False) is expected


def test_env_flag_unset_takes_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GTNH_TEST_PROBE", raising=False)
    assert _env_flag("GTNH_TEST_PROBE", default=True) is True
    assert _env_flag("GTNH_TEST_PROBE", default=False) is False


def test_lower_priority_actually_lowers_it() -> None:
    """The call must *work*, not merely not raise.

    ``SetPriorityClass`` returns 0 instead of raising when it is handed a truncated handle, so an
    assertion on "no exception" would pass against a no-op. This asserts the observable priority,
    which is the thing the user asked for.
    """
    assert _lower_priority() is True
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetPriorityClass.restype = wintypes.DWORD
        kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
        assert kernel32.GetPriorityClass(kernel32.GetCurrentProcess()) == 0x00004000
    else:
        assert os.nice(0) > 0


# ------------------------------------------------------------- the hypothesis example budget


@pytest.mark.parametrize("full", [200, 50, 300])
def test_property_examples_is_full_in_ci(monkeypatch: pytest.MonkeyPatch, full: int) -> None:
    """CI is the run that has to prove the invariant, so it never gets the reduced budget."""
    monkeypatch.setenv("CI", "true")
    assert property_examples(full) == full


def test_property_examples_is_reduced_locally(
    monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    assert property_examples(200) == 50
    assert property_examples(300) == 75


def test_property_examples_keeps_the_ratio_between_budgets(clean_env: None) -> None:
    """The three budgets are not interchangeable, so scaling must not flatten them."""
    assert property_examples(300) > property_examples(200) > property_examples(50)


def test_property_examples_never_returns_a_token_few(clean_env: None) -> None:
    """A floor, because 2 examples would pass instantly and prove nothing."""
    assert property_examples(1) == 10
    assert property_examples(0) == 10


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1.0", 200), ("0.5", 100), ("bogus", 50), ("0", 50), ("-1", 50), ("3", 50)],
)
def test_property_examples_honors_the_override_or_falls_back(
    monkeypatch: pytest.MonkeyPatch, clean_env: None, raw: str, expected: int
) -> None:
    monkeypatch.setenv("GTNH_TEST_HYPOTHESIS_FRACTION", raw)
    assert property_examples(200) == expected


# ------------------------------------------------------------- the hypothesis profile


def test_property_tests_run_without_a_deadline() -> None:
    """A contended machine must not fail a property test on wall clock alone (#216).

    Checked twice. The registered profile, because under ``CI`` Hypothesis's own ``ci`` profile is
    deadline-free too, so only this catches the profile going missing there. The *active* settings,
    because a profile that is registered but never loaded, or loaded over, changes nothing. Not the
    profile's name: ``--hypothesis-verbosity`` legitimately loads a renamed child of it.
    """
    assert settings.get_profile(HYPOTHESIS_PROFILE).deadline is None
    assert settings().deadline is None
    # A test's own @settings keeps what it names and inherits the rest from the profile, so the
    # property_examples() budgets survive and the deadline still goes.
    per_test = settings(max_examples=property_examples(300))
    assert per_test.max_examples == property_examples(300)
    assert per_test.deadline is None


# ------------------------------------------------------- the shared shipped-line fixtures


def test_solved_sand_is_a_real_valid_layout(solved_sand: tuple[InputIR, LayoutResult]) -> None:
    """A cached stand-in for a solve must itself be a genuine solve, or it proves nothing."""
    ir, layout = solved_sand
    assert layout.status is LayoutStatus.VALID
    assert layout.placements
    assert len(layout.placements) == len(ir.machines)


def test_solved_sand_hands_out_independent_copies(
    solved_sand: tuple[InputIR, LayoutResult], request: pytest.FixtureRequest
) -> None:
    """The isolation that makes session caching safe: edit your copy, the session's is untouched.

    Without this the fixture is a foot-gun - ``LayoutResult`` is mutable, so one test reordering a
    layout in place would surface as a failure in whichever test happened to run next.

    Reaching for the session-scoped original through ``getfixturevalue`` is deliberate: it is
    private precisely so tests take the copy, and this is the one test that must see both.
    """
    ir, layout = solved_sand
    session_ir, session_layout = request.getfixturevalue("_sand_session")
    assert layout is not session_layout
    assert ir is not session_ir
    assert layout.placements[0] is not session_layout.placements[0]

    before = session_layout.placements[0].cell
    layout.placements[0].cell = CellCoord(x=99, y=99, z=99)
    ir.machines[0].type = "mutated"
    assert session_layout.placements[0].cell == before
    assert session_ir.machines[0].type != "mutated"


def test_solved_nitrobenzene_carries_the_pipes_that_make_it_worth_caching(
    solved_nitrobenzene: tuple[InputIR, LayoutResult],
) -> None:
    """Nitrobenzene is the expensive fixture (~5.6s) because it is the line that lays real pipes.

    Its status is deliberately not asserted: against the committed fixtures it solves
    PARTIAL_INVALID, and pinning that here would fail for reasons unrelated to caching
    (docs/TESTING.md, "CI sees a smaller dataset than you do").
    """
    ir, layout = solved_nitrobenzene
    assert len(ir.machines) > len(adapt_file(_SAND).machines)
    assert layout.routes
