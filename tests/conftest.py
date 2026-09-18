"""How much of the machine a test run is allowed to take.

``pyproject.toml`` runs the suite under ``-n auto`` because it is CPU-bound and every test is
independent (see the ``addopts`` comment). ``auto`` means *every* core, so a local ``pytest``
pins the box at 100% for the whole run and nothing else on the machine stays responsive. This
file changes nothing about *what* is tested - only how much of the machine the run holds.

Two dials::

    GTNH_TEST_CPU_FRACTION=0.75  ->  -n auto yields floor(0.75 * cores), floor 1
    GTNH_TEST_NICE=0             ->  keep normal scheduler priority (default: drop below it)

**The core fraction defaults to 1.0**: a run takes the whole machine, as ``-n auto`` always did.
The dial exists to hand cores back on demand, not to withhold them by default. Measured on the
4-core reference box at ``--no-cov``: ``-n 4`` 56s, ``-n 3`` 54s, ``-n 2`` 58s - the last worker
oversubscribes the cores the controller also needs, so dropping to ``0.75`` there costs nothing
and leaves a core for the desktop.

Priority *is* lowered by default, and it is the dial that does the real work: it costs no wall
clock at all on an otherwise-idle machine and still lets the foreground preempt the run.

An explicit ``-n 4`` wins over the fraction: the hook below only runs for ``auto``/``logical``. CI
wants the whole runner and the full property-test budget, so both dials are off when ``CI`` is set
(GitHub Actions sets it).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from gtnh_solver.adapter import adapt_file
from gtnh_solver.ir import InputIR, LayoutResult
from gtnh_solver.solver import solve

_DEFAULT_CPU_FRACTION = 1.0
"""Take every core, which is what ``-n auto`` means. Lower it with ``GTNH_TEST_CPU_FRACTION`` when
you want the machine back; the priority drop below already keeps the foreground responsive."""

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

    An unparseable or out-of-range value is a typo, so it falls back to the default rather than
    being read as some other share.
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


# --------------------------------------------------------------- the shipped example lines

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_SAND = _EXAMPLES / "gtnh-sand.json"
_NITROBENZENE = _EXAMPLES / "gtnh-nitrobenzene.json"


def _solved(path: Path) -> tuple[InputIR, LayoutResult]:
    """One genuine solve of a shipped line: ``adapt_file(path)`` with no physical dataset.

    **The absent dataset is part of the key.** ``adapt_file(path)`` and
    ``adapt_file(path, physical=_load_physical_or_warn())`` are different problems - the second
    gives a known machine its real footprint and hatch slots - so a test that passes one cannot
    be served the other. These fixtures cover the no-dataset form only, because that is what the
    tests they replace were calling; anything wanting the resolved dataset keeps its own solve,
    and docs/TESTING.md explains why the two configurations must stay distinguishable.
    """
    ir = adapt_file(path)
    return ir, solve(ir)


@pytest.fixture(scope="session")
def _sand_session() -> tuple[InputIR, LayoutResult]:
    return _solved(_SAND)


@pytest.fixture(scope="session")
def _nitrobenzene_session() -> tuple[InputIR, LayoutResult]:
    return _solved(_NITROBENZENE)


@pytest.fixture
def solved_sand(_sand_session: tuple[InputIR, LayoutResult]) -> tuple[InputIR, LayoutResult]:
    """The sand line adapted and annealed once per session, handed out as a private deep copy.

    For a test that needs *a* real, valid layout to render, measure or validate. It is still a
    genuine ``solve`` - the fixture runs the real thing - so a consumer asserting on the layout
    is asserting on real solver output. What a consumer may *not* do is assert on the act of
    solving: a determinism test needs two independent solves to compare and must call ``solve``
    itself. ``test_cli`` has cached its own sand solve since the guide tests landed, with the
    note that re-solving a real line per test made it the slowest file in the suite; this is
    that fixture lifted to the whole suite.

    The copy is not paranoia about a specific test: ``InputIR`` and ``LayoutResult`` are
    ``StrictModel``, so they are mutable, and a session-scoped object that one test edits is a
    failure the *next* test reports. A deep copy costs ~0.4ms against a ~570ms solve (~1:1350),
    so the safe thing is also the free thing.
    """
    ir, layout = _sand_session
    return ir.model_copy(deep=True), layout.model_copy(deep=True)


@pytest.fixture
def solved_nitrobenzene(
    _nitrobenzene_session: tuple[InputIR, LayoutResult],
) -> tuple[InputIR, LayoutResult]:
    """The nitrobenzene line, same contract as :func:`solved_sand` - and the one that pays.

    A nitrobenzene solve is ~5.6s against sand's ~0.6s, and it was being run from scratch by
    every module that wanted a realistic layout with actual pipes in it.
    """
    ir, layout = _nitrobenzene_session
    return ir.model_copy(deep=True), layout.model_copy(deep=True)
