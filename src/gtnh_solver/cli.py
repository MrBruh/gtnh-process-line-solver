"""cli - the ``gtnh-solve`` entry point.

Wires the Phase 1 pipeline into one command: a gtnh-factory-flow exported plan JSON in, the
solved layout out::

    gtnh-solve examples/gtnh-sand.json            # print the LayoutResult contract as JSON
    gtnh-solve plan.json > layout.json            # ...which is how it goes to a file
    gtnh-solve plan.json --preview view.html      # write a double-clickable 3D preview
    gtnh-solve plan.json --schematic line.schematic  # write a Schematica build ghost
    gtnh-solve --inspect-schematic line.schematic # ...and read one back: blocks + machines
    gtnh-solve --dataset-coverage                 # what the local dataset cannot draw, ranked
    gtnh-solve plan.json --seed 3                 # pick the solver seed
    gtnh-solve plan.json --fast                   # skip optimization (instant, constructive)
    gtnh-solve plan.json --objective volume       # what "compact" means: footprint|volume|balanced
    gtnh-solve plan.json --me items --me fluids   # leave those to ME: no pipes laid for them

It loads + adapts the export, solves (place -> auto-output -> item/fluid + power route ->
self-validate), and writes what was asked for: a self-contained three.js viewer with
``--preview``, a Schematica ghost with ``--schematic``. Asked for neither, it prints the
``LayoutResult`` itself (docs/IR.md) on stdout, the machine-readable answer a script consumes.
**stdout carries that JSON and nothing else**: every warning, note and log line goes to stderr, so
``gtnh-solve plan.json | python -m json.tool`` always parses. With an artifact flag stdout stays
empty, and the artifact is the answer.

Exit code: 0 when the layout is fully VALID, 1 when the run could only return an explicit
infeasibility (the reason is printed to stderr - from the solver, or from the adapter for a plan
that maps cleanly and states a line no layout satisfies), 2 when the export could not be loaded, 3
when the run hit a bug in this program (an exception no stage claimed). 1 and 2 are *answers*
about the plan; 3 exists so a caller can tell an answer from a crash. An infeasible run still
prints its ``LayoutResult``, whose ``status`` and ``infeasibility`` say what the stderr line says;
exit 2 and exit 3 print nothing on stdout, because there is no layout to print.

``--preview`` and ``--schematic`` are written whatever the status, because a partial layout is what
someone debugging a line needs to see; for a non-VALID one a warning comes first, naming what is
unconnected and saying not to build it (#214).
"""

from __future__ import annotations

import argparse
import contextlib
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
    Node,
    Plan,
    PlanProducer,
    Recipe,
    describe_markers,
    load_plan,
    plan_pack_version,
    resolve_producer,
    to_input_ir,
)
from gtnh_solver.adapter.core import _effective_handler
from gtnh_solver.dataset import PhysicalDataset, list_versions, load_physical_dataset
from gtnh_solver.dataset.coverage import format_report, measure
from gtnh_solver.dataset.roots import extractor_hint, resolve_dataset_path
from gtnh_solver.ir import Commodity, Infeasibility, InputIR, LayoutResult, LayoutStatus, METoggles
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

#: The two halves of a ``data/<version>/`` dump, which two separate extractor runs write, and what a
#: run loses when it follows the plan's pack without one of them (see ``_dataset_version_for``).
_DUMP_HALVES: Final = {
    "multiblocks": "every multiblock reserves a 1x1x1 footprint",
    "textures/manifest.json": "--preview draws placeholder boxes and --schematic cannot export",
}

#: The ``--me`` choices: ``METoggles``' own field names, so the flag and the contract cannot drift.
_ME_COMMODITIES: Final = tuple(METoggles.model_fields)

