"""cli - the ``gtnh-solve`` entry point.

Wires the Phase 1 pipeline into one command: a gtnh-factory-flow exported plan JSON in (or a
ShadowTheAge calculator plan, ``.gtnh``, with the 'shadow' extra), the solved layout out::

    gtnh-solve examples/gtnh-sand.json            # print the LayoutResult contract as JSON
    gtnh-solve plan.gtnh --shadow-data data.bin   # a ShadowTheAge plan, against its recipe data
    gtnh-solve plan.json > layout.json            # ...which is how it goes to a file
    gtnh-solve plan.json --preview view.html      # write a double-clickable 3D preview
    gtnh-solve plan.json --schematic line.schematic  # write a Schematica build ghost
    gtnh-solve plan.json --schematic line.schematic --world saves/MyWorld  # ...with its covers
    gtnh-solve --inspect-schematic line.schematic # ...and read one back: blocks + machines
    gtnh-solve --dataset-coverage                 # what the local dataset cannot draw, ranked
    gtnh-solve plan.json --seed 3                 # pick the solver seed
    gtnh-solve plan.json --fast                   # skip optimization (instant, constructive)
    gtnh-solve plan.json --effort minimal         # one short attempt: a quick preview or check
    gtnh-solve plan.json --time-budget 60         # keep searching, round after round, for ~60 s
    gtnh-solve plan.json --rounds 3               # exactly 3 rounds (replays a timed run)
    gtnh-solve plan.json --objective volume       # what "compact" means: footprint|volume|balanced
    gtnh-solve plan.json --jobs 1                 # keep every attempt in one process
    gtnh-solve plan.json --list-nets              # the nets a user may move to ME (NetList JSON)
    gtnh-solve plan.json --me-plan me.json        # move the nets an MEPlan names to ME
    gtnh-solve plan.json --me items --me fluids   # ...or every item and fluid net, to one network

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

**ME is chosen per net, in two runs** (#332): ``--list-nets`` adapts the plan, prints its NetList
(every item and fluid net, its ends, its rate and the ME device each end would get) and exits
without solving; ``--me-plan FILE`` reads back an MEPlan naming the ME networks and the nets that
ride them. ``--me items`` / ``--me fluids`` is shorthand for one attached network carrying every net
of that commodity, so it cannot be given with ``--me-plan``; ``--me power`` leaves the line's EU
supply to the builder and combines with either. A plan made against another plan or dataset is
refused (exit 2), since its net ids may name other nets.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import math
import os
import sys
import traceback
import zipfile
from pathlib import Path
from typing import Final, get_args

from pydantic import ValidationError

from gtnh_solver import __version__
from gtnh_solver.adapter import (
    AdapterError,
    InfeasiblePlanError,
    MEPlanError,
    Node,
    Plan,
    PlanProducer,
    Recipe,
    describe_markers,
    list_nets,
    load_plan,
    load_shadow_plan,
    plan_pack_version,
    resolve_producer,
    to_input_ir,
)
from gtnh_solver.adapter.core import _effective_handler, _recipe_map
from gtnh_solver.dataset import PhysicalDataset, list_versions, load_physical_dataset
from gtnh_solver.dataset.coverage import format_report, measure
from gtnh_solver.dataset.me import DEFAULT_GRID_BUFFER_AE, PROVIDER_BUFFER_AE
from gtnh_solver.dataset.roots import extractor_hint, resolve_dataset_path
from gtnh_solver.ir import (
    Commodity,
    Infeasibility,
    InputIR,
    LayoutResult,
    LayoutStatus,
    MEMode,
    MEPlan,
    MEPower,
)
from gtnh_solver.previewer import write_preview
from gtnh_solver.previewer.jar import cached_jar
from gtnh_solver.previewer.textures import TextureManifest
from gtnh_solver.schematic import SchematicError, item_ids, read_schematic, write_schematic
from gtnh_solver.schematic.read import Schematic
from gtnh_solver.solver import Effort, solve
from gtnh_solver.system_io import RATE_STEM, MENetworkIO, resource_label, system_io
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

#: The ``--me`` choices, and the commodity each names: items and fluids ride one attached network
#: (the shorthand for an MEPlan), power is left to the builder (``MEConfig.power_external``).
_ME_COMMODITIES: Final = {
    "items": Commodity.ITEM,
    "fluids": Commodity.FLUID,
    "power": Commodity.POWER,
}

#: Exit code for an exception no stage claimed: a bug in this program, not a verdict about the
#: plan. Distinct from 1 (an explicit infeasibility) and 2 (the export could not be loaded)
#: because those two are *answers*, and a caller must be able to tell an answer from a crash.
INTERNAL_ERROR_EXIT: Final = 3


def _positive_int(text: str) -> int:
    """An argparse type for a count that has to be at least 1 (``--jobs``, ``--rounds``)."""
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _seconds(text: str) -> float:
    """An argparse type for a finite, non-negative number of seconds (``--time-budget``)."""
    value = float(text)
    if not 0 <= value < math.inf:
        raise argparse.ArgumentTypeError(f"must be a finite number of seconds >= 0, got {text}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gtnh-solve",
        description=(
            "Physical place-and-route solver for GregTech: New Horizons - turns a "
            "gtnh-factory-flow exported plan (or a ShadowTheAge calculator .gtnh plan, with "
            "--shadow-data) into a buildable layout. With no --preview or --schematic, prints the "
            "layout (the LayoutResult contract) as JSON on stdout."
        ),
    )
    parser.add_argument("--version", action="version", version=f"gtnh-solve {__version__}")
    parser.add_argument(
        "export",
        nargs="?",
        help=(
            "path to a gtnh-factory-flow exported plan JSON, or to a ShadowTheAge calculator plan "
            "(.gtnh, which needs --shadow-data)"
        ),
    )
    parser.add_argument(
        "--shadow-data",
        metavar="DATA_BIN",
        help=(
            "the ShadowTheAge calculator's data.bin, which a .gtnh plan is read against (download "
            "it with 'gtnh-shadow-convert fetch-data'); needs the 'shadow' extra"
        ),
    )
    parser.add_argument("--seed", type=int, default=0, help="RNG seed for the solver (default: 0)")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="skip placement optimization: a near-instant constructive layout (no SA/LNS)",
    )
    parser.add_argument(
        "--effort",
        choices=get_args(Effort),
        help=(
            "how hard the optimizer works: full (the default) anneals 8 placements and keeps the "
            "best; minimal routes one short anneal, quick but a worse layout, for a preview or a "
            "check that the line solves; ignored with --fast"
        ),
    )
    more = parser.add_mutually_exclusive_group()
    more.add_argument(
        "--time-budget",
        type=_seconds,
        metavar="SECONDS",
        help=(
            "keep searching for about SECONDS: after the usual attempts, run more rounds of them "
            "with fresh seeds while the next round still fits, and keep the best layout of all. "
            "Never worse than the same seed without it, and it may overrun by about one round. "
            "Says how many rounds ran, so --rounds replays it; ignored with --fast"
        ),
    )
    more.add_argument(
        "--rounds",
        type=_positive_int,
        metavar="N",
        help=(
            "run exactly N rounds of attempts, each with fresh seeds (default: 1), the "
            "deterministic form of --time-budget; ignored with --fast"
        ),
    )
    parser.add_argument(
        "--jobs",
        type=_positive_int,
        default=os.cpu_count() or 1,
        metavar="N",
        help=(
            "how many processes the optimizer's attempts may run in (default: one per CPU). "
            "A line whose attempts are quick stays in one process whatever N is, and N never "
            "changes the layout, only how long it takes"
        ),
    )
    parser.add_argument(
        "--me",
        action="append",
        choices=tuple(_ME_COMMODITIES),
        metavar="COMMODITY",
        help=(
            "move COMMODITY over ME (AE2) instead of pipes and cables: one of "
            f"{', '.join(_ME_COMMODITIES)}; repeat for more than one. items and fluids put every "
            "net of that kind on one network attached to your main ME network (not with "
            "--me-plan); power leaves the line's EU supply to you. A net on ME gets its ME devices "
            "and AE2 cable instead of a pipe (the preview does not draw them yet)"
        ),
    )
    parser.add_argument(
        "--list-nets",
        action="store_true",
        help=(
            "print the plan's item and fluid nets (the NetList contract, as JSON), with the ME "
            "device each end would get, then exit; solves nothing. Choose from it for --me-plan"
        ),
    )
    parser.add_argument(
        "--me-plan",
        metavar="FILE",
        help=(
            "move the nets the MEPlan JSON in FILE names onto its ME networks; made against "
            "--list-nets for this same plan and dataset"
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
        "--world",
        metavar="DIR",
        help=(
            "with --schematic: the save folder (or level.dat) of the world the build goes in. Its "
            "item ids let the export write each conveyor and pump cover, so the ghost shows them; "
            "the ids differ per world, so the file is right only for that one"
        ),
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
            "throughput block) or 'arodoid-v1' (carries machineHandlers instead), or "
            "'shadow-v1' for a ShadowTheAge plan gtnh-shadow-convert wrote (carries a converter "
            "block); 'auto', the default, reads the plan's own structure and warns if it cannot "
            "tell"
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


def _me_commodities(words: list[str] | None) -> tuple[frozenset[Commodity], bool]:
    """What ``--me`` asked for: the commodities whose nets ride the shorthand network, and whether
    power is left to the builder.

    ``None`` is argparse's answer when the flag was never given, and means everything is built
    physically, the contract's default. Naming one twice is the same as naming it once.
    """
    named = {_ME_COMMODITIES[word] for word in words or ()}
    return frozenset(named - {Commodity.POWER}), Commodity.POWER in named


def _read_me_plan(path: str | None) -> MEPlan | None:
    """The MEPlan ``--me-plan`` names, or ``None`` without the flag. Raises what a load raises
    (``OSError``, ``ValidationError``), which ``main`` reports as unloadable input (exit 2)."""
    if path is None:
        return None
    return MEPlan.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _note_me(problem: InputIR) -> None:
    """Say that power is left to the builder (``--me power``, #225): no source or cable is laid
    for it, so the layout reads as a line that forgot its power unless the run says so. Once, on
    stderr beside the other notes, and the exit code is left alone."""
    if problem.me.power_external:
        print(
            "note: power is left to you (--me power) - no source or cable is laid for it, so the "
            "builder must supply it",
            file=sys.stderr,
        )


def _note_me_networks(problem: InputIR, layout: LayoutResult) -> None:
    """Say what each ME network asks of the player (``system_io.MENetworkIO``, #335).

    What its storage must hold for the line to run and what lands there, and how many of the main
    network's channels it spends: none of it is in the build, so a builder who reads only the
    layout would not know to stock the main network or keep channels free for it. Then what it
    draws (#336, :func:`_me_power_note`). Two lines per network, on stderr like the other notes.
    """
    names = problem.resource_names
    io = system_io(problem, layout)
    for network in io.me:
        if network.mode is MEMode.ATTACHED:
            channels = f"{network.main_channels} channel(s) of your main network"
        elif network.main_channels:
            channels = f"{network.main_channels} channel(s) of your main network, one per link"
        else:
            channels = "none of your main network's channels"
        parts = [f"{network.devices} device(s) on {channels}"]
        for verb, flows in (("stock", network.supplies), ("it stores", network.absorbs)):
            if flows:
                listed = ", ".join(
                    f"{', '.join(resource_label(r, names) for r in flow.resources)} "
                    f"{flow.rate:g} {RATE_STEM[flow.commodity]}/t"
                    for flow in flows
                )
                parts.append(f"{verb} {listed}")
        print(
            f"note: ME network {network.network} ({network.mode.value}): {'; '.join(parts)}",
            file=sys.stderr,
        )
        power = _me_power_note(problem, network, io.power_amps_by_source)
        print(
            f"note: ME network {network.network} ({network.mode.value}): {power}", file=sys.stderr
        )


def _me_power_note(problem: InputIR, network: MENetworkIO, amps_by_source: dict[str, int]) -> str:
    """What one ME network draws, said the way the builder supplies it (spike 6): an attached
    network adds to the main network's draw, an external subnet is fed through a quartz fiber, and
    an acceptor network's Energy Acceptor draws from the line's own power, taking every amp its
    source offers until it is full, which is why its cable is sized for that source's whole output.
    An external network whose GT ME output flushes more at once than AE's default buffer holds is
    told to keep that much stored where it draws its power."""
    draw = f"{network.ae_per_tick:g} AE/t ({network.eu_per_tick:g} EU/t)"
    if network.power is MEPower.ACCEPTOR:
        rated = f"rated {network.acceptor_eu_per_tick or 0:g} EU/t"
        source = network.acceptor_source
        if source is None:
            return (
                f"its Energy Acceptor draws {draw}, {rated}, from your own power supply"
                if problem.me.power_external
                else f"its Energy Acceptor draws {draw}, {rated}, but no power cable reaches it"
            )
        amps = amps_by_source.get(source)
        full = f"{amps} A" if amps is not None else "output"
        return (
            f"its Energy Acceptor draws {draw}, {rated}, on {source}; it takes every amp offered "
            f"until it stores {PROVIDER_BUFFER_AE:,.0f} AE, so its cable is sized for {source}'s "
            f"full {full}: feed {source} no more than that"
        )
    if network.mode is MEMode.ATTACHED:
        said = f"adds {draw} to your main network's power draw"
    else:
        said = f"feed it {draw} through a quartz fiber from a powered network"
    if network.flush_ae > DEFAULT_GRID_BUFFER_AE:
        said += (
            f"; one GT ME output flush spends {network.flush_ae:,.0f} AE at once, so the network "
            f"powering it must store that much"
        )
    return said


def _note_rounds(args: argparse.Namespace, layout: LayoutResult) -> None:
    """Say how many rounds a timed solve searched, and how to get exactly this layout again.

    How far a time budget gets depends on the machine and what else it is doing, so the same command
    can return a different layout tomorrow. The round count is what pins it: the same seed with
    ``--rounds`` that many is deterministic, and the layout's ``metrics.rounds`` carries it too.
    """
    rounds = layout.metrics.rounds
    if args.time_budget is None or rounds is None:
        return
    print(
        f"note: searched {rounds} round{'s' if rounds != 1 else ''} in --time-budget "
        f"{args.time_budget:g}s; replay this layout with --seed {args.seed} --rounds {rounds}",
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
    manifest: TextureManifest | None, machine_type: str, tier: str, recipe_map: str | None = None
) -> bool:
    """Whether ``manifest`` records ``machine_type`` at ``tier`` as a single-block machine class.

    **A heuristic**, and the only one :func:`_warn_if_plan_pack_undumped` uses: the entry is found
    the way the previewer finds a single-block machine (:meth:`TextureManifest.mte_block`, by its
    recipe map and tier where the plan states the map, else by name), and its
    ``source_class`` is read for one of :data:`_SINGLE_BLOCK_CLASS_MARKERS`. Measured against the
    full local dumps, no class it accepts is a dumped multiblock controller: 509 accepted MTEs
    against 296 controllers at 2.9.0-beta-2, 525 against 208 at 2.8.4, overlap zero in both. It
    can only ever say "single": no manifest, no entry or any other class leaves the answer unknown
    (``False``), and an unknown type is listed rather than guessed away. Tighten it here.
    """
    if manifest is None:
        return False
    found = manifest.mte_block(machine_type, tier, recipe_map)
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
    return not _manifest_says_single_block(
        manifest, recipe.machine_type, node.overclock_tier, _recipe_map(recipe)
    )


def _warn_if_plan_pack_undumped(
    plan: Plan, dataset_version: str | None, physical: PhysicalDataset | None, problem: InputIR
) -> None:
    """Say so when the plan's pack has no local dump and its multiblocks fell to 1x1x1 (#207).

    The fresh-clone hazard. :func:`_dataset_version_for` declines a pack no local dump provides, so
    the solve falls back to the newest dump or the committed fixtures. Two of those fallbacks are
    already reported, and this stays out of their way rather than say it twice: a **census** of
    another pack draws the adapter's mismatch warning (``_check_dataset_version``), and a failed load
    draws :func:`_load_physical_or_warn`'s. The quiet one is a **sample**: the committed fixtures
    hold only the controllers the shipped examples use, the adapter rightly declines to judge a
    sample's nominal pack, and every other multiblock in the plan reserves a 1x1x1 footprint - which the previewer then draws (via
    ``TextureManifest.mte_block``) as a lone controller that never forms, and ``--schematic``
    exports the same way. Nothing said so.

    **Only what may be a multiblock is named.** A machine type is left out when it found its
    structure (it lost nothing), or when it is known to be a single block (:func:`_may_be_multiblock`:
    the plan's handler says so, else the resolved texture manifest's class does). The warning stays
    quiet when nothing is left, so the shipped lines say nothing on a fresh clone (sand has only
    Forge Hammers, and the sample carries nitrobenzene's multiblocks) while a line whose
    multiblocks the sample lacks names each of them.
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

    Silent when power is left to the builder (#225): no cable is laid, so there is nothing for the
    gate to measure, and blaming a missing amp ceiling would give the wrong reason. The ME note
    (:func:`_note_me`) already says power is the builder's to supply.
    """
    if problem.me.power_external:
        return
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

    A net is connected by a pipe ``Route`` or a free ``AutoConnection``; a net left to ME is
    delivered by ME and needs neither, so it is never counted as missing.
    """
    connected = {r.net_id for r in layout.routes} | {a.net_id for a in layout.auto_connections}
    return [net.id for net in problem.nets if net.id not in connected and not problem.rides_me(net)]


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


#: ``ForgeDirection`` ordinal -> its name, for printing a tile entity's facings.
_DIRECTION_NAME = {0: "down", 1: "up", 2: "north", 3: "south", 4: "west", 5: "east"}


def _print_basic_machine_facings(schematic: Schematic) -> None:
    """List every basic machine's working face, output face and what it auto-outputs (#249).

    A basic machine is the tile that records ``mMainFacing``; its ``mFacing`` is the output face,
    and ``mItemTransfer`` / ``mFluidTransfer`` say whether items and fluids leave through it. This
    is what GT reads from the file, so it is how an export is checked without hand-parsing it.
    """
    machines = [t for t in schematic.tile_entities if t.main_facing is not None]
    if not machines:
        return
    print("\nbasic machines (working face -> output face, auto-output)")
    for tile in sorted(machines, key=lambda t: t.pos):
        auto = [
            what
            for what, tag in (("items", "mItemTransfer"), ("fluids", "mFluidTransfer"))
            if tile.raw.get(tag)
        ]
        main = _DIRECTION_NAME.get(tile.main_facing or 0, str(tile.main_facing))
        output = _DIRECTION_NAME.get(tile.facing or 0, str(tile.facing))
        print(
            f"  mID {tile.mid!s:<6} at {tile.pos}: {main} -> {output}, "
            f"{' + '.join(auto) if auto else 'none'}"
        )


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
    except OSError, ValueError:
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

    _print_basic_machine_facings(schematic)

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


def _list_nets(plan: Plan, physical: PhysicalDataset | None, producer: PlanProducer | None) -> int:
    """Print the plan's NetList as JSON on stdout (``--list-nets``). Returns the exit code.

    0 with the list, 1 for a plan that maps cleanly and states a line no layout satisfies (the
    reason on stderr, nothing on stdout: there is nothing to choose from), 2 for one that does not
    map. ASCII-escaped like the layout (:func:`_layout_json`), for the same reason.
    """
    try:
        nets = list_nets(plan, physical=physical, producer=producer)
    except InfeasiblePlanError as exc:
        return _report_infeasibility(LayoutStatus.INFEASIBLE, exc.infeasibility)
    except _LOAD_ERRORS as exc:
        print(f"error: could not map the plan: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(nets.model_dump(mode="json"), indent=2))
    return 0


#: The suffix the ShadowTheAge calculator gives a saved plan.
_SHADOW_SUFFIX: Final = ".gtnh"


def _read_plan(args: argparse.Namespace) -> Plan:
    """The plan ``args.export`` names: a gtnh-factory-flow export as is, or a ShadowTheAge
    ``.gtnh`` plan converted against ``--shadow-data``. Each needs the other's flag absent, so a
    mistyped path is refused rather than read the wrong way; every refusal is an
    :class:`AdapterError`, which ``main`` reports as an unloadable export (exit 2)."""
    shadow = Path(args.export).suffix.lower() == _SHADOW_SUFFIX
    if shadow:
        if args.shadow_data is None:
            raise AdapterError(
                "a ShadowTheAge .gtnh plan holds only recipe ids; pass the calculator's data.bin "
                "with --shadow-data (download it with 'gtnh-shadow-convert fetch-data')"
            )
        return load_shadow_plan(args.export, args.shadow_data)
    if args.shadow_data is not None:
        raise AdapterError(
            f"--shadow-data is only read with a ShadowTheAge {_SHADOW_SUFFIX} plan, and "
            f"{args.export!r} is not one"
        )
    return load_plan(args.export)


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

    # Read before solving, so a wrong --world fails in a second rather than after a long search.
    world_items: dict[str, int] | None = None
    if args.world is not None:
        if not args.schematic:
            print("error: --world only applies with --schematic", file=sys.stderr)
            return 2
        try:
            world_items = item_ids(args.world)
        except SchematicError as exc:
            print(f"error: cannot read --world: {exc}", file=sys.stderr)
            return 2

    # The plan is loaded before the dataset, because it says which pack it was balanced against and
    # that is the better default for which dump to load. Loaded here rather than through adapt_file
    # so an undetermined producer can be reported first: the advice is to pass --plan-schema, which
    # only the CLI can give.
    try:
        plan = _read_plan(args)
    except _LOAD_ERRORS as exc:
        print(f"error: could not load {args.export!r}: {exc}", file=sys.stderr)
        return 2

    # "auto" is the absence of a pin, which is what resolve_producer's None already means.
    pin = None if args.plan_schema == "auto" else PlanProducer(args.plan_schema)
    producer = resolve_producer(plan, pin)
    if producer is None:
        print(
            f"warning: could not tell which gtnh-factory-flow fork or converter wrote "
            f"{args.export} ({describe_markers(plan)}); producer-specific handling is disabled. "
            f"Pass --plan-schema to say which it is.",
            file=sys.stderr,
        )

    dataset_version = _dataset_version_for(plan, args.dataset_version)
    physical = _load_physical_or_warn(dataset_version)  # real footprints; None -> 1x1x1

    if args.list_nets:
        return _list_nets(plan, physical, producer)

    # Asked for no artifact, the answer is the layout itself, as JSON on stdout. An artifact flag
    # makes the artifact the answer and keeps stdout empty.
    publish = not (args.preview or args.schematic)

    me_commodities, me_power = _me_commodities(args.me)
    try:
        me_plan = _read_me_plan(args.me_plan)
    except _LOAD_ERRORS as exc:
        print(f"error: could not load --me-plan {args.me_plan!r}: {exc}", file=sys.stderr)
        return 2
    try:
        problem = to_input_ir(
            plan,
            physical=physical,
            producer=producer,
            me_plan=me_plan,
            me_commodities=me_commodities,
            me_power=me_power,
        )
    except MEPlanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
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
    _note_me(problem)
    if args.fast and (args.time_budget is not None or args.rounds is not None):
        print(
            "note: --fast lays one constructive placement; --time-budget/--rounds ignored",
            file=sys.stderr,
        )

    try:
        layout = solve(
            problem,
            seed=args.seed,
            optimize=not args.fast,
            objective=args.objective,
            jobs=args.jobs,
            # None when not given, so the solver's own default applies (solver.DEFAULT_EFFORT).
            effort=args.effort,
            time_budget=args.time_budget,
            rounds=args.rounds,
        )
        _note_rounds(args, layout)
        _note_me_networks(problem, layout)
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
            write_schematic(
                problem, layout, args.schematic, version=dataset_version, item_ids=world_items
            )
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
