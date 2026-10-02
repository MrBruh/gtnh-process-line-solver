"""Tests for ``router.item_pipes``: the router's own reading of what GT charges each pipe block (#200).

The router lays an item pipe at the larger of its run-wide bound (#165) and the busiest block's
charge. The cases are the validator's (``test_validator``, "Item pipe throughput"), read back
through the router: one net on a straight run of blocks along x at ``(x, 1, 1)``, each dock a
machine wired to one block. The property test at the end holds the two readings to each other: the
router never lays a size the validator refuses unless nothing bigger exists, and it raises a size
only where the validator would refuse the run-wide one.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gtnh_solver.dataset import item_pipe_size_for, route_material
from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    Facing,
    InputIR,
    IODirection,
    LayoutResult,
    LayoutStatus,
    Machine,
    MachineFaceRef,
    Net,
    PipeSize,
    Placement,
    Port,
    Terminal,
)
from gtnh_solver.ir.geometry import Cell
from gtnh_solver.router import route
from gtnh_solver.router.core import _as_route, _crowded_side, _Laid, _pipe_size
from gtnh_solver.router.item_pipes import block_insertions
from gtnh_solver.validator import validate
from gtnh_solver.validator.report import ViolationCode
from tests._helpers import at, machine, property_examples

#: Per dock slot: the machine's offset from its block, the face it docks with, and its front.
_DOCK_SLOTS: list[tuple[tuple[int, int, int], Facing, Facing]] = [
    ((0, -1, 0), Facing.UP, Facing.NORTH),
    ((0, 1, 0), Facing.DOWN, Facing.NORTH),
    ((0, 0, -1), Facing.SOUTH, Facing.NORTH),
    ((0, 0, 1), Facing.NORTH, Facing.SOUTH),
]

_Dock = tuple[int, IODirection, float | None]  # (block, direction, items/t)
_OUT, _IN = IODirection.OUTPUT, IODirection.INPUT

#: The issue's reproduction: producers at 0.15 and 0.05 items/t on blocks 1 and 2, consumers at 0.1
#: each on the ends. Nearest first makes three streams, where either side has two endpoints.
_ISSUE_200: tuple[_Dock, ...] = ((1, _OUT, 0.15), (2, _OUT, 0.05), (0, _IN, 0.1), (3, _IN, 0.1))


@dataclass(frozen=True)
class _Run:
    """One item net laid on a straight run, as the router hands it to ``_pipe_size``."""

    net: Net
    machines: dict[str, Machine]
    placements: list[Placement]
    laid: _Laid
    length: int

    def charges(self, *, split: bool = False) -> dict[int, float]:
        """Block x -> the insertions per 40 ticks the router reads GT charging it."""
        charged = block_insertions(
            self.net, self.laid.terminals, self.laid.legs, self.machines, split=split
        )
        return {x: need for (x, _, _), need in charged.items()}

    def size(self, *, split: bool = False) -> PipeSize:
        return _pipe_size(self.net, self.laid, self.machines, split=split)

    def run_wide(self) -> PipeSize:
        """The size the run-wide bound alone lays (#165), the router's rule before #200."""
        return item_pipe_size_for(max(_crowded_side(self.net, self.machines), 1.0)) or PipeSize.HUGE

    def layout(self, size: PipeSize | None = None) -> tuple[InputIR, LayoutResult]:
        """The validator's view: the route the router emits, or that route at ``size``."""
        problem = InputIR(
            bounding_region=CellBox(sx=self.length, sy=3, sz=3),
            machines=list(self.machines.values()),
            nets=[self.net],
        )
        piped = _as_route(self.net, self.laid, self.machines, split=False)
        if size is not None:
            piped = piped.model_copy(update={"material": route_material(Commodity.ITEM, size=size)})
        layout = LayoutResult(
            status=LayoutStatus.VALID, seed=0, placements=self.placements, routes=[piped]
        )
        return problem, layout


def _straight(
    *docks: _Dock,
    length: int = 3,
    throughput: float | None = None,
    items: tuple[str, ...] = (),
) -> _Run:
    """One net on a run of ``length`` blocks, each dock a machine with one port on one block.

    ``throughput`` defaults to what the producers' rates add up to. ``items`` makes it a merged run.
    """
    machines: dict[str, Machine] = {}
    placements: list[Placement] = []
    terminals: list[Terminal] = []
    endpoints: list[MachineFaceRef] = []
    used: Counter[int] = Counter()
    for i, (block, direction, rate) in enumerate(docks):
        (dx, dy, dz), face, front = _DOCK_SLOTS[used[block]]
        used[block] += 1
        mid, port = f"m{i}", "out" if direction is _OUT else "in"
        machines[mid] = machine(
            mid,
            [Port(id=port, commodity=Commodity.ITEM, direction=direction, rate=rate)],
            orientation=front,
        )
        placements.append(at(mid, block + dx, 1 + dy, 1 + dz, orientation=front))
        terminals.append(
            Terminal(machine_id=mid, port_id=port, face=face, cell=CellCoord(x=block, y=1, z=1))
        )
        endpoints.append(MachineFaceRef(machine_id=mid, port_id=port))
    if throughput is None:
        throughput = sum(rate or 0.0 for _, direction, rate in docks if direction is _OUT)
    net = Net(
        id="n",
        commodity=Commodity.ITEM,
        fluid_or_item=None if items else "minecraft:stone",
        items=items,
        throughput=throughput,
        endpoints=endpoints,
    )
    leg = tuple((x, 1, 1) for x in range(length))
    laid = _Laid(terminals=tuple(terminals), legs=(leg,) if length > 1 else ())
    return _Run(net, machines, placements, laid, length)


def _refused(problem: InputIR, layout: LayoutResult) -> int:
    """How many blocks the validator refuses as too thin for their streams."""
    return validate(problem, layout).codes().count(ViolationCode.ITEM_PIPE_SIZE_INSUFFICIENT)


# ------------------------------------------------------------------------------- the issue (#200)


def test_a_split_producer_makes_more_streams_than_either_side_has_endpoints() -> None:
    """``a`` (block 1) serves ``c1`` first and sends its rest the long way to ``c2``, which ``b``
    (block 2) also serves: three streams cross the middle blocks. The run-wide bound counts two
    endpoints a side, so it laid large; the busiest block needs three insertions, which is huge."""
    run = _straight(*_ISSUE_200, length=4)
    assert run.charges() == {0: 2, 1: 3, 2: 3, 3: 2}
    assert _crowded_side(run.net, run.machines) == 2
    assert run.run_wide() is PipeSize.LARGE
    assert run.size() is PipeSize.HUGE


def test_the_issue_line_routes_valid_end_to_end() -> None:
    """Through ``route``: four machines in a row with one row of space above them, so the pipe is
    the straight run over their tops. It used to come back large and refused at the two middle
    blocks (``partial_invalid``); it is now huge and valid."""
    machines = [
        machine(mid, [Port(id=port, commodity=Commodity.ITEM, direction=direction, rate=rate)])
        for mid, port, direction, rate in (
            ("a", "out", _OUT, 0.15),
            ("b", "out", _OUT, 0.05),
            ("c1", "in", _IN, 0.1),
            ("c2", "in", _IN, 0.1),
        )
    ]
    net = Net(
        id="n",
        commodity=Commodity.ITEM,
        fluid_or_item="minecraft:stone",
        throughput=0.2,
        endpoints=[MachineFaceRef(machine_id=m.id, port_id=m.faces.ports[0].id) for m in machines],
    )
    problem = InputIR(bounding_region=CellBox(sx=4, sy=2, sz=1), machines=machines, nets=[net])
    placements = [at("c1", 0, 0, 0), at("a", 1, 0, 0), at("b", 2, 0, 0), at("c2", 3, 0, 0)]
    result = route(problem, placements)
    assert result.ok, result.infeasibility
    (piped,) = result.routes
    assert piped.material is not None
    assert piped.material.size is PipeSize.HUGE
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=placements, routes=[piped])
    assert validate(problem, layout).ok

    large = piped.model_copy(
        update={"material": route_material(Commodity.ITEM, size=PipeSize.LARGE)}
    )
    report = validate(problem, layout.model_copy(update={"routes": [large]}))
    assert report.codes() == (ViolationCode.ITEM_PIPE_SIZE_INSUFFICIENT,) * 2


