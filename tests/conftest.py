"""How much of the machine a test run is allowed to take.

``pyproject.toml`` runs the suite under ``-n auto`` because it is CPU-bound and every test is
independent (see the ``addopts`` comment). ``auto`` means *every* core, so a local ``pytest``
pins the box at 100% for the whole run and nothing else on the machine stays responsive. This
file changes nothing about *what* is tested - only how much of the machine the run holds.

Two dials, because worker count alone is not enough: three busy workers out of four cores still
leave the desktop fighting the run for the fourth::

    GTNH_TEST_CPU_FRACTION=0.8   ->  -n auto yields floor(0.8 * cores), floor 1
    GTNH_TEST_NICE=0             ->  keep normal scheduler priority (default: drop below it)

Measured on the 4-core reference box, suite at ``--no-cov``: ``-n 4`` 56s, ``-n 3`` 54s, ``-n 2``
58s. The fourth worker buys nothing - it oversubscribes the cores the controller also needs - so
the cap costs no wall clock and hands back a core.

An explicit ``-n 4`` still wins: the hook below only runs for ``auto``/``logical``. CI wants the
whole runner, so both dials are off when ``CI`` is set (GitHub Actions sets it).
"""

from __future__ import annotations

import os
import sys

import pytest

_DEFAULT_CPU_FRACTION = 0.8
"""Leave a core's worth of headroom. See the module docstring for the measurements behind it."""

_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
"""Windows ``SetPriorityClass`` value. Below ``NORMAL`` (0x20), above ``IDLE`` - the run keeps
making progress on an idle machine but yields to anything in the foreground."""

_POSIX_NICE_INCREMENT = 5


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def _in_ci() -> bool:
    return _env_flag("CI", default=False)


def _cpu_fraction() -> float:
    """The share of cores ``-n auto`` may use, clamped to ``(0, 1]``.

    An unparseable or out-of-range value is a typo, not an instruction to take the whole box, so
    it falls back to the default rather than to 1.0.
    """
    raw = os.environ.get("GTNH_TEST_CPU_FRACTION")
    if raw is None:
        return _DEFAULT_CPU_FRACTION
    try:
        fraction = float(raw)
    except ValueError:
        return _DEFAULT_CPU_FRACTION
    if not 0.0 < fraction <= 1.0:
        return _DEFAULT_CPU_FRACTION
    return fraction


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    """Trim ``-n auto`` to ``GTNH_TEST_CPU_FRACTION`` of the cores.

    Returning ``None`` declines the hook, so xdist's own implementation (and the
    ``PYTEST_XDIST_AUTO_NUM_WORKERS`` escape hatch it honors first) keeps working untouched.
    """
    if _in_ci() or os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"):
        return None
    cores = os.cpu_count() or 1
    return max(1, int(cores * _cpu_fraction()))


def _lower_priority() -> bool:
    """Drop this process below the foreground. True if the priority actually changed.

    Done per-process rather than once in the controller: xdist workers are separate processes and
    this conftest is imported by each of them, so every worker de-prioritizes itself without
    relying on Windows priority-class inheritance through ``execnet``'s popen.

    ``psutil`` would do this in one line, but it is not a dependency here and nothing else in the
    suite needs it - ``ctypes`` plus ``os.nice`` costs a few lines and no install.

    **The ``argtypes`` below are load-bearing.** ``GetCurrentProcess`` returns the pseudo-handle
    ``(HANDLE)-1``; left untyped, ctypes marshals it as a 32-bit ``int`` and ``SetPriorityClass``
    fails and returns 0 - silently, since it does not raise. That is why this returns a bool and
    the caller reports it, rather than assuming the call worked.
    """
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as wintypes

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.GetCurrentProcess.argtypes = []
            kernel32.SetPriorityClass.restype = wintypes.BOOL
            kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            handle = kernel32.GetCurrentProcess()
            return bool(kernel32.SetPriorityClass(handle, _BELOW_NORMAL_PRIORITY_CLASS))
        except (OSError, AttributeError):
            return False
    try:
        os.nice(_POSIX_NICE_INCREMENT)
    except (OSError, AttributeError):
        return False
    return True


def pytest_configure(config: pytest.Config) -> None:
    if _in_ci() or not _env_flag("GTNH_TEST_NICE", default=True):
        return
    if not _lower_priority():
        # Not fatal - an un-niced run is still a correct run - but say so, because the whole point
        # of this file is that the machine stays usable, and silence would look like success.
        config.issue_config_time_warning(
            pytest.PytestConfigWarning(
                "could not lower test-process priority; the run will compete with the foreground"
            ),
            stacklevel=2,
        )
