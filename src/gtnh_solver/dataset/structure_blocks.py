"""GT's tiered structure blocks: the casings, pipes and coils a multiblock's channels choose between.

A GT multiblock names some of its parts by a StructureLib **channel** (``GTStructureChannels``)
rather than by one block: the part is built from any block on a tier ladder, and the tier changes
what the machine does. The ExxonMobil Chemical Plant (``MTEChemicalPlant``, GT 5.09.54.133) has four
such parts, and each decides whether, or how fast, the built plant runs::

    channel          blocks GT accepts                what GT does with the tier
    ---------------  -------------------------------  --------------------------------------------
    casing           SOLID_CASINGS, tiers 0-7         runs a recipe only if its special value is
                                                      <= the tier (validateRecipe)
    pipe             gt.blockcasings2 metas 12-15     parallels = 2 x (meta - 11)
    coil             HEATING_COILS                    speed bonus 2 / (1 + tier): Cupronickel,
                                                      tier 0, runs at half speed
    machine_casing   gt.blockcasings metas 0-9        fails to form if meta < the highest hatch
                     (meta = tier, ULV..UHV)          tier, unless UHV (checkMachine)

A heating coil also has a **heat** (#318): 1801 K for Cupronickel, then 900 K per coil tier
(:data:`HEATING_COIL_HEATS`). The Electric Blast Furnace family refuses a recipe whose special
value, read as kelvin, is above the heat it reaches (:data:`COIL_HEAT_GATES`)::

    machine                                  heat a recipe's special value must not exceed
    ---------------------------------------  ---------------------------------------------
    EBF, Mega EBF, Exothermic Hearth         coil heat + 100 K x (supplied tier - MV)
    Volcanus, DTPF, Digester, Utupu-Tanuri   coil heat

This module holds that **rule data and nothing a plan decides** (the split ``voltage.py`` has with
``adapter/power.py``): the tables, the voltage-to-machine-casing and voltage-to-heat rules, and
:func:`channel_blocks`, the one place every consumer asks which blocks a channel accepts.

**The dump under-records two of the plant's channels.** The extractor records a channel's
alternatives by building the structure from each trigger stack and noting what lands. GT++'s
``GTPPMultiBlockBase.addTieredBlock`` places meta ``minMeta + stack`` for a stack of at least 1,
but its ``check()`` accepts every meta from ``minMeta`` (lines 699-748), so the lowest tier is valid
in game and never placed: Bronze pipe casing (meta 12) and the ULV machine casing (meta 0) are
absent from every dump. :func:`channel_blocks` fills exactly those from the cited rule, and only
for the plant, where the rule is read off its own structure definition. A dump that records a block
outside the rule's set contradicts the rule, so the dump wins and a warning says so. The extractor
recording what ``check()`` accepts would retire this.

**Cells are matched to a channel by membership.** The dump tags no cell with its channel, so a cell
belongs to a channel when its block is one the channel accepts. Never by a channel's first entry
(each Industrial Coke Oven form built from stack N carries coil N) and never by ``channel_value``
(the hand-written EBF fixture numbers its coils 0-3).
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .roots import DatasetWarning
from .voltage import VOLTAGE_BY_TIER

#: A block as ``(registry name, meta)``: the identity a dump cell, a channel alternative and a
#: texture-manifest entry share.
BlockId = tuple[str, int]

#: The ExxonMobil Chemical Plant's controller block (``MTEChemicalPlant``).
CHEMICAL_PLANT = "gregtech:gt.blockmachines@998"

#: GT's channel ids (``GTStructureChannels``), the keys a dump's ``substitutions`` use.
SOLID_CASING = "casing"  # METAL_MACHINE_CASING
MACHINE_CASING = "machine_casing"  # TIER_MACHINE_CASING
COIL = "coil"  # HEATING_COIL
PIPE = "pipe"  # PIPE_CASING


@dataclass(frozen=True)
class TieredBlock:
    """One rung of a tier ladder: the key a plan names it by, and the block GT builds it from."""

    key: str
    block: str
    meta: int

    @property
    def block_id(self) -> BlockId:
        return (self.block, self.meta)


_GT = "gregtech:"
_GTPP_SOLID = "miscutils:gtplusplus.blockspecialcasings.2"

#: The plant's solid casings, index = GT tier (``GregtechAlgaeContent.registerMachineCasingForTier``,
#: lines 37-52). The keys are the ones gtnh-shadow-convert writes as ``machineConfigTiers.solidCasing``.
SOLID_CASINGS: tuple[TieredBlock, ...] = (
    TieredBlock("bronze", _GTPP_SOLID, 0),
    TieredBlock("steel", f"{_GT}gt.blockcasings2", 0),
    TieredBlock("aluminium", _GTPP_SOLID, 1),
    TieredBlock("stainless_steel", f"{_GT}gt.blockcasings4", 1),
    TieredBlock("titanium", f"{_GT}gt.blockcasings4", 2),
    TieredBlock("tungstensteel", f"{_GT}gt.blockcasings4", 0),
    TieredBlock("laurenium", _GTPP_SOLID, 2),
    TieredBlock("botmium", _GTPP_SOLID, 3),
)

#: The plant's pipe casings, GT pipe tier = index + 1 (``addTieredBlock(sBlockCasings2, ..., 12, 16)``).
#: The keys are gtnh-factory-flow's ``pipeCasing`` control keys.
PIPE_CASINGS: tuple[TieredBlock, ...] = tuple(
    TieredBlock(key, f"{_GT}gt.blockcasings2", 12 + index)
    for index, key in enumerate(("bronze", "steel", "titanium", "tungstensteel"))
)

#: ``pipeCasing`` keys gtnh-factory-flow offers that the plant's ``check()`` refuses (PTFE is
#: ``gt.blockcasings8``, PBI ``gt.blockcasings9``). The planner itself rates them as Tungstensteel
#: for the plant (``normalizeFluidPipeSettings``), so they build as that.
PIPE_CASING_ALIASES: dict[str, str] = {"ptfe": "tungstensteel", "pbi": "tungstensteel"}

#: GT's heating coils, index = coil tier (``HeatingCoilLevel.getTier()``, LV/Cupronickel = 0). The
#: keys are gtnh-factory-flow's ``heatingCoil`` control keys; the metas follow
#: ``BlockCasings5.getCoilHeatFromDamage``, which is why HSS-S (9) and Trinium (10) sit out of order.
HEATING_COILS: tuple[TieredBlock, ...] = tuple(
    TieredBlock(key, f"{_GT}gt.blockcasings5", meta)
    for key, meta in (
        ("cupronickel", 0),
        ("kanthal", 1),
        ("nichrome", 2),
        ("tpv", 3),
        ("hss_g", 4),
        ("hss_s", 9),
        ("naquadah", 5),
        ("naquadah_alloy", 6),
        ("trinium", 10),
        ("electrum_flux", 7),
        ("awakened_draconium", 8),
        ("infinity", 11),
        ("hypogen", 12),
        ("eternal", 13),
    )
)

#: Each heating coil's heat in kelvin, index = coil tier as in :data:`HEATING_COILS`.
#: ``HeatingCoilLevel.getHeat()`` is ``1 + 900 * ordinal`` (lines 32-34), and the coil tier is the
#: ordinal less 2 (``getTier()``, lines 39-41): Cupronickel, meta 0, is ``LV``, ordinal 2
#: (``BlockCasings5.getCoilHeatFromDamage``, lines 225-243), so 1801 K, then 900 K per tier.
HEATING_COIL_HEATS: tuple[int, ...] = tuple(1801 + 900 * tier for tier in range(len(HEATING_COILS)))

#: The controllers whose ``validateRecipe`` refuses a recipe whose special value is above their
#: heat, mapped to whether that heat adds the hatch-tier bonus (:func:`heat_bonus_for`) to the
#: coil's. Lines are GT 5.09.54.133 (pack 2.9.0-beta-3), then 5.09.51.482 (pack 2.8.4) where the
#: machine exists there; every one compares ``recipe.mSpecialValue <=`` the heat. Left out: the
#: Godforge modules, whose heat comes from upgrades over a fixed coil, and every machine whose coil
#: sets only its speed, parallels or EU/t (Multi Smelter, Pyrolyse Oven, Oil Cracker, Industrial
#: Coke Oven, Large Fluid Extractor, the Chemical Plant, ...).
COIL_HEAT_GATES: dict[str, bool] = {
    # Electric Blast Furnace, MTEElectricBlastFurnace: validateRecipe 213-215, heat = coil heat
    # + 100 * (GTUtility.getTier(getMaxInputVoltage()) - 2) at 237 (2.8.4: 210-212, 229).
    "gregtech:gt.blockmachines@1000": True,
    # Mega Electric Blast Furnace, bartworks MTEMegaBlastFurnaceLegacy: validateRecipe 301-304,
    # heat = coil heat + 100 * (BWUtil.getTier(getMaxInputEu()) - 2) at 370-371 (2.8.4: the class
    # is MTEMegaBlastFurnace, 304-307 and 370-371).
    "gregtech:gt.blockmachines@12730": True,
    # Exothermic Hearth, MTEExothermicHearth: validateRecipe 326-333, heat = coil heat
    # + 100 * (GTUtility.getTierExtended(getMaxInputEu()) - 2) at 523-524. Not in 2.8.4.
    "gregtech:gt.blockmachines@15517": True,
    # Volcanus, gtPlusPlus MTEAdvEBF: validateRecipe 257-259 compares getCoilLevel().getHeat()
    # (2.8.4: 242-244).
    "gregtech:gt.blockmachines@963": False,
    # Dimensionally Transcendent Plasma Forge, MTEPlasmaForge: validateRecipe 735-737, heat = coil
    # heat at 826, "No free heat from extra EU!" (2.8.4: 788-790, 897).
    "gregtech:gt.blockmachines@1004": False,
    # Digester, gtnhlanth MTEDigester: validateRecipe 138-141 compares getCoilLevel().getHeat()
    # (2.8.4: 113-116).
    "gregtech:gt.blockmachines@10500": False,
    # Utupu-Tanuri, gtPlusPlus MTEIndustrialDehydrator: validateRecipe 247-249 compares
    # getCoilLevel().getHeat() (2.8.4: 231-233).
    "gregtech:gt.blockmachines@995": False,
}

#: The machine casing block: meta = voltage tier, ULV 0 through UHV 9 (``MACHINE_<tier>_*``).
_MACHINE_CASING_BLOCK = f"{_GT}gt.blockcasings"
_UHV_MACHINE_CASING = 9

#: The blocks GT's ``check()`` accepts for the channels :func:`channel_blocks` patches, keyed by
#: ``(controller, channel)``. Read off ``MTEChemicalPlant.getStructureDefinition``: ``M`` is
#: ``addTieredBlock(sBlockCasings1, ..., 10)`` (metas 0-9), ``P`` is
#: ``addTieredBlock(sBlockCasings2, ..., 12, 16)`` (metas 12-15), and ``C`` is the 8-tier chain.
_ACCEPTED: dict[tuple[str, str], tuple[BlockId, ...]] = {
    (CHEMICAL_PLANT, SOLID_CASING): tuple(t.block_id for t in SOLID_CASINGS),
    (CHEMICAL_PLANT, PIPE): tuple(t.block_id for t in PIPE_CASINGS),
    (CHEMICAL_PLANT, MACHINE_CASING): tuple(
        (_MACHINE_CASING_BLOCK, meta) for meta in range(_UHV_MACHINE_CASING + 1)
    ),
}


def tier_block(
    table: Sequence[TieredBlock], key: str, aliases: Mapping[str, str] | None = None
) -> tuple[TieredBlock | None, bool]:
    """The rung of ``table`` that ``key`` names, and whether ``key`` reached it through an alias.

    ``(None, False)`` for a key the table does not know. ``aliases`` maps a key the ladder does not
    accept onto the one GT builds instead (:data:`PIPE_CASING_ALIASES`).
    """
    canonical = (aliases or {}).get(key)
    wanted = key if canonical is None else canonical
    for rung in table:
        if rung.key == wanted:
            return rung, canonical is not None
    return None, False


def machine_casing_for(voltage_tier: str) -> BlockId | None:
    """The machine casing a plant supplied at ``voltage_tier`` needs, or ``None`` off the ladder.

    ``MTEChemicalPlant.checkMachine`` refuses to form when the casing meta is below the highest
    hatch tier, unless the casing is UHV, so the casing of the supplied tier always forms: every
    hatch the export places is that tier or below. Above UHV there is no casing, and UHV is exempt.
    """
    if voltage_tier not in VOLTAGE_BY_TIER:
        return None
    return (
        _MACHINE_CASING_BLOCK,
        min(_UHV_MACHINE_CASING, list(VOLTAGE_BY_TIER).index(voltage_tier)),
    )


def heat_bonus_for(voltage_tier: str) -> int:
    """The kelvin an EBF-family machine supplied at ``voltage_tier`` adds to its coil's heat.

    GT adds ``100 * (tier - 2)`` (:data:`COIL_HEAT_GATES`): 100 K per tier above MV and, with no
    floor, 100 K taken off per tier below it, so ULV is -200 and MAX +1200. 0 for a tier off the
    ladder. GT reads the tier off the SUM of the machine's energy hatches (the EBF their voltage, the
    Mega EBF and Exothermic Hearth their voltage times amperage), and this counts one hatch of the
    supplied tier, so it is conservative: GT can read two or more hatches, or a hatch's 2 A, as a
    tier higher, so any error leaves the coil chosen from it hotter than GT needs, never colder.
    """
    if voltage_tier not in VOLTAGE_BY_TIER:
        return 0
    return 100 * (list(VOLTAGE_BY_TIER).index(voltage_tier) - 2)


def channel_blocks(
    block_key: str, dumped: Mapping[str, Sequence[BlockId]]
) -> dict[str, tuple[BlockId, ...]]:
    """Every channel the dump records for ``block_key``, mapped to the blocks it accepts.

    For a channel the cited rule covers (``_ACCEPTED``, the plant's ``casing``, ``pipe`` and
    ``machine_casing``) that is GT's whole accepted set, which adds the tier ``addTieredBlock`` never
    places. A dump listing a block outside that set disagrees with the rule, so it warns and keeps
    the dump's own list. Every other channel is the dump's list as recorded.
    """
    accepted: dict[str, tuple[BlockId, ...]] = {}
    for channel, blocks in dumped.items():
        listed = tuple(blocks)
        rule = _ACCEPTED.get((block_key, channel))
        stray = [] if rule is None else [b for b in listed if b not in rule]
        if stray:
            warnings.warn(
                f"{block_key}'s {channel!r} channel lists {stray}, which GT's check() does not "
                f"accept by the rule this build cites; using the dump's list as recorded. Re-check "
                f"the rule in dataset/structure_blocks.py against this pack's GT source.",
                DatasetWarning,
                stacklevel=2,
            )
        accepted[channel] = listed if rule is None or stray else rule
    return accepted
