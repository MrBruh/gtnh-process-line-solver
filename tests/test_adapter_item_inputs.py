"""Tests for bringing a single block short of faces within them on its input side (#277).

A single block has five faces that can carry a connection, one each. Real plans draw machines that
need more: an input fed from two places spends a face per feed (platline's Chemical Reactors), and
three item inputs spend three (a fertilizer Mixer). The adapter folds the feeds of one input into
one pipe, and if the machine is still short, brings its item inputs in through one face on a feed
run (``adapter.core._merge_item_inputs``). What these pin:

- **when**: only a machine short of faces (MORE connections than faces), and only a proven single
  block, so every line that lays out today is untouched;
- **the shape**: the folded net, the feed run's items, endpoints and rate, the machine's one
  ``input:items`` port;
- **what stays out**: a net that also feeds another machine, one an Item Filter sources, and a run
  whose machines do not all take every item on it;
- **that it builds**: a solved feed run is a valid layout.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from gtnh_solver.adapter import (
    AdapterWarning,
    Edge,
    MachineHandler,
    Node,
    Plan,
    Recipe,
    Resource,
    Storage,
    adapt_file,
    to_input_ir,
)
from gtnh_solver.adapter.core import _feed_item_inputs
from gtnh_solver.ir import (
    Commodity,
    FaceSpec,
    Facing,
    InputIR,
    IODirection,
    LayoutStatus,
    Machine,
    MachineFaceRef,
    Net,
    Port,
)
from gtnh_solver.ir.nets import SINGLE_BLOCK_IO_FACES, connection_counts
from gtnh_solver.placement import single_block_shortfalls
from gtnh_solver.solver import solve
from gtnh_solver.validator import validate

_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLES = sorted((_ROOT / "examples").glob("*.json"))
_ITEMS = ("dust.c", "dust.a", "dust.b")  # deliberately unsorted: the run sorts them


def _mixer_plan(
    *,
    count: int = 1,
    handler: str | None = "single",
    items: tuple[str, ...] = _ITEMS,
    fluids: tuple[str, ...] = ("water",),
    second_feed: str | None = None,
) -> Plan:
    """A Mixer node of ``count`` machines: ``items`` and ``fluids`` in, one item out, at LV.

    Every input comes from a storage of its own; ``second_feed`` feeds one item from a second
    storage too, so that input has two nets. Three items, water, the product and power are six
    connections, one more than a single block has faces for.
    """
    handlers = (
        [MachineHandler(id="h", kind=handler, label="Mixer", machine_type="Mixer")]
        if handler is not None
        else []
    )
    recipe = Recipe(
        id="r",
        machine_type="Mixer",
        eut=16.0,
        duration_ticks=100.0,
        inputs=[
            *(Resource(kind="item", id=item, amount=1.0) for item in items),
            *(Resource(kind="fluid", id=fluid, amount=100.0) for fluid in fluids),
        ],
        outputs=[Resource(kind="item", id="product", amount=1.0)],
        machine_handlers=handlers,
    )
    storages: list[Storage] = [Storage(id="drain", kind="item")]
    edges = [
        Edge(id="e-out", source="m", target="drain", resource_kind="item", resource_id="product")
    ]
    feeds = [("item", item) for item in items] + [("fluid", fluid) for fluid in fluids]
    if second_feed is not None:
        feeds.append(("item", second_feed))
    for index, (kind, resource) in enumerate(feeds):
        storages.append(Storage(id=f"s{index}", kind=kind))
        edges.append(
            Edge(
                id=f"e{index}-{resource}",
                source=f"s{index}",
                target="m",
                resource_kind=kind,
                resource_id=resource,
            )
        )
    return Plan(
        schema_version=1,
        recipes=[recipe],
        nodes=[Node(id="m", recipe_id="r", overclock_tier="LV", machine_count=count)],
        storages=storages,
        edges=edges,
    )


def _adapt(plan: Plan, **kwargs: object) -> InputIR:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", AdapterWarning)  # notes this file is not about
        return to_input_ir(plan, **kwargs)  # type: ignore[arg-type]


def _machine(ir: InputIR, machine_id: str) -> Machine:
    return next(m for m in ir.machines if m.id == machine_id)


def _runs(ir: InputIR) -> list[Net]:
    return [n for n in ir.nets if n.items]


def _ref(machine_id: str, port_id: str) -> MachineFaceRef:
    return MachineFaceRef(machine_id=machine_id, port_id=port_id)


def _input_ports(ir: InputIR, machine_id: str) -> set[str]:
    return {
        p.id
        for p in _machine(ir, machine_id).faces.ports
        if p.direction is IODirection.INPUT and p.commodity is not Commodity.POWER
    }


# ------------------------------------------------------------------ the feed run


def test_a_short_single_block_takes_its_item_inputs_on_one_run() -> None:
    ir = _adapt(_mixer_plan())
    (run,) = _runs(ir)
    assert run.items == ("dust.a", "dust.b", "dust.c")
    assert run.fluid_or_item is None
    assert run.id == "e0-dust.c+e1-dust.a+e2-dust.b"
    assert run.endpoints == [
        _ref("s0", "output:dust.c"),
        _ref("s1", "output:dust.a"),
        _ref("s2", "output:dust.b"),
        _ref("m", "input:items"),
    ]
    assert _input_ports(ir, "m") == {"input:water", "input:items"}


def test_the_run_and_its_port_carry_every_items_rate() -> None:
    # 1 of each item per 100 ticks: 0.01/t each, 0.03/t on the run.
    ir = _adapt(_mixer_plan())
    (run,) = _runs(ir)
    assert run.throughput == pytest.approx(0.03)
    port = next(p for p in _machine(ir, "m").faces.ports if p.id == "input:items")
    assert port.commodity is Commodity.ITEM
    assert port.rate == pytest.approx(0.03)


def test_the_merged_machine_is_within_its_faces() -> None:
    ir = _adapt(_mixer_plan())
    assert connection_counts(ir.nets)["m"] == 4
    assert not single_block_shortfalls(ir)


def test_a_parallel_nodes_machines_share_one_run() -> None:
    # They share their nets already, so one run from each storage feeds all three.
    ir = _adapt(_mixer_plan(count=3))
    (run,) = _runs(ir)
    assert run.endpoints[3:] == [_ref(f"m#{i}", "input:items") for i in (1, 2, 3)]
    for machine_id in ("m#1", "m#2", "m#3"):
        assert _input_ports(ir, machine_id) == {"input:water", "input:items"}


# ------------------------------------------------------------------ one input, one pipe


def test_two_feeds_of_one_input_fold_into_one_pipe() -> None:
    # One item fed from two storages, plus two fluids: water, oil, two dust feeds, the product and
    # power are six. Folding the dust brings it to five, so there is no run to build.
    ir = _adapt(_mixer_plan(items=("dust.a",), fluids=("water", "oil"), second_feed="dust.a"))
    assert not _runs(ir)
    (fold,) = [n for n in ir.nets if n.fluid_or_item == "dust.a"]
    assert fold.id == "e0-dust.a+e3-dust.a"
    assert fold.endpoints == [
        _ref("s0", "output:dust.a"),
        _ref("s3", "output:dust.a"),
        _ref("m", "input:dust.a"),
    ]
    assert connection_counts(ir.nets)["m"] == SINGLE_BLOCK_IO_FACES


def test_a_fold_that_is_not_enough_joins_the_run() -> None:
    # Three items, one fed twice: seven connections, six after the fold, four with the run.
    ir = _adapt(_mixer_plan(second_feed="dust.a"))
    (run,) = _runs(ir)
    assert run.id == "e0-dust.c+e1-dust.a+e4-dust.a+e2-dust.b"
    assert _ref("s4", "output:dust.a") in run.endpoints
    assert connection_counts(ir.nets)["m"] == 4


# ------------------------------------------------------------------ when it applies


def test_a_machine_with_exactly_five_connections_is_untouched() -> None:
    # Two items, water, the product and power: no face to spare, but none short. Unlike the output
    # side, the input side merges only what cannot be built, so this line stays as it lays out now.
    ir = _adapt(_mixer_plan(items=("dust.a", "dust.b")))
    assert connection_counts(ir.nets)["m"] == SINGLE_BLOCK_IO_FACES
    assert not _runs(ir)
    assert _input_ports(ir, "m") == {"input:dust.a", "input:dust.b", "input:water"}


def test_an_unproven_single_block_is_left_short_of_faces() -> None:
    # No handler and no census: it may be a multiblock whose structure is missing.
    ir = _adapt(_mixer_plan(handler=None))
    assert not _runs(ir)
    assert single_block_shortfalls(ir)


def test_a_machine_short_on_fluids_with_items_on_me_gets_no_run() -> None:
    # Items ride ME and dock nothing, so merging them would free no face.
    plan = _mixer_plan(items=("dust.a", "dust.b"), fluids=("f1", "f2", "f3", "f4", "f5"))
    ir = _adapt(plan, me_commodities={Commodity.ITEM})
    assert not _runs(ir)
    assert {"input:dust.a", "input:dust.b"} <= _input_ports(ir, "m")


def test_a_machine_short_on_fluids_alone_has_no_run_to_build() -> None:
    ir = _adapt(_mixer_plan(items=("dust.a",), fluids=("f1", "f2", "f3", "f4")))
    assert not _runs(ir)
    assert single_block_shortfalls(ir)


# ------------------------------------------------------------------ what stays out


def _item_machine(
    mid: str, *ports: tuple[str, IODirection], filter_items: tuple[str, ...] = ()
) -> Machine:
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[
                Port(id=pid, commodity=Commodity.ITEM, direction=direction)
                for pid, direction in ports
            ]
        ),
        filter_items=filter_items,
    )


def _item_net(nid: str, item: str, source: str, *sinks: str) -> Net:
    return Net(
        id=nid,
        commodity=Commodity.ITEM,
        fluid_or_item=item,
        throughput=1.0,
        endpoints=[
            _ref(source, f"output:{item}"),
            *(_ref(sink, f"input:{item}") for sink in sinks),
        ],
    )


_IN, _OUT = IODirection.INPUT, IODirection.OUTPUT


def test_a_net_that_also_feeds_another_machine_stays_out_of_the_run() -> None:
    # The run would hand that machine items it never asked for, so only b and c merge.
    machines = [
        _item_machine("m", ("input:a", _IN), ("input:b", _IN), ("input:c", _IN)),
        _item_machine("n", ("input:a", _IN)),
        *(_item_machine(f"s{i}", (f"output:{i}", _OUT)) for i in "abc"),
    ]
    nets = [
        _item_net("na", "a", "sa", "m", "n"),
        _item_net("nb", "b", "sb", "m"),
        _item_net("nc", "c", "sc", "m"),
    ]
    _, out = _feed_item_inputs(machines, nets, {"m"})
    assert [n.id for n in out] == ["na", "nb+nc"]
    assert out[1].items == ("b", "c")


def test_a_net_an_item_filter_sources_stays_out_of_the_run() -> None:
    # A filter's output must carry only its own items, so it cannot join a run that carries more.
    machines = [
        _item_machine("m", ("input:a", _IN), ("input:b", _IN)),
        _item_machine("fa", ("output:a", _OUT), filter_items=("a",)),
        _item_machine("sb", ("output:b", _OUT)),
    ]
    nets = [_item_net("na", "a", "fa", "m"), _item_net("nb", "b", "sb", "m")]
    assert _feed_item_inputs(machines, nets, {"m"}) == (machines, nets)


def test_machines_that_take_different_items_share_no_run() -> None:
    # One net feeds both, so they would be one run, carrying c to m and b to k.
    machines = [
        _item_machine("m", ("input:a", _IN), ("input:b", _IN)),
        _item_machine("k", ("input:a", _IN), ("input:c", _IN)),
        *(_item_machine(f"s{i}", (f"output:{i}", _OUT)) for i in "abc"),
    ]
    nets = [
        _item_net("na", "a", "sa", "m", "k"),
        _item_net("nb", "b", "sb", "m"),
        _item_net("nc", "c", "sc", "k"),
    ]
    assert _feed_item_inputs(machines, nets, {"m", "k"}) == (machines, nets)


def test_a_fluid_holding_the_merged_port_id_blocks_the_run() -> None:
    machines = [
        _item_machine("m", ("input:a", _IN), ("input:b", _IN), ("input:items", _IN)),
        *(_item_machine(f"s{i}", (f"output:{i}", _OUT)) for i in "ab"),
    ]
    machines[0] = machines[0].model_copy(
        update={
            "faces": FaceSpec(
                ports=[
                    *machines[0].faces.ports[:2],
                    Port(id="input:items", commodity=Commodity.FLUID, direction=_IN),
                ]
            )
        }
    )
    nets = [_item_net("na", "a", "sa", "m"), _item_net("nb", "b", "sb", "m")]
    with pytest.warns(AdapterWarning, match="cannot arrive on one run"):
        assert _feed_item_inputs(machines, nets, {"m"}) == (machines, nets)


def test_an_item_named_items_still_joins_the_run() -> None:
    # Its own port goes with the merge, so the run's port id is free again.
    ir = _adapt(_mixer_plan(items=("items", "dust.a", "dust.b")))
    (run,) = _runs(ir)
    assert run.items == ("dust.a", "dust.b", "items")
    ports = [p.id for p in _machine(ir, "m").faces.ports]
    assert ports.count("input:items") == 1


# ------------------------------------------------------------------ the shipped examples


@pytest.mark.parametrize("path", _EXAMPLES, ids=[p.stem for p in _EXAMPLES])
def test_no_shipped_example_merges_its_inputs(path: Path) -> None:
    # REGRESSION: every example lays out today, so none may change.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", AdapterWarning)
        ir = adapt_file(path)
    assert not [
        n for n in ir.nets if n.items and any(e.port_id == "input:items" for e in n.endpoints)
    ]


# ------------------------------------------------------------------ it builds


def test_a_solved_feed_run_is_a_valid_layout() -> None:
    # The router lays the run from three storages into the machine's one face, and the validator,
    # which refuses a merged run whose consumer is not an Item Filter, accepts it as a feed.
    ir = _adapt(_mixer_plan())
    layout = solve(ir)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert validate(ir, layout).ok
