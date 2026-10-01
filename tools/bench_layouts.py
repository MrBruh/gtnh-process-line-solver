"""Benchmark the layouts a checkout lays, and pair two benchmarks seed by seed.

A change to the placer, the router or the solver is judged on what it does to real lines across many
seeds, never on one solve: a single seed swings by tens of route cells on its own. This runs the
``gtnh-solve`` CLI on each plan at each seed, one solve at a time, records what every layout cost,
and pairs two such runs per seed so the go/no-go is a table of wins, ties, losses and VALID flips
rather than two medians eyeballed side by side::

    PLAN x SEED --> gtnh-solve (the --root checkout's code) --> rows.jsonl + layouts/<plan>-s<seed>.json
                                                                   |
                    --summary: VALID n/N, medians over VALID  <----+----> --compare A B: paired deltas

**Which code runs is the --root checkout's**, whichever venv runs this tool: the CLI is started with
``PYTHONPATH=<root>/src`` and the same interpreter, so one tool measures ``main`` and a branch alike.
That is checked before anything runs (``gtnh_solver`` must import from ``<root>/src``), because an
editable install of another checkout answering instead would benchmark the wrong code and look
fine. The dataset comes from ``<root>/data`` the same way, so a worktree needs the local dumps
linked in; a solve that warns the plan's pack has no local dump is flagged loudly, since its
multiblocks then reserve 1x1x1 footprints and the numbers mean nothing.

**The key is floor area plus route cells**, the ranking the solver itself uses for the default
``footprint`` objective (``solver._structure.structure_quality``), and a unit test pins the two
equal. It is computed from the layout JSON alone, without importing the IR, so this tool reads a
branch's layouts even when that branch has moved the contract on.

**Seeds must be spaced** at least attempts x rounds apart: ``solve(seed=s)`` anneals seeds ``s`` to
``s + 7`` at full effort, so seeds 0, 1, 2 share most of their attempts and one lucky anneal is
counted many times. The tool warns when they are not.

Usage (from any checkout's dev venv)::

    python tools/bench_layouts.py PLAN... --root DIR --out DIR [--seeds 0:1600:100]
        [--effort full] [--time-budget S | --rounds N] [--label L] [--classes]
    python tools/bench_layouts.py --summary DIR...
    python tools/bench_layouts.py --compare A B [--per-seed]

``--seeds`` takes ``start:stop:step`` (a range), a comma list, or one seed. ``--out`` is resumable:
a (plan, seed) already in its ``rows.jsonl`` is not solved again. ``--classes`` also splits each
layout's route cells into trunk, filter to machine, filter to chest, cable and other, which needs
the adapted problem, so the plan is adapted once more by the root's own adapter.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import statistics
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

#: Attempts per seed at each effort (``solver.core._BUDGETS``), for the seed-spacing warning only.
ATTEMPTS = {"full": 8, "minimal": 1}
#: What ``gtnh-solve`` prints when the plan's pack has no local dump (``cli._warn_if_plan_pack_undumped``).
UNDUMPED = "no local dump for the plan's pack"
#: Route classes, in report order. ``trunk`` is the adapter's shared ``item-trunk:`` run from a
#: machine's outputs to its Item Filters; ``filter_*`` is a filter's own output, into storage or into
#: the next machine; ``cable`` is every power route; ``other`` is every remaining pipe.
ROUTE_CLASSES = ("trunk", "filter_machine", "filter_chest", "cable", "other")
#: The per-layout numbers a summary takes medians of, in report order.
METRICS = ("floor", "layers", "route", "pipe", "cable", "key", "time")

Cell = tuple[int, int, int]
Row = dict[str, Any]


# ------------------------------------------------------------------------------ parsing


def parse_seeds(text: str) -> list[int]:
    """``0:1600:100`` is a range, ``0,100,300`` a list, ``7`` one seed."""
    if ":" in text:
        parts = [int(p) for p in text.split(":")]
        if len(parts) not in (2, 3):
            raise ValueError(f"seed range must be start:stop[:step], got {text!r}")
        seeds = list(range(*parts))
    else:
        seeds = [int(p) for p in text.split(",") if p.strip()]
    if not seeds:
        raise ValueError(f"no seeds in {text!r}")
    return seeds


def spacing_warning(seeds: Sequence[int], attempts: int, rounds: int) -> str | None:
    """Why ``seeds`` share attempts, or None. One solve anneals ``attempts * rounds`` seeds from its
    own, so two solves closer than that anneal some of the same seeds."""
    window = attempts * rounds
    ordered = sorted(set(seeds))
    gaps = [b - a for a, b in itertools.pairwise(ordered)]
    if gaps and min(gaps) < window:
        return (
            f"seeds {min(gaps)} apart share attempts: each solve anneals {window} seeds "
            f"({attempts} attempts x {rounds} rounds), so space them at least {window} apart"
        )
    return None


# ------------------------------------------------------------------------------ one layout


def route_cells(route: dict[str, Any]) -> set[Cell]:
    """Every cell a route occupies, read off its JSON exactly as ``Route.cells`` reads the model:
    both ends of each hop, or its terminals' cells for a route with no segments (a one-block pipe)."""

    def cell(c: dict[str, int]) -> Cell:
        return (c["x"], c["y"], c["z"])

    segments = route.get("segments") or []
    if not segments:
        return {cell(t["cell"]) for t in route.get("terminals", [])}
    return {cell(s[end]) for s in segments for end in ("start", "end")}


