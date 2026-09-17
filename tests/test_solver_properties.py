"""The never-silently-invalid invariant, proven over *generated* inputs.

``docs/TESTING.md`` makes one promise the whole design rests on: for any input graph, ``solve``
returns a valid layout **or** an explicit infeasibility report, never a silently-invalid one.
Until now it was asserted on two hand-made fixtures (``test_solver``), which proves the two lines
in ``examples/`` work and says nothing about the space around them.

The promise has two halves, and they are one mechanism - which is why both live here:

1. ``solve`` may end in VALID only when the **independent** validator agrees: ``solver.core``
   runs ``validate`` on its own assembled layout and downgrades to ``partial_invalid`` when it
   proves anything (docs/ARCHITECTURE.md #4). The invariant over generated ``InputIR``s is
   therefore the real end-to-end check, not an internal ``place.ok and route.ok``.
2. That downgrade is only as good as a validator that always *answers*. ``validate`` is
   documented never to raise (``validator/report``); if it raised on a layout its own solver had
   just built, ``solve`` would propagate the exception instead of downgrading and the promise
   would fail as a crash rather than as a bad layout. So the second test fuzzes ``validate`` with
   arbitrary schema-valid layouts, most of them nonsense.

How the problems are generated, and why:

- **Power comes from ``adapter.power.synthesize_power``**, the same call the adapter makes, rather
  than being hand-rolled here. A power topology is not a free axis - the hatch split, the
  per-tier partition into cable runs and the synthesized source machines all follow from the
  draws - and reimplementing that in a strategy would test the reimplementation.
- **Sinks are used by at most one net per commodity.** Two nets landing on one machine's single
  input port is representable but always contends for the same hatch, so admitting it would spend
  the budget re-proving one ``partial_invalid`` instead of exploring layouts that can be valid.
- **Pinned I/O is drawn rarely and on purpose.** Nothing in the router routes *to* a pin yet, so a
  pin reliably drives the assembled layout into the downgrade path - which is the half of the
  invariant an all-valid corpus would never exercise.

**A green property test can hide a space that proves nothing**, so the outcome mix is reported by
``event()`` - run with ``--hypothesis-show-statistics``. All three outcomes stay well represented
on either path (at the time of writing 35-45% valid, ~30% partial_invalid, 20-30% infeasible; the
exact split moves with hypothesis's own example database), and this file alone reaches 98% of
``solver/core``, 98% of ``solver/repair``, 94% of ``router/power`` and 85% of ``validator/core``.
If a change ever collapses that spread - all-infeasible is the likely direction, from a tighter
region rule or a costlier default - the test still passes while proving much less. Check the mix,
not just the exit code.

**One gate this corpus does not reach:** ``POWER_SUPPLY_INSUFFICIENT``. A generated machine is a
bare ``type="t"`` with no dataset behind it, so the adapter cannot name which intake rule applies
and rightly abstains (#114/#129) - of 53 VALID powered layouts in a 250-example sample, 51 carried
a ``ValidationReport.unverified_power_intake`` entry and only 2 were fully measured. The invariant
itself is unaffected (an abstention is not a violation, and ``report.ok`` is untouched), but do
not read a green run here as cover for that check; ``test_validator`` measures it directly.
"""

from __future__ import annotations

import itertools
import math
from typing import TypeVar

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from gtnh_solver.adapter.power import synthesize_power
from gtnh_solver.ir import (
    AutoConnection,
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    Infeasibility,
    InputIR,
    IODirection,
    LayoutMetrics,
    LayoutResult,
    LayoutStatus,
    Machine,
    MachineFaceRef,
    METoggles,
    Net,
    PinnedIO,
    PipeFamily,
    PlacedHatch,
    Placement,
    Port,
    Route,
    RouteMaterial,
    Segment,
    Terminal,
)
from gtnh_solver.ir.enums import HORIZONTAL_FACINGS_ORDERED
from gtnh_solver.ir.output import CABLE_THICKNESSES
from gtnh_solver.solver import solve
from gtnh_solver.validator import ValidationReport, ViolationCode, validate

# ------------------------------------------------------------------------------------- problems

_T = TypeVar("_T")

#: Tiers to draw machines at. On-ladder and low, so the synthesized supply is always well
#: defined: ULV is excluded because it cannot survive the design cable run at all, which raises
#: ``UnpowerableError`` out of the adapter (#112) - a real bug, but the adapter's, not this
#: invariant's, and admitting it here would only mask #112 behind a failure of this test.
_TIERS = ("LV", "MV", "HV")

