"""Derive the committed multiblock fixtures the shipped example lines need from a full local dump.

The structure dump (~300 controllers, ~60 MB) is local and version-namespaced
(``data/<version>/multiblocks/``, gitignored). A fresh clone has only the committed
``data/multiblocks/``, and a machine that data lacks is placed as a 1x1x1 box: a Distillation Tower
with seven connections cannot fit on one, so without these files the nitrobenzene lines cannot solve
in CI. This copies into ``data/multiblocks/`` each controller an example resolves to, trimmed to the
built forms the examples reserve, then proves the trim changed nothing the adapter hands the solver::

    the full dump (every form the extractor swept: 10 tower heights, 16 coke-oven lengths)
      |  adapt every example against it; each machine's footprint and hatch slots name the
      |  form it reserved
      v
    the reserved forms, plus every smaller form of a layer-indexed machine: a tower picks the
      |  shortest form with room for its outputs, and reads the forms as a height ladder only
      |  while they climb one layer at a time from the bottom (MachinePhysical.is_layer_indexed)
      v
    data/multiblocks/<the dump's file name>: the dump's own document, those variants only
      |  re-adapt every example against the committed directory
      v
    every machine the full dump resolved comes out identical, or the run stops

The two hand-authored fixtures (``_HAND_AUTHORED``) are never touched. Any other controller file in
``data/multiblocks/`` is this tool's output, so one the examples no longer need is deleted. The
maintainer allows trimmed per-controller files for the shipped examples, never a verbatim dump
(``data/multiblocks/README.md``). Rerun when the examples change or the pack moves.

Usage (from the repo root, in the dev venv)::

    python tools/derive_example_multiblocks.py [DUMP_DIR]

``DUMP_DIR`` defaults to the newest local ``data/<version>/multiblocks/`` that is a census.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from gtnh_solver.adapter import adapt_file
from gtnh_solver.dataset import (
    MachinePhysical,
    PhysicalDataset,
    list_versions,
    load_meta,
    load_physical_dataset,
)
from gtnh_solver.ir import Machine

REPO = Path.cwd()
COMMITTED = REPO / "data" / "multiblocks"
#: The illustrative fixtures written by hand (their README): not derived, never rewritten.
_HAND_AUTHORED = frozenset({"gregtech_machine_1000.json", "gregtech_machine_1001.json"})
#: A bracketed run of numbers or of plain strings, which ``json.dumps(indent=2)`` spreads one element
#: per line.
_INLINE_ARRAY = re.compile(r'\[\s+((?:-?\d+|"[^"\n]*")(?:,\s+(?:-?\d+|"[^"\n]*"))*)\s+\]')
#: An object whose values are all scalars or one-line arrays: a block, hint or hatch slot entry.
_FLAT_VALUE = r'(?:-?\d+|"[^"\n]*"|\[[^\[\]\n]*\]|true|false|null)'
_FLAT_OBJECT = re.compile(
    r'\{\s+("[^"\n]+": ' + _FLAT_VALUE + r'(?:,\s+"[^"\n]+": ' + _FLAT_VALUE + r")*)\s+\}"
)
#: The widest a flat object may be and still be joined onto one line, so a long controller name or
#: facing note stays readable.
_FLAT_WIDTH = 100


def _find_full_dump() -> Path:
    """The newest local ``data/<version>/multiblocks/`` that is a census of the pack."""
    for vdir in list_versions():
        candidate = vdir / "multiblocks"
        meta = candidate / "_meta.json"
        if meta.is_file() and load_meta(meta).census:
            return candidate
    raise SystemExit(
        "no census dump under data/<version>/multiblocks/; pass one explicitly or run the "
        "extractor first (see docs/dataset-extraction/implementation.md)"
    )


def _adapted(dataset: PhysicalDataset) -> dict[str, list[Machine]]:
    """Every shipped example's machines, adapted against ``dataset``, by example file name."""
    return {
        example.name: adapt_file(example, physical=dataset).machines
        for example in sorted((REPO / "examples").glob("*.json"))
    }


def _reserved_forms(
    dump: PhysicalDataset, adapted: dict[str, list[Machine]]
) -> dict[str, set[int]]:
    """``block_key`` -> the trigger stacks of the forms the examples reserve on that controller.

    Read back off the adapted machines rather than by re-running the adapter's selection: a
    machine's footprint and hatch slots are the form it reserved, so the answer is exactly what the
    solver was handed.
    """
    forms: dict[str, set[int]] = {}
    for machines in adapted.values():
        for machine in machines:
            record = dump.by_block_key.get(machine.block_key or "")
            if record is None:
                continue
            shape = next(
                s
                for s in record.variants
                if s.footprint == machine.footprint and s.slots == machine.hatch_slots
            )
            forms.setdefault(record.block_key, set()).add(shape.trigger_stack_size)
    return forms