#: Exit code for an exception no stage claimed: a bug in this program, not a verdict about the
#: plan. Distinct from 1 (an explicit infeasibility) and 2 (the export could not be loaded)
#: because those two are *answers*, and a caller must be able to tell an answer from a crash.
INTERNAL_ERROR_EXIT: Final = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gtnh-solve",
        description=(
            "Physical place-and-route solver for GregTech: New Horizons - turns a "
            "gtnh-factory-flow exported plan into a buildable layout. With no --preview or "
            "--schematic, prints the layout (the LayoutResult contract) as JSON on stdout."
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
        "--me",
        action="append",
        choices=_ME_COMMODITIES,
        metavar="COMMODITY",
        help=(
            "move COMMODITY over ME (AE2) instead of pipes and cables: one of "
            f"{', '.join(_ME_COMMODITIES)}; repeat for more than one. Nothing is routed for it, "
            "and no ME interface is placed or drawn yet, so the builder supplies that"
        ),
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


def _me_toggles(commodities: list[str] | None) -> METoggles:
    """The ``METoggles`` that ``--me`` asked for: each named commodity on, the rest off.

    ``None`` is argparse's answer when the flag was never given, and means every commodity is routed
    physically, the contract's default. Naming one twice is the same as naming it once.
    """
    return METoggles.model_validate(dict.fromkeys(commodities or (), True))


def _note_me_toggles(toggles: METoggles) -> None:
    """Say which commodities were left to ME, and that nothing stands in for them in the build yet.

    A toggled commodity is only skipped: the solver lays no pipe, cable or auto-output for its nets,
    and nothing places the ME interface, bus or P2P tunnel that would carry it instead
    (docs/DOMAIN.md, Phase 2). Without this the layout reads as a line that forgot its pipes, so the
    run says so once, on stderr beside the other notes, and leaves the exit code alone.
    """
    left = [c.value for c in Commodity if toggles.toggled(c)]
    if not left:
        return
    print(
        f"note: {', '.join(left)} nets left to ME (--me) - nothing is routed for them, and no ME "
        f"interface or endpoint is placed or drawn yet, so the builder must supply it",
        file=sys.stderr,
    )


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

    **Half a dump still wins, and says which half is missing** (#206). A dump comes from two
    extractor runs, ``multiblocks/`` and ``textures/manifest.json``, so a folder can hold either
    alone. Declining it would not fall back as a whole: resolution is per sub-path, so the missing
    half would come from whatever other pack is newest while the present half came from this one.
    A 2.9 plan drawn and exported from a 2.8.4 manifest is exactly that - block ids and machine names
    move between packs, so the sprites and the ``.schematic`` ids would be wrong with nothing said.
    Following the plan's pack keeps the run on one pack and makes the gap visible instead: with no
    manifest the preview draws placeholder boxes and ``--schematic`` refuses; with no multiblocks
    every multiblock reserves a 1x1x1 footprint. Either way a warning on stderr names the missing
    path and the extractor run that makes it.
    """
    if pinned is not None:
        return pinned
    stated = plan_pack_version(plan)
    if stated is None:
        return None
    folder = next((v for v in list_versions() if v.name == stated), None)
    if folder is None:
        return None
    missing = [rel for rel in _DUMP_HALVES if not (folder / rel).exists()]
    if len(missing) == len(_DUMP_HALVES):
        return None  # an empty folder provides nothing to follow
    for rel in missing:
        print(
            f"warning: the plan's pack {stated} has no {folder / rel}, so {_DUMP_HALVES[rel]} "
            f"(another pack's is not substituted: block ids and machine names move between "
            f"packs); {extractor_hint(rel, stated)}",
            file=sys.stderr,
        )
    return stated


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


#: ``source_class`` markers of a GT **single-block** machine, for
#: :func:`_manifest_says_single_block`: a basic machine (``...implementations.MTEBasicMachine``, and
#: its ``MTEBasicMachineWithRecipe`` subclass) or a steam single block
#: (``gregtech.common.tileentities.machines.steam.*``, e.g. ``MTESteamForgeHammerBronze``). The steam
#: marker keeps its leading ``.machines`` so a ``...machines.multi.steam...`` package cannot match.
_SINGLE_BLOCK_CLASS_MARKERS: Final = (".MTEBasicMachine", ".machines.steam.")


def _manifest_says_single_block(
    manifest: TextureManifest | None, machine_type: str, tier: str
) -> bool:
    """Whether ``manifest`` records ``machine_type`` at ``tier`` as a single-block machine class.

    **A heuristic**, and the only one :func:`_warn_if_plan_pack_undumped` uses: the entry is found
    the way the previewer finds a single-block machine (:meth:`TextureManifest.mte_block`), and its
    ``source_class`` is read for one of :data:`_SINGLE_BLOCK_CLASS_MARKERS`. Measured against the
    full local dumps, no class it accepts is a dumped multiblock controller: 509 accepted MTEs
    against 296 controllers at 2.9.0-beta-2, 525 against 208 at 2.8.4, overlap zero in both. It
    can only ever say "single": no manifest, no entry or any other class leaves the answer unknown
    (``False``), and an unknown type is listed rather than guessed away. Tighten it here.
    """
    if manifest is None:
        return False
    found = manifest.mte_block(machine_type, tier)
    if found is None:
        return False
    source_class = manifest.source_class(*found)
    return any(marker in source_class for marker in _SINGLE_BLOCK_CLASS_MARKERS)


def _may_be_multiblock(recipe: Recipe, node: Node, manifest: TextureManifest | None) -> bool:
    """Whether a node's machine is not known to be a single block, for the #207 warning.

    The plan answers first when it can: an arodoid handler states ``kind``, and "multiblock" keeps
    the machine listed whatever the manifest thinks, since the plan names the machine it built.
    MrBruh's fork states no kind, so otherwise :func:`_manifest_says_single_block` decides.
    """
    handler = _effective_handler(recipe, node)
    if handler is not None and handler.kind in ("single", "multiblock"):
        return handler.kind == "multiblock"
    return not _manifest_says_single_block(manifest, recipe.machine_type, node.overclock_tier)


def _warn_if_plan_pack_undumped(
    plan: Plan, dataset_version: str | None, physical: PhysicalDataset | None, problem: InputIR
) -> None:
    """Say so when the plan's pack has no local dump and its multiblocks fell to 1x1x1 (#207).

    The fresh-clone hazard. :func:`_dataset_version_for` declines a pack no local dump provides, so
    the solve falls back to the newest dump or the committed fixtures. Two of those fallbacks are
    already reported, and this stays out of their way rather than say it twice: a **census** of
    another pack draws the adapter's mismatch warning (``_check_dataset_version``), and a failed load
    draws :func:`_load_physical_or_warn`'s. The quiet one is a **sample**: the committed fixtures
    hold two controllers, the adapter rightly declines to judge a sample's nominal pack, and every
    other multiblock in the plan reserves a 1x1x1 footprint - which the previewer then draws (via
    ``TextureManifest.mte_block``) as a lone controller that never forms, and ``--schematic``
    exports the same way. Nothing said so.

    **Only what may be a multiblock is named.** A machine type is left out when it found its
    structure (it lost nothing), or when it is known to be a single block (:func:`_may_be_multiblock`:
    the plan's handler says so, else the resolved texture manifest's class does). The warning stays
    quiet when nothing is left, so the shipped sand line (Forge Hammers only) says nothing on a
    fresh clone while the nitrobenzene line names its four multiblocks.
    """
    stated = plan_pack_version(plan)
    if stated is None or dataset_version is not None:
        return  # no pack stated, or its dump (or the user's pin) is what loaded
    if physical is None or physical.meta.census:
        return  # already reported: the failed load, or the adapter's pack mismatch
    sized = {m.type for m in problem.machines if m.footprint.volume > 1}
    recipes = {r.id: r for r in plan.recipes}
    nodes = [
        (recipes[n.recipe_id], n)
        for n in plan.nodes
        if n.recipe_id in recipes and recipes[n.recipe_id].machine_type not in sized
    ]
    if not nodes:
        return
    manifest: TextureManifest | None = None  # none to consult: every unsized type stays listed
    with contextlib.suppress(OSError, ValueError):
        manifest = TextureManifest.load(resolve_dataset_path("textures/manifest.json"))
    listed = sorted({r.machine_type for r, n in nodes if _may_be_multiblock(r, n, manifest)})
    if not listed:
        return
    print(
        f"warning: no local dump for the plan's pack {stated}, so these reserve 1x1x1 footprints "
        f"and may show as lone controllers in --preview/--schematic: {', '.join(listed)}. Fix: "
        f"make {resolve_dataset_path('multiblocks', version=stated)} with the extractor "
        f"(tools/gtnh-extractor/README.md)",
        file=sys.stderr,
    )


def _warn_unmeasured_power_intake(problem: InputIR, layout: LayoutResult) -> None:
    """Say how many machines the under-supply gate could not measure, if any.

    The validator abstains on a machine whose power connections state no amp ceiling, and it
    abstains often: the ceiling comes from a GT rule that has to be *named* (2 A per energy hatch
    for a machine with a structural record, ``maxAmperesIn`` for a machine a census dataset proves
    is a single block), and with no local dump neither applies. That silence used to be
    indistinguishable from "checked and fine", which is the complaint #114 was filed about - so a
    run says plainly how much of its power intake went unchecked. On stderr, like the dataset
    warnings, so the layout JSON on stdout stays parseable; it is a coverage note, not a defect, and
    it does not touch the exit code.
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


#: How many unconnected nets the incomplete-export warning names before it summarises the rest.
_NAMED_NETS: Final = 3


def _unconnected_nets(problem: InputIR, layout: LayoutResult) -> list[str]:
    """The nets ``layout`` builds no connection for, in problem order.

    A net is connected by a pipe ``Route`` or a free ``AutoConnection``; an ME-toggled commodity is
    delivered by ME and needs neither, so it is never counted as missing.
    """
    connected = {r.net_id for r in layout.routes} | {a.net_id for a in layout.auto_connections}
    return [
        net.id
        for net in problem.nets
        if net.id not in connected and not problem.me_toggles.toggled(net.commodity)
    ]


def _warn_incomplete_export(problem: InputIR, layout: LayoutResult, artifacts: list[str]) -> None:
    """Say, before writing them, that artifacts of a non-VALID layout describe no finished build.

    The CLI writes ``--preview`` and ``--schematic`` whatever the status, which is deliberate: a
    partial layout is exactly what someone debugging a line wants to look at (#214). But a
    ``.schematic`` loads into Schematica as a ghost to build from, and nothing in it says that some
    of its nets were never connected; the status was only printed at the very end of the run,
    after the "wrote ..." lines, where it reads like a footnote. So this warns first, names what is
    missing, and says not to build it. The exit code is unchanged: this is about the files, and
    the verdict already has its code.
    """
    if layout.status is LayoutStatus.VALID or not artifacts:
        return
    unconnected = _unconnected_nets(problem, layout)
    if unconnected:
        shown = ", ".join(unconnected[:_NAMED_NETS])
        if len(unconnected) > _NAMED_NETS:
            shown += f" and {len(unconnected) - _NAMED_NETS} more"
        missing = f"{len(unconnected)} of {len(problem.nets)} net(s) are unconnected ({shown})"
    else:
        missing = "every net is connected, but the layout fails validation"
    reason = layout.infeasibility.constraint if layout.infeasibility is not None else "no reason"
    print(
        f"warning: the layout is {layout.status.value} ({reason}), so the "
        f"{' and '.join(artifacts)} written below is INCOMPLETE: {missing}. Do not build it "
        f"as-is; the full reason is at the end of this output.",
        file=sys.stderr,
    )


def _layout_json(layout: LayoutResult) -> str:
    """``layout`` as the CLI publishes it on stdout: the ``LayoutResult`` contract, as JSON.

    Field names, because the contract declares no aliases and docs/IR.md documents those names;
    every field, defaults included, so ``version`` and a null ``infeasibility`` are stated rather
    than left for a reader to know were omitted. Indented by two, like the JSON ``tools/`` writes.
    It reads back with ``LayoutResult.model_validate_json``.

    **ASCII-escaped**, which is why this goes through ``json.dumps`` rather than
    ``model_dump_json``. stdout is a console or a redirect whose encoding this program does not
    choose (cp1252 on a Windows pipe), and a plan's machine and resource names are free text: a
    raw non-ASCII name can fail to encode on the way out, or land in ``> layout.json`` as bytes a
    UTF-8 reader then decodes as something else, where a JSON escape reads back as the same string
    in every parser. pydantic 2.0, the floor this package declares, has no ``ensure_ascii`` switch
    on ``model_dump_json``.
    """
    return json.dumps(layout.model_dump(mode="json"), indent=2)


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

    The stretch after the export is loaded - solve, serialize, preview - has no per-stage error
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

    # Asked for no artifact, the answer is the layout itself, as JSON on stdout. An artifact flag
    # makes the artifact the answer and keeps stdout empty.
    publish = not (args.preview or args.schematic)

    try:
        problem = to_input_ir(
            plan, physical=physical, producer=producer, me_toggles=_me_toggles(args.me)
        )
    except InfeasiblePlanError as exc:
        # Listed first because it IS an AdapterError (a ValueError): a plan that maps cleanly and
        # states an unbuildable line is an infeasibility (exit 1), not an unloadable export
        # (exit 2). Reported in the same shape as the solver's own, since the user cannot act on
        # "which stage noticed" and the reason reads the same either way (#112). That holds on
        # stdout as well: the layout published is the one the solver itself returns when the
        # machines do not fit at all (the verdict and the seed, nothing placed), so a script
        # reading stdout gets one shape whichever stage said no.
        if publish:
            try:
                unbuilt = LayoutResult(
                    status=LayoutStatus.INFEASIBLE, seed=args.seed, infeasibility=exc.infeasibility
                )
                print(_layout_json(unbuilt))
            except Exception as err:  # the last-resort guard; see _internal_error
                return _internal_error(err)
        return _report_infeasibility(LayoutStatus.INFEASIBLE, exc.infeasibility)
    except _LOAD_ERRORS as exc:
        print(f"error: could not load {args.export!r}: {exc}", file=sys.stderr)
        return 2
    _warn_if_plan_pack_undumped(plan, dataset_version, physical, problem)
    _note_me_toggles(problem.me_toggles)

    try:
        layout = solve(problem, seed=args.seed, optimize=not args.fast, objective=args.objective)
        _warn_unmeasured_power_intake(problem, layout)
        # Serialized inside the guard: a layout the contract cannot dump is a bug in this program,
        # not a verdict about the plan.
        payload = _layout_json(layout) if publish else None
    except Exception as exc:  # the last-resort guard; see _internal_error
        return _internal_error(exc)

    if payload is not None:
        # Printed on an infeasible run too, ahead of the report below: the JSON carries `status`
        # and `infeasibility`, so a script reads the verdict from stdout while a person reads the
        # same reason on stderr.
        print(payload)

    _warn_incomplete_export(
        problem,
        layout,
        [
            flag
            for flag, path in (("--preview", args.preview), ("--schematic", args.schematic))
            if path
        ],
    )

    if args.preview:
        # Surface the previewer's texture-resolution summary (which machines got a real GT texture
        # vs a placeholder box, and the jar fetch) on stderr - a per-user info log, added only for
        # the preview path so the plain JSON run stays quiet.
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
            write_schematic(problem, layout, args.schematic, version=dataset_version)
        except SchematicError as exc:
            # A block the dataset cannot type is refused rather than guessed: a .schematic that
            # rebuilds a cable as a machine looks buildable and is not (GitHub #96).
            print(f"error: cannot export {args.schematic}: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"error: could not write {args.schematic}: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # the lowering, not the write; see _internal_error (#212)
            return _internal_error(exc)
        print(f"wrote schematic to {args.schematic}", file=sys.stderr)

    if layout.status is LayoutStatus.VALID:
        return 0

    detail = layout.infeasibility
    if detail is None:
        return 1
    return _report_infeasibility(layout.status, detail)


if __name__ == "__main__":
    raise SystemExit(main())
