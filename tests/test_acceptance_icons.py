"""Local-only acceptance: a real icon index pictures everything the shipped lines move (#297).

``tools/derive_icons.py`` makes ``data/<pack>/icons/`` from a NESQL export run in a full pack
instance (``docs/dataset-extraction/icons.md``). The unit tests hold the reader and the tool to
the rules on a hand-written export; this holds a real one to the job. Every fluid and item each
plan in ``examples/`` moves (``previewer.icons.carried_kinds``, what a preview asks the index for)
must come back with a name and a PNG, looked up the way a preview looks it up::

    examples/*.json --adapt_file--> InputIR --carried_kinds--> {id: item | fluid}
        --IconPack.lookup--> entry with a name --IconPack.png--> a PNG under the size cap

**It needs the real index, so it skips without one**, as it does in CI and on a fresh clone. The
suite pins the dataset every other test resolves to the committed copy (``tests/conftest.py``), so
the index is read from the repo's own ``data/`` by explicit path rather than through ``roots``.

A resource the export has no picture of is listed in :data:`KNOWN_GAPS` with the reason, never
skipped quietly; a listed gap that the index has since filled fails too, so the list stays true.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from gtnh_solver.adapter import adapt_file
from gtnh_solver.dataset.icons import ICON_INDEX, IconPack
from gtnh_solver.ir import Commodity
from gtnh_solver.previewer.icons import carried_kinds

_ROOT = Path(__file__).resolve().parents[1]
_PACK = "2.9.0-beta-2"
_INDEX = _ROOT / "data" / _PACK / ICON_INDEX
_EXAMPLES = sorted((_ROOT / "examples").glob("*.json"))

#: Resource id -> why the real export has no name or picture for it. Starts empty: the first dry
#: run against a real 2.9 export fills it, each id with the reason the export cannot show it. Not
#: a place to park an id the index failed to join; that is a bug in the join.
KNOWN_GAPS: dict[str, str] = {}

pytestmark = pytest.mark.skipif(
    not _INDEX.is_file(),
    reason=f"no local {_PACK} icon index at {_INDEX}; docs/dataset-extraction/icons.md makes one",
)


@pytest.fixture(scope="module")
def pack() -> Iterator[IconPack]:
    with IconPack.load(_INDEX) as loaded:
        yield loaded


def _gap(pack: IconPack, resource: str, kind: Commodity) -> str | None:
    """Why ``pack`` cannot show ``resource``, or ``None`` when it has a name and a PNG for it.
    An image that is there but unusable raises, as it would in a preview."""
    entry = pack.lookup(resource, kind)
    if entry is None:
        return "not in the index"
    if not entry.name:
        return "no name"
    if pack.png(entry) is None:
        return "no picture"
    return None


@pytest.mark.parametrize("example", _EXAMPLES, ids=[path.stem for path in _EXAMPLES])
def test_every_resource_a_shipped_line_moves_has_a_name_and_a_picture(
    pack: IconPack, example: Path
) -> None:
    gaps = {
        resource: gap
        for resource, kind in carried_kinds(adapt_file(example)).items()
        if resource not in KNOWN_GAPS and (gap := _gap(pack, resource, kind)) is not None
    }
    assert not gaps, f"{example.name}: resources the index cannot show: {gaps}"


def test_every_known_gap_is_still_a_gap(pack: IconPack) -> None:
    carried: dict[str, Commodity] = {}
    for example in _EXAMPLES:
        carried.update(carried_kinds(adapt_file(example)))
    stale = {
        resource
        for resource in KNOWN_GAPS
        if resource not in carried or _gap(pack, resource, carried[resource]) is None
    }
    assert not stale, f"KNOWN_GAPS lists ids no shipped line still lacks: {sorted(stale)}"
