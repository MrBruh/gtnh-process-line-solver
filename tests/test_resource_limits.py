"""The run's resource dials in ``tests/conftest.py``.

These hold a promise about the *machine*, not the code under test: a local ``pytest`` must leave
the box usable. That promise is invisible to every other test here and easy to break by accident
(the Windows priority call fails silently when its ``argtypes`` are dropped - that bug is exactly
what ``test_lower_priority_actually_lowers_it`` would have caught), so it gets its own file.
"""

from __future__ import annotations

import os
import sys

import pytest

from .conftest import (
    _DEFAULT_CPU_FRACTION,
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
    ],
)
def test_cpu_fraction_parses_or_falls_back(
    monkeypatch: pytest.MonkeyPatch, clean_env: None, raw: str, expected: float
) -> None:
    monkeypatch.setenv("GTNH_TEST_CPU_FRACTION", raw)
    assert _cpu_fraction() == pytest.approx(expected)


def test_cpu_fraction_unset_is_the_default(clean_env: None) -> None:
    assert _cpu_fraction() == pytest.approx(_DEFAULT_CPU_FRACTION)


def test_auto_workers_leaves_headroom(monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    assert pytest_xdist_auto_num_workers(None) == 3  # type: ignore[arg-type]
    monkeypatch.setenv("GTNH_TEST_CPU_FRACTION", "0.5")
    assert pytest_xdist_auto_num_workers(None) == 2  # type: ignore[arg-type]


def test_auto_workers_never_returns_zero(monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
    """A single-core box, or a tiny fraction, must still get one worker - 0 would run nothing."""
    monkeypatch.setattr(os, "cpu_count", lambda: 1)
    assert pytest_xdist_auto_num_workers(None) == 1  # type: ignore[arg-type]
    monkeypatch.setenv("GTNH_TEST_CPU_FRACTION", "0.01")
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
