"""Session-wide test setup: what the suite resolves, and how much of the machine it takes.

Five things live here. **The dataset pin** (``_pinned_dataset_root``) fixes the one input that
otherwise varies per machine, so ``pytest`` answers the same question everywhere. **The solve
effort** (``_minimal_solves``) makes every solve a ``minimal`` one, since the suite checks that the
code is correct and not how good its layouts are; tests marked ``full_solve`` judge that, and only
run with ``--full-solve``. **The shipped example solves** (``solved_sand``, ``solved_nitrobenzene``)
are run once per session and handed out as private copies. **The two resource dials** below bound
how much of the box a run holds; they change nothing about *what* is tested. **The hypothesis
profile** (``HYPOTHESIS_PROFILE``, at the bottom) keeps a contended box from failing a property test
on wall clock alone.

``pyproject.toml`` runs the suite under ``-n auto`` because it is CPU-bound and every test is
independent (see the ``addopts`` comment). ``auto`` means *every* core, so a local ``pytest`` pins
the box at 100% for the whole run and nothing else on the machine stays responsive. Hence::

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
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import settings

from gtnh_solver.adapter import adapt_file
from gtnh_solver.dataset import roots
from gtnh_solver.ir import InputIR, LayoutResult
from gtnh_solver.solver import core as solver_core
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
        except OSError, AttributeError:
            return False
    try:
        os.nice(_POSIX_NICE_INCREMENT)
    except OSError, AttributeError:
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


# --------------------------------------------------------------- the dataset the suite resolves

_COMMITTED_DATA = Path(__file__).resolve().parents[1] / "data"

#: The sub-paths ``resolve_dataset_path`` falls back to, and the whole of what a fresh clone (and
#: therefore every CI job) carries: the two multiblock fixtures and the example-scoped texture
#: manifest. Everything else under ``data/`` is a gitignored ``data/<version>/`` dump.
_COMMITTED_SUBPATHS = ("multiblocks", "textures/manifest.json")


@pytest.fixture(scope="session", autouse=True)
def _pinned_dataset_root(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Resolve every unpinned dataset lookup to the committed data, on every machine (#182).

    ``resolve_dataset_path`` answers with the newest local ``data/<version>/`` that provides the
    wanted sub-path, and only falls back to the committed fixtures when none does. That preference
    is right for the CLI - "the version you most recently generated wins" is the documented
    convenience, and ``--dataset-version`` is its escape hatch - but it makes ``pytest`` ask a
    different question on every machine. Measured on the same tree: 954 passed / 7 skipped with no
    local dump, 955 passed / 6 skipped with a 2.9 dump staged, and twice the wall clock. CI is
    always a clean clone, so it only ever sees the first answer, which is how #176 (a break that
    appears at the newer pack) stayed invisible for weeks.

    The pin is a copy of the committed sub-paths in a session temp dir, with
    :data:`~gtnh_solver.dataset.roots.DEFAULT_DATA` pointed at it. A *copy* rather than the repo's
    own ``data/``, because that directory is exactly where local dumps live; a root holding only
    the committed data has no version folder to prefer, so ``list_versions`` is empty and every
    resolution lands on the fallback by its own logic rather than by a stubbed function. Patching
    the one module attribute covers every caller - ``cli``, the previewer, the schematic exporter,
    ``load_physical_dataset`` - because they all read it through ``roots`` at call time, including
    the ones that imported ``list_versions`` by name.

    **A test that needs another dataset states it**, and several do: pass ``data_dir`` (most dataset
    tests), stage a dump under ``tmp_path`` and point ``DEFAULT_DATA`` at that (``test_schematic``),
    or monkeypatch ``list_versions`` where the version *list* is what is under test (``test_cli``).
    ``test_dataset_roots`` passes ``data_dir`` throughout, so the real mtime preference keeps its
    direct coverage. What no test may do is inherit whatever happens to sit on the machine.
    """
    root = tmp_path_factory.mktemp("committed-dataset")
    for rel in _COMMITTED_SUBPATHS:
        source = _COMMITTED_DATA / rel
        target = root / rel
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(roots, "DEFAULT_DATA", root)
        yield root


# --------------------------------------------------------------- how hard the suite solves

_FULL_SOLVE = "full_solve"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--full-solve",
        action="store_true",
        help="also run the full_solve tests, which judge layout quality on full-effort solves",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the ``full_solve`` tests unless the run asked for them."""
    if config.getoption("--full-solve"):
        return
    skip = pytest.mark.skip(reason="judges layout quality on a full solve; run with --full-solve")
    for item in items:
        if item.get_closest_marker(_FULL_SOLVE) is not None:
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def _minimal_solves() -> Iterator[None]:
    """Make every solve in the session ``minimal``: one short attempt instead of eight long ones.

    The suite's job is correctness: every stage runs, and whatever comes out is valid or explicitly
    infeasible. A ``minimal`` solve does all of that on small budgets (``solver.core``), while a
    full one spends most of its time searching for a *better* layout, which is a question about
    quality. Quality is what the ``full_solve`` tests ask, and only when benchmarking or chasing a
    drop in it; a small drop is expected as more of GT's rules land, so it is not a failure of the
    everyday run. Session-scoped, like the dataset pin, so the session fixtures below solve
    ``minimal`` too; a test that needs the multi-start itself passes ``effort="full"``.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(solver_core, "DEFAULT_EFFORT", "minimal")
        yield


