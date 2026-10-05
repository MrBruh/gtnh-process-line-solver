"""previewer.me_textures - embed the AE2 and AE2FluidCraft art an ME network is drawn with (#338).

The ME half of the texture pass. ``scene["me"]`` names, per box face, the AE2 or FC icon it wears
(``modid:Name``, from the committed render data, :mod:`gtnh_solver.dataset.ae_render`), and per
light pass the mask it draws. This fetches those PNGs from the mods' own jars, through the same
``png_provider`` the GT pass uses (:func:`gtnh_solver.previewer.jar.multi_jar_png_provider` routes
each ``assets/<modid>/`` path to the jar that owns it), and puts them where the viewer reads them::

    scene["me"] boxes' faces   -> scene["textures"]       one 16x16 tile per icon, packed into the
                                                          atlas with the GT faces (previewer.atlas)
    scene["me"] boxes' lights  -> scene["me"]["lights"]   a data: URI per mask, at its own size:
                                                          the channel masks are 64x64, and squeezed
                                                          into a 16x16 tile their lines would vanish

**A missing icon costs only itself.** A jar that fails to download or open hands back no icons
(the provider logs it), and a face whose icon did not arrive is drawn in its box's flat colour, the
same way a route with no sprite keeps its coloured bar. The GT pass ran first and is untouched.

**The licence travels with the art** (spike 9.2): AE2's textures are CC BY-NC-SA 3.0, and FC's are
treated the same (nobody has checked whether they derive from AE2's). So :func:`texturize_me`
reports which mods' art it embedded, and :func:`credit` is what a page carrying any of it must
show: the attribution, the licence and its link, and that the page may be shared only for
non-commercial purposes. ``NOTICE`` says the same for the repository.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from gtnh_solver.dataset.ae_render import asset_path
from gtnh_solver.dataset.mod_jars import AE2, AE2FC

from .bake import BakeUnavailableError, _require_pillow, bake_layers
from .textures import PngProvider, _png_data_uri

_log = logging.getLogger(__name__)

#: AE2's art licence (``Applied-Energistics-2-Unofficial/README.md``, spike 9.2).
AE2_LICENCE = "CC BY-NC-SA 3.0"
AE2_LICENCE_URL = "https://creativecommons.org/licenses/by-nc-sa/3.0/"

#: How the credit names each mod whose art a page can carry, by its asset namespace.
_MOD_CREDITS: dict[str, str] = {
    AE2.modid: "Applied Energistics 2, (c) 2013 - 2015 AlgorithmX2 et al",
    AE2FC.modid: "AE2FluidCraft-Rework, by GTNewHorizons",
}


def me_icons(scene: dict[str, Any]) -> tuple[set[str], set[str]]:
    """The icons ``scene["me"]`` draws faces with, and the ones it draws light passes with."""
    faces: set[str] = set()
    lights: set[str] = set()
    me = scene.get("me") or {}
    for entry in (*me.get("cells", ()), *me.get("blocks", ())):
        for box in entry.get("boxes", ()):
            faces.update(icon for icon in box.get("faces", ()) if icon)
            lights.update(light["icon"] for light in box.get("lights", ()))
    return faces, lights


def texturize_me(scene: dict[str, Any], png_provider: PngProvider) -> frozenset[str]:
    """Embed every AE2 and FC icon ``scene["me"]`` draws, in place (module docstring), and return
    the asset namespaces (``appliedenergistics2``, ``ae2fc``) whose art is now on the page.

    Face icons join ``scene["textures"]``, which :func:`~gtnh_solver.previewer.atlas.pack_atlas`
    packs with the GT faces, so this runs after
    :func:`~gtnh_solver.previewer.textures.texturize_scene` and before the atlas. A scene with no ME
    network, or no Pillow, embeds nothing and asks no jar for anything.
    """
    me = scene.get("me")
    if not me:
        return frozenset()
    faces, lights = me_icons(scene)
    wanted = faces | lights
    if not wanted:
        return frozenset()
    pngs = png_provider({icon: asset_path(icon) for icon in sorted(wanted)})
    pool: dict[str, str] = scene.setdefault("textures", {})
    masks: dict[str, str] = me.setdefault("lights", {})
    embedded: set[str] = set()
    try:
        for icon in sorted(faces):
            baked = bake_layers([{"icon": icon, "rgba": [], "glow": False}], pngs)
            if baked is not None:
                pool[icon] = _png_data_uri(baked)
                embedded.add(icon)
        for icon in sorted(lights):
            png = pngs.get(icon)
            if png is not None:
                masks[icon] = _png_data_uri(_first_frame(png))
                embedded.add(icon)
    except BakeUnavailableError as exc:  # no Pillow: the first bake raises, before any embeds
        _log.warning("textures: %s; the ME network keeps its flat colours", exc)
        return frozenset()
    missing = sorted(wanted - embedded)
    if missing:
        _log.warning(
            "textures: %d of %d ME icon(s) did not arrive, so those faces keep a flat colour: %s",
            len(missing),
            len(wanted),
            ", ".join(missing),
        )
    else:
        _log.info("textures: %d ME icon(s) embedded", len(embedded))
    return frozenset(icon.partition(":")[0] for icon in embedded)


def _first_frame(png: bytes) -> bytes:
    """``png`` cut to its first square frame (an animated strip is frames stacked downward), at
    its own resolution: a light mask's detail is finer than a block's sixteen pixels."""
    image_mod = _require_pillow()
    image = image_mod.open(io.BytesIO(png)).convert("RGBA")
    width, height = image.size
    if height > width and width > 0 and height % width == 0:
        image = image.crop((0, 0, width, width))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def credit(mods: frozenset[str]) -> dict[str, Any] | None:
    """What a page that embeds the art of ``mods`` must show (module docstring), or ``None`` when
    it embeds none of AE2's or FC's: ``text`` in full for the legend, ``short`` for the HUD, each
    followed by the licence's name linked to ``url``. It names what is embedded, whether that art
    is an ME network's, a GT block's or an item's: AE2's credit wherever AE2's art is (FC's beside
    it), and FC alone with the note ``NOTICE`` gives it. Read by the viewer as data; the words are
    this module's own, never the scene's."""
    named = [credit for modid, credit in _MOD_CREDITS.items() if modid in mods]
    if not named:
        return None
    terms = "share this preview for non-commercial purposes only, and with this credit."
    if AE2.modid in mods:
        # AE2's own art is CC BY-NC-SA 3.0, and FC's beside it rides the same terms.
        fc = " and GTNewHorizons (AE2FluidCraft)" if AE2FC.modid in mods else ""
        short = f"Textures (c) AlgorithmX2 et al (AE2){fc}, non-commercial sharing only:"
        text = f"Textures from {', and from '.join(named)}. Licensed {AE2_LICENCE}: {terms}"
    else:
        # FC alone declares LGPL-3.0, but whether its art derives from AE2's is unchecked (NOTICE).
        short = "Textures from AE2FluidCraft (GTNewHorizons), on AE2's terms, non-commercial only:"
        text = (
            f"Textures from {named[0]}. It declares LGPL-3.0, but whether its art derives from "
            f"Applied Energistics 2's ({AE2_LICENCE}) is unchecked, so it is shared on those "
            f"terms: {terms}"
        )
    return {
        "mods": named,
        "licence": AE2_LICENCE,
        "url": AE2_LICENCE_URL,
        # The HUD's one line, which no fold hides; the legend carries ``text`` in full.
        "short": short,
        "text": text,
    }
