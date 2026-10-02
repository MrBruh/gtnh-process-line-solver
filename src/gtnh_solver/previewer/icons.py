"""previewer.icons - the item and fluid pictures a preview embeds, from a local icon index (#297).

``write_preview`` hands the page one picture per resource the line moves, so a hover tag, a net row
or a system-i/o row can show the thing beside its name. The pictures come from an icon index
(``dataset.icons``), embedded as one ``data:`` URI each: a line moves a few dozen resources, so a
handful of 64 px PNGs drawn by ``<img>`` needs neither an atlas nor Pillow. Only what the line
carries is embedded, never the index's tens of thousands. Where there is no index, or it has no
picture of a resource, the page draws the plan's own colour instead (``InputIR.resource_colors``).

The index's names come back too, for the ids the plan names nothing: ``build_scene`` prefers the
plan's name, then the export's, then shows the bare id.
"""

from __future__ import annotations

from gtnh_solver.dataset.icons import IconPack
from gtnh_solver.ir import Commodity, InputIR
from gtnh_solver.system_io import port_resource

from .textures import _png_data_uri


def carried_kinds(problem: InputIR) -> dict[str, Commodity]:
    """Every fluid and item ``problem`` moves, by id, with which of the two it is: on a net, through
    a non-power port, or let through by an Item Filter. Sorted by id."""
    kinds: dict[str, Commodity] = {}
    for net in problem.nets:
        for resource in net.resources:
            kinds.setdefault(resource, net.commodity)
    for machine in problem.machines:
        for item in machine.filter_items:
            kinds.setdefault(item, Commodity.ITEM)
        for port in machine.faces.ports:
            if port.commodity is not Commodity.POWER:
                kinds.setdefault(port_resource(port), port.commodity)
    return dict(sorted(kinds.items()))


def resource_art(problem: InputIR, pack: IconPack) -> tuple[dict[str, str], dict[str, str]]:
    """What ``pack`` has for the resources ``problem`` moves: ``(names, icons)``, each keyed by the
    plan's id, a name being the export's display name and an icon a ``data:image/png`` URI. A
    resource the index lacks is in neither, and one it has no image for only in ``names``.

    Raises :class:`~gtnh_solver.dataset.icons.IconPackError` for an entry or image that cannot be
    used, so a corrupt index is reported rather than half drawn.
    """
    names: dict[str, str] = {}
    icons: dict[str, str] = {}
    for resource, kind in carried_kinds(problem).items():
        entry = pack.lookup(resource, kind)
        if entry is None:
            continue
        if entry.name:
            names[resource] = entry.name
        png = pack.png(entry)
        if png is not None:
            icons[resource] = _png_data_uri(png)
    return names, icons
