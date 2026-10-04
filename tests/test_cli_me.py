"""Tests for choosing ME (AE2) from the command line: ``--list-nets``, ``--me-plan`` and ``--me``.

ME is chosen per net in two runs (#332): ``--list-nets`` prints the plan's NetList and solves
nothing, and ``--me-plan FILE`` reads back the user's MEPlan. ``--me items`` / ``--me fluids`` is
shorthand for one attached network carrying every net of that kind (#222), and ``--me power``
leaves the EU supply to the builder, with no power source synthesized, placed or exported (#225).
These pin how each reaches the problem, the exit codes a script keys on, the stderr note that says
nothing stands in for a net on ME yet, and the sand line solving VALID with its items left to ME.

Flag plumbing runs against the session-cached sand layout (``solved_sand``); only the end-to-end
tests solve for real, the way ``test_cli.py`` splits the two.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import gtnh_solver.cli as cli_module
from gtnh_solver.adapter import adapt_file, list_nets, load_plan, plan_digest, to_input_ir
from gtnh_solver.cli import _me_commodities, _note_me, _note_me_networks, build_parser, main
from gtnh_solver.ir import (
    AEColor,
    Commodity,
    Facing,
    InputIR,
    LayoutResult,
    LayoutStatus,
    Machine,
    MEDeviceKind,
    MEMode,
    MENetworkSpec,
    MEPlan,
    MERole,
    NetList,
)
from gtnh_solver.placement import Objective
from gtnh_solver.previewer.textures import TextureManifest
from gtnh_solver.schematic import read_schematic
from gtnh_solver.schematic.core import POWER_SOURCE_STAND_IN
from gtnh_solver.solver import Effort, solve
from gtnh_solver.validator import validate
from tests._helpers import on_me
from tests._me_fixtures import SUB, comb
from tests._me_fixtures import endpoint as me_endpoint

_ROOT = Path(__file__).resolve().parents[1]
_SAND = str(_ROOT / "examples" / "gtnh-sand.json")
_COMMITTED_MANIFEST = _ROOT / "data" / "textures" / "manifest.json"
#: Every shipped gtnh-factory-flow plan, which --list-nets must list.
_EXAMPLES = sorted(str(p) for p in (_ROOT / "examples").glob("*.json"))

#: The line the sand run prints with ``--me items``, verbatim: a builder reads it, so its wording
#: is part of what is pinned, not only its presence. Sand's stone comes from the main network and
#: its sand goes back there, through six devices (#335).
_ITEMS_NOTE = (
    "note: ME network main (attached): 6 device(s) on 6 channel(s) of your main network; "
    "stock Stone (minecraft:stone) 0.1 items/t; it stores Sand (minecraft:sand) 0.1 items/t"
)


@pytest.fixture
def solved_problems(
    monkeypatch: pytest.MonkeyPatch, solved_sand: tuple[InputIR, LayoutResult]
) -> list[InputIR]:
    """Swap the CLI's solver for the cached sand layout, recording each problem it is handed.

    The problem is what the ME flags have to reach: a flag parsed and then dropped on the way to
    the adapter would still exit 0, but its choice would not show up here.
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


def _on_me(problem: InputIR) -> dict[str, str | None]:
    """``net id -> the ME network it rides``, for every net that rides one."""
    return {n.id: n.me_network for n in problem.nets if n.me_network is not None}


def _write_plan(tmp_path: Path, me_plan: MEPlan) -> str:
    path = tmp_path / "me-plan.json"
    path.write_text(me_plan.model_dump_json(), encoding="utf-8")
    return str(path)


def _sand_listed() -> NetList:
    """The sand line's NetList against the dataset the CLI itself resolves for it, which is what an
    MEPlan the CLI accepts has to have been made against."""
    plan = load_plan(_SAND)
    physical = cli_module._load_physical_or_warn(cli_module._dataset_version_for(plan, None))
    return list_nets(plan, physical=physical)


def _sand_plan(nets: dict[str, str], *networks: MENetworkSpec) -> MEPlan:
    """An MEPlan for the sand line, made against the NetList ``--list-nets`` prints for it."""
    listed = _sand_listed()
    return MEPlan(
        plan_digest=listed.plan_digest,
        dataset_version=listed.dataset_version,
        networks=list(networks),
        nets=nets,
    )


# ------------------------------------------------------------------ parsing


def test_me_is_off_unless_asked_for() -> None:
    args = build_parser().parse_args([_SAND])
    assert (args.me, args.me_plan, args.list_nets) == (None, None, False)
    assert _me_commodities(None) == (frozenset(), False)


