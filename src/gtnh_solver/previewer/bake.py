"""previewer.bake - composite a GT ``ITexture`` layer stack into one flat PNG.

Lane 7 v2 (plan section 5.5). The layered texture manifest (lane 6 v2) gives, per
``(block, meta, side, state)``, an ordered bottom-to-top layer list ``[{icon, rgba, glow}]`` - the
same stack GT's renderer draws: a base casing sprite tinted by an RGBA multiply, then overlay
sprites alpha-composited on top. This module pre-bakes that composite into a single flat PNG so the
three.js previewer only ever loads flat images and never composites at runtime. Keeping the fiddly
part here (tint multiply, alpha compositing, animation-frame pick) is deliberate: it is pure and
unit-tested, where the WebGL last mile is not.

**Why a multiply, not a skip.** GT machine casings ship as neutral-ish sprites and only take their
tier colour from the ``mRGBa`` multiply the layer carries (``Dyes.MACHINE_METAL`` by default). A
bake that dropped the multiply would render every machine grey, so the multiply is applied here and
pinned by a golden test (plan section 7).

Pillow is an optional dependency (the ``preview`` extra). :func:`bake_layers` imports it lazily and
raises :class:`BakeUnavailableError` if it is missing, so the previewer can catch that and degrade
to placeholder boxes rather than hard-failing a solver-only install.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

#: Sprites are 16x16 pixel art. Animated sprites are a vertical strip of stacked 16x16 frames
#: (height a multiple of the width); we bake frame 0 (the top square), per plan section 10 non-goals.
_TILE = 16


class Layer(TypedDict):
    """One resolved texture layer: an iconset name, its RGBA multiply, and the glow flag."""

    icon: str
    rgba: list[int]
    glow: bool


class BakeUnavailableError(RuntimeError):
    """Pillow (the ``preview`` extra) is not installed, so baking cannot run."""


def _require_pillow() -> Any:
    """Import Pillow's ``Image`` lazily so a solver-only install need not carry it; raise if absent."""
    try:
        from PIL import Image
    except (
        ModuleNotFoundError
    ) as exc:  # pragma: no cover - exercised via the previewer degrade path
        raise BakeUnavailableError(
            "Pillow is required to bake textures; install the 'preview' extra "
            "(pip install gtnh_solver[preview])"
        ) from exc
    return Image


def _multiplier(rgba: Sequence[int]) -> tuple[float, float, float, float]:
    """Turn a GT ``[r, g, b, a]`` (0-255) layer colour into per-channel multipliers in ``[0, 1]``.

    GT stores the tint as a short quadruple and multiplies each sprite channel by ``value / 255``.
    The alpha slot is ``0`` for the common opaque layers (it is a modulation colour, not a coverage
    value), so a zero alpha is treated as fully opaque; any other alpha scales coverage. A missing
    or short tuple defaults to identity (no tint), so an un-tinted layer bakes its sprite unchanged.
    """
    r, g, b, a = [*rgba, 255, 255, 255, 255][:4]
    alpha = 1.0 if a == 0 else a / 255.0
    return (r / 255.0, g / 255.0, b / 255.0, alpha)


def _frame0(image: Any, image_mod: Any) -> Any:
    """The top ``16x16`` frame of a sprite: identity for a still, frame 0 for an animated strip."""
    w, h = image.size
    if h > w and w > 0 and h % w == 0:
        image = image.crop((0, 0, w, w))
    if image.size != (_TILE, _TILE):
        image = image.resize((_TILE, _TILE), image_mod.NEAREST)
    return image


def _tinted(png: bytes, rgba: Sequence[int], image_mod: Any) -> Any:
    """Open ``png``, take frame 0, and apply the layer's RGBA multiply, returning an RGBA image."""
    img = _frame0(image_mod.open(io.BytesIO(png)).convert("RGBA"), image_mod)
    mr, mg, mb, ma = _multiplier(rgba)
    if (mr, mg, mb, ma) == (1.0, 1.0, 1.0, 1.0):
        return img
    px = img.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            px[x, y] = (round(r * mr), round(g * mg), round(b * mb), round(a * ma))
    return img


def bake_layers(layers: Sequence[Mapping[str, Any]], icon_png: Mapping[str, bytes]) -> bytes | None:
    """Composite an ordered ``layers`` stack into one flat 16x16 PNG, or ``None`` if nothing resolves.

    Each layer is drawn bottom-to-top: its iconset sprite (looked up in ``icon_png`` by ``icon``)
    tinted by the layer's ``rgba`` multiply, alpha-composited onto the accumulator. Layers whose PNG
    bytes are not in ``icon_png`` are skipped (a partially fetched jar still bakes what it has); glow
    layers are composited flat (v1 does not model emissive glow, plan section 10). Returns the PNG
    bytes, or ``None`` when not a single layer's sprite was available (the caller then keeps a
    placeholder for that face).
    """
    image_mod = _require_pillow()
    base: Any | None = None
    for layer in layers:
        png = icon_png.get(layer["icon"])
        if png is None:
            continue
        tinted = _tinted(png, layer.get("rgba", []), image_mod)
        base = tinted if base is None else image_mod.alpha_composite(base, tinted)
    if base is None:
        return None
    out = io.BytesIO()
    base.save(out, format="PNG")
    return out.getvalue()