#: EU/t a machine may draw. 0 (an unpowered block) plus draws that need one, two and several
#: energy hatches at the lower tiers, so the hatch split and the per-tier cable partition both
#: get exercised without the numbers running away.
_EUT = (0.0, 8.0, 32.0, 480.0)

#: The I/O a generated machine may expose. One port per (commodity, direction), because a second
#: identical port would only duplicate the pairing below; power ports are never drawn here - they
#: are synthesized from ``eut`` like the adapter does.
_PORT_KINDS: tuple[tuple[str, Commodity, IODirection], ...] = (
    ("item:out", Commodity.ITEM, IODirection.OUTPUT),
    ("item:in", Commodity.ITEM, IODirection.INPUT),
    ("fluid:out", Commodity.FLUID, IODirection.OUTPUT),
    ("fluid:in", Commodity.FLUID, IODirection.INPUT),
)

#: ``(commodity, source port, sink port)`` - the pairs ``_nets_over`` wires machines together on.
_ROUTED_COMMODITIES: tuple[tuple[Commodity, str, str], ...] = (
    (Commodity.ITEM, "item:out", "item:in"),
    (Commodity.FLUID, "fluid:out", "fluid:in"),
)


def _subsets(items: tuple[_T, ...], *, empty: bool) -> tuple[tuple[_T, ...], ...]:
    """Every subset of ``items``, in size order. Enumerated up front so the strategies below can
    ``sampled_from`` them: drawing a unique list of the same things instead makes hypothesis
    *filter* the rejected draws, and at four items that churn was a sixth of the budget."""
    sizes = range(0 if empty else 1, len(items) + 1)
    return tuple(itertools.chain.from_iterable(itertools.combinations(items, n) for n in sizes))


_PORT_SUBSETS = _subsets(_PORT_KINDS, empty=True)
_ORIENTATION_SUBSETS = _subsets(HORIZONTAL_FACINGS_ORDERED, empty=False)


@st.composite
def _machines(draw: st.DrawFn, mid: str) -> Machine:
    """One machine: a subset of the four routed ports, a small footprint, and a draw.

    ``hatch_cells`` is drawn as unknown (a single-block machine, or any plan adapted without the
    physical dataset) or as a small count, because the two take genuinely different paths - only a
    machine with a structural record splits its draw across several energy hatches, and only one
    can be proved to have wired more connections than it has cells to host them.
    """
    kinds = draw(st.sampled_from(_PORT_SUBSETS))
    return Machine(
        id=mid,
        type="t",
        voltage_tier=draw(st.sampled_from(_TIERS)),
        eut=draw(st.sampled_from(_EUT)),
        footprint=CellBox(
            sx=draw(st.integers(min_value=1, max_value=2)),
            sy=1,
            sz=draw(st.integers(min_value=1, max_value=2)),
        ),
        faces=FaceSpec(ports=[Port(id=pid, commodity=c, direction=d) for pid, c, d in kinds]),
        orientation_options=list(draw(st.sampled_from(_ORIENTATION_SUBSETS))),
        hatch_cells=draw(st.none() | st.integers(min_value=1, max_value=6)),
    )


def _nets_over(draw: st.DrawFn, machines: list[Machine]) -> list[Net]:
    """Wire the drawn machines together: each source port to a disjoint set of sink ports."""
    nets: list[Net] = []
    for commodity, out_port, in_port in _ROUTED_COMMODITIES:
        sources = [m.id for m in machines if any(p.id == out_port for p in m.faces.ports)]
        free_sinks = [m.id for m in machines if any(p.id == in_port for p in m.faces.ports)]
        for index, src in enumerate(sources):
            takeable = [s for s in free_sinks if s != src]
            count = draw(st.integers(min_value=0, max_value=min(2, len(takeable))))
            sinks = takeable[:count]
            if not sinks:
                continue
            free_sinks = [s for s in free_sinks if s not in sinks]
            nets.append(
                Net(
                    id=f"{commodity.value}-{index}",
                    commodity=commodity,
                    fluid_or_item="x",
                    throughput=draw(st.sampled_from((0.0, 1.0, 20.0))),
                    endpoints=[
                        MachineFaceRef(machine_id=src, port_id=out_port),
                        *(MachineFaceRef(machine_id=s, port_id=in_port) for s in sinks),
                    ],
                )
            )
    return nets


