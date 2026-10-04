"""Which GT cover empties an output face, and at which tier: the rule data an exported cover is
chosen from.

Sibling of :mod:`gtnh_solver.dataset.pipe_capacity`, and the same trade: shared rule DATA, read
from GT5-Unofficial at the tag the pack pins (5.09.54.133 for 2.9.0-beta-3). A face that is not a
block's auto-output face is emptied by a cover (:mod:`gtnh_solver.output_faces`): a conveyor for
items, a pump for fluids. Both are meta-items of one item, ``gregtech:gt.metaitem.01``, numbered
32000 + their ``IDMetaItem01`` id (``MetaGeneratedItemX32`` line 53), and each tier moves a fixed
amount (``MetaGeneratedItem01.registerCovers``)::

    tier  conveyor  CoverConveyor(tickRate, stacks, items)   pump  CoverPump(rate)
    LV    32630     100 ticks, 1 x 16  = 0.16 items/t        32610   32 L/t
    MV    32631     100 ticks, 1 x 64  = 0.64 items/t        32611  128 L/t
    HV    32632      20 ticks, 1 x 64  = 3.2 items/t         32612  512 L/t
    EV    32633       4 ticks, 1 x 64  = 16 items/t          32613 2048 L/t
    IV    32634       1 tick,  1 x 64  = 64 items/t          32614 8192 L/t

A conveyor moves its stacks once per ``tickRate`` ticks (``CoverConveyor.getMinimumTickRate``); a
pump moves its rate every tick (``CoverPump.getMinimumTickRate`` is 1). The table stops at IV: one
face moving more than an IV cover does is not a line this solver builds.

:func:`cover_for` takes the lowest tier that keeps up with what leaves through the face. A face
whose rate the plan does not state gets the machine's own voltage tier, which is what a player
fits by default, clamped onto the table.
"""

from __future__ import annotations

from dataclasses import dataclass

from .voltage import VOLTAGE_BY_TIER

#: The registry name of the item every conveyor and pump cover is a meta of.
COVER_ITEM = "gregtech:gt.metaitem.01"

#: ``kind -> tier -> (meta, moved per tick)``, lowest tier first; items/t for a conveyor, L/t for a
#: pump. See the module docstring for where each figure is registered.
COVER_TIERS: dict[str, dict[str, tuple[int, float]]] = {
    "conveyor": {
        "LV": (32630, 16 / 100),
        "MV": (32631, 64 / 100),
        "HV": (32632, 64 / 20),
        "EV": (32633, 64 / 4),
        "IV": (32634, 64 / 1),
    },
    "pump": {
        "LV": (32610, 32.0),
        "MV": (32611, 128.0),
        "HV": (32612, 512.0),
        "EV": (32613, 2048.0),
        "IV": (32614, 8192.0),
    },
}


@dataclass(frozen=True)
class CoverChoice:
    """One cover to fit: its kind, tier, meta-item number and what it moves per tick."""

    kind: str
    tier: str
    meta: int
    per_tick: float

    @property
    def label(self) -> str:
        """``"HV conveyor"``: how a builder names it."""
        return f"{self.tier} {self.kind}"


def cover_for(kind: str, rate: float | None, machine_tier: str | None) -> CoverChoice:
    """The cover of ``kind`` to fit on a face moving ``rate`` per tick.

    The lowest tier that moves at least ``rate``; IV when even that is short. With no ``rate``, the
    machine's own tier, clamped onto the table (ULV fits LV, anything above IV fits IV).

    Raises :class:`KeyError` for a ``kind`` the table does not have.
    """
    tiers = COVER_TIERS[kind]
    ladder = list(tiers)
    if rate is not None:
        tier = next((t for t in ladder if tiers[t][1] >= rate), ladder[-1])
    elif machine_tier is not None and machine_tier in tiers:
        tier = machine_tier
    else:
        tier = ladder[-1] if _above_table(machine_tier, ladder) else ladder[0]
    meta, per_tick = tiers[tier]
    return CoverChoice(kind=kind, tier=tier, meta=meta, per_tick=per_tick)


def _above_table(machine_tier: str | None, ladder: list[str]) -> bool:
    """Whether ``machine_tier`` is a voltage tier above the table's last one."""
    if machine_tier is None or machine_tier not in VOLTAGE_BY_TIER:
        return False
    return VOLTAGE_BY_TIER[machine_tier] > VOLTAGE_BY_TIER[ladder[-1]]
