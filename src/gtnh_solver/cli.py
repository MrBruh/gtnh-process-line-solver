"""cli - the ``gtnh-solve`` entry point.

Wires the Phase 1 pipeline into one command: a gtnh-factory-flow exported plan JSON in, a
human-readable build guide out::

    gtnh-solve examples/gtnh-sand.json            # print the build guide to stdout
    gtnh-solve plan.json -o guide.txt             # ...or write it to a file
    gtnh-solve plan.json --preview view.html      # write a double-clickable 3D preview
    gtnh-solve plan.json --seed 3                 # pick the solver seed
    gtnh-solve plan.json --fast                   # skip optimization (instant, constructive)
    gtnh-solve plan.json --objective volume       # what "compact" means: footprint|volume|balanced

It loads + adapts the export, solves (place -> auto-output -> item/fluid + power route ->
self-validate), and renders ``build_guide`` (and, with ``--preview``, a self-contained three.js
viewer). Exit code: 0 when the layout is fully VALID, 1 when the solver could only return an
explicit infeasibility (the reason is printed to stderr), 2 when the export could not be loaded.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from pydantic import ValidationError

from gtnh_solver import __version__
from gtnh_solver.adapter import adapt_file
from gtnh_solver.buildguide import build_guide
from gtnh_solver.dataset import PhysicalDataset, list_versions, load_physical_dataset
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
        "--fast",
        action="store_true",
        help="skip placement optimization: a near-instant constructive layout (no SA/LNS)",
    )
    parser.add_argument(
        "--objective",
        choices=("footprint", "volume", "balanced"),
        default="footprint",
        help=(
            "what the optimizer treats as compact: minimum floor area (footprint, the default - "
            "stacks tall), minimum enclosing box (volume - stays flat/cubic), or both (balanced); "
            "ignored with --fast"
        ),
    )
    parser.add_argument(
        "-o", "--output", metavar="FILE", help="write the build guide to FILE instead of stdout"
    )
    parser.add_argument(
        "--preview",
        metavar="FILE",
        help="write a self-contained 3D preview (a double-clickable .html) to FILE",
    )
    parser.add_argument(
        "--dataset-version",
        metavar="VERSION",
        help=(
            "use the generated dataset in data/<VERSION>/ (multiblocks + textures); default resolves "
            "the newest local data/<version>/ if any is present, else the committed fixtures"
        ),
    )
    parser.add_argument(
        "--list-dataset-versions",
        action="store_true",
        help="list the generated dataset versions available under data/, then exit",
    )
    return parser


def _load_physical_or_warn(version: str | None = None) -> PhysicalDataset | None:
    """The resolved multiblock dataset (real footprints), or ``None`` with a stderr warning.

    Wiring the physical dataset into the solve path is what gives multiblocks their real footprints
    instead of the crude 1x1x1 default (GAP A, the overlap fix). It stays a GRACEFUL enhancement: a
    missing, unreadable, or empty ``data/multiblocks/`` dump warns and falls back to ``physical=None``
    (the historical single-block behaviour) rather than crashing, so the documented 0/1/2 exit-code
    contract is untouched. ``DatasetError`` is a ``ValueError``; ``OSError`` covers a missing dir and
    ``ValidationError`` a malformed file, so the whole load can never take the CLI down.
    """
    try:
        physical = load_physical_dataset(version=version)
    except (OSError, ValueError, ValidationError) as exc:
        print(
            f"warning: physical multiblock dataset unavailable ({exc}); using 1x1x1 footprints",
            file=sys.stderr,
        )
        return None
    if not physical.machines:
        print(
            "warning: physical multiblock dataset is empty; using 1x1x1 footprints",
            file=sys.stderr,
        )
        return None
    return physical


def _enable_previewer_logging() -> None:
    """Route ``gtnh_solver`` INFO logs to stderr (idempotently) so ``--preview`` shows the texture
    summary. Scoped to this logger and guarded against double-attaching a handler on re-entry."""
    logger = logging.getLogger("gtnh_solver")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_dataset_versions:
        versions = list_versions()
        for v in versions:
            print(v.name)  # newest first; the folder name is the version
        if not versions:
            print("no generated dataset versions; using the committed fixtures", file=sys.stderr)
        return 0

    # `export` is nargs="?" with this manual check (not argparse `required`) so main([]) can be
    # unit-tested for the exit-2 path without argparse raising SystemExit.
    if args.export is None:
        print("error: an export path is required (try 'gtnh-solve --help')", file=sys.stderr)
        return 2

    physical = _load_physical_or_warn(args.dataset_version)  # real footprints; None -> 1x1x1
    try:
        problem = adapt_file(args.export, physical=physical)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"error: could not load {args.export!r}: {exc}", file=sys.stderr)
        return 2

    layout = solve(problem, seed=args.seed, optimize=not args.fast, objective=args.objective)
    guide = build_guide(problem, layout)

    if args.output:
        try:
            Path(args.output).write_text(guide, encoding="utf-8")
        except OSError as exc:
            print(f"error: could not write {args.output}: {exc}", file=sys.stderr)
            return 2
        print(f"wrote build guide to {args.output}", file=sys.stderr)
    elif not args.preview:
        print(guide, end="")  # default to stdout, unless the user asked only for the visual preview

    if args.preview:
        # Surface the previewer's texture-resolution summary (which machines got a real GT texture
        # vs a placeholder box, and the jar fetch) on stderr - a per-user info log, added only for
        # the preview path so the normal build-guide run stays quiet.
        _enable_previewer_logging()
        try:
            write_preview(problem, layout, args.preview, version=args.dataset_version)
        except OSError as exc:
            print(f"error: could not write {args.preview}: {exc}", file=sys.stderr)
            return 2
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