def test_me_repeats_for_several_commodities() -> None:
    args = build_parser().parse_args([_SAND, "--me", "items", "--me", "power", "--me", "items"])
    assert args.me == ["items", "power", "items"]
    # Naming one twice is the same as naming it once; power is the builder's, not a network's.
    assert _me_commodities(args.me) == (frozenset({Commodity.ITEM}), True)


@pytest.mark.parametrize(
    ("word", "commodities", "power"),
    [
        ("items", {Commodity.ITEM}, False),
        ("fluids", {Commodity.FLUID}, False),
        ("power", set(), True),
    ],
)
def test_each_me_choice_names_its_own_commodity(
    word: str, commodities: set[Commodity], power: bool
) -> None:
    assert _me_commodities([word]) == (frozenset(commodities), power)


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


# ------------------------------------------------------------------ --list-nets


def test_list_nets_prints_the_net_list_and_solves_nothing(
    capsys: pytest.CaptureFixture[str], solved_problems: list[InputIR]
) -> None:
    assert main([_SAND, "--list-nets"]) == 0
    out = capsys.readouterr().out
    listed = NetList.model_validate_json(out)
    assert not solved_problems
    # Sand: stone in, two internal hops, sand out; the power net is never listed.
    assert [n.kind.value for n in listed.nets] == [
        "internal",
        "internal",
        "boundary_input",
        "boundary_output",
    ]
    assert all(n.commodity is Commodity.ITEM for n in listed.nets)
    assert listed.plan_digest == plan_digest(load_plan(_SAND))
    assert listed.line_tier == "LV"
    # A boundary storage is not an end: riding ME replaces it.
    stone = listed.nets[2]
    assert stone.producers == ()
    assert [e.suggested for e in stone.consumers] == ["ME Export Bus"]


def test_list_nets_ids_are_the_problems_own(capsys: pytest.CaptureFixture[str]) -> None:
    # The ids a user picks by are the ones the adapted problem carries, so an MEPlan reaches them.
    assert main([_SAND, "--list-nets"]) == 0
    listed = NetList.model_validate_json(capsys.readouterr().out)
    problem = adapt_file(_SAND)
    assert {n.id for n in listed.nets} == {
        n.id for n in problem.nets if n.commodity is not Commodity.POWER
    }


def test_list_nets_is_deterministic(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([_SAND, "--list-nets"]) == 0
    first = capsys.readouterr().out
    assert main([_SAND, "--list-nets"]) == 0
    assert capsys.readouterr().out == first


@pytest.mark.parametrize("example", _EXAMPLES, ids=lambda p: Path(p).name)
def test_list_nets_lists_every_shipped_example(
    example: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([example, "--list-nets"]) == 0
    listed = NetList.model_validate_json(capsys.readouterr().out)
    assert listed.nets
    assert all(n.commodity is not Commodity.POWER for n in listed.nets)


def test_list_nets_on_a_plan_that_will_not_load_is_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert main([str(broken), "--list-nets"]) == 2
    assert capsys.readouterr().out == ""


# ------------------------------------------------------------------ --me-plan


def test_an_me_plan_puts_its_nets_on_its_networks(
    tmp_path: Path, solved_problems: list[InputIR]
) -> None:
    listed = _sand_listed()
    stone, sand = listed.nets[2].id, listed.nets[3].id
    me_plan = _sand_plan(
        {stone: "main", sand: "line"},
        MENetworkSpec(id="main", mode=MEMode.ATTACHED),
        MENetworkSpec(id="line", mode=MEMode.SUBNET, colour=AEColor.ORANGE),
    )
    assert main([_SAND, "--me-plan", _write_plan(tmp_path, me_plan)]) == 0
    problem = solved_problems[-1]
    assert _on_me(problem) == {stone: "main", sand: "line"}
    assert [n.id for n in problem.me.networks] == ["main", "line"]
    assert problem.me.colour("line") is AEColor.ORANGE


def test_the_shorthand_is_the_plan_it_stands_for() -> None:
    # `--me items` is one attached network "main" carrying every item net: the same problem an
    # MEPlan saying so gives.
    plan = load_plan(_SAND)
    listed = list_nets(plan)
    spelled_out = MEPlan(
        plan_digest=listed.plan_digest,
        dataset_version=listed.dataset_version,
        networks=[MENetworkSpec(id="main", mode=MEMode.ATTACHED)],
        nets={n.id: "main" for n in listed.nets if n.commodity is Commodity.ITEM},
    )
    assert to_input_ir(plan, me_commodities={Commodity.ITEM}) == to_input_ir(
        plan, me_plan=spelled_out
    )


def test_an_me_plan_and_the_shorthand_together_are_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], solved_problems: list[InputIR]
) -> None:
    me_plan = _sand_plan({}, MENetworkSpec(id="main", mode=MEMode.ATTACHED))
    assert main([_SAND, "--me-plan", _write_plan(tmp_path, me_plan), "--me", "items"]) == 2
    assert "give one of them" in capsys.readouterr().err
    assert not solved_problems