@pytest.fixture(autouse=True)
def _full_solves_when_marked(request: pytest.FixtureRequest) -> Iterator[None]:
    """Restore the full default for a ``full_solve`` test, for that test alone.

    It has to solve inside the test: a session fixture was built under the minimal default.
    """
    if request.node.get_closest_marker(_FULL_SOLVE) is None:
        yield
        return
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(solver_core, "DEFAULT_EFFORT", "full")
        yield


# --------------------------------------------------------------- the shipped example lines

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_SAND = _EXAMPLES / "gtnh-sand.json"
_NITROBENZENE = _EXAMPLES / "gtnh-nitrobenzene.json"


def _solved(path: Path, seed: int = 0) -> tuple[InputIR, LayoutResult]:
    """One genuine solve of a shipped line: ``adapt_file(path)`` with no physical dataset.

    **The absent dataset is part of the key.** ``adapt_file(path)`` and
    ``adapt_file(path, physical=_load_physical_or_warn())`` are different problems - the second
    gives a known machine its real footprint and hatch slots - so a test that passes one cannot
    be served the other. These fixtures cover the no-dataset form only, because that is what the
    tests they replace were calling; anything wanting the resolved dataset keeps its own solve,
    and docs/TESTING.md explains why the two configurations must stay distinguishable.
    """
    ir = adapt_file(path)
    return ir, solve(ir, seed=seed)


@pytest.fixture(scope="session")
def _sand_session() -> tuple[InputIR, LayoutResult]:
    return _solved(_SAND)


@pytest.fixture(scope="session")
def _nitrobenzene_session() -> tuple[InputIR, LayoutResult]:
    # Seed 1, because what this fixture is for is the pipes, and which seeds lay them moves with
    # the search: without the dataset every machine here is a single block, so the one-cell nudge
    # applies, and seed 0's partial layout then keeps its cable and loses every pipe. That holds for
    # the session's minimal solve as it did for the full one.
    return _solved(_NITROBENZENE, seed=1)


@pytest.fixture
def solved_sand(_sand_session: tuple[InputIR, LayoutResult]) -> tuple[InputIR, LayoutResult]:
    """The sand line adapted and annealed once per session, handed out as a private deep copy.

    For a test that needs *a* real, valid layout to render, measure or validate. It is still a
    genuine ``solve`` - the fixture runs the real thing - so a consumer asserting on the layout
    is asserting on real solver output. What a consumer may *not* do is assert on the act of
    solving: a determinism test needs two independent solves to compare and must call ``solve``
    itself. ``test_cli`` has long cached its own sand solve, with the note that re-solving a
    real line per test made it the slowest file in the suite; this is that fixture lifted to the
    whole suite.

    The copy is not paranoia about a specific test: ``InputIR`` and ``LayoutResult`` are
    ``StrictModel``, so they are mutable, and a session-scoped object that one test edits is a
    failure the *next* test reports. A deep copy costs ~0.4ms against a ~60ms minimal solve, so
    the safe thing is also the free thing.

    Like every solve in the suite it is ``minimal`` (``_minimal_solves``), so a ``full_solve``
    test cannot take it: it has to solve for itself.
    """
    ir, layout = _sand_session
    return ir.model_copy(deep=True), layout.model_copy(deep=True)


@pytest.fixture
def solved_nitrobenzene(
    _nitrobenzene_session: tuple[InputIR, LayoutResult],
) -> tuple[InputIR, LayoutResult]:
    """The nitrobenzene line, same contract as :func:`solved_sand` - and the one that pays.

    Even minimal, a nitrobenzene solve is ~1.7s of CPU against sand's ~0.06s (a full one is ~18s),
    and it was being run from scratch by every module that wanted a realistic layout with actual
    pipes in it.
    """
    ir, layout = _nitrobenzene_session
    return ir.model_copy(deep=True), layout.model_copy(deep=True)


# --------------------------------------------------------------- the hypothesis profile

HYPOTHESIS_PROFILE = "gtnh"
"""The settings profile every property test runs under: Hypothesis's own pick, minus the deadline.

Hypothesis fails an example that runs past 200 ms, with ``DeadlineExceeded``, or with ``Flaky``
when the replay comes in under it. That budget is wall clock, and wall clock is exactly what
``-n auto`` on a busy machine takes away: an example that slows down because every core is taken
measures the box, not the code (#216). Three solver properties had opted out one by one; the other
24 ``@given`` tests kept the default.

**Built on whichever built-in profile Hypothesis loaded, not on ``default``.** With ``CI`` set,
Hypothesis loads its ``ci`` profile, which already has no deadline and also derandomizes, drops the
example database and prints reproduction blobs. A profile built on ``default`` would silently take
those away from CI; built this way it changes nothing there, and locally only the deadline.

It sets no ``max_examples``, so the ``property_examples()`` budgets are untouched: whatever a test's
own ``@settings`` names wins, and the profile only fills in what the test leaves unset. It loads at
import because a ``settings`` object copies the active profile when it is created, which is when
its decorator runs, and pytest imports this module before any test module.
"""

settings.register_profile(
    HYPOTHESIS_PROFILE,
    settings.get_profile(settings.get_current_profile_name()),
    deadline=None,
)
settings.load_profile(HYPOTHESIS_PROFILE)
