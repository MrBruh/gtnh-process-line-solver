"""``tools/derive_pipe_names.py``: the filter behind the local 2.9 cable/pipe name list.

The list itself is dumper output and never committed, so the 2.9 check in ``test_dataset_pipes``
only runs where someone generated it, and never in CI. What CI *can* hold is the derivation: that
it is exactly "``kind == "pipe"``, take ``display_name``" and nothing shaped by the solver, that it
refuses a pruned manifest (a list taken from one would agree with the policy by construction), and
that a 2.9 dump lands at the path the check reads. These run on synthetic manifests.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "derive_pipe_names.py"


def _tool() -> ModuleType:
    """The tool imported by path: ``tools/`` is a scripts folder, deliberately not a package."""
    spec = importlib.util.spec_from_file_location("derive_pipe_names", _TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _manifest(pipes: int) -> dict[str, Any]:
    """A 2.9-shaped manifest with ``pipes`` distinct pipe names, plus what the filter must drop."""
    blocks: dict[str, Any] = {
        f"gt.blockmachines|{i}": {"kind": "pipe", "display_name": f"Pipe {i:04d}"}
        for i in range(pipes)
    }
    # Two blocks one name (the dump has key collisions), a nameless pipe, and non-pipe kinds.
    blocks["gt.blockmachines|dup"] = {"kind": "pipe", "display_name": "Pipe 0000"}
    blocks["gt.blockmachines|anon"] = {"kind": "pipe"}
    blocks["gt.blockmachines|mte"] = {"kind": "mte", "display_name": "Basic Forge Hammer"}
    blocks["minecraft:stone|0"] = {"kind": "block", "display_name": "Stone"}
    return {
        "provenance": {
            "pack_version": "2.9.0-beta-2",
            "mod_versions": {"GT5-Unofficial": "5.09.54.20"},
            "generated_at": "2026-09-20T20:07:59.456Z",
        },
        "blocks": blocks,
    }


def test_the_list_is_every_pipe_display_name_and_nothing_else() -> None:
    tool = _tool()
    n = tool.MIN_FULL_DUMP_PIPES
    doc = tool.derive(_manifest(n), "data/2.9.0-beta-2/textures/manifest.json")
    assert doc["display_names"] == [f"Pipe {i:04d}" for i in range(n)]  # sorted, each once
    assert doc["pipe_entries"] == n + 2
    assert doc["pack_version"] == "2.9.0-beta-2"
    assert doc["source"] == "data/2.9.0-beta-2/textures/manifest.json"


def test_a_pruned_manifest_is_refused_rather_than_written() -> None:
    tool = _tool()
    with pytest.raises(SystemExit, match="looks pruned"):
        tool.derive(_manifest(tool.MIN_FULL_DUMP_PIPES - 1), "small.json")


def test_a_29_dump_is_written_where_the_29_check_reads_it(tmp_path: Path) -> None:
    tool = _tool()
    read_by_the_29_check = Path(__file__).resolve().parent / "fixtures" / "local"
    assert read_by_the_29_check == tool.LOCAL_FIXTURES
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(_manifest(tool.MIN_FULL_DUMP_PIPES)), encoding="utf-8")

    out = tool.main([str(source)], out_dir=tmp_path / "local")

    assert out == tmp_path / "local" / "manifest_pipe_names_2.9.json"
    assert len(json.loads(out.read_text(encoding="utf-8"))["display_names"]) == (
        tool.MIN_FULL_DUMP_PIPES
    )