def test_power_combines_with_an_me_plan(tmp_path: Path, solved_problems: list[InputIR]) -> None:
    me_plan = _sand_plan({}, MENetworkSpec(id="main", mode=MEMode.ATTACHED))
    assert main([_SAND, "--me-plan", _write_plan(tmp_path, me_plan), "--me", "power"]) == 0
    assert solved_problems[-1].me.power_external


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"plan_digest": "0" * 64}, "made against another plan"),
        ({"dataset_version": "2.9.0-beta-3@then"}, "made against another dataset"),
        ({"nets": {"no-such-net": "main"}}, "no item or fluid net for: no-such-net"),
    ],
)
def test_an_me_plan_that_does_not_fit_the_plan_is_exit_2(
    change: dict[str, object],
    message: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    solved_problems: list[InputIR],
) -> None:
    me_plan = _sand_plan({}, MENetworkSpec(id="main", mode=MEMode.ATTACHED))
    path = _write_plan(tmp_path, me_plan.model_copy(update=change))
    assert main([_SAND, "--me-plan", path]) == 2
    assert message in capsys.readouterr().err
    assert not solved_problems


@pytest.mark.parametrize(
    "payload",
    [
        "{",  # not JSON
        json.dumps({"plan_digest": "x", "version": 99}),  # another contract version
        json.dumps(
            {  # a Fluix subnet would merge with the main network
                "plan_digest": "x",
                "networks": [{"id": "s", "mode": "subnet", "colour": "fluix"}],
            }
        ),
        json.dumps(
            {  # two subnets of one colour would merge with each other
                "plan_digest": "x",
                "networks": [
                    {"id": "a", "mode": "subnet", "colour": "red"},
                    {"id": "b", "mode": "subnet", "colour": "red"},
                ],
            }
        ),
        json.dumps({"plan_digest": "x", "nets": {"n": "nowhere"}}),  # an unknown network
    ],
)
def test_an_me_plan_that_will_not_load_is_exit_2(
    payload: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    solved_problems: list[InputIR],
) -> None:
    path = tmp_path / "me-plan.json"
    path.write_text(payload, encoding="utf-8")
    assert main([_SAND, "--me-plan", str(path)]) == 2
    assert "could not load --me-plan" in capsys.readouterr().err
    assert not solved_problems


def test_a_missing_me_plan_file_is_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([_SAND, "--me-plan", str(tmp_path / "absent.json")]) == 2
    assert "could not load --me-plan" in capsys.readouterr().err


# ------------------------------------------------------------------ the shorthand and the note


def test_the_shorthand_puts_every_net_of_its_kind_on_one_network(
    solved_problems: list[InputIR],
) -> None:
    assert main([_SAND, "--me", "items", "--me", "fluids"]) == 0
    problem = solved_problems[-1]
    assert [(n.id, n.mode) for n in problem.me.networks] == [("main", MEMode.ATTACHED)]
    item_nets = {n.id for n in problem.nets if n.commodity is Commodity.ITEM}
    assert set(_on_me(problem)) == item_nets


def test_without_me_the_problem_keeps_every_net_physical(
    capsys: pytest.CaptureFixture[str], solved_problems: list[InputIR]
) -> None:
    assert main([_SAND]) == 0
    assert _on_me(solved_problems[-1]) == {}
    assert "ME network" not in capsys.readouterr().err  # nothing to say, so nothing said


def test_a_net_on_me_prints_the_note_once_on_stderr_only(
    capsys: pytest.CaptureFixture[str], solved_problems: list[InputIR]
) -> None:
    # Advisory, like the other notes: the exit code is the layout's, and stdout (the layout's
    # JSON, #203) stays free of it so it still parses when piped.
    assert main([_SAND, "--me", "items"]) == 0
    out, err = capsys.readouterr()
    assert err.splitlines().count(_ITEMS_NOTE) == 1
    assert "ME network" not in out