def test_a_one_block_pipe_carries_every_stream() -> None:
    """With every dock on one block, that block pays for all three streams of the issue's rates:
    more than either side's two endpoints, so even the run-wide rule's own premise undercounts."""
    run = _straight(*((0, direction, rate) for _, direction, rate in _ISSUE_200), length=1)
    assert run.charges() == {0: 3}
    assert run.run_wide() is PipeSize.LARGE
    assert run.size() is PipeSize.HUGE


# ------------------------------------------------------------- the validator's cases, read back


def test_the_stone_run_charges_the_chest_block_for_every_hammer() -> None:
    # The parallel sand build's stone run in miniature: the chest and the nearest hammer on block 2.
    run = _straight((2, _OUT, 0.3), (2, _IN, 0.1), (1, _IN, 0.1), (0, _IN, 0.1))
    assert run.charges() == {2: 3, 1: 2, 0: 1}
    assert run.size() is PipeSize.HUGE


def test_the_floor_never_lowers_a_run() -> None:
    """Producers paired with the consumer beside them charge one stream a block, which a normal
    pipe makes; the run-wide bound still asks for huge, and the run keeps it (#200 only raises)."""
    paired = tuple(dock for block in range(3) for dock in ((block, _OUT, 0.1), (block, _IN, 0.1)))
    run = _straight(*paired)
    assert run.charges() == {0: 1, 1: 1, 2: 1}
    assert run.size() is run.run_wide() is PipeSize.HUGE