def classify_nets(nets: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Each net id's route class (``ROUTE_CLASSES``), from the descriptors ``net_kinds`` prints."""
    classes: dict[str, str] = {}
    for net in nets:
        ends = net["endpoints"]
        if net["commodity"] == "power":
            kind = "cable"
        elif net["id"].startswith("item-trunk:"):
            kind = "trunk"
        elif any(e["filter"] for e in ends):
            rest = [e for e in ends if not e["filter"]]
            kind = "filter_chest" if rest and all(e["storage"] for e in rest) else "filter_machine"
        else:
            kind = "other"
        classes[net["id"]] = kind
    return classes


def layout_row(layout: dict[str, Any], classes: dict[str, str] | None = None) -> Row:
    """What one layout cost: floor, layers, route cells (pipe and cable apart), and the key.

    ``floor`` and ``layers`` are the solver's own ``metrics``, measured over machine and route
    cells alike; ``key`` adds the route cells to the floor, the solver's ranking for ``footprint``.
    With ``classes`` (net id to class) the route cells are split by class as well."""
    routes = layout.get("routes", [])
    every: set[Cell] = set()
    cable: set[Cell] = set()
    by_class: dict[str, set[Cell]] = {c: set() for c in ROUTE_CLASSES}
    for route in routes:
        cells = route_cells(route)
        every |= cells
        if route["commodity"] == "power":
            cable |= cells
        if classes is not None:
            by_class[classes.get(route["net_id"], "other")] |= cells
    metrics = layout.get("metrics") or {}
    floor = metrics.get("footprint")
    row: Row = {
        "status": layout["status"],
        "floor": floor,
        "layers": metrics.get("layers"),
        "route": len(every),
        "pipe": len(every - cable),
        "cable": len(cable),
        "key": None if floor is None else floor + len(every),
        "rounds": metrics.get("rounds"),
    }
    if classes is not None:
        row["classes"] = {c: len(cells) for c, cells in by_class.items()}
    return row


def net_kinds(plan: str) -> list[dict[str, Any]]:
    """Each net of ``plan`` as the CLI adapts it: id, commodity, and per endpoint whether it is an
    Item Filter or boundary storage. Runs inside the --root checkout's interpreter (``--net-kinds``),
    so the classes come from that checkout's adapter, which is what laid the routes being split."""
    from gtnh_solver.adapter import load_plan, resolve_producer, to_input_ir
    from gtnh_solver.cli import _dataset_version_for, _load_physical_or_warn
    from gtnh_solver.system_io import is_boundary_storage

    loaded = load_plan(plan)
    physical = _load_physical_or_warn(_dataset_version_for(loaded, None))
    problem = to_input_ir(loaded, physical=physical, producer=resolve_producer(loaded, None))
    types = {m.id: m.type for m in problem.machines}
    return [
        {
            "id": net.id,
            "commodity": net.commodity.value,
            "endpoints": [
                {
                    "filter": "Item Filter" in types[e.machine_id],
                    "storage": is_boundary_storage(types[e.machine_id]),
                }
                for e in net.endpoints
            ],
        }
        for net in problem.nets
    ]


# ------------------------------------------------------------------------------ summaries


def _median(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return statistics.median(present) if present else None


def summarize(rows: Iterable[Row]) -> dict[str, dict[str, Any]]:
    """Per plan: the VALID count over every row, and the medians over the VALID rows only (a
    partial layout leaves nets unrouted, so its route cells are not comparable)."""
    by_plan: dict[str, list[Row]] = {}
    for row in rows:
        by_plan.setdefault(row["plan"], []).append(row)
    out: dict[str, dict[str, Any]] = {}
    for plan, plan_rows in by_plan.items():
        valid = [r for r in plan_rows if r["status"] == "valid"]
        summary: dict[str, Any] = {"n": len(plan_rows), "valid": len(valid)}
        for metric in METRICS:
            summary[metric] = _median(r.get(metric) for r in valid)
        summary["rounds"] = _median(r.get("rounds") for r in plan_rows)
        if any("classes" in r for r in valid):
            summary["classes"] = {
                c: _median(r["classes"][c] for r in valid if "classes" in r) for c in ROUTE_CLASSES
            }
        out[plan] = summary
    return out


def compare(a: Iterable[Row], b: Iterable[Row]) -> dict[str, dict[str, Any]]:
    """Per plan, ``b`` against ``a`` on the seeds both ran.

    A win is a seed both laid VALID where ``b``'s key is smaller; a flip is a seed VALID in one and
    not the other (``gained`` VALID in ``b``, ``lost``). Deltas are ``b - a`` medians over the
    seeds both laid VALID, so a negative key delta is an improvement."""
    rows_a = {(r["plan"], r["seed"]): r for r in a}
    rows_b = {(r["plan"], r["seed"]): r for r in b}
    out: dict[str, dict[str, Any]] = {}
    for plan, seed in sorted(rows_a.keys() & rows_b.keys()):
        ra, rb = rows_a[plan, seed], rows_b[plan, seed]
        entry = out.setdefault(
            plan,
            {"pairs": 0, "valid_a": 0, "valid_b": 0, "gained": [], "lost": [], "seeds": []},
        )
        entry["pairs"] += 1
        va, vb = ra["status"] == "valid", rb["status"] == "valid"
        entry["valid_a"] += va
        entry["valid_b"] += vb
        if vb and not va:
            entry["gained"].append(seed)
        if va and not vb:
            entry["lost"].append(seed)
        if va and vb:
            entry["seeds"].append((seed, ra, rb))
    for entry in out.values():
        both = entry.pop("seeds")
        deltas = [rb["key"] - ra["key"] for _, ra, rb in both]
        entry["wins"] = sum(d < 0 for d in deltas)
        entry["ties"] = sum(d == 0 for d in deltas)
        entry["losses"] = sum(d > 0 for d in deltas)
        for metric in METRICS:
            entry[f"d_{metric}"] = _median(_delta(ra, rb, metric) for _, ra, rb in both)
            entry[f"{metric}_a"] = _median(ra.get(metric) for _, ra, _ in both)
            entry[f"{metric}_b"] = _median(rb.get(metric) for _, _, rb in both)
        entry["per_seed"] = [(seed, rb["key"] - ra["key"]) for seed, ra, rb in both]
    return out


def _delta(a: Row, b: Row, metric: str) -> float | None:
    if a.get(metric) is None or b.get(metric) is None:
        return None
    return float(b[metric] - a[metric])


def _fmt(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:g}" if float(value).is_integer() else f"{value:.1f}"


def format_summary(summary: dict[str, dict[str, Any]], label: str = "") -> str:
    lines = []
    for plan, s in summary.items():
        head = f"{plan:34s} {label:14s} VALID {s['valid']:2d}/{s['n']:<2d}"
        body = "  ".join(f"{m} {_fmt(s[m])}" for m in METRICS if m != "time")
        seconds = "-" if s["time"] is None else f"{s['time']:.1f}s"
        line = f"{head}  {body}  time {seconds}"
        if s.get("rounds") is not None:
            line += f"  rounds {_fmt(s['rounds'])}"
        lines.append(line)
        if "classes" in s:
            parts = "  ".join(f"{c} {_fmt(v)}" for c, v in s["classes"].items())
            lines.append(f"{'':34s}   route by class: {parts}")
    return "\n".join(lines)


def format_compare(table: dict[str, dict[str, Any]], *, per_seed: bool = False) -> str:
    lines = [
        f"{'plan':34s} {'pairs':>5s}  {'VALID a->b':>10s}  {'w/t/l':>8s}  "
        f"{'key a':>7s} {'key b':>7s} {'d key':>6s}  {'d route':>7s} {'d pipe':>6s} "
        f"{'d cable':>7s} {'d floor':>7s}  {'time a':>6s} {'time b':>6s}"
    ]
    for plan, e in table.items():
        wtl = f"{e['wins']}/{e['ties']}/{e['losses']}"
        lines.append(
            f"{plan:34s} {e['pairs']:5d}  {e['valid_a']:>4d} -> {e['valid_b']:<4d}  {wtl:>8s}  "
            f"{_fmt(e['key_a']):>7s} {_fmt(e['key_b']):>7s} {_fmt(e['d_key']):>6s}  "
            f"{_fmt(e['d_route']):>7s} {_fmt(e['d_pipe']):>6s} {_fmt(e['d_cable']):>7s} "
            f"{_fmt(e['d_floor']):>7s}  {_fmt(e['time_a']):>6s} {_fmt(e['time_b']):>6s}"
        )
        if e["gained"] or e["lost"]:
            lines.append(f"{'':34s} VALID gained on seeds {e['gained']}, lost on {e['lost']}")
        if per_seed:
            deltas = ", ".join(f"{seed}:{d:+d}" for seed, d in e["per_seed"])
            lines.append(f"{'':34s} key b - a per seed: {deltas}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------ running


def read_rows(path: Path) -> list[Row]:
    """The rows of a run: ``path`` is its ``rows.jsonl`` or the ``--out`` folder holding it."""
    if path.is_dir():
        path = path / "rows.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run(cmd: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    """``cmd`` under the root's code, below normal priority on Windows (the maintainer's desktop
    stays responsive; the solver's own pool processes inherit it)."""
    flags = subprocess.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 0
    return subprocess.run(
        cmd,
        cwd=root,
        env=_env(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        creationflags=flags,
        check=False,
    )


def check_root(root: Path) -> None:
    """Refuse to run unless ``gtnh_solver`` imports from ``<root>/src`` under the root's env."""
    probe = _run([sys.executable, "-c", "import gtnh_solver; print(gtnh_solver.__file__)"], root)
    where = Path(probe.stdout.strip()).resolve() if probe.returncode == 0 else None
    src = (root / "src").resolve()
    if where is None or not where.is_relative_to(src):
        raise SystemExit(
            f"error: gtnh_solver imports from {where or probe.stderr.strip()!s}, not {src}; "
            "the benchmark would measure the wrong checkout"
        )


def _commit(root: Path) -> str:
    probe = subprocess.run(
        ["git", "-C", str(root), "describe", "--always", "--dirty"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.stdout.strip() or "unknown"


def _classes_for(plan: Path, root: Path) -> dict[str, str]:
    probe = _run([sys.executable, str(Path(__file__).resolve()), "--net-kinds", str(plan)], root)
    if probe.returncode != 0:
        raise SystemExit(f"error: could not adapt {plan} for --classes:\n{probe.stderr}")
    return classify_nets(json.loads(probe.stdout))


def solve_once(
    plan: Path,
    seed: int,
    root: Path,
    effort: str,
    extra: Sequence[str],
    classes: dict[str, str] | None,
    layouts: Path,
) -> Row:
    """One ``gtnh-solve`` run, timed, its layout saved and measured."""
    cmd = [sys.executable, "-m", "gtnh_solver.cli", str(plan), "--seed", str(seed)]
    cmd += ["--effort", effort, *extra]
    started = time.perf_counter()
    done = _run(cmd, root)
    elapsed = time.perf_counter() - started
    row: Row = {"plan": plan.stem, "seed": seed, "exit": done.returncode, "time": elapsed}
    row["undumped"] = UNDUMPED in done.stderr
    if done.returncode not in (0, 1) or not done.stdout.strip():
        row["status"] = "error"
        row["stderr"] = done.stderr[-2000:]
        return row
    (layouts / f"{plan.stem}-s{seed}.json").write_text(done.stdout, encoding="utf-8")
    row.update(layout_row(json.loads(done.stdout), classes))
    return row


def _row_line(row: Row) -> str:
    if row["status"] == "error":
        return f"{row['plan']} s{row['seed']}: error (exit {row['exit']}) {row['time']:.1f}s"
    line = (
        f"{row['plan']} s{row['seed']}: {row['status']:15s} floor {row['floor']} layers "
        f"{row['layers']} route {row['route']} (pipe {row['pipe']} cable {row['cable']}) "
        f"key {row['key']}  {row['time']:.1f}s"
    )
    if row.get("rounds") is not None:
        line += f"  rounds {row['rounds']}"
    return line


def run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    out = Path(args.out)
    layouts = out / "layouts"
    layouts.mkdir(parents=True, exist_ok=True)
    rows_path = out / "rows.jsonl"
    check_root(root)
    seeds = parse_seeds(args.seeds)
    extra: list[str] = []
    if args.time_budget is not None:
        extra += ["--time-budget", str(args.time_budget)]
    if args.rounds is not None:
        extra += ["--rounds", str(args.rounds)]
    attempts = ATTEMPTS.get(args.effort, 1)
    warning = spacing_warning(seeds, attempts, args.rounds or 1)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    label = args.label or root.name
    commit = _commit(root)
    done = {(r["plan"], r["seed"]) for r in read_rows(rows_path)}
    plans = [Path(p).resolve() for p in args.plans]
    with rows_path.open("a", encoding="utf-8") as sink:
        for plan in plans:
            classes = _classes_for(plan, root) if args.classes else None
            for seed in seeds:
                if (plan.stem, seed) in done:
                    continue
                row = solve_once(plan, seed, root, args.effort, extra, classes, layouts)
                row.update(label=label, commit=commit, effort=args.effort)
                row.update(time_budget=args.time_budget, rounds_flag=args.rounds)
                sink.write(json.dumps(row) + "\n")
                sink.flush()
                print(_row_line(row), flush=True)
                if row["undumped"]:
                    print(
                        f"warning: {plan.name} solved without its pack's dump (multiblocks are "
                        f"1x1x1): link the local data/<version>/ folders into {root / 'data'}",
                        file=sys.stderr,
                    )
    rows = read_rows(rows_path)
    widest = max((r.get("rounds") or 1 for r in rows), default=1)
    late = spacing_warning(seeds, attempts, widest)
    if late and not warning:
        print(f"warning: {late}", file=sys.stderr)
    print(format_summary(summarize(rows), label))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("plans", nargs="*", metavar="PLAN", help="gtnh-factory-flow plan JSONs")
    parser.add_argument("--root", default=str(REPO), help="the checkout whose code runs")
    parser.add_argument("--out", help="folder for rows.jsonl and layouts/ (resumable)")
    parser.add_argument("--seeds", default="0:1600:100", help="start:stop:step, a,b,c, or one")
    parser.add_argument("--effort", default="full", choices=sorted(ATTEMPTS))
    budget = parser.add_mutually_exclusive_group()
    budget.add_argument("--time-budget", type=float, metavar="S", help="passed to gtnh-solve")
    budget.add_argument("--rounds", type=int, metavar="N", help="passed to gtnh-solve")
    parser.add_argument("--label", help="a name for this run (default: the root folder's)")
    parser.add_argument("--classes", action="store_true", help="split route cells by class")
    parser.add_argument("--summary", nargs="+", metavar="RUN", help="summarize saved runs")
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"), help="pair two saved runs")
    parser.add_argument("--per-seed", action="store_true", help="with --compare: every key delta")
    parser.add_argument("--net-kinds", metavar="PLAN", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.net_kinds:
        print(json.dumps(net_kinds(args.net_kinds)))
        return 0
    if args.summary:
        for run_path in args.summary:
            rows = read_rows(Path(run_path))
            print(format_summary(summarize(rows), Path(run_path).name))
        return 0
    if args.compare:
        a, b = (read_rows(Path(p)) for p in args.compare)
        print(format_compare(compare(a, b), per_seed=args.per_seed))
        return 0
    if not args.plans or not args.out:
        print("error: give PLAN... and --out, or --summary / --compare", file=sys.stderr)
        return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
