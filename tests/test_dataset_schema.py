"""Schema-v1 validation tests for the committed ``data/multiblocks/`` dataset.

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
    MultiblockDoc,
    load_meta,
    load_multiblock_doc,
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


def test_schema_field_loads_by_alias_and_by_name() -> None:
    # JSON carries the literal key "schema"; Python may also build by the field name.
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
