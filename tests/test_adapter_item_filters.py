"""Tests for merging a single block's item outputs through Item Filters (#249).

A single block has five faces that can carry a connection, one each, so an Ore Washer with an item
in, a fluid in, three item outputs and power cannot be built as the plan draws it. The adapter sends
such a machine's items out of one face on a trunk and places an Item Filter per item to sort them
(``adapter.core._merge_item_outputs``). What these pin:

- **when** it merges: PROVEN a single block (a ``single`` handler, or a census miss for the plan's
  own pack) with two or more item outputs, whether or not it has a face to spare, and never when
  the line's items ride ME;
- **the shape** every later stage relies on: filter and trunk ids, types, face pins, rates, and
  one trunk and one filter per item for a whole parallel node, shared by its machines;
- **what moves**: each downstream net is sourced by its item's filter, nets one filter sources fold
  into one, and a line that merges nothing (every shipped example) is untouched.
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
    plan_digest,
    to_input_ir,
)
from gtnh_solver.adapter.core import _bounding_region, _fold_nets
from gtnh_solver.adapter.plan import RecipeSource
from gtnh_solver.dataset import (
    DatasetMeta,
    MachinePhysical,
    PhysicalDataset,
    load_physical_dataset,
)
from gtnh_solver.dataset.schema import SCHEMA_VERSION
from gtnh_solver.ir import (
    CellBox,
    Commodity,
    FaceSpec,
    Facing,
    InputIR,
    IODirection,
    Machine,
    MachineFaceRef,
    MEMode,
    MENetworkSpec,
    MEPlan,
    Net,
    Port,
    RelativeFace,
)
from gtnh_solver.ir.enums import HORIZONTAL_FACINGS_ORDERED
from gtnh_solver.ir.nets import SINGLE_BLOCK_IO_FACES, connection_counts
from gtnh_solver.placement import single_block_shortfalls

_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLES = sorted((_ROOT / "examples").glob("*.json"))
_ITEMS = ("gt.dust.c", "gt.dust.a", "gt.dust.b")  # deliberately unsorted: the trunk sorts them
_PLAN_PACK = "2.8.4"


def _washer_plan(
    *,
    count: int = 3,
    handler: str | None = "single",
    items: tuple[str, ...] = _ITEMS,
    fluids: tuple[str, ...] = (),
    drained: tuple[str, ...] | None = None,
    second_drain: str | None = None,
) -> Plan:
    """An Ore Washer node of ``count`` machines: item + fluid in, ``items`` out, powered at LV.

    Each item in ``drained`` (default: all of them) goes to a storage of its own, and
    ``second_drain`` sends one item to a second storage too, so its output feeds two nets. An item
    that is not drained, and every fluid output, is collected by an output buffer.
    """
    handlers = (
        [MachineHandler(id="h", kind=handler, label="Ore Washer", machine_type="Ore Washer")]
        if handler is not None
        else []
    )
    recipe = Recipe(
        id="r",
        machine_type="Ore Washer",
        eut=16.0,
        duration_ticks=100.0,
        inputs=[
            Resource(kind="item", id="crushed", amount=1.0),
            Resource(kind="fluid", id="water", amount=1000.0),
        ],
        outputs=[
            *(Resource(kind="item", id=item, amount=1.0) for item in items),
            *(Resource(kind="fluid", id=fluid, amount=100.0) for fluid in fluids),
        ],
        source=RecipeSource(
            dataset_version_id=f"stable-{_PLAN_PACK}", raw_recipe_id="gt.recipe.orewasher:abc"
        ),
        machine_handlers=handlers,
    )
    storages = [Storage(id="ore", kind="item"), Storage(id="water", kind="fluid")]
    edges = [
        Edge(id="e-ore", source="ore", target="w", resource_kind="item", resource_id="crushed"),
        Edge(id="e-water", source="water", target="w", resource_kind="fluid", resource_id="water"),
    ]
    for item in items if drained is None else drained:
        storages.append(Storage(id=f"drain-{item}", kind="item"))
        edges.append(
            Edge(
                id=f"e-{item}",
                source="w",
                target=f"drain-{item}",
                resource_kind="item",
                resource_id=item,
            )
        )
    if second_drain is not None:
        storages.append(Storage(id="drain-2", kind="item"))
        edges.append(
            Edge(
                id="e-second",
                source="w",
                target="drain-2",
                resource_kind="item",
                resource_id=second_drain,
            )
        )
    return Plan(
        schema_version=1,
        recipes=[recipe],
        nodes=[Node(id="w", recipe_id="r", overclock_tier="LV", machine_count=count)],
        storages=storages,
        edges=edges,
    )


def _dataset(
    *records: MachinePhysical, census: bool = True, pack_version: str = _PLAN_PACK
) -> PhysicalDataset:
    """A structure dump holding ``records`` (by default none, so the washer misses it)."""
    return PhysicalDataset(
        meta=DatasetMeta.model_validate(
            {
                "schema": SCHEMA_VERSION,
                "pack_version": pack_version,
                "generated_at": "2026-01-01T00:00:00Z",
                "extractor_sha": "0" * 40,
                "controller_count": len(records),
                "census": census,
            }
        ),
        machines={record.key: record for record in records},
        records=records,
    )


def _washer_record() -> MachinePhysical:
    return MachinePhysical(
        key="Ore Washer",
        registry_name="test:block",
        meta=0,
        source_class="test.Controller",
        footprint=CellBox(sx=3, sy=3, sz=3),
        io_faces=frozenset({Facing.NORTH}),
        hint_layers=frozenset({0}),
        coil_layer_count=0,
        variant_count=1,
        hatch_cells=8,
        energy_hatch_cells=8,
        upkeep_hatch_count=1,
    )


def _adapt(plan: Plan, **kwargs: object) -> InputIR:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", AdapterWarning)  # pack/census notes this file is not about
        return to_input_ir(plan, **kwargs)  # type: ignore[arg-type]


def _machine(ir: InputIR, machine_id: str) -> Machine:
    return next(m for m in ir.machines if m.id == machine_id)


def _net(ir: InputIR, net_id: str) -> Net:
    return next(n for n in ir.nets if n.id == net_id)


def _filters(ir: InputIR) -> list[Machine]:
    return [m for m in ir.machines if m.filter_items]


def _trunks(ir: InputIR) -> list[Net]:
    return [n for n in ir.nets if n.items]


def _ref(machine_id: str, port_id: str) -> MachineFaceRef:
    return MachineFaceRef(machine_id=machine_id, port_id=port_id)


def _assert_unmerged(ir: InputIR) -> None:
    assert not _filters(ir)
    assert not _trunks(ir)
    for machine_id in ("w#1", "w#2", "w#3"):
        outputs = {p.id for p in _machine(ir, machine_id).faces.ports}
        assert {f"output:{item}" for item in _ITEMS} <= outputs
        assert "output:items" not in outputs


# ------------------------------------------------------------------ the merged shape


def test_a_parallel_node_shares_one_trunk_and_one_filter_per_item() -> None:
    # Three washers of one node: one trunk from all three output faces to three filters, not nine.
    ir = _adapt(_washer_plan())
    assert [m.id for m in _filters(ir)] == [f"item-filter:w:{item}" for item in sorted(_ITEMS)]
    assert [t.id for t in _trunks(ir)] == ["item-trunk:w"]
    assert single_block_shortfalls(ir) == {}


def test_the_merged_machine_keeps_one_item_output_rated_at_the_sum() -> None:
    unmerged = _adapt(_washer_plan(handler=None))
    rates = {p.id: p.rate for p in _machine(unmerged, "w#1").faces.ports}
    washer = _machine(_adapt(_washer_plan()), "w#1")
    ports = {p.id: p for p in washer.faces.ports}
    assert not {f"output:{item}" for item in _ITEMS} & ports.keys()
    merged = ports["output:items"]
    assert (merged.commodity, merged.direction, merged.faces) == (
        Commodity.ITEM,
        IODirection.OUTPUT,
        None,
    )
    assert merged.rate == pytest.approx(sum(rates[f"output:{item}"] or 0.0 for item in _ITEMS))
    # Its inputs and power are untouched.
    assert {"input:crushed", "input:water"} <= ports.keys()
    assert washer.power_input_ports


def test_each_filter_is_an_unpowered_ulv_item_filter_pinned_front_in_back_out() -> None:
    ir = _adapt(_washer_plan())
    unmerged = _adapt(_washer_plan(handler=None))
    rates = [
        next(p.rate for p in _machine(unmerged, f"w#{i}").faces.ports if p.id == "output:gt.dust.a")
        for i in (1, 2, 3)
    ]
    item_filter = _machine(ir, "item-filter:w:gt.dust.a")
    assert item_filter.type == "Ultra Low Voltage Item Filter"
    assert item_filter.block_key == "gregtech:gt.blockmachines@9240"
    assert (item_filter.voltage_tier, item_filter.eut) == ("ULV", 0.0)
    assert item_filter.footprint == CellBox()
    assert item_filter.orientation_options == list(HORIZONTAL_FACINGS_ORDERED)
    assert item_filter.filter_items == ("gt.dust.a",)
    assert not item_filter.power_input_ports
    ports = {p.id: p for p in item_filter.faces.ports}
    assert ports.keys() == {"input:gt.dust.a", "output:gt.dust.a"}
    taken, passed = ports["input:gt.dust.a"], ports["output:gt.dust.a"]
    assert (taken.commodity, taken.direction) == (Commodity.ITEM, IODirection.INPUT)
    assert taken.faces == (
        RelativeFace.FRONT,
        RelativeFace.LEFT,
        RelativeFace.RIGHT,
        RelativeFace.UP,
        RelativeFace.DOWN,
    )
    assert (passed.commodity, passed.direction) == (Commodity.ITEM, IODirection.OUTPUT)
    assert passed.faces == (RelativeFace.BACK,)
    # It sorts every machine's share of the item, so it is rated at their sum.
    assert taken.rate == passed.rate == pytest.approx(sum(rate or 0.0 for rate in rates))


def test_the_trunk_carries_every_item_from_the_machines_to_their_filters() -> None:
    ir = _adapt(_washer_plan())
    trunk = _net(ir, "item-trunk:w")
    ordered = tuple(sorted(_ITEMS))
    assert trunk.commodity is Commodity.ITEM
    assert (trunk.items, trunk.fluid_or_item) == (ordered, None)
    assert trunk.endpoints == [
        *(_ref(f"w#{i}", "output:items") for i in (1, 2, 3)),
        *(_ref(f"item-filter:w:{item}", f"input:{item}") for item in ordered),
    ]
    merged = [
        next(p.rate for p in _machine(ir, f"w#{i}").faces.ports if p.id == "output:items")
        for i in (1, 2, 3)
    ]
    assert trunk.throughput == pytest.approx(sum(rate or 0.0 for rate in merged))


def test_each_filter_follows_its_machine() -> None:
    ids = [m.id for m in _adapt(_washer_plan(count=1)).machines]
    at = ids.index("w")
    assert ids[at : at + 4] == ["w", *(f"item-filter:w:{item}" for item in sorted(_ITEMS))]


def test_a_parallel_nodes_filters_follow_its_last_machine() -> None:
    ids = [m.id for m in _adapt(_washer_plan()).machines]
    at = ids.index("w#1")
    assert ids[at : at + 6] == [
        "w#1",
        "w#2",
        "w#3",
        *(f"item-filter:w:{item}" for item in sorted(_ITEMS)),
    ]


def test_downstream_nets_are_sourced_by_the_filters_at_the_same_rate() -> None:
    unmerged = _net(_adapt(_washer_plan(handler=None)), "e-gt.dust.a")
    net = _net(_adapt(_washer_plan()), "e-gt.dust.a")
    # The three machines shared this net already; their one filter sources it once.
    assert net.endpoints == [
        _ref("item-filter:w:gt.dust.a", "output:gt.dust.a"),
        _ref("drain-gt.dust.a", "input:gt.dust.a"),
    ]
    assert net.throughput == unmerged.throughput
    assert net.fluid_or_item == "gt.dust.a"


def test_nets_one_filter_sources_fold_into_one() -> None:
    # Two edges off one output were two nets, fine on a face each; a filter's back feeds one pipe.
    unmerged = _adapt(_washer_plan(handler=None, second_drain="gt.dust.a"))
    ir = _adapt(_washer_plan(second_drain="gt.dust.a"))
    assert not any(n.id in {"e-gt.dust.a", "e-second"} for n in ir.nets)
    net = _net(ir, "e-gt.dust.a+e-second")
    assert net.endpoints == [
        _ref("item-filter:w:gt.dust.a", "output:gt.dust.a"),
        _ref("drain-gt.dust.a", "input:gt.dust.a"),
        _ref("drain-2", "input:gt.dust.a"),
    ]
    # The producers' rate once, the figure each of the two nets already carried.
    assert net.throughput == pytest.approx(_net(unmerged, "e-gt.dust.a").throughput)
    assert net.throughput == pytest.approx(_net(unmerged, "e-second").throughput)


def test_an_unconsumed_output_is_collected_from_its_filters() -> None:
    ir = _adapt(_washer_plan(drained=()))
    buffer_net = _net(ir, "output-net:w:gt.dust.b")
    assert buffer_net.endpoints == [
        _ref("item-filter:w:gt.dust.b", "output:gt.dust.b"),
        _ref("output-buffer:w:gt.dust.b", "input:gt.dust.b"),
    ]


def test_only_item_outputs_merge() -> None:
    # A fluid output keeps its own face: there is no fluid filter block.
    ir = _adapt(_washer_plan(fluids=("sludge",)))
    ports = {p.id for p in _machine(ir, "w#1").faces.ports}
    assert {"output:items", "output:sludge"} <= ports
    assert _net(ir, "item-trunk:w").items == tuple(sorted(_ITEMS))
    assert _net(ir, "output-net:w:sludge").endpoints[0] == _ref("w#1", "output:sludge")


def test_a_single_machine_node_names_its_filters_by_its_bare_id() -> None:
    ir = _adapt(_washer_plan(count=1))
    assert [m.id for m in _filters(ir)] == [f"item-filter:w:{item}" for item in sorted(_ITEMS)]
    assert [t.id for t in _trunks(ir)] == ["item-trunk:w"]


def test_the_filters_are_inside_the_bounding_region() -> None:
    # The region is sized after the merge, from every footprint the filters included.
    ir = _adapt(_washer_plan())
    assert ir.bounding_region == _bounding_region([m.footprint for m in ir.machines])
    assert ir.bounding_region.sx >= 2 * len(ir.machines)


def test_a_merged_problem_round_trips_through_its_own_serialization() -> None:
    ir = _adapt(_washer_plan())
    assert InputIR.model_validate_json(ir.model_dump_json()) == ir


# ------------------------------------------------------------------ what proves a single block


def test_a_census_miss_for_the_plans_own_pack_proves_a_single_block() -> None:
    ir = _adapt(_washer_plan(handler=None), physical=_dataset())
    assert len(_filters(ir)) == 3


def test_a_census_for_another_pack_proves_nothing() -> None:
    # Names move between packs, so another pack's census missing the machine is no evidence.
    ir = _adapt(_washer_plan(handler=None), physical=_dataset(pack_version="2.9.0-beta-2"))
    _assert_unmerged(ir)


def test_a_sample_dump_proves_nothing() -> None:
    ir = _adapt(_washer_plan(handler=None), physical=_dataset(census=False))
    _assert_unmerged(ir)


def test_no_evidence_leaves_the_machine_short_of_faces() -> None:
    # No handler and no census: the 1x1x1 box may be a multiblock whose structure is missing, and
    # filters on one would be nonsense. The solver keeps reporting it instead.
    ir = _adapt(_washer_plan(handler=None))
    _assert_unmerged(ir)
    assert single_block_shortfalls(ir) == {"w#1": 6, "w#2": 6, "w#3": 6}


def test_a_multiblock_handler_never_merges() -> None:
    ir = _adapt(_washer_plan(handler="multiblock"), physical=_dataset())
    _assert_unmerged(ir)


def test_a_structure_record_never_merges() -> None:
    ir = _adapt(_washer_plan(handler="single"), physical=_dataset(_washer_record()))
    assert _machine(ir, "w#1").footprint == CellBox(sx=3, sy=3, sz=3)
    _assert_unmerged(ir)


# ------------------------------------------------------------------ what needs merging


def test_a_machine_with_no_face_to_spare_merges() -> None:
    # Item in, fluid in, two item outputs, power: five connections on five faces. Not a shortfall,
    # but every face would need a route, top and bottom included, which is what kept iron.json's
    # first Macerator from routing on every seed; merged, it has two faces free.
    unmerged = _adapt(_washer_plan(items=("gt.dust.a", "gt.dust.b"), handler=None))
    assert connection_counts(unmerged.nets)["w#1"] == SINGLE_BLOCK_IO_FACES
    assert not single_block_shortfalls(unmerged)
    ir = _adapt(_washer_plan(items=("gt.dust.a", "gt.dust.b")))
    assert len(_filters(ir)) == 2
    assert [t.items for t in _trunks(ir)] == [("gt.dust.a", "gt.dust.b")]
    assert connection_counts(ir.nets)["w#1"] == SINGLE_BLOCK_IO_FACES - 1


def test_a_machine_with_a_face_to_spare_merges_too() -> None:
    # The same two item outputs with the water on ME: four connections, a face to spare. It still
    # ejects both items through one output face, so it merges; kept a face per output, the second
    # would need a cover pulling it out.
    unmerged = _adapt(
        _washer_plan(items=("gt.dust.a", "gt.dust.b"), handler=None),
        me_commodities={Commodity.FLUID},
    )
    assert connection_counts(unmerged.nets)["w#1"] == SINGLE_BLOCK_IO_FACES - 1
    ir = _adapt(_washer_plan(items=("gt.dust.a", "gt.dust.b")), me_commodities={Commodity.FLUID})
    assert [m.id for m in _filters(ir)] == ["item-filter:w:gt.dust.a", "item-filter:w:gt.dust.b"]
    assert [t.items for t in _trunks(ir)] == [("gt.dust.a", "gt.dust.b")]
    assert _net(ir, "e-gt.dust.a").endpoints[0] == _ref(
        "item-filter:w:gt.dust.a", "output:gt.dust.a"
    )


def test_items_on_me_need_no_faces_and_no_merge() -> None:
    ir = _adapt(_washer_plan(), me_commodities={Commodity.ITEM})
    _assert_unmerged(ir)


def test_an_output_on_me_is_left_out_of_the_merge() -> None:
    # ME is chosen per net (#332): one product rides ME, so the trunk and its filters sort the
    # other two, and the net on ME keeps its id and its machines' own ports. The washers still eject
    # every item through one face; a GT pipe takes from it only what a filter accepts.
    plan = _washer_plan()
    me_plan = MEPlan(
        plan_digest=plan_digest(plan),
        networks=[MENetworkSpec(id="main", mode=MEMode.ATTACHED)],
        nets={"e-gt.dust.c": "main"},
    )
    ir = _adapt(plan, me_plan=me_plan)
    assert [m.id for m in _filters(ir)] == ["item-filter:w:gt.dust.a", "item-filter:w:gt.dust.b"]
    assert [t.items for t in _trunks(ir)] == [("gt.dust.a", "gt.dust.b")]
    on_me = _net(ir, "e-gt.dust.c")
    assert on_me.me_network == "main"
    assert on_me.endpoints[:3] == [_ref(f"w#{i}", "output:gt.dust.c") for i in (1, 2, 3)]
    for machine_id in ("w#1", "w#2", "w#3"):
        outputs = {p.id for p in _machine(ir, machine_id).faces.ports}
        assert {"output:gt.dust.c", "output:items"} <= outputs
        assert "output:gt.dust.a" not in outputs


def test_one_item_output_has_nothing_to_sort() -> None:
    # Short of faces through its fluids, with a single item output: a filter would sort nothing.
    ir = _adapt(_washer_plan(items=("gt.dust.a",), fluids=("f1", "f2", "f3")))
    assert not _filters(ir)
    assert single_block_shortfalls(ir)


def test_a_fluid_holding_the_merged_port_id_blocks_the_merge() -> None:
    with pytest.warns(AdapterWarning, match="cannot merge"):
        ir = to_input_ir(_washer_plan(fluids=("items",)))
    assert not _filters(ir)


def test_an_item_named_items_still_merges() -> None:
    # Its own port goes with the merge, so the merged port's id is free again.
    ir = _adapt(_washer_plan(count=1, items=("items", "gt.dust.a", "gt.dust.b")))
    ports = [p.id for p in _machine(ir, "w").faces.ports]
    assert ports.count("output:items") == 1
    assert _net(ir, "item-trunk:w").items == ("gt.dust.a", "gt.dust.b", "items")
    assert _net(ir, "e-items").endpoints[0] == _ref("item-filter:w:items", "output:items")


# ------------------------------------------------------------------ the shipped examples


def _committed_dataset() -> PhysicalDataset:
    # By path, not by resolution: a checkout with a local dump would otherwise resolve that one.
    return load_physical_dataset(_ROOT / "data" / "multiblocks")


@pytest.mark.parametrize("with_data", [False, True], ids=["no-dataset", "committed-data"])
@pytest.mark.parametrize("path", _EXAMPLES, ids=[p.stem for p in _EXAMPLES])
def test_no_shipped_example_merges(path: Path, with_data: bool) -> None:
    # REGRESSION: every example must adapt exactly as before; none has a machine that needs it.
    physical = _committed_dataset() if with_data else None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", AdapterWarning)
        ir = adapt_file(path, physical=physical)
    assert not _filters(ir)
    assert not _trunks(ir)
    assert not any(m.type == "Ultra Low Voltage Item Filter" for m in ir.machines)


def test_a_fold_with_an_unrated_producer_keeps_the_largest_members_rate() -> None:
    # Every producer the adapter builds is rated; one that is not must not be read as zero flow.
    def _item_machine(mid: str, port_id: str, direction: IODirection) -> Machine:
        port = Port(id=port_id, commodity=Commodity.ITEM, direction=direction)
        return Machine(
            id=mid,
            type="t",
            voltage_tier="LV",
            orientation_options=[Facing.NORTH],
            faces=FaceSpec(ports=[port]),
        )

    machines = [
        _item_machine("f", "output:x", IODirection.OUTPUT),
        _item_machine("a", "input:x", IODirection.INPUT),
        _item_machine("b", "input:x", IODirection.INPUT),
    ]
    source = _ref("f", "output:x")
    nets = [
        Net(
            id=nid,
            commodity=Commodity.ITEM,
            fluid_or_item="x",
            throughput=rate,
            endpoints=[source, _ref(sink, "input:x")],
        )
        for nid, sink, rate in (("n1", "a", 2.0), ("n2", "b", 3.0))
    ]
    (folded,) = _fold_nets(nets, {source}, machines)
    assert folded.id == "n1+n2"
    assert folded.throughput == 3.0
