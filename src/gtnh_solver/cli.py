"""cli - the ``gtnh-solve`` entry point.

Wires the Phase 1 pipeline into one command: a gtnh-factory-flow exported plan JSON in, a
human-readable build guide out::

    gtnh-solve examples/gtnh-sand.json            # print the build guide to stdout
    gtnh-solve plan.json -o guide.txt             # ...or write it to a file
    gtnh-solve plan.json --preview view.html      # write a double-clickable 3D preview
    gtnh-solve plan.json --schematic line.schematic  # write a Schematica build ghost
    gtnh-solve --inspect-schematic line.schematic # ...and read one back: blocks + machines
    gtnh-solve --dataset-coverage                 # what the local dataset cannot draw, ranked
    gtnh-solve plan.json --seed 3                 # pick the solver seed
    gtnh-solve plan.json --fast                   # skip optimization (instant, constructive)
    gtnh-solve plan.json --objective volume       # what "compact" means: footprint|volume|balanced

It loads + adapts the export, solves (place -> auto-output -> item/fluid + power route ->
self-validate), and renders ``build_guide`` (and, with ``--preview``, a self-contained three.js
viewer). Exit code: 0 when the layout is fully VALID, 1 when the run could only return an
explicit infeasibility (the reason is printed to stderr - from the solver, or from the adapter
for a plan that maps cleanly and states a line no layout satisfies), 2 when the export could not
be loaded, 3 when the run hit a bug in this program (an exception no stage claimed). 1 and 2 are
*answers* about the plan; 3 exists so a caller can tell an answer from a crash.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
import zipfile
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from gtnh_solver import __version__
from gtnh_solver.adapter import (
    InfeasiblePlanError,
    Plan,
    PlanProducer,
    describe_markers,
    load_plan,
    plan_pack_version,
    resolve_producer,
    to_input_ir,
)
from gtnh_solver.buildguide import build_guide
from gtnh_solver.dataset import PhysicalDataset, list_versions, load_physical_dataset
from gtnh_solver.dataset.coverage import format_report, measure
from gtnh_solver.dataset.roots import resolve_dataset_path
from gtnh_solver.ir import Infeasibility, InputIR, LayoutResult, LayoutStatus
from gtnh_solver.previewer import write_preview
from gtnh_solver.previewer.jar import cached_jar
from gtnh_solver.previewer.textures import TextureManifest
from gtnh_solver.schematic import SchematicError, read_schematic, write_schematic
from gtnh_solver.solver import solve
from gtnh_solver.validator import validate

#: Every GT machine, cable and pipe is a meta of this one block; an mID IS its meta.
_GT_BLOCK: Final = "gregtech:gt.blockmachines"

#: What "the export could not be loaded" (exit 2) is made of. ``ArithmeticError`` is the one that
#: is not obvious: a figure the export carries can be well-typed, in range and still unusable - a
#: non-finite ``eut``, a ``parallel`` with 310 digits - and the arithmetic that finds out raises
#: ``OverflowError``, which is an ``ArithmeticError`` and NOT a ``ValueError``. Without it such a
#: plan left ``main`` as a traceback with exit 1, the code reserved for an explicit infeasibility,
#: so a script keying on the exit code read a crash as a clean verdict (#115). Both boundary
#: contracts now refuse those values outright (``adapter.plan``, ``ir._base``); this stays as the
#: net under any arithmetic they do not cover.
_LOAD_ERRORS: Final = (OSError, ValueError, ArithmeticError, ValidationError)

#: Exit code for an exception no stage claimed: a bug in this program, not a verdict about the
#: plan. Distinct from 1 (an explicit infeasibility) and 2 (the export could not be loaded)
#: because those two are *answers*, and a caller must be able to tell an answer from a crash.
INTERNAL_ERROR_EXIT: Final = 3


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
        "--schematic",
        metavar="FILE",
        help="write a Schematica .schematic build ghost to FILE (1.7.10; not Litematica)",
    )
    parser.add_argument(
        "--inspect-schematic",
        metavar="FILE",
        help=(
            "decode an existing .schematic and print what is in it (blocks, machines, hatches, "
            "routes), then exit; takes no plan"
        ),
    )
    parser.add_argument(
        "--plan-schema",
        choices=("auto", *(producer.value for producer in PlanProducer)),
        default="auto",
        help=(
            "which gtnh-factory-flow fork exported the plan: 'mrbruh-v2' (carries a resolved "
            "throughput block) or 'arodoid-v1' (carries machineHandlers instead); 'auto', "
            "the default, reads the plan's own structure and warns if it cannot tell"
        ),
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
        "--dataset-coverage",
        action="store_true",
        help=(
            "report what the resolved dataset cannot draw (controllers that never dumped, blocks "
            "with no sprite, sprites with no PNG), then exit; takes no plan"
        ),
    )
    parser.add_argument(
        "--list-dataset-versions",
        action="store_true",
        help="list the generated dataset versions available under data/, then exit",
    )
    return parser


def _dataset_version_for(plan: Plan, pinned: str | None) -> str | None:
    """Which ``data/<version>/`` dump to load: the pin, else the pack the plan names, else nothing.

    Following the plan keeps a 2.9 plan from silently resolving its footprints against a 2.8.4 dump
    merely because that is the newest one on the machine (the default resolution is by modification
    time, not by pack).

    **A derived version is a preference, not a pin.** If the plan names a pack no local dump
    provides, this returns ``None`` so resolution falls back to the newest dump, or the committed
    fixtures - best-effort footprints plus the adapter's mismatch warning, which beats pinning a
    folder that does not exist and losing every real footprint to the 1x1x1 default. An explicit
    ``pinned`` is never second-guessed this way: asking for a missing version should fail visibly.
    """
    if pinned is not None:
        return pinned
    stated = plan_pack_version(plan)
    if stated is None:
        return None
    if any(v.name == stated and (v / "multiblocks").is_dir() for v in list_versions()):
        return stated
    return None


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


def _warn_unmeasured_power_intake(problem: InputIR, layout: LayoutResult) -> None:
    """Say how many machines the under-supply gate could not measure, if any.

    The validator abstains on a machine whose power connections state no amp ceiling, and it
    abstains often: the ceiling comes from a GT rule that has to be *named* (2 A per energy hatch
    for a machine with a structural record, ``maxAmperesIn`` for a machine a census dataset proves
    is a single block), and with no local dump neither applies. That silence used to be
    indistinguishable from "checked and fine", which is the complaint #114 was filed about - so a
    run says plainly how much of its power intake went unchecked. On stderr, like the dataset
    warnings, so piping the build guide is unaffected; it is a coverage note, not a defect, and it
    does not touch the exit code.
    """
    unmeasured = validate(problem, layout).unverified_power_intake
    if not unmeasured:
        return
    powered = sum(1 for m in problem.machines if m.eut > 0 and m.power_input_ports)
    print(
        f"note: power intake unmeasured for {len(unmeasured)} of {powered} powered machine(s) - "
        f"no per-connection amp ceiling is known for them, so the under-supply check did not run "
        f"on {'them' if len(unmeasured) > 1 else 'it'}",
        file=sys.stderr,
    )


def _report_infeasibility(status: LayoutStatus, detail: Infeasibility) -> int:
    """Print why no layout was produced, and return the exit code for it (always 1).

    One printer for both stages that can reach this verdict: the solver, which returns it on a
    :class:`~gtnh_solver.ir.LayoutResult`, and the adapter, which raises it before a solve is
    even attempted (``InfeasiblePlanError``, #112). A reader cannot act on which stage noticed, so
    the two must not read differently.
    """
    print(f"\n[{status.value}] {detail.constraint}: {detail.detail}", file=sys.stderr)
    if detail.suggested_relaxation:
        print(f"  try: {detail.suggested_relaxation}", file=sys.stderr)
    return 1


def _internal_error(exc: BaseException) -> int:
    """Report an exception the pipeline did not expect, and return the exit code for it.

    The stretch after the export is loaded - solve, guide, preview - has no per-stage error
    contract: every failure mode it *knows* about is returned as an ``Infeasibility``, so anything
    raised there is a bug. It still must not reach the shell as a bare traceback with whatever
    exit code the interpreter chose (1, the code that means "an explicit infeasibility"), which is
    how a crash came to be indistinguishable from a clean verdict (#115).

    So it is named as an internal error, on its own exit code, with the traceback kept: this is
    the one place where the traceback is the useful part, because the only thing to do with it is
    file it.
    """
    print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
    print(
        "this is a bug in gtnh-solve, not a problem with the export; "
        "please report it with the traceback below",
        file=sys.stderr,
    )
    traceback.print_exception(exc, file=sys.stderr)
    return INTERNAL_ERROR_EXIT


def _enable_previewer_logging() -> None:
    """Route ``gtnh_solver`` INFO logs to stderr (idempotently) so ``--preview`` shows the texture
    summary. Scoped to this logger and guarded against double-attaching a handler on re-entry."""
    logger = logging.getLogger("gtnh_solver")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _dataset_coverage(version: str | None) -> int:
    """Print the dataset coverage report. Returns the process exit code.

    Exit 0 even with gaps: the local dump is expected to have them (the shipped examples are what
    must resolve, and they do), so this is a measurement rather than a gate. Exit 2 is reserved for
    "could not read the dataset at all", which is the same contract the rest of the CLI uses.

    The sprite-bytes half needs the GT jar. It is checked only when the jar is ALREADY cached: a
    coverage report is not worth a 135 MB download the caller did not ask for, and saying the
    question was skipped is honest in a way that silently passing it is not.
    """
    multiblocks = resolve_dataset_path("multiblocks", version=version)
    manifest_path = resolve_dataset_path("textures/manifest.json", version=version)
    if not multiblocks.is_dir():
        print(f"error: no multiblock dataset at {multiblocks}", file=sys.stderr)
        return 2
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: could not read {manifest_path}: {exc}", file=sys.stderr)
        return 2

    jar_assets: frozenset[str] | None = None
    jar = cached_jar(manifest_path)
    if jar is None:
        print("note: no cached GT jar; sprite bytes not checked", file=sys.stderr)
    else:
        try:
            with zipfile.ZipFile(jar) as archive:
                jar_assets = frozenset(archive.namelist())
        except (OSError, zipfile.BadZipFile) as exc:
            print(f"warning: could not read {jar}: {exc}", file=sys.stderr)

    print(f"dataset: {multiblocks}", file=sys.stderr)
    print(f"manifest: {manifest_path}", file=sys.stderr)
    print(format_report(measure(multiblocks, raw, jar_assets=jar_assets)), end="")
    return 0


def _inspect_schematic(path: str, version: str | None) -> int:
    """Print what is in the ``.schematic`` at ``path``. Returns the process exit code.

    Reading is pure (:func:`~gtnh_solver.schematic.read.read_schematic`); the only thing the
    dataset is needed for is turning an ``mID`` into a machine name, so a missing manifest degrades
    to raw ids rather than failing. **Which manifest answered is printed**, because the committed
    one is example-scoped: against it most of a real build's machines resolve to nothing, and an
    unqualified "not in this manifest" reads like a corrupt file when it only means the small
    manifest was asked.
    """
    try:
        schematic = read_schematic(path)
    except (OSError, SchematicError) as exc:
        print(f"error: could not read {path}: {exc}", file=sys.stderr)
        return 2

    manifest_path = resolve_dataset_path("textures/manifest.json", version=version)
    manifest: TextureManifest | None = None
    try:
        manifest = TextureManifest.load(manifest_path)
    except (OSError, ValueError):
        print(f"warning: no texture manifest at {manifest_path}; mIDs stay raw", file=sys.stderr)

    width, height, length = schematic.size
    solid = sum(schematic.histogram().values())
    print(f"{Path(path).name}: {width}x{height}x{length} = {schematic.volume} cells, {solid} solid")
    print(
        f"Materials={schematic.materials!r}  "
        f"entities={len(schematic.root.get('Entities', []))}  "
        f"tile entities={len(schematic.tile_entities)}"
    )

    print("\nblocks")
    for name, count in schematic.histogram().items():
        print(f"  {count:5d}  {name}")

    if not schematic.tile_entities:
        return 0

    tally: dict[tuple[int | None, str], int] = {}
    for tile in schematic.tile_entities:
        tally[(tile.mid, tile.id)] = tally.get((tile.mid, tile.id), 0) + 1

    print(f"\ntile entities (names from {manifest_path})")
    unresolved: set[int] = set()
    for (mid, tile_id), count in sorted(tally.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        if mid is None:
            label = "not a GT machine"
        else:
            machine = manifest.display_name(_GT_BLOCK, mid) if manifest is not None else None
            if machine is None:
                unresolved.add(mid)
                label = "NOT IN THIS MANIFEST"
            else:
                label = machine
        print(f"  {count:5d}  mID {mid!s:<6} {tile_id:<26} {label}")

    if unresolved:
        print(
            f"\n{len(unresolved)} mID(s) did not resolve: {', '.join(str(m) for m in sorted(unresolved))}",
            file=sys.stderr,
        )
        print(
            "the committed manifest is example-scoped; a full local dump "
            "(--dataset-version, see data/<version>/) names far more",
            file=sys.stderr,
        )
    return 0


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

    if args.dataset_coverage:
        return _dataset_coverage(args.dataset_version)

    if args.inspect_schematic:
        return _inspect_schematic(args.inspect_schematic, args.dataset_version)

    # `export` is nargs="?" with this manual check (not argparse `required`) so main([]) can be
    # unit-tested for the exit-2 path without argparse raising SystemExit.
    if args.export is None:
        print("error: an export path is required (try 'gtnh-solve --help')", file=sys.stderr)
        return 2

    # The plan is loaded before the dataset, because it says which pack it was balanced against and
    # that is the better default for which dump to load. Loaded here rather than through adapt_file
    # so an undetermined producer can be reported first: the advice is to pass --plan-schema, which
    # only the CLI can give.
    try:
        plan = load_plan(args.export)
    except _LOAD_ERRORS as exc:
        print(f"error: could not load {args.export!r}: {exc}", file=sys.stderr)
        return 2

    # "auto" is the absence of a pin, which is what resolve_producer's None already means.
    pin = None if args.plan_schema == "auto" else PlanProducer(args.plan_schema)
    producer = resolve_producer(plan, pin)
    if producer is None:
        print(
            f"warning: could not tell which gtnh-factory-flow fork exported {args.export} "
            f"({describe_markers(plan)}); producer-specific handling is disabled. "
            f"Pass --plan-schema to say which it is.",
            file=sys.stderr,
        )

    dataset_version = _dataset_version_for(plan, args.dataset_version)
    physical = _load_physical_or_warn(dataset_version)  # real footprints; None -> 1x1x1

    try:
        problem = to_input_ir(plan, physical=physical, producer=producer)
    except InfeasiblePlanError as exc:
        # Listed first because it IS an AdapterError (a ValueError): a plan that maps cleanly and
        # states an unbuildable line is an infeasibility (exit 1), not an unloadable export
        # (exit 2). Reported in the same shape as the solver's own, since the user cannot act on
        # "which stage noticed" and the reason reads the same either way (#112).
        return _report_infeasibility(LayoutStatus.INFEASIBLE, exc.infeasibility)
    except _LOAD_ERRORS as exc:
        print(f"error: could not load {args.export!r}: {exc}", file=sys.stderr)
        return 2

    try:
        layout = solve(problem, seed=args.seed, optimize=not args.fast, objective=args.objective)
        _warn_unmeasured_power_intake(problem, layout)
        guide = build_guide(problem, layout)
    except Exception as exc:  # the last-resort guard; see _internal_error
        return _internal_error(exc)

    if args.output:
        try:
            # Make the directory it sits in, as --preview and --schematic do: an explicit output
            # path reads as "put it here", and this write comes after the whole solve (#150).
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(guide, encoding="utf-8")
        except OSError as exc:
            print(f"error: could not write {args.output}: {exc}", file=sys.stderr)
            return 2
        print(f"wrote build guide to {args.output}", file=sys.stderr)
    elif not (args.preview or args.schematic):
        print(guide, end="")  # default to stdout, unless the user asked only for an artifact

    if args.preview:
        # Surface the previewer's texture-resolution summary (which machines got a real GT texture
        # vs a placeholder box, and the jar fetch) on stderr - a per-user info log, added only for
        # the preview path so the normal build-guide run stays quiet.
        _enable_previewer_logging()
        try:
            # The same resolved version the footprints came from, so the textures cannot be drawn
            # from a different pack than the geometry.
            write_preview(problem, layout, args.preview, version=dataset_version)
        except OSError as exc:
            print(f"error: could not write {args.preview}: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # the scene build, not the write; see _internal_error
            return _internal_error(exc)
        print(f"wrote preview to {args.preview}", file=sys.stderr)

    if args.schematic:
        try:
            write_schematic(problem, layout, args.schematic, version=args.dataset_version)
        except SchematicError as exc:
            # A block the dataset cannot type is refused rather than guessed: a .schematic that
            # rebuilds a cable as a machine looks buildable and is not (GitHub #96).
            print(f"error: cannot export {args.schematic}: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"error: could not write {args.schematic}: {exc}", file=sys.stderr)
            return 2
        print(f"wrote schematic to {args.schematic}", file=sys.stderr)

    if layout.status is LayoutStatus.VALID:
        return 0

    detail = layout.infeasibility
    if detail is None:
        return 1
    return _report_infeasibility(layout.status, detail)


if __name__ == "__main__":
    raise SystemExit(main())
