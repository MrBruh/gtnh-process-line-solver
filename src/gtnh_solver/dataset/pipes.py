"""The cable and pipe a route is *drawn and costed as* - a labelled stand-in, not a build spec.

GT gives a voltage tier many cable materials: at LV alone tin, lead, cobalt, zinc, soldering alloy
and redstone alloy all carry 32 V, differing in amperage and loss rather than in what they can
power. Nothing in this solver has ever chosen between them - the router sizes a cable by *gauge*
(amperage), never by material, and no dataset names one. So "the LV cable" is not a fact waiting to
be looked up; it is a choice, and this module is where it is made once, in the open.

**It is a stand-in and says so.** Every :class:`RouteMaterial` built here carries ``stand_in=True``,
which the build guide prints, the previewer's legend footnotes, and a ``.schematic`` exporter (#96)
must refuse to lower into a real block. A cable rendered in Tin when the build needs Aluminium is
plausible, confident and wrong, which docs/dataset-extraction/texture-resolution.md names as the one
failure nothing downstream can detect. Counts, gauges and thicknesses are real; only the material is
representative.

The ladder below is the community-standard one - the material a player actually builds at each
tier - because recognisability is the entire point of a stand-in. Every entry is verified present in
the extracted dataset with all six gauges; the test asserts that rather than trusting this comment.

**One cable has two names, and which one a dump carries depends on the pack.** Up to 2.8.4 the
texture dump recorded GT's *unlocalized* name (``cable.tin.02``, ``gt_pipe_bronze``); from GT5U
5.09.54.20 (pack 2.9) it records the *localized* display name (``2x Tin Cable``, ``Bronze Fluid
Pipe``), and the old join silently missed every one of them (#176). :func:`manifest_names` gives
both spellings of one block, newest first, so a lookup resolves against either dump and neither
dataset dates the other. The unlocalized form stays the name a layout *publishes*: it is the
locale-independent one, and it is what the build guide, the previewer scene and the schematic
goldens already carry.

Sibling of :mod:`gtnh_solver.dataset.voltage`: shared rule data, so the router chooses from it and
the validator re-derives against it independently (docs/ARCHITECTURE.md decision 4).
"""

from __future__ import annotations

from gtnh_solver.ir import Commodity, PipeFamily, RouteMaterial

from .voltage import VOLTAGE_BY_TIER, UnknownTierError

#: Voltage tier -> the insulated cable material a route at that tier is drawn as. GT's unlocalized
#: material name: the locale-independent one, which is why it is the id every other table here
#: keys on and the one a layout publishes.
#:
#: Stops at UV on purpose. Above it GT ships **no insulated cable at all** - UHV and beyond are
#: carried by superconductor *bare wire*, which is lossless and a different block family - so there
#: is nothing representative to name and :func:`route_material` says so instead of inventing one.
CABLE_MATERIAL_BY_TIER: dict[str, str] = {
    "ULV": "redalloy",
    "LV": "tin",
    "MV": "copper",
    "HV": "gold",
    "EV": "aluminium",
    "IV": "tungsten",
    "LuV": "niobiumtitanium",
    "ZPM": "naquadah",
    "UV": "naquadahalloy",
}

#: The same materials, spelled the way a 2.9+ dump's display name spells them ("12x **Niobium-
#: Titanium** Cable"). Every value is copied from a real manifest, never derived from the id beside
#: it: no casefold rule turns ``redalloy`` into "Red Alloy" or ``niobiumtitanium`` into
#: "Niobium-Titanium", and a spelling guessed from the id is exactly the plausible-confident-wrong
#: failure this module exists to avoid. A material absent here has no localized name to try, which
#: the test turns into a failure rather than a silent gap by asserting the two tables agree.
CABLE_DISPLAY_MATERIAL: dict[str, str] = {
    "redalloy": "Red Alloy",
    "tin": "Tin",
    "copper": "Copper",
    "gold": "Gold",
    "aluminium": "Aluminium",
    "tungsten": "Tungsten",
    "niobiumtitanium": "Niobium-Titanium",
    "naquadah": "Naquadah",
    "naquadahalloy": "Naquadah Alloy",
}

#: The pipe material each commodity is drawn as. One per family in v1, because nothing yet models
#: pipe throughput: a route is not sized, so a size ladder would be a distinction without a
#: difference. Both are the first pipe of their kind a player builds.
PIPE_MATERIAL: dict[Commodity, str] = {
    Commodity.FLUID: "bronze",
    Commodity.ITEM: "tin",
}

#: Pipe material -> what a 2.9+ display name calls it, family word and all ("Small **Bronze Fluid**
#: Pipe"). The family rides in the value rather than being looked up per commodity because the
#: material already determines it here - ``tin`` is this policy's *item* pipe - and one table copied
#: from a real manifest cannot disagree with itself. Same evidence rule as
#: :data:`CABLE_DISPLAY_MATERIAL`.
PIPE_DISPLAY_STEM: dict[str, str] = {
    "bronze": "Bronze Fluid",
    "tin": "Tin Item",
}

#: Cable gauge (amperage multiple) -> its rendered thickness in blocks, from GT's own constructors
#: (docs/DOMAIN.md). Keys are ``ir.CABLE_THICKNESSES``; an insulated cable is one step fatter than
#: the bare wire inside it.
CABLE_THICKNESS_BLOCKS: dict[int, float] = {
    1: 0.25,
    2: 0.375,
    4: 0.5,
    8: 0.625,
    12: 0.75,
    16: 0.875,
}