@st.composite
def _problems(draw: st.DrawFn) -> InputIR:
    """A small, referentially-intact ``InputIR``: machines, nets, region, reserved cells, ME."""
    machines = [draw(_machines(f"m{i}")) for i in range(draw(st.integers(0, 4)))]
    nets = _nets_over(draw, machines)
    # Exactly what the adapter does, and a no-op when nothing draws power.
    machines, nets = synthesize_power(machines, nets)

    # The region is mostly drawn to *fit* - a square floor at least as big as the machines' own,
    # plus slack to route in - with a deliberate minority of tight ones. "Does not fit" is a real
    # outcome the invariant covers, but a space dominated by it would spend the budget re-proving
    # the placer's one early return and never reach the routing the promise is really about.
    floor = math.ceil(math.sqrt(sum(m.footprint.sx * m.footprint.sz for m in machines) or 1))
    side = (
        st.integers(min_value=1, max_value=3)
        if draw(st.integers(min_value=0, max_value=3)) == 0
        else st.integers(min_value=floor, max_value=floor + 2)
    )
    region = CellBox(sx=draw(side), sy=draw(st.integers(min_value=1, max_value=3)), sz=draw(side))
    cells = st.builds(
        CellCoord,
        x=st.integers(min_value=0, max_value=region.sx - 1),
        y=st.integers(min_value=0, max_value=region.sy - 1),
        z=st.integers(min_value=0, max_value=region.sz - 1),
    )
    pins: list[PinnedIO] = []
    if nets and draw(st.integers(min_value=0, max_value=9)) == 0:
        pins = [
            PinnedIO(
                net_id=draw(st.sampled_from([n.id for n in nets])),
                cell=draw(cells),
                kind=draw(st.sampled_from(list(IODirection))),
            )
        ]
    return InputIR(
        bounding_region=region,
        machines=machines,
        nets=nets,
        reserved_cells=draw(st.lists(cells, max_size=2)),
        pinned=pins,
        me_toggles=METoggles(
            items=draw(st.booleans()), fluids=draw(st.booleans()), power=draw(st.booleans())
        ),
    )


# ----------------------------------------------------------------------------- the invariant (1)


@pytest.mark.parametrize("optimize", [True, False])
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(problem=_problems(), seed=st.integers(min_value=0, max_value=3))
def test_solve_is_valid_or_explicitly_infeasible(
    problem: InputIR, seed: int, optimize: bool
) -> None:
    """Either the independent validator passes the layout, or the result says why it could not.

    Asserted on both paths the site exposes: the annealed feedback loop and ``--fast``. They
    assemble layouts differently (``_solve_fast`` places constructively and repairs nothing), so a
    promise that held on one of them would be half a promise.
    """
    layout = solve(problem, seed=seed, optimize=optimize)
    event(f"status={layout.status.value}")

    if layout.status is LayoutStatus.VALID:
        report = validate(problem, layout)
        assert report.ok, f"solve returned VALID for a layout the validator rejects:\n{report}"
    else:
        # The contract forbids the alternative (a non-VALID result must carry one), but that is
        # exactly the thing being promised, so it is asserted rather than assumed.
        assert layout.infeasibility is not None


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(problem=_problems(), seed=st.integers(min_value=0, max_value=3))
def test_solve_is_deterministic_for_a_given_problem_and_seed(problem: InputIR, seed: int) -> None:
    """The same problem + seed always yields the same layout (``solver.core``: no wall-clock).

    The invariant above is worth little if the answer wobbles: "valid or explicit" has to mean
    valid-or-explicit *every* time, not on the run that happened to be observed. ``test_solver``
    pins this on the two example lines; what generated inputs add is the branchy corner - a
    partial layout ranked against another partial, a tie between two equal-quality attempts -
    where an unstable iteration order would actually show. Deliberately the cheapest budget in
    the file (two solves per example), since it is the invariant's guard rail, not the invariant.
    """
    first = solve(problem, seed=seed)
    second = solve(problem, seed=seed)
    assert first.model_dump() == second.model_dump()


# ------------------------------------------------------------------------- arbitrary layouts (2)

_LAYOUT_CELLS = st.builds(
    CellCoord,
    x=st.integers(min_value=-2, max_value=6),
    y=st.integers(min_value=-2, max_value=4),
    z=st.integers(min_value=-2, max_value=6),
)


def _ids(known: list[str]) -> st.SearchStrategy[str]:
    """Ids that mostly resolve against the problem, and sometimes name nothing at all."""
    return st.sampled_from([*known, "ghost"]) if known else st.just("ghost")


def _materials(commodity: Commodity) -> st.SearchStrategy[RouteMaterial | None]:
    """A schema-legal material for ``commodity``, or none (which means "unspecified pipe")."""
    family = {
        Commodity.ITEM: PipeFamily.ITEM_PIPE,
        Commodity.FLUID: PipeFamily.FLUID_PIPE,
        Commodity.POWER: PipeFamily.CABLE,
    }[commodity]
    return st.none() | st.builds(
        RouteMaterial,
        family=st.just(family),
        # "invented" is in the pool deliberately: an unsanctioned stand-in is a violation the
        # validator has to *report*, and reporting is the thing under test.
        material=st.sampled_from(("tin", "polyethylene", "steel", "invented")),
        tier=st.sampled_from(_TIERS) if family is PipeFamily.CABLE else st.none(),
    )


