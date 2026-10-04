"""What a plan says a multiblock's tiered parts must be built from (#312).

A GT multiblock builds some parts from a tier ladder (``dataset/structure_blocks.py`` holds the
ladders and what GT does with each tier). The plan decides the tier, though rarely in one place,
so each part has a precedence ladder; the first rung that names a usable block wins, and a rung
naming one that is not usable warns and falls through::

    coil (any multiblock whose coil channel has a choice)
        node.coilTier -> machineConfigTiers.heatingCoil -> heatingCoil control default
        -> nothing: the dump's own coil (a Chemical Plant warns: Cupronickel runs it at half speed)
      then, on a machine that refuses a recipe hotter than its coil (the EBF family, #318) and
      whose recipes state a heat, at the tier it is supplied at (PlannedStructure.blocks):
        that coil (nothing named: the dump's coolest), if it reaches the hottest recipe's heat
        -> the cheapest accepted coil that does, with a warning
        -> none does: the hottest accepted coil, with a warning

    solid casing (Chemical Plant)
        machineConfigTiers.solidCasing, raised to the requirement with a warning if below it
        -> the cheapest casing whose tier meets the highest special value the node's recipes
           state (above botmium: botmium, with a warning)
        -> nothing stated: bronze, with a warning

    pipe casing (Chemical Plant)
        machineConfigTiers.pipeCasing -> pipeCasing control default -> bronze
        (PTFE and PBI build as Tungstensteel, with a warning: the plant's check() refuses both)

    machine casing (Chemical Plant)
        the casing of the voltage tier the machine is supplied at, read AFTER the power synthesis
        (PlannedStructure.blocks), which can raise that tier. Every hatch the export places is
        that tier or below, so the plant always forms.

A control's default is read from the node's handler first, then its recipe. A coil channel with a
single block is a fixed part (a Large Chemical Reactor-style single coil, a Dyson Swarm's), and is
skipped silently, since arodoid writes a ``coilTier`` on every node. A block the machine's own
channel does not accept is never chosen: that warns and falls through like an unknown key.

A recipe's **special value** (GT's ``mSpecialValue``) is a requirement each machine reads its own
way; the Chemical Plant reads it as the lowest solid casing tier the recipe runs on, and an
Electric Blast Furnace as the heat in kelvin its coil must reach (with 100 K per voltage tier above
MV added to the coil's, ``dataset.heat_bonus_for``). Exporters state it in up to three places, none
of them on every plan (a converted ShadowTheAge plan states it nowhere)::

    recipe.specialValue                arodoid
    recipe.metadata.specialValue       arodoid
    recipe.nei.additionalInfo          both forks: "Special value: 4" (the only place MrBruh's does)

:func:`recipe_special_value` reads all three.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from gtnh_solver.dataset import (
    CHEMICAL_PLANT,
    COIL,
    COIL_HEAT_GATES,
    HEATING_COIL_HEATS,
    HEATING_COILS,
    MACHINE_CASING,
    PIPE,
    PIPE_CASING_ALIASES,
    PIPE_CASINGS,
    SOLID_CASING,
    SOLID_CASINGS,
    BlockId,
    MachinePhysical,
    TieredBlock,
    heat_bonus_for,
    machine_casing_for,
    tier_block,
)
from gtnh_solver.ir import StructureBlock

from ._errors import AdapterWarning
from .plan import MachineHandler, Node, Recipe

#: How NEI states a recipe's special value, the pattern gtnh-factory-flow's own reader matches
#: (``getRecipeSpecialValue``).
_SPECIAL_VALUE_LINE = re.compile(r"special\s+value\s*:\s*(-?\d+)", re.IGNORECASE)

#: The machine-configuration keys a plan names each part by. ``heatingCoil`` and ``pipeCasing`` are
#: gtnh-factory-flow's control ids; ``solidCasing`` is the key gtnh-shadow-convert writes.
_COIL_CONTROL = "heatingCoil"
_PIPE_CONTROL = "pipeCasing"
_SOLID_CASING_KEY = "solidCasing"


def recipe_special_value(recipe: Recipe) -> int | None:
    """The special value ``recipe`` states, or ``None`` when it states none.

    Every place it is stated counts, so two that disagree are not settled by which is read first:
    that warns and takes the highest, since a machine built for the higher requirement also runs
    the lower. A negative value is read as 0, the lowest requirement GT's comparison can mean.
    """
    stated: list[int] = []
    if recipe.special_value is not None:
        stated.append(recipe.special_value)
    if recipe.metadata is not None and recipe.metadata.special_value is not None:
        stated.append(recipe.metadata.special_value)
    if recipe.nei is not None:
        for line in recipe.nei.additional_info or ():
            match = _SPECIAL_VALUE_LINE.search(line)
            if match is not None:
                stated.append(int(match.group(1)))
    if not stated:
        return None
    if len(set(stated)) > 1:
        warnings.warn(
            f"recipe {recipe.id!r} states special values {sorted(set(stated))}; building for the "
            f"highest, {max(stated)}",
            AdapterWarning,
            stacklevel=2,
        )
    return max(0, max(stated))


@dataclass(frozen=True)
class CoilHeat:
    """The heat a heat-gated machine's coil must reach (``dataset.COIL_HEAT_GATES``), which
    :meth:`PlannedStructure.blocks` settles at the tier the machine is supplied at."""

    #: The highest special value the node's recipes state, read as kelvin.
    required: int
    #: Whether GT adds the hatch-tier bonus (``dataset.heat_bonus_for``) to the coil's heat.
    tier_bonus: bool
    #: The coils the machine's channel accepts, in tier order (coolest first).
    coils: tuple[TieredBlock, ...]
    #: The coil the plan names, or ``None`` when it names none the channel accepts.
    named: TieredBlock | None = None


@dataclass(frozen=True)
class PlannedStructure:
    """The tiered blocks one node's machines are built from, all but those the supplied tier settles.

    The machine casing follows the voltage tier a machine is supplied at, which the power synthesis
    can still raise, and so does the heat an EBF's hatches add to its coil's, so :meth:`blocks`
    adds the machine casing, and settles a heat-gated coil, from the final tier.
    """

    node_id: str
    #: The blocks chosen from the plan, as ``(channel, block)``, sorted by channel.
    chosen: tuple[tuple[str, BlockId], ...] = ()
    #: The machine casings the machine's channel accepts; empty when it has no such part to plan.
    machine_casings: tuple[BlockId, ...] = ()
    #: What a heat-gated machine's coil must reach; ``None`` when nothing gates it, and then the
    #: coil, if the plan names one, is in :attr:`chosen`.
    coil_heat: CoilHeat | None = None

    def blocks(self, voltage_tier: str) -> dict[str, StructureBlock]:
        """``Machine.structure_blocks`` for one machine of the node, supplied at ``voltage_tier``."""
        blocks = dict(self.chosen)
        if self.coil_heat is not None:
            coil = _heated_coil(self.node_id, self.coil_heat, voltage_tier)
            if coil is not None:
                blocks[COIL] = coil
        if self.machine_casings:
            casing = machine_casing_for(voltage_tier)
            if casing is not None and casing in self.machine_casings:
                blocks[MACHINE_CASING] = casing
            else:
                warnings.warn(
                    f"node {self.node_id!r} is supplied at {voltage_tier!r}, which has no machine "
                    f"casing its structure accepts; it is drawn with the dump's own, which may keep "
                    f"it from forming",
                    AdapterWarning,
                    stacklevel=2,
                )
        return {
            channel: StructureBlock(block=block, meta=meta)
            for channel, (block, meta) in sorted(blocks.items())
        }


def plan_structure(
    recipes: Sequence[Recipe],
    node: Node,
    handler: MachineHandler | None,
    record: MachinePhysical | None,
) -> PlannedStructure | None:
    """What the plan says ``node``'s machines are built from, or ``None`` without a record.

    ``recipes`` are every recipe the machines run, the node's own first: the solid casing has to
    meet the highest special value among them, since one plant runs them all, and a heat-gated coil
    the highest heat. The node's own recipe and ``handler`` supply the control defaults. Warns once
    per node for each rung that names something unusable (see the module docstring for the
    ladders); a heat-gated coil is settled, and warns, in :meth:`PlannedStructure.blocks`.
    """
    if record is None:
        return None
    accepted = record.channel_blocks
    chosen: dict[str, BlockId] = {}
    coil = _coil(node, handler, recipes[0], accepted.get(COIL, ()), record)
    coil_heat = _coil_heat(recipes, record.block_key, coil, accepted.get(COIL, ()))
    if coil is not None and coil_heat is None:
        chosen[COIL] = coil
    machine_casings: tuple[BlockId, ...] = ()
    if record.block_key == CHEMICAL_PLANT:
        if SOLID_CASING in accepted:
            casing = _solid_casing(recipes, node, accepted[SOLID_CASING])
            if casing is not None:
                chosen[SOLID_CASING] = casing
        if PIPE in accepted:
            pipe = _pipe_casing(node, handler, recipes[0], accepted[PIPE])
            if pipe is not None:
                chosen[PIPE] = pipe
        machine_casings = accepted.get(MACHINE_CASING, ())
    return PlannedStructure(
        node_id=node.id,
        chosen=tuple(sorted(chosen.items())),
        machine_casings=machine_casings,
        coil_heat=coil_heat,
    )


def _control_default(control_id: str, handler: MachineHandler | None, recipe: Recipe) -> str:
    """The ``default_key`` of control ``control_id``: the handler's controls first, then the
    recipe's (where both forks put a GT++ multiblock's), ``""`` when neither declares one."""
    controls = [
        *(handler.machine_config_controls if handler is not None else ()),
        *recipe.machine_config_controls,
    ]
    return next((c.default_key for c in controls if c.id == control_id and c.default_key), "")


def _first_usable(
    node: Node,
    part: str,
    rungs: Sequence[tuple[str, str]],
    table: Sequence[TieredBlock],
    accepted: Sequence[BlockId],
    aliases: Mapping[str, str] | None = None,
) -> TieredBlock | None:
    """The block the first rung names that ``table`` knows and ``accepted`` takes, or ``None``.

    ``rungs`` are ``(where the plan says it, key)`` in precedence order. An empty key is no rung at
    all; a key the table does not know, or whose block the machine's channel does not accept, warns
    and falls through to the next. A key ``aliases`` maps onto another builds as that one, warning.
    """
    for source, key in rungs:
        if not key:
            continue
        rung, aliased = tier_block(table, key, aliases)
        if rung is None:
            warnings.warn(
                f"node {node.id!r} sets {source} to {key!r}, which is not a {part} this build "
                f"knows; ignoring it",
                AdapterWarning,
                stacklevel=4,
            )
            continue
        if rung.block_id not in accepted:
            warnings.warn(
                f"node {node.id!r} sets {source} to {key!r}, but its machine's {part} does not "
                f"accept {rung.block}@{rung.meta}; ignoring it",
                AdapterWarning,
                stacklevel=4,
            )
            continue
        if aliased:
            warnings.warn(
                f"node {node.id!r} sets {source} to {key!r}, a {part} its machine does not "
                f"accept; building it with {rung.key}, which the planner rates the same",
                AdapterWarning,
                stacklevel=4,
            )
        return rung
    return None


def _coil(
    node: Node,
    handler: MachineHandler | None,
    recipe: Recipe,
    accepted: Sequence[BlockId],
    record: MachinePhysical,
) -> BlockId | None:
    """The coil the plan names for a machine whose coil channel has a choice, else ``None``."""
    if len(accepted) < 2:
        return None  # no coil part, or a fixed one: nothing the plan can choose
    rung = _first_usable(
        node,
        "heating coil",
        (
            ("coilTier", node.coil_tier),
            (
                f"machineConfigTiers.{_COIL_CONTROL}",
                node.machine_config_tiers.get(_COIL_CONTROL, ""),
            ),
            (
                f"the {_COIL_CONTROL} control's default",
                _control_default(_COIL_CONTROL, handler, recipe),
            ),
        ),
        HEATING_COILS,
        accepted,
    )
    if rung is not None:
        return rung.block_id
    if record.block_key == CHEMICAL_PLANT:
        warnings.warn(
            f"node {node.id!r} names no heating coil for its Chemical Plant, so it is drawn with the "
            f"dump's Cupronickel, which runs it at half speed (GT's speed bonus is 2 / (1 + coil "
            f"tier)); set the node's coil to the one the plan was balanced with",
            AdapterWarning,
            stacklevel=3,
        )
    return None


def _coil_heat(
    recipes: Sequence[Recipe], block_key: str, named: BlockId | None, accepted: Sequence[BlockId]
) -> CoilHeat | None:
    """What a heat-gated machine's coil must reach, or ``None`` when there is nothing to check.

    ``None`` for a controller GT does not gate on coil heat or whose channel offers no choice of
    coils, both checked before any special value is read, so a Chemical Plant's recipes warn of a
    disagreement once, not twice. ``None``, silently, when no recipe states a heat: a converted
    ShadowTheAge plan states none, and its converter warns of a coil too cold itself. A node that
    time-shares recipes is coiled for the hottest, since one furnace runs them all.
    """
    tier_bonus = COIL_HEAT_GATES.get(block_key)
    coils = tuple(rung for rung in HEATING_COILS if rung.block_id in accepted)
    if tier_bonus is None or len(coils) < 2:
        return None
    stated = [value for value in map(recipe_special_value, recipes) if value is not None]
    if not stated:
        return None
    return CoilHeat(
        required=max(stated),
        tier_bonus=tier_bonus,
        coils=coils,
        named=next((rung for rung in coils if rung.block_id == named), None),
    )


def _heated_coil(node_id: str, heat: CoilHeat, voltage_tier: str) -> BlockId | None:
    """The coil a heat-gated machine supplied at ``voltage_tier`` is built with, or ``None`` to
    draw the dump's own.

    A coil reaches its heat plus, where GT adds it, the supplied tier's bonus. The coil that would
    be built (the plan's, else the dump's coolest) is kept when it reaches what the recipes need,
    which is ``None`` when the plan names none, so the coil stays as dumped. Otherwise the cheapest
    accepted coil that reaches it, in tier order, not meta order (HSS-G, meta 4, is followed by
    HSS-S, meta 9), else the hottest accepted coil, on which GT still refuses the recipe; both warn.
    """
    bonus = heat_bonus_for(voltage_tier) if heat.tier_bonus else 0
    reach = {rung: HEATING_COIL_HEATS[HEATING_COILS.index(rung)] + bonus for rung in heat.coils}
    built = heat.named if heat.named is not None else heat.coils[0]
    if reach[built] >= heat.required:
        return None if heat.named is None else heat.named.block_id
    cheapest = next((rung for rung in heat.coils if reach[rung] >= heat.required), None)
    if cheapest is None:
        top = heat.coils[-1]
        warnings.warn(
            f"node {node_id!r} runs a recipe of {heat.required} K, above the {reach[top]} K of "
            f"{top.key} at {voltage_tier}, the hottest coil its machine's dump records; building "
            f"it with {top.key}, on which GT will still refuse that recipe",
            AdapterWarning,
            stacklevel=3,
        )
        return top.block_id
    if heat.named is not None:
        warnings.warn(
            f"node {node_id!r} names {heat.named.key} heating coil ({reach[heat.named]} K at "
            f"{voltage_tier}), below the {heat.required} K its recipes need; building it with "
            f"{cheapest.key}",
            AdapterWarning,
            stacklevel=3,
        )
    else:
        warnings.warn(
            f"node {node_id!r} names no heating coil its machine accepts, so it would be drawn "
            f"with the dump's {built.key} ({reach[built]} K at {voltage_tier}), below the "
            f"{heat.required} K its recipes need; building it with {cheapest.key}",
            AdapterWarning,
            stacklevel=3,
        )
    return cheapest.block_id


def _solid_casing(
    recipes: Sequence[Recipe], node: Node, accepted: Sequence[BlockId]
) -> BlockId | None:
    """The Chemical Plant's solid casing: the plan's own if it meets the requirement, else the
    cheapest that does (``MTEChemicalPlant.validateRecipe`` refuses a recipe whose special value is
    above the casing's tier)."""
    stated = [value for value in map(recipe_special_value, recipes) if value is not None]
    required = max(stated) if stated else None
    named = _first_usable(
        node,
        "solid casing",
        (
            (
                f"machineConfigTiers.{_SOLID_CASING_KEY}",
                node.machine_config_tiers.get(_SOLID_CASING_KEY, ""),
            ),
        ),
        SOLID_CASINGS,
        accepted,
    )
    if named is not None and (required is None or SOLID_CASINGS.index(named) >= required):
        return named.block_id
    if required is None:
        warnings.warn(
            f"node {node.id!r} runs a Chemical Plant whose recipes state no special value and "
            f"whose plan names no solid casing; building it with {SOLID_CASINGS[0].key}, on which "
            f"GT runs only recipes of special value 0",
            AdapterWarning,
            stacklevel=3,
        )
        return _accepted(node, SOLID_CASINGS[0], accepted)
    cheapest = SOLID_CASINGS[min(required, len(SOLID_CASINGS) - 1)]
    if required >= len(SOLID_CASINGS):
        warnings.warn(
            f"node {node.id!r} runs a recipe of special value {required}, above every Chemical "
            f"Plant solid casing (the highest, {cheapest.key}, is tier {len(SOLID_CASINGS) - 1}); "
            f"building it with {cheapest.key}, on which GT will still refuse that recipe",
            AdapterWarning,
            stacklevel=3,
        )
    elif named is not None:
        warnings.warn(
            f"node {node.id!r} names {named.key} solid casing (tier {SOLID_CASINGS.index(named)}), "
            f"below the tier {required} its recipes need; building it with {cheapest.key}",
            AdapterWarning,
            stacklevel=3,
        )
    return _accepted(node, cheapest, accepted)


def _pipe_casing(
    node: Node, handler: MachineHandler | None, recipe: Recipe, accepted: Sequence[BlockId]
) -> BlockId | None:
    """The Chemical Plant's pipe casing, which sets its parallels (2 per pipe tier)."""
    rung = _first_usable(
        node,
        "pipe casing",
        (
            (
                f"machineConfigTiers.{_PIPE_CONTROL}",
                node.machine_config_tiers.get(_PIPE_CONTROL, ""),
            ),
            (
                f"the {_PIPE_CONTROL} control's default",
                _control_default(_PIPE_CONTROL, handler, recipe),
            ),
        ),
        PIPE_CASINGS,
        accepted,
        PIPE_CASING_ALIASES,
    )
    return _accepted(node, rung or PIPE_CASINGS[0], accepted)


def _accepted(node: Node, rung: TieredBlock, accepted: Sequence[BlockId]) -> BlockId | None:
    """``rung``'s block if the machine's channel takes it, else ``None`` (drawn as dumped), warning.

    Reached only when the rule chose a block on its own, so a refusal here means the dump disagrees
    with the cited GT rule (``dataset.channel_blocks`` warned about that already).
    """
    if rung.block_id in accepted:
        return rung.block_id
    warnings.warn(
        f"node {node.id!r} needs {rung.key} ({rung.block}@{rung.meta}), which its machine's dump "
        f"does not accept; drawing that part as the dump does",
        AdapterWarning,
        stacklevel=4,
    )
    return None