def test_power_left_to_the_builder_is_said_before_the_solve(
    capsys: pytest.CaptureFixture[str], solved_sand: tuple[InputIR, LayoutResult]
) -> None:
    # The nets on ME are built now (#335), so only power, which nothing lays, needs saying up front.
    problem, _ = solved_sand
    _note_me(on_me(problem, Commodity.ITEM, power=True))
    (line,) = capsys.readouterr().err.splitlines()
    assert line == (
        "note: power is left to you (--me power) - no source or cable is laid for it, so the "
        "builder must supply it"
    )


def test_the_note_is_silent_with_nothing_on_me(
    capsys: pytest.CaptureFixture[str], solved_sand: tuple[InputIR, LayoutResult]
) -> None:
    problem, _ = solved_sand
    _note_me(problem)
    assert capsys.readouterr().err == ""


# ------------------------------------------------------------------ end to end


def test_sand_with_items_on_me_solves_valid_on_an_me_network(
    real_solves: list[tuple[InputIR, LayoutResult]], capsys: pytest.CaptureFixture[str]
) -> None:
    # The issue's acceptance run, for real: `gtnh-solve examples/gtnh-sand.json --me items`.
    # Sand is items end to end, so every item net rides the network (no pipe, no auto-output) and
    # its devices sit on AE2 cable from the stub (#335), while the power cable is still laid, and
    # the layout must still be certified.
    assert main([_SAND, "--me", "items"]) == 0
    ((problem, layout),) = real_solves
    assert layout.status is LayoutStatus.VALID
    assert validate(problem, layout).ok  # the independent gate agrees, not only the solver
    item_nets = {n.id for n in problem.nets if n.commodity is Commodity.ITEM}
    assert item_nets  # the line has item nets to skip, so the empties below mean something
    assert [r.commodity for r in layout.routes] == [Commodity.POWER]
    assert not [ac for ac in layout.auto_connections if ac.net_id in item_nets]
    (network,) = layout.me_networks
    built = {(d.machine_id, d.endpoint_id) for d in network.devices}
    assert built == {(m.id, e.id) for m in problem.machines for e in m.me_endpoints}
    assert _ITEMS_NOTE in capsys.readouterr().err.splitlines()


# ------------------------------------------------------------------ power left to the builder (#225)


def test_power_left_to_the_builder_synthesizes_no_power_source() -> None:
    """With power left to the builder no cable is laid, so a synthesized source would stand in the
    build connected to nothing. The adapter drops the sources and their nets, and nothing else:
    every powered machine keeps the energy ports that say what it draws."""
    physical = adapt_file(_SAND)
    external = adapt_file(_SAND, me_power=True)
    assert [m.id for m in physical.machines if m.is_power_source] == ["power-source:LV"]
    assert [n.id for n in physical.nets if n.commodity is Commodity.POWER] == ["power:LV"]
    assert external.machines == [m for m in physical.machines if not m.is_power_source]
    assert external.nets == [n for n in physical.nets if n.commodity is not Commodity.POWER]
    powered = [m for m in external.machines if m.eut > 0]
    assert powered
    assert all(m.power_input_ports for m in powered)


def test_power_left_to_the_builder_still_cross_checks_the_plans_resolved_power(
    recwarn: pytest.WarningsRecorder,
) -> None:
    # Sand's export states its own power total. The cross-check sums the synthesized nets' draw,
    # so it has to run before those nets are dropped: after, it would read 0 EU/t and report the
    # export as inconsistent.
    adapt_file(_SAND, me_power=True)
    assert not [w for w in recwarn if "resolved power total" in str(w.message)]


def test_sand_with_power_left_to_the_builder_places_and_exports_no_power_source(
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


def test_a_subnet_says_whose_channels_it_spends(capsys: pytest.CaptureFixture[str]) -> None:
    problem, layout = comb(2, mode=MEMode.SUBNET)
    _note_me_networks(problem, layout)
    assert capsys.readouterr().err.splitlines() == [
        "note: ME network sub (subnet): 2 device(s) on none of your main network's channels; "
        "stock n0 1 items/t, n1 1 items/t"
    ]
    linked = InputIR.model_validate(
        {
            **problem.model_dump(),
            "machines": [
                *(m.model_dump() for m in problem.machines),
                Machine(
                    id="link",
                    type="ME Smart Cable",
                    voltage_tier="LV",
                    orientation_options=[Facing.WEST],
                    me_role=MERole.LINK,
                    me_network=SUB,
                    outside_front=True,
                    me_endpoints=(me_endpoint("bus", (), MEDeviceKind.STORAGE_BUS, network=SUB),),
                ).model_dump(),
            ],
        }
    )
    _note_me_networks(linked, layout)
    (line,) = capsys.readouterr().err.splitlines()
    assert "3 device(s) on 1 channel(s) of your main network, one per link" in line
