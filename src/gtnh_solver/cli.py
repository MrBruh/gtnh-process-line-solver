"""cli - the ``gtnh-solve`` entry point.

Wires the Phase 1 pipeline into one command: a gtnh-factory-flow exported plan JSON in, a
human-readable build guide out::

    gtnh-solve examples/gtnh-sand.json            # print the build guide to stdout
    gtnh-solve plan.json -o guide.txt             # ...or write it to a file
    gtnh-solve plan.json --preview view.html      # write a double-clickable 3D preview
    gtnh-solve plan.json --seed 3                 # pick the solver seed

It loads + adapts the export, solves (place -> auto-output -> item/fluid + power route ->
self-validate), and renders ``build_guide`` (and, with ``--preview``, a self-contained three.js
viewer). Exit code: 0 when the layout is fully VALID, 1 when the solver could only return an
explicit infeasibility (the reason is printed to stderr), 2 when the export could not be loaded.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from gtnh_solver import __version__
from gtnh_solver.adapter import adapt_file
from gtnh_solver.buildguide import build_guide
from gtnh_solver.ir import LayoutStatus
from gtnh_solver.previewer import write_preview
from gtnh_solver.solver import solve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gtnh-solve",
        description=(
            "Physical place-and-route solver for GregTech: New Horizons - turns a "
            "gtnh-factory-flow exported plan into a buildable layout and a text build guide."
        ),
    )
    parser.add_argument("--version", action="version", version=f"gtnh-solve {__version__}")
    parser.add_argument("export", nargs="?", help="path to a gtnh-factory-flow exported plan JSON")
    parser.add_argument("--seed", type=int, default=0, help="RNG seed for the solver (default: 0)")
    parser.add_argument(
        "-o", "--output", metavar="FILE", help="write the build guide to FILE instead of stdout"
    )
    parser.add_argument(
        "--preview",
        metavar="FILE",
        help="write a self-contained 3D preview (a double-clickable .html) to FILE",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.export is None:
        print("error: an export path is required (try 'gtnh-solve --help')", file=sys.stderr)
        return 2

    try:
        problem = adapt_file(args.export)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"error: could not load {args.export!r}: {exc}", file=sys.stderr)
        return 2

    layout = solve(problem, seed=args.seed)
    guide = build_guide(problem, layout)

    if args.output:
        Path(args.output).write_text(guide, encoding="utf-8")
        print(f"wrote build guide to {args.output}", file=sys.stderr)
    elif not args.preview:
        print(guide, end="")  # default to stdout, unless the user asked only for the visual preview

    if args.preview:
        write_preview(problem, layout, args.preview)
        print(f"wrote preview to {args.preview}", file=sys.stderr)

    if layout.status is LayoutStatus.VALID:
        return 0

    detail = layout.infeasibility
    if detail is not None:
        print(f"\n[{layout.status.value}] {detail.constraint}: {detail.detail}", file=sys.stderr)
        if detail.suggested_relaxation:
            print(f"  try: {detail.suggested_relaxation}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