@st.composite
def _layouts(draw: st.DrawFn, problem: InputIR) -> LayoutResult:
    """An arbitrary layout that satisfies the *schema* and very little else.

    Deliberately not a solve: segments need not be unit hops or contiguous, terminals need not
    touch anything, hatches may face into their own machine and ids may resolve to nothing. The
    schema's own rules are the one thing honored (a power route carries an aligned thickness, a
    valid status carries no infeasibility), because a layout that violates *those* cannot be
    constructed at all and so can never reach ``validate``.
    """
    machine_ids = [m.id for m in problem.machines]
    net_ids = [n.id for n in problem.nets]
    port_ids = sorted({p.id for m in problem.machines for p in m.faces.ports})
    facings = st.sampled_from(list(Facing))

    placements = draw(
        st.lists(
            st.builds(
                Placement, machine_id=_ids(machine_ids), cell=_LAYOUT_CELLS, orientation=facings
            ),
            max_size=4,
        )
    )
    terminals = st.builds(
        Terminal,
        machine_id=_ids(machine_ids),
        port_id=_ids(port_ids),
        face=facings,
        cell=_LAYOUT_CELLS,
    )
    routes: list[Route] = []
    # Not unique: two routes claiming one net is itself a violation the validator has a code for,
    # so letting the draw collide is coverage rather than noise.
    for net_id, commodity in draw(
        st.lists(st.tuples(_ids(net_ids), st.sampled_from(list(Commodity))), max_size=3)
    ):
        segments = draw(
            st.lists(
                st.builds(
                    Segment,
                    start=_LAYOUT_CELLS,
                    end=_LAYOUT_CELLS,
                    channel=st.integers(min_value=0, max_value=2),
                ),
                max_size=4,
            )
        )
        power = commodity is Commodity.POWER
        routes.append(
            Route(
                net_id=net_id,
                commodity=commodity,
                terminals=draw(st.lists(terminals, max_size=3)),
                segments=segments,
                thickness_per_segment=(
                    [draw(st.sampled_from(CABLE_THICKNESSES)) for _ in segments] if power else None
                ),
                material=draw(_materials(commodity)),
            )
        )

    autos = draw(
        st.lists(
            st.builds(
                AutoConnection,
                net_id=_ids(net_ids),
                source_machine_id=_ids(machine_ids),
                source_face=facings,
                target_machine_id=_ids(machine_ids),
                target_face=facings,
            ),
            max_size=2,
        )
    )
    hatches = draw(
        st.lists(
            st.builds(
                PlacedHatch,
                machine_id=_ids(machine_ids),
                kind=st.sampled_from(
                    ("InputBus", "OutputHatch", "Energy", "Maintenance", "Muffler")
                ),
                cell=_LAYOUT_CELLS,
                facing=facings,
                port_id=st.none() | _ids(port_ids),
            ),
            max_size=4,
        )
    )
    status = draw(st.sampled_from(list(LayoutStatus)))
    return LayoutResult(
        status=status,
        infeasibility=(
            None
            if status is LayoutStatus.VALID
            else Infeasibility(constraint="generated", detail="generated")
        ),
        placements=placements,
        routes=routes,
        auto_connections=autos,
        hatches=hatches,
        metrics=LayoutMetrics(),
        seed=draw(st.integers(min_value=0, max_value=3)),
    )


@settings(max_examples=300, deadline=None)
@given(pair=_problems().flatmap(lambda p: st.tuples(st.just(p), _layouts(p))))
def test_validate_reports_but_never_raises(pair: tuple[InputIR, LayoutResult]) -> None:
    """``validate`` answers for every schema-valid layout, however unrelated to the problem.

    This is the contract in ``validator/report``: a layout that breaks a rule is *reported*, not
    raised. It matters beyond tidiness because ``solver.core`` calls ``validate`` on its own
    output to decide VALID - an exception there would escape ``solve`` instead of downgrading
    the layout, turning the never-silently-invalid promise into a traceback.
    """
    problem, layout = pair

    report = validate(problem, layout)

    assert isinstance(report, ValidationReport)
    assert report.ok is (not report.violations)
    assert all(isinstance(v.code, ViolationCode) for v in report.violations)
    assert str(report)  # the CLI renders this; a report that cannot be shown is not an answer
    event(f"violations={'none' if report.ok else 'some'}")
