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

Sibling of :mod:`gtnh_solver.dataset.voltage`: shared rule data, so the router chooses from it and
the validator re-derives against it independently (docs/ARCHITECTURE.md decision 4).
"""

from __future__ import annotations

from gtnh_solver.ir import Commodity, PipeFamily, RouteMaterial

from .voltage import VOLTAGE_BY_TIER, UnknownTierError

#: Voltage tier -> the insulated cable material a route at that tier is drawn as. GT's unlocalized
#: material name, which is what the texture manifest keys on and what is stable across locales.
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

#: The pipe material each commodity is drawn as. One per family in v1, because nothing yet models
#: pipe throughput: a route is not sized, so a size ladder would be a distinction without a
#: difference. Both are the first pipe of their kind a player builds.
PIPE_MATERIAL: dict[Commodity, str] = {
    Commodity.FLUID: "bronze",
    Commodity.ITEM: "tin",
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

#: Which transport family carries each commodity (the same mapping ``Route`` validates against).
_FAMILY_FOR: dict[Commodity, PipeFamily] = {
    Commodity.ITEM: PipeFamily.ITEM_PIPE,
    Commodity.FLUID: PipeFamily.FLUID_PIPE,
    Commodity.POWER: PipeFamily.CABLE,
}


def cable_display_name(material: str, gauge: int) -> str:
    """The dataset's name for one insulated cable, e.g. ``cable.tin.02``.

    GT's unlocalized naming, which the texture dump records verbatim, so this is the join key
    between the policy above and the manifest entry that carries the sprite.
    """
    return f"cable.{material}.{gauge:02d}"


def pipe_display_name(material: str, size: str = DEFAULT_PIPE_SIZE) -> str:
    """The dataset's name for one fluid or item pipe, e.g. ``gt_pipe_bronze``.

    The normal size is the bare name - GT suffixes only the others (``gt_pipe_bronze_large``), which
    is why the default has no suffix rather than an explicit one.
    """
    return f"gt_pipe_{material}" if size == DEFAULT_PIPE_SIZE else f"gt_pipe_{material}_{size}"


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