def test_a_block_beside_a_sender_pays_for_a_delivery_that_went_the_other_way() -> None:
    # Block 0 is as near block 1's producer as block 2, its consumer: a tie GT may break either way.
    run = _straight((0, _OUT, 0.1), (0, _IN, 0.1), (1, _OUT, 0.1), (2, _IN, 0.1))
    assert run.charges() == {0: 2, 1: 1, 2: 1}
    assert run.size() is PipeSize.LARGE


def test_a_branch_as_near_as_the_target_pays_for_the_delivery() -> None:
    """The hops are the tree's, not a line's: a sender at a junction charges a dead-end arm as near
    it as the consumer it serves, and the far consumer's arm only for its own stream."""
    hub = (1, 1, 1)
    run = _straight((1, _OUT, 0.2), (0, _IN, 0.1), length=1)
    far = machine("far", [Port(id="in", commodity=Commodity.ITEM, direction=_IN, rate=0.1)])
    laid = _Laid(
        terminals=(
            *run.laid.terminals,
            Terminal(machine_id="far", port_id="in", face=Facing.UP, cell=CellCoord(x=1, y=1, z=3)),
        ),
        legs=(((0, 1, 1), hub, (2, 1, 1)), (hub, (1, 1, 2), (1, 1, 3))),
    )
    net = run.net.model_copy(
        update={"endpoints": [*run.net.endpoints, MachineFaceRef(machine_id="far", port_id="in")]}
    )
    charged = block_insertions(
        net, laid.terminals, laid.legs, {**run.machines, "far": far}, split=False
    )
    want: dict[Cell, float] = {hub: 2, (0, 1, 1): 2, (2, 1, 1): 2, (1, 1, 2): 2, (1, 1, 3): 1}
    assert charged == want


def test_a_merged_run_feeds_every_filter_from_every_producer() -> None:
    """A merged run's filters each take only their own item, so nearest first does not apply: both
    producers feed both filters. Unmerged, the same docks pair off and the middle block carries
    nothing."""
    docks = ((0, _OUT, 0.1), (0, _IN, 0.1), (2, _OUT, 0.1), (2, _IN, 0.1))
    assert _straight(*docks, items=("a", "b")).charges() == {0: 3, 1: 2, 2: 3}
    assert _straight(*docks).charges() == {0: 1, 1: 0, 2: 1}