def _kept_stacks(record: MachinePhysical, reserved: set[int]) -> set[int]:
    """The trigger stacks of the forms to keep so ``record`` still reserves ``reserved``.

    A layer-indexed machine keeps every form up to the tallest reserved one, since dropping a
    rung breaks the ladder and it would fall back to its largest form. Any other machine selects
    by trigger stack or takes its largest form, so its reserved forms alone select the same way.
    """
    if not record.is_layer_indexed:
        return reserved
    ladder = [shape.trigger_stack_size for shape in record.variants]  # smallest first
    top = max(ladder.index(stack) for stack in reserved)
    return set(ladder[: top + 1])


def _dump_files(dump_dir: Path) -> dict[str, Path]:
    """Every controller document in the dump, by ``block_key``."""
    files: dict[str, Path] = {}
    for path in sorted(dump_dir.glob("*.json")):
        if path.name == "_meta.json":
            continue
        controller = json.loads(path.read_text(encoding="utf-8"))["controller"]
        files[f"{controller['registry_name']}@{controller['meta']}"] = path
    return files


def _render(doc: dict[str, Any]) -> str:
    """``doc`` as indented JSON with each short array, and each short flat object, on one line.

    A variant is hundreds of three-field block entries, which plain ``indent=2`` spreads over nine
    lines each; one line apiece makes the file a third the size and reads like the list it is.
    """

    def join(match: re.Match[str], opener: str, closer: str) -> str:
        joined = opener + re.sub(r",\s+", ", ", match.group(1)) + closer
        return joined if len(joined) <= _FLAT_WIDTH else match.group(0)

    text = json.dumps(doc, indent=2, ensure_ascii=False)
    text = _INLINE_ARRAY.sub(lambda m: join(m, "[", "]"), text)
    return _FLAT_OBJECT.sub(lambda m: join(m, "{", "}"), text) + "\n"


def _check_unchanged(
    dump: PhysicalDataset, full: dict[str, list[Machine]], trimmed: dict[str, list[Machine]]
) -> None:
    """Stop the run unless every machine the full dump resolved adapts identically from the trim."""
    changed = []
    for example, machines in full.items():
        again = {m.id: m for m in trimmed[example]}
        changed.extend(
            f"{example}: {machine.id} ({machine.type})"
            for machine in machines
            if machine.block_key in dump.by_block_key and again.get(machine.id) != machine
        )
    if changed:
        raise SystemExit(
            "the trimmed fixtures adapt differently from the full dump for:\n  "
            + "\n  ".join(changed)
        )


def main() -> None:
    if not (REPO / "pyproject.toml").is_file():
        raise SystemExit(f"run from the repo root (cwd={REPO} has no pyproject.toml)")
    dump_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else _find_full_dump()
    dump = load_physical_dataset(dump_dir)
    if not dump.meta.census:
        raise SystemExit(f"{dump_dir} is not a census dump, so it cannot stand for the pack")

    full = _adapted(dump)
    sources = _dump_files(dump_dir)
    written: set[str] = set()
    for block_key, reserved in sorted(_reserved_forms(dump, full).items()):
        source = sources[block_key]
        keep = _kept_stacks(dump.by_block_key[block_key], reserved)
        doc = json.loads(source.read_text(encoding="utf-8"))
        doc["variants"] = [v for v in doc["variants"] if v["trigger_stack_size"] in keep]
        (COMMITTED / source.name).write_text(_render(doc), encoding="utf-8")
        written.add(source.name)
        print(f"  {source.name}: {doc['controller']['display_name']}, stacks {sorted(keep)}")

    for stale in sorted(COMMITTED.glob("*.json")):
        if stale.name not in written | _HAND_AUTHORED | {"_meta.json"}:
            stale.unlink()
            print(f"  removed {stale.name}: no example needs it")

    meta_path = COMMITTED / "_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["controller_count"] = len(written | _HAND_AUTHORED)
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    _check_unchanged(dump, full, _adapted(load_physical_dataset(COMMITTED)))
    print(
        f"wrote {len(written)} trimmed controller(s) to {COMMITTED.relative_to(REPO)} from {dump_dir}"
    )


if __name__ == "__main__":
    main()
