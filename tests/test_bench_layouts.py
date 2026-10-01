"""``tools/bench_layouts.py``: the numbers every layout-quality change is judged on.

The tool decides go or no-go for placer, router and solver changes, so a quiet error in it is a
wrong decision with a plausible table behind it. Three things are pinned: the **key** it reads off a
layout's JSON is the solver's own ranking (``structure_quality``), the **summary and pairing maths**
count VALID seeds, medians, wins and flips the way its docstring says, and the **seed parsing and
spacing guard** catch the overlapping-seed trap that once counted one lucky anneal many times.
Running the CLI itself is exercised by running the tool.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from gtnh_solver.ir import InputIR, LayoutResult
from gtnh_solver.solver._structure import structure_quality

_REPO = Path(__file__).resolve().parents[1]
_TOOL = _REPO / "tools" / "bench_layouts.py"


def _tool() -> ModuleType:
    """``tools/bench_layouts.py`` imported as a module (``tools/`` is not a package)."""
    spec = importlib.util.spec_from_file_location("bench_layouts", _TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bench = _tool()


def _row(
    plan: str, seed: int, status: str = "valid", key: int = 100, **more: Any
) -> dict[str, Any]:
    """One result row with every metric set; ``key`` drives the comparisons."""
    row = {
        "plan": plan,
        "seed": seed,
        "status": status,
        "floor": key - 40,
        "layers": 3,
        "route": 40,
        "pipe": 30,
        "cable": 10,
        "key": key,
        "time": 2.0,
    }
    row.update(more)
    return row


# ------------------------------------------------------------------------------ the key


@pytest.mark.parametrize("fixture", ["solved_sand", "solved_nitrobenzene"])
def test_key_is_the_solvers_footprint_ranking(fixture: str, request: pytest.FixtureRequest) -> None:
    """Read off the layout JSON alone, the key is ``structure_quality``'s lead term for the default
    objective: floor area plus every route cell. Nitrobenzene lays pipes, sand only cable."""
    problem, layout = request.getfixturevalue(fixture)
    assert isinstance(problem, InputIR)
    assert isinstance(layout, LayoutResult)
    row = bench.layout_row(json.loads(layout.model_dump_json()))
    quality = structure_quality(problem, layout.placements, layout.routes, "footprint")
    assert row["key"] == quality[0]
    assert row["floor"] == quality[1]
    every = set().union(*(r.cells() for r in layout.routes))
    assert row["route"] == len(every)
    assert row["pipe"] + row["cable"] == row["route"]


def test_route_cells_read_a_one_block_pipe_off_its_terminals() -> None:
    """A pipe with no segments is one block, the cell its terminals share (``Route.cells``)."""
    cell = {"x": 1, "y": 2, "z": 3}
    route = {"terminals": [{"cell": cell}, {"cell": cell}], "segments": []}
    assert bench.route_cells(route) == {(1, 2, 3)}


def test_layout_row_splits_route_cells_by_class() -> None:
    def hop(x: int) -> dict[str, Any]:
        return {"start": {"x": x, "y": 0, "z": 0}, "end": {"x": x + 1, "y": 0, "z": 0}}

    layout = {
        "status": "valid",
        "metrics": {"footprint": 10, "layers": 2},
        "routes": [
            {"net_id": "t", "commodity": "item", "segments": [hop(0)]},
            {"net_id": "p", "commodity": "power", "segments": [hop(5), hop(6)]},
            {"net_id": "unknown", "commodity": "fluid", "segments": [hop(10)]},
        ],
    }
    row = bench.layout_row(layout, {"t": "trunk", "p": "cable"})
    assert (row["route"], row["pipe"], row["cable"], row["key"]) == (7, 4, 3, 17)
    assert row["classes"] == {
        "trunk": 2,
        "filter_machine": 0,
        "filter_chest": 0,
        "cable": 3,
        "other": 2,
    }


def test_classify_nets() -> None:
    def ends(*kinds: str) -> list[dict[str, bool]]:
        return [{"filter": k == "filter", "storage": k == "chest"} for k in kinds]

    nets = [
        {"id": "power:LV", "commodity": "power", "endpoints": ends("machine", "machine")},
        {"id": "item-trunk:n1", "commodity": "item", "endpoints": ends("machine", "filter")},
        {"id": "e1", "commodity": "item", "endpoints": ends("filter", "chest")},
        {"id": "e2", "commodity": "item", "endpoints": ends("filter", "machine", "machine")},
        {"id": "e3", "commodity": "fluid", "endpoints": ends("chest", "machine")},
    ]
    assert bench.classify_nets(nets) == {
        "power:LV": "cable",
        "item-trunk:n1": "trunk",
        "e1": "filter_chest",
        "e2": "filter_machine",
        "e3": "other",
    }


# ------------------------------------------------------------------------------ the maths


def test_summary_counts_every_row_and_takes_medians_over_valid_ones() -> None:
    rows = [
        _row("iron", 0, key=100),
        _row("iron", 100, key=120),
        _row("iron", 200, key=110),
        _row("iron", 300, status="partial_invalid", key=50),
        _row("sand", 0, key=7),
    ]
    summary = bench.summarize(rows)
    assert (summary["iron"]["n"], summary["iron"]["valid"]) == (4, 3)
    assert summary["iron"]["key"] == 110  # the partial layout's 50 is not a candidate
    assert summary["sand"]["valid"] == 1
    assert summary["iron"]["rounds"] is None


def test_summary_of_a_plan_with_no_valid_seed_has_no_medians() -> None:
    summary = bench.summarize([_row("iron", 0, status="partial_invalid")])
    assert summary["iron"]["valid"] == 0
    assert summary["iron"]["key"] is None
    assert "VALID  0/1" in bench.format_summary(summary)


def test_compare_pairs_seeds_and_counts_wins_ties_losses_and_flips() -> None:
    a = [
        _row("iron", 0, key=100),
        _row("iron", 100, key=100),
        _row("iron", 200, key=100),
        _row("iron", 300, key=100),
        _row("iron", 400, status="partial_invalid"),
        _row("iron", 500, key=100),  # b never ran it: unpaired, left out
    ]
    b = [
        _row("iron", 0, key=90),
        _row("iron", 100, key=96),
        _row("iron", 200, key=100),
        _row("iron", 300, status="partial_invalid"),
        _row("iron", 400, key=80),
    ]
    entry = bench.compare(a, b)["iron"]
    assert entry["pairs"] == 5
    assert (entry["valid_a"], entry["valid_b"]) == (4, 4)
    assert (entry["gained"], entry["lost"]) == ([400], [300])
    assert (entry["wins"], entry["ties"], entry["losses"]) == (2, 1, 0)
    assert entry["d_key"] == -4  # median of -10, -4, 0 over the seeds both laid VALID
    assert entry["per_seed"] == [(0, -10), (100, -4), (200, 0)]
    table = bench.format_compare({"iron": entry}, per_seed=True)
    assert "gained on seeds [400], lost on [300]" in table
    assert "0:-10, 100:-4, 200:+0" in table


def test_read_rows_takes_a_folder_or_its_rows_file(tmp_path: Path) -> None:
    rows = [_row("iron", 0), _row("iron", 100)]
    (tmp_path / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert bench.read_rows(tmp_path) == rows
    assert bench.read_rows(tmp_path / "rows.jsonl") == rows
    assert bench.read_rows(tmp_path / "missing") == []


# ------------------------------------------------------------------------------ the seeds


@pytest.mark.parametrize(
    ("text", "seeds"),
    [
        ("0:1600:100", list(range(0, 1600, 100))),
        ("0:3", [0, 1, 2]),
        ("0,100,300", [0, 100, 300]),
        ("7", [7]),
    ],
)
def test_parse_seeds(text: str, seeds: list[int]) -> None:
    assert bench.parse_seeds(text) == seeds


@pytest.mark.parametrize("text", ["5:5", "1:2:3:4", ","])
def test_parse_seeds_refuses_no_seeds_or_a_bad_range(text: str) -> None:
    with pytest.raises(ValueError, match="seed"):
        bench.parse_seeds(text)


def test_spacing_warning_names_seeds_that_share_attempts() -> None:
    assert bench.spacing_warning([0, 100, 200], attempts=8, rounds=1) is None
    assert bench.spacing_warning([0, 100], attempts=8, rounds=12) is None
    warning = bench.spacing_warning([0, 100], attempts=8, rounds=13)
    assert warning is not None
    assert "at least 104 apart" in warning
    assert bench.spacing_warning([0, 1], attempts=1, rounds=1) is None
    assert bench.spacing_warning([5], attempts=8, rounds=64) is None
