"""The one rule in ``tools/derive_small_manifest.py`` that must never degrade quietly.

The tool writes a **committed** artifact, which makes its failure mode unlike the rest of the
codebase: a manifest that silently lost its cables is byte-for-byte a plausible manifest, it gets
committed, and every consumer downstream then reports a *different* symptom (the exporter refuses,
the previewer draws flat bars, the build guide says nothing) with no trace of the one cause. So the
route rule fails the run instead of keeping what it happens to find - that is the regression GT's
2.9 rename produced and this pins (#176).

The rest of the tool is exercised by running it; this covers only the guard, because the guard is
the part that has to hold when a *new* dataset arrives and nobody is watching - plus the one example
the tool must skip, which is the same kind of quiet failure pointed the other way: a committed
manifest that grew to cover a line it was never meant to (#204).
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[1]
_TOOL = _REPO / "tools" / "derive_small_manifest.py"


def _tool() -> ModuleType:
    """``tools/derive_small_manifest.py`` imported as a module.

    Loaded by path rather than imported: ``tools/`` is a scripts folder, deliberately not a
    package, so there is nothing on ``sys.path`` to import it from.
    """
    spec = importlib.util.spec_from_file_location("derive_small_manifest", _TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pipe(display_name: str) -> dict[str, Any]:
    """One ``kind: "pipe"`` manifest entry, carrying only what the route lookup reads.

    Which is the name and the kind: the rule under test decides *which blocks to keep*, and the
    sprite stacks a preview would then bake are the previewer's problem, tested there.
    """
    return {"kind": "pipe", "display_name": display_name}


#: The six cable gauges plus every pipe size the router lays, as a dump spells them at each pack.
#: An LV line wants every one of these, so either list is a *complete* answer and neither is a
#: partial one. The large and huge tin pipes joined when the router began sizing item runs (#165);
#: both spellings are copied from real extractions, the 2.9 ones from the 2.9.0-beta-2 dump.
_GAUGES = (1, 2, 4, 8, 12, 16)
_AS_284 = [f"cable.tin.{gauge:02d}" for gauge in _GAUGES] + [
    "gt_pipe_bronze",
    "gt_pipe_tin",
    "gt_pipe_tin_large",
    "gt_pipe_tin_huge",
]
_AS_29 = [f"{gauge}x Tin Cable" for gauge in _GAUGES] + [
    "Bronze Fluid Pipe",
    "Tin Item Pipe",
    "Large Tin Item Pipe",
    "Huge Tin Item Pipe",
]


def _dump(names: list[str]) -> dict[str, Any]:
    return {
        "blocks": {
            f"gregtech:gt.blockmachines|{1240 + i}": _pipe(name) for i, name in enumerate(names)
        }
    }


def test_a_cable_the_dump_does_not_name_stops_the_run() -> None:
    """The silent empty set this replaced. Run against a dump whose spelling had moved on, the old
    rule kept nothing and said nothing, and the committed manifest went out with no cables in it.
    """
    tool = _tool()

    with pytest.raises(SystemExit) as raised:
        tool._route_keys(_dump(_AS_284[1:]), {"LV"})  # every wanted block but the 1x cable

    message = str(raised.value)
    assert "cable.tin.01" in message, "the run must name what it could not find"
    assert "1x Tin Cable" in message, "and both spellings it looked for"
    assert "cable.tin.02" not in message, "only the missing one; the rest resolved"


def test_a_pipe_size_the_router_lays_but_the_dump_lacks_stops_the_run() -> None:
    """The same guard for a size: a committed manifest without the huge pipe would pass every
    eyeball check and then refuse to export the parallel sand line (#165)."""
    tool = _tool()

    with pytest.raises(SystemExit) as raised:
        tool._route_keys(_dump([n for n in _AS_284 if n != "gt_pipe_tin_huge"]), {"LV"})

    message = str(raised.value)
    assert "gt_pipe_tin_huge" in message
    assert "Huge Tin Item Pipe" in message


def test_both_of_gts_spellings_are_accepted_for_the_same_block() -> None:
    """A dump carries one spelling or the other, never both, and either is a complete answer. The
    2.9 half is the fix: those names joined against nothing before, for every cable at once.
    """
    tool = _tool()

    kept = tool._route_keys(_dump(_AS_29), {"LV"})
    assert kept == tool._route_keys(_dump(_AS_284), {"LV"})
    assert len(kept) == len(_AS_29), "every gauge is kept, not only the ones a line routes today"


def test_the_2_9_acceptance_fixture_stays_out_of_the_committed_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ev-nitrobenzene.json`` is committed to test 2.9 work against, not to be skinned out of the
    box (#204). Were the tool to prune for it, the next regeneration would quietly pull its EV tier
    and its 2.9 machine names into the committed manifest.

    Run against an ``examples/`` holding only the excluded plans, so the answer cannot lean on what
    the other examples happen to use: skipped by name, they contribute nothing at all. A name that
    no longer matches a committed file would make the exclusion a silent no-op, hence the check.
    """
    tool = _tool()
    examples = tmp_path / "examples"
    examples.mkdir()
    for name in sorted(tool._NOT_MANIFEST_SCOPED):
        source = _REPO / "examples" / name
        assert source.is_file(), f"{name} is excluded by name but is not in examples/"
        shutil.copyfile(source, examples / name)
    monkeypatch.setattr(tool, "REPO", tmp_path)

    assert tool._example_types_and_tiers() == (set(), set())
