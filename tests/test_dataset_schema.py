"""Schema-v2 validation tests for the committed ``data/multiblocks/`` dataset.

Proves the extractor contract holds for every committed file (schema.py) and that the loader
fails loud on a malformed one - the "entries load + validate; bad footprint raises clearly" gate
of docs/TESTING.md, and the schema-validation half of the golden strategy (plan section 7).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from gtnh_solver.dataset import (
    SCHEMA_VERSION,
    DatasetSchemaError,
    MultiblockDoc,
    load_meta,
    load_multiblock_doc,
    load_physical_dataset,
    multiblock_json_schema,
)

_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "multiblocks"
_META = _DATA_DIR / "_meta.json"

#: Lenient ceiling on how many controllers the extractor may fail to dump (plan section 7:
#: "start lenient, ratchet down"). The illustrative fixtures have zero; a real dump should stay
#: well under this and the number should shrink as coverage improves.
_MAX_FAILURES = 25


def _multiblock_files() -> list[Path]:
    return sorted(p for p in _DATA_DIR.glob("*.json") if p.name != "_meta.json")


def test_data_dir_has_multiblock_files() -> None:
    assert _multiblock_files(), "expected at least one committed data/multiblocks/*.json fixture"


@pytest.mark.parametrize("path", _multiblock_files(), ids=lambda p: p.name)
def test_every_multiblock_file_validates(path: Path) -> None:
    doc = load_multiblock_doc(path)
    assert doc.schema_version == SCHEMA_VERSION
    assert doc.controller.display_name
    assert doc.variants  # min_length=1 is enforced, but assert the intent explicitly


def test_meta_validates_and_matches_schema_version() -> None:
    meta = load_meta(_META)
    assert meta.schema_version == SCHEMA_VERSION
    assert meta.pack_version
    assert meta.mod_versions  # a real dump names the mods it was built from


def test_meta_controller_count_matches_committed_files() -> None:
    meta = load_meta(_META)
    assert meta.controller_count == len(_multiblock_files())


def test_meta_failure_list_below_threshold() -> None:
    meta = load_meta(_META)
    assert len(meta.failures) <= _MAX_FAILURES


def test_unknown_field_fails_loud() -> None:
    payload = json.loads((_DATA_DIR / "gregtech_machine_1000.json").read_text(encoding="utf-8"))
    payload["controller"]["surprise"] = "not in the schema"
    with pytest.raises(ValidationError):
        MultiblockDoc.model_validate(payload)


def test_missing_required_field_fails_loud() -> None:
    payload = json.loads((_DATA_DIR / "gregtech_machine_1000.json").read_text(encoding="utf-8"))
    del payload["variants"][0]["bbox"]
    with pytest.raises(ValidationError):
        MultiblockDoc.model_validate(payload)


# ------------------------------------------------------------------ hatch slots (schema v2)


def test_hatch_slots_parse_with_their_kinds() -> None:
    payload = json.loads((_DATA_DIR / "gregtech_machine_1000.json").read_text(encoding="utf-8"))
    payload["variants"][0]["hatch_slots"] = [{"d": [0, 1, 1], "kinds": ["OutputHatch", "InputBus"]}]
    doc = MultiblockDoc.model_validate(payload)
    slot = doc.variants[0].hatch_slots[0]
    assert slot.d == (0, 1, 1)
    assert slot.kinds == ["OutputHatch", "InputBus"]


def test_hatch_slot_with_no_kinds_fails_loud() -> None:
    # A slot that accepts nothing is not a slot; the extractor omits the cell entirely rather than
    # emitting an empty list, so an empty one means the dump is malformed.
    payload = json.loads((_DATA_DIR / "gregtech_machine_1000.json").read_text(encoding="utf-8"))
    payload["variants"][0]["hatch_slots"] = [{"d": [0, 1, 1], "kinds": []}]
    with pytest.raises(ValidationError):
        MultiblockDoc.model_validate(payload)


def test_a_file_without_hatch_slots_still_parses() -> None:
    # The field defaults, so a pre-v2 body loads in-process; it is the `schema` version, not a parse
    # error, that identifies a stale dump. Guards against making the field required by accident.
    payload = json.loads((_DATA_DIR / "gregtech_machine_1000.json").read_text(encoding="utf-8"))
    for variant in payload["variants"]:
        variant.pop("hatch_slots", None)
    doc = MultiblockDoc.model_validate(payload)
    assert doc.variants[0].hatch_slots == []


def test_schema_field_loads_by_alias_and_by_name() -> None:
    # JSON carries the literal key "schema"; Python may also build by the field name. The version
    # is gated on the way in from DISK (see the section below), not in the model, so a payload
    # assembled in-process is free to say anything - it is not a stale dump.
    by_alias = MultiblockDoc.model_validate(
        {
            "schema": 1,
            "controller": {
                "registry_name": "gregtech:gt.blockmachines",
                "meta": 0,
                "display_name": "X",
                "source_class": "C",
            },
            "variants": [
                {
                    "trigger_stack_size": 1,
                    "blocks": [{"d": [0, 0, 0], "block": "b"}],
                    "bbox": [1, 1, 1],
                }
            ],
        }
    )
    assert by_alias.schema_version == 1


def test_multiblock_json_schema_is_derived_and_uses_the_alias() -> None:
    schema = multiblock_json_schema()
    assert schema["properties"].keys() >= {"schema", "controller", "variants", "substitutions"}


# ------------------------------------------------------------ the version gate (#113)
#
# SCHEMA_VERSION was declared, exported, parsed into both models and never compared, so a dump
# from an older extractor run - the expected state of a gitignored, regenerated-on-demand
# data/<version>/ folder - parsed clean and was read as a machine with no hatch cells recorded.
# These hold the two file loaders to refusing it instead, in both directions.


def _doc_file(tmp_path: Path, schema: int, name: str = "machine.json") -> Path:
    """A committed fixture re-stamped with ``schema``, written where a loader will read it."""
    payload = json.loads((_DATA_DIR / "gregtech_machine_1000.json").read_text(encoding="utf-8"))
    payload["schema"] = schema
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _meta_file(tmp_path: Path, schema: int) -> Path:
    payload = json.loads(_META.read_text(encoding="utf-8"))
    payload["schema"] = schema
    payload["controller_count"] = 1
    path = tmp_path / "_meta.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize("stale", [1, SCHEMA_VERSION + 1])
def test_a_multiblock_file_of_another_schema_is_refused(tmp_path: Path, stale: int) -> None:
    """Named in both directions, because neither version can be read as this one.

    The older direction is the one that used to pass silently (a v1 file carries no key this build
    does not know, so ``extra="forbid"`` never fires); the newer direction usually trips on an
    unknown field, but must not depend on the next bump happening to add one.
    """
    path = _doc_file(tmp_path, stale)
    with pytest.raises(DatasetSchemaError) as exc:
        load_multiblock_doc(path)
    message = str(exc.value)
    assert str(stale) in message
    assert str(SCHEMA_VERSION) in message
    assert path.name in message  # which file, not just which versions


def test_a_meta_file_of_another_schema_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DatasetSchemaError, match=r"schema version 1"):
        load_meta(_meta_file(tmp_path, 1))


def test_a_stale_dump_is_refused_as_a_whole_before_any_machine_is_read(tmp_path: Path) -> None:
    """The acceptance case: a stale ``data/<version>/multiblocks/`` fails loud, not quietly.

    ``_meta.json`` is read first, so the whole dump is refused on its run summary; the machine
    files never get the chance to parse as "no hatch slots recorded".
    """
    _meta_file(tmp_path, 1)
    _doc_file(tmp_path, 1)
    with pytest.raises(DatasetSchemaError, match=r"_meta\.json"):
        load_physical_dataset(tmp_path)


def test_the_refusal_is_a_valueerror_so_the_cli_still_degrades(tmp_path: Path) -> None:
    """``cli._load_physical_or_warn`` catches ``(OSError, ValueError, ValidationError)`` to keep
    the documented 0/1/2 exit contract. A stale dump must land in that net: warn and fall back to
    1x1x1 footprints, never take the run down."""
    assert issubclass(DatasetSchemaError, ValueError)
    with pytest.raises(ValueError, match="schema version"):
        load_multiblock_doc(_doc_file(tmp_path, 1))


def test_the_committed_dump_passes_its_own_gate() -> None:
    # Whatever else moves, the shipped fixtures must load through the gate, not around it.
    assert load_meta(_META).schema_version == SCHEMA_VERSION
    for path in _multiblock_files():
        assert load_multiblock_doc(path).schema_version == SCHEMA_VERSION


def test_a_file_with_no_schema_key_reports_the_missing_field(tmp_path: Path) -> None:
    # The gate abstains on a payload that does not state a version: that is a malformed file, and
    # the model says so far better than a version comparison could.
    payload = json.loads((_DATA_DIR / "gregtech_machine_1000.json").read_text(encoding="utf-8"))
    del payload["schema"]
    path = tmp_path / "machine.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError, match="schema"):
        load_multiblock_doc(path)