def test_a_split_pipe_is_charged_for_what_its_own_producers_send() -> None:
    """Two producers of 0.01 items/t into a consumer taking 0.03, the rest of which auto-outputs
    into it (#270). The pipe moves 0.02, a normal pipe's 0.8 insertions; charged for the consumer's
    whole intake it would need 1.2, a large."""
    run = _straight((0, _OUT, 0.01), (2, _OUT, 0.01), (1, _IN, 0.03))
    split = run.charges(split=True)
    assert split[1] == pytest.approx(0.8)
    assert run.size(split=True) is PipeSize.NORMAL
    whole = run.charges(split=False)
    assert whole[1] == pytest.approx(1.2)
    assert run.size(split=False) is PipeSize.LARGE


def test_a_net_with_no_throughput_keeps_a_whole_insertion_a_stream() -> None:
    # A zero rate states no rate, not an idle endpoint: each consumer is still served once.
    run = _straight((2, _OUT, 0.0), (2, _IN, 0.0), (1, _IN, 0.0), (0, _IN, 0.0), throughput=0.0)
    assert run.charges() == {2: 3, 1: 2, 0: 1}


def test_an_unrated_port_takes_an_even_share_of_the_net() -> None:
    # 4 items/t over two consumers is 2 each, 80 items per 40 ticks: two insertions a stream.
    run = _straight((0, _OUT, None), (1, _IN, None), (2, _IN, None), throughput=4.0)
    assert run.charges() == {0: 4, 1: 4, 2: 2}
    assert run.size() is PipeSize.HUGE


def test_an_endpoint_the_router_cannot_resolve_is_left_out() -> None:
    run = _straight((2, _OUT, 0.3), (2, _IN, 0.1), (1, _IN, 0.1), (0, _IN, 0.1))
    machines = dict(run.machines)
    del machines["m3"]  # its machine is gone
    machines["m2"] = machine("m2", [])  # and this one no longer has the port
    charged = block_insertions(run.net, run.laid.terminals, run.laid.legs, machines, split=False)
    assert charged == {(0, 1, 1): 0, (1, 1, 1): 0, (2, 1, 1): 1}


def test_a_run_with_no_sender_charges_nothing() -> None:
    run = _straight((0, _IN, 0.1), (2, _IN, 0.1))
    assert run.charges() == {0: 0, 1: 0, 2: 0}
    assert run.size() is PipeSize.LARGE  # the run-wide bound still counts its two consumers


# ------------------------------------------------------------------ held against the validator


@st.composite
def _runs(draw: st.DrawFn) -> _Run:
    """A straight run of 1 to 5 blocks with 2 to 8 docks, at most four on a block."""
    length = draw(st.integers(min_value=1, max_value=5))
    # Every dock slot of every block, shuffled: the first few are the docks, with no retries.
    spots = [(block, slot) for block in range(length) for slot in range(len(_DOCK_SLOTS))]
    count = draw(st.integers(min_value=2, max_value=min(8, len(spots))))
    rates = st.one_of(st.none(), st.floats(min_value=0.0, max_value=3.0))
    docks = [
        (block, draw(st.sampled_from([_OUT, _IN])), draw(rates))
        for block, _ in draw(st.permutations(spots))[:count]
    ]
    merged = draw(st.booleans())
    return _straight(
        *docks,
        length=length,
        throughput=draw(st.sampled_from([0.0, 0.2, 1.0])),
        items=("a", "b") if merged else (),
    )


@settings(max_examples=property_examples(200))
@given(run=_runs())
def test_the_router_lays_what_the_validator_accepts_and_raises_only_what_it_refuses(
    run: _Run,
) -> None:
    """The two readings of GT's rule agree (#200). The router never lays below the run-wide bound;
    a size it lays is refused only when nothing bigger exists; and it differs from the run-wide size
    only where the validator would refuse that size, so every run the validator accepted keeps it."""
    ladder = list(PipeSize)
    size, run_wide = run.size(), run.run_wide()
    assert ladder.index(size) >= ladder.index(run_wide)
    if _refused(*run.layout()):
        assert size is PipeSize.HUGE
    if size is not run_wide:
        assert _refused(*run.layout(run_wide))
