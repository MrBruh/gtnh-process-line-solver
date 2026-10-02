"""Tests for ``gtnh-solve --me``: a commodity left to ME (AE2) instead of pipes and cables (#222).

The toggles themselves were always honoured downstream (placement, routing, repair, the validator);
what was missing was any way to turn one on. So these pin the three new pieces: the flag parses
into the ``METoggles`` the adapter stamps on the problem, the run says on stderr that nothing
stands in for the skipped nets yet, and the sand line solves VALID with its items left to ME. With
power left to ME, no power source is synthesized, placed or exported, and the note that blames a
missing amp ceiling for unmeasured intake stays silent (#225).

Flag plumbing runs against the session-cached sand layout (``solved_sand``); only the end-to-end
test solves for real, the way ``test_cli.py`` splits the two.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import gtnh_solver.cli as cli_module
from gtnh_solver.adapter import adapt_file
from gtnh_solver.cli import _me_toggles, _note_me_toggles, build_parser, main
from gtnh_solver.ir import Commodity, InputIR, LayoutResult, LayoutStatus, METoggles
from gtnh_solver.placement import Objective
from gtnh_solver.previewer.textures import TextureManifest
from gtnh_solver.schematic import read_schematic
from gtnh_solver.schematic.core import POWER_SOURCE_STAND_IN
from gtnh_solver.solver import Effort, solve
from gtnh_solver.validator import validate

_ROOT = Path(__file__).resolve().parents[1]
_SAND = str(_ROOT / "examples" / "gtnh-sand.json")
_COMMITTED_MANIFEST = _ROOT / "data" / "textures" / "manifest.json"

#: The line the sand run prints with ``--me items``, verbatim: a builder reads it, so its wording
#: is part of what is pinned, not only its presence.
_ITEMS_NOTE = (
    "note: item nets left to ME (--me) - nothing is routed for them, and no ME interface or "
    "endpoint is placed or drawn yet, so the builder must supply it"
)


@pytest.fixture
def solved_problems(
    monkeypatch: pytest.MonkeyPatch, solved_sand: tuple[InputIR, LayoutResult]
) -> list[InputIR]:
    """Swap the CLI's solver for the cached sand layout, recording each problem it is handed.

    The problem is what ``--me`` has to reach: a flag parsed and then dropped on the way to the
    adapter would still exit 0, but its toggles would not show up here.
    """
    _, layout = solved_sand
    problems: list[InputIR] = []

    def recording_solve(problem: InputIR, **kwargs: object) -> LayoutResult:
        problems.append(problem)
        return layout

    monkeypatch.setattr(cli_module, "solve", recording_solve)
    return problems


@pytest.fixture
def real_solves(monkeypatch: pytest.MonkeyPatch) -> list[tuple[InputIR, LayoutResult]]:
    """Let the CLI solve for real, recording each problem it is handed and the layout it got."""
    solved: list[tuple[InputIR, LayoutResult]] = []

    def real_solve(
        problem: InputIR,
        *,
        seed: int,
        optimize: bool,
        objective: Objective,
        jobs: int,
        effort: Effort | None,
        time_budget: float | None,
        rounds: int | None,
    ) -> LayoutResult:
        layout = solve(
            problem,
            seed=seed,
            optimize=optimize,
            objective=objective,
            jobs=jobs,
            effort=effort,
            time_budget=time_budget,
            rounds=rounds,
        )
        solved.append((problem, layout))
        return layout

    monkeypatch.setattr(cli_module, "solve", real_solve)
    return solved


# ------------------------------------------------------------------ parsing


def test_me_is_off_unless_asked_for() -> None:
    assert build_parser().parse_args([_SAND]).me is None
    assert _me_toggles(None) == METoggles()  # the contract's default: route all three physically


def test_me_takes_one_commodity() -> None:
    assert build_parser().parse_args([_SAND, "--me", "items"]).me == ["items"]


def test_me_repeats_for_several_commodities() -> None:
    args = build_parser().parse_args([_SAND, "--me", "items", "--me", "power", "--me", "items"])
    assert args.me == ["items", "power", "items"]
    # Naming one twice is the same as naming it once.
    assert _me_toggles(args.me) == METoggles(items=True, power=True)


@pytest.mark.parametrize(
    ("word", "commodity"),
    [("items", Commodity.ITEM), ("fluids", Commodity.FLUID), ("power", Commodity.POWER)],
)
def test_each_me_choice_turns_on_its_own_commodity_and_no_other(
    word: str, commodity: Commodity
) -> None:
    toggles = _me_toggles([word])
    assert [c for c in Commodity if toggles.toggled(c)] == [commodity]


def test_an_unknown_commodity_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], solved_problems: list[InputIR]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([_SAND, "--me", "gas"])
    assert exit_info.value.code == 2  # argparse's usage error: a run that could not start
    err = capsys.readouterr().err
    assert "--me" in err
    assert "invalid choice: 'gas'" in err
    assert not solved_problems  # refused at parse time, before any work


# ------------------------------------------------------------------ wiring and the note


def test_the_cli_hands_the_toggles_to_the_problem(solved_problems: list[InputIR]) -> None:
    assert main([_SAND, "--me", "items", "--me", "fluids"]) == 0
    assert solved_problems[-1].me_toggles == METoggles(items=True, fluids=True)


def test_without_me_the_problem_keeps_every_commodity_physical(
    capsys: pytest.CaptureFixture[str], solved_problems: list[InputIR]
) -> None:
    assert main([_SAND]) == 0
    assert solved_problems[-1].me_toggles == METoggles()
    assert "left to ME" not in capsys.readouterr().err  # nothing to say, so nothing said


def test_a_toggle_prints_the_note_once_on_stderr_only(
    capsys: pytest.CaptureFixture[str], solved_problems: list[InputIR]
) -> None:
    # Advisory, like the other notes: the exit code is the layout's, and stdout (the layout's
    # JSON, #203) stays free of it so it still parses when piped.
    assert main([_SAND, "--me", "items"]) == 0
    out, err = capsys.readouterr()
    assert err.splitlines().count(_ITEMS_NOTE) == 1
    assert "left to ME" not in out


def test_the_note_names_every_commodity_left_to_me(capsys: pytest.CaptureFixture[str]) -> None:
    _note_me_toggles(METoggles(items=True, fluids=True, power=True))
    (line,) = capsys.readouterr().err.splitlines()
    assert line.startswith("note: item, fluid, power nets left to ME (--me) - ")


def test_the_note_is_silent_with_no_toggle(capsys: pytest.CaptureFixture[str]) -> None:
    _note_me_toggles(METoggles())
    assert capsys.readouterr().err == ""


# ------------------------------------------------------------------ end to end


def test_sand_with_items_on_me_solves_valid_with_no_item_routing(
    real_solves: list[tuple[InputIR, LayoutResult]], capsys: pytest.CaptureFixture[str]
) -> None:
    # The issue's acceptance run, for real: `gtnh-solve examples/gtnh-sand.json --me items`.
    # Sand is items end to end, so every item net must be left unrouted (no pipe, no auto-output)
    # while the power cable is still laid, and the layout must still be certified.
    assert main([_SAND, "--me", "items"]) == 0
    ((problem, layout),) = real_solves
    assert layout.status is LayoutStatus.VALID
    assert validate(problem, layout).ok  # the independent gate agrees, not only the solver
    item_nets = {n.id for n in problem.nets if n.commodity is Commodity.ITEM}
    assert item_nets  # the line has item nets to skip, so the empties below mean something
    assert [r.commodity for r in layout.routes] == [Commodity.POWER]
    assert not [ac for ac in layout.auto_connections if ac.net_id in item_nets]
    assert _ITEMS_NOTE in capsys.readouterr().err.splitlines()


# ------------------------------------------------------------------ power on ME (#225)


def test_power_on_me_synthesizes_no_power_source() -> None:
    """With power left to ME no cable is laid, so a synthesized source would stand in the build
    connected to nothing. The adapter drops the sources and their nets, and nothing else: every
    powered machine keeps the energy ports that say what it draws."""
    physical = adapt_file(_SAND)
    on_me = adapt_file(_SAND, me_toggles=METoggles(power=True))
    assert [m.id for m in physical.machines if m.is_power_source] == ["power-source:LV"]
    assert [n.id for n in physical.nets if n.commodity is Commodity.POWER] == ["power:LV"]
    assert on_me.machines == [m for m in physical.machines if not m.is_power_source]
    assert on_me.nets == [n for n in physical.nets if n.commodity is not Commodity.POWER]
    powered = [m for m in on_me.machines if m.eut > 0]
    assert powered
    assert all(m.power_input_ports for m in powered)


def test_power_on_me_still_cross_checks_the_plans_resolved_power(
    recwarn: pytest.WarningsRecorder,
) -> None:
    # Sand's export states its own power total. The cross-check sums the synthesized nets' draw,
    # so it has to run before those nets are dropped: after, it would read 0 EU/t and report the
    # export as inconsistent.
    adapt_file(_SAND, me_toggles=METoggles(power=True))
    assert not [w for w in recwarn if "resolved power total" in str(w.message)]


def test_sand_with_power_on_me_places_and_exports_no_power_source(
    real_solves: list[tuple[InputIR, LayoutResult]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # The issue's acceptance run, for real: `gtnh-solve examples/gtnh-sand.json --me power
    # --schematic x.schematic` places no power source, so none is exported, and the note that
    # blamed a missing amp ceiling for intake no cable reaches says nothing.
    schematic = tmp_path / "x.schematic"
    assert main([_SAND, "--me", "power", "--schematic", str(schematic)]) == 0
    ((problem, layout),) = real_solves
    assert layout.status is LayoutStatus.VALID
    assert validate(problem, layout).ok
    assert not any(m.is_power_source for m in problem.machines)
    assert {p.machine_id for p in layout.placements} == {m.id for m in problem.machines}
    assert not [r for r in layout.routes if r.commodity is Commodity.POWER]
    assert "power intake unmeasured" not in capsys.readouterr().err

    stand_in = TextureManifest.load(_COMMITTED_MANIFEST).mte_block(POWER_SOURCE_STAND_IN)
    assert stand_in is not None
    mids = {t.mid for t in read_schematic(schematic).tile_entities}
    assert mids  # the hammers and chests are still exported
    assert stand_in[1] not in mids