#: The pipe size v1 draws, and its thickness in blocks. GT's ladder runs tiny/small/normal/large/huge
#: (and quadruple/nonuple for fluids, which render as full cubes), but sizing a pipe means modelling
#: throughput, which is Phase 2 - so v1 draws the middle of the ladder and says nothing it cannot
#: back up. Verified identical for both families at this size.
DEFAULT_PIPE_SIZE = "normal"
DEFAULT_PIPE_THICKNESS_BLOCKS = 0.5

#: That ladder -> the word a 2.9+ display name puts in front of the material. The normal size is
#: bare in both spellings, which is why its word is empty rather than absent. Quadruple and nonuple
#: exist for fluids only, so the pair built for an item pipe names no real block; it is a candidate
#: that simply never matches, and v1 asks for neither.
_PIPE_SIZE_WORD: dict[str, str] = {
    "tiny": "Tiny ",
    "small": "Small ",
    DEFAULT_PIPE_SIZE: "",
    "large": "Large ",
    "huge": "Huge ",
    "quadruple": "Quadruple ",
    "nonuple": "Nonuple ",
}

#: Which transport family carries each commodity (the same mapping ``Route`` validates against).
_FAMILY_FOR: dict[Commodity, PipeFamily] = {
    Commodity.ITEM: PipeFamily.ITEM_PIPE,
    Commodity.FLUID: PipeFamily.FLUID_PIPE,
    Commodity.POWER: PipeFamily.CABLE,
}


def cable_display_name(material: str, gauge: int) -> str:
    """The canonical name for one insulated cable, e.g. ``cable.tin.02``.

    GT's unlocalized naming, and the name a layout publishes: locale-independent, so it identifies
    the block whatever a given dump chose to record. Ask :func:`manifest_names` for the keys a
    manifest may actually carry it under - a 2.9+ dump records the localized one instead.
    """
    return f"cable.{material}.{gauge:02d}"


def pipe_display_name(material: str, size: str = DEFAULT_PIPE_SIZE) -> str:
    """The canonical name for one fluid or item pipe, e.g. ``gt_pipe_bronze``.

    The normal size is the bare name - GT suffixes only the others (``gt_pipe_bronze_large``), which
    is why the default has no suffix rather than an explicit one. Same two-names caveat as
    :func:`cable_display_name`: :func:`manifest_names` is what a lookup joins on.
    """
    return f"gt_pipe_{material}" if size == DEFAULT_PIPE_SIZE else f"gt_pipe_{material}_{size}"


#: Canonical name -> the localized name a 2.9+ dump records the same block under. Built from the
#: tables above rather than written out, so the two spellings of one cable cannot drift apart and a
#: material added to the policy is either given a display name or caught by the test that pins the
#: tables against each other.
_LOCALIZED_BY_CANONICAL: dict[str, str] = {
    **{
        cable_display_name(material, gauge): f"{gauge}x {display} Cable"
        for material, display in CABLE_DISPLAY_MATERIAL.items()
        for gauge in CABLE_THICKNESS_BLOCKS
    },
    **{
        pipe_display_name(material, size): f"{word}{stem} Pipe"
        for material, stem in PIPE_DISPLAY_STEM.items()
        for size, word in _PIPE_SIZE_WORD.items()
    },
}


def manifest_names(canonical: str) -> tuple[str, ...]:
    """Every key a texture manifest may record this cable or pipe under, newest spelling first.

    The join between the policy above and the manifest entry carrying the sprite, and it has to
    try two names because GT renamed them: 2.8.4 dumps key on the unlocalized name this module
    generates, 2.9 dumps on the localized display name (#176). Ordered newest-first so a current
    dump answers on the first try; a caller resolves by taking the first name it holds and must
    say so loudly when it holds none, because a cable that resolves to nothing renders as a flat
    bar with no error anywhere.

    A name this policy never generated has no localized form to offer and comes back alone, which
    keeps the lookup exact: this maps between two spellings of a *known* block, it does not guess
    at one it has never heard of.
    """
    localized = _LOCALIZED_BY_CANONICAL.get(canonical)
    return (localized, canonical) if localized is not None else (canonical,)


def route_material(commodity: Commodity, tier: str | None = None) -> RouteMaterial | None:
    """The stand-in a route of this commodity is drawn as, or ``None`` when none is known.

    ``None`` is a real answer, not a failure: a tier above UV has no insulated cable to be
    representative of, and a route with no material renders exactly as it did before this existed -
    an honest flat bar. An *unknown* tier is different and raises, because a typo must not
    degrade silently into "no material".
    """
    if commodity is not Commodity.POWER:
        return RouteMaterial(
            family=_FAMILY_FOR[commodity], material=PIPE_MATERIAL[commodity], stand_in=True
        )
    if tier is None:
        return None  # the caller could not agree on one tier for this trunk; say nothing
    if tier not in VOLTAGE_BY_TIER:
        raise UnknownTierError(tier)
    material = CABLE_MATERIAL_BY_TIER.get(tier)
    if material is None:
        return None  # above UV: superconductor bare wire, no insulated cable exists
    return RouteMaterial(family=PipeFamily.CABLE, material=material, tier=tier, stand_in=True)
