"""previewer.atlas - pack a scene's baked face textures into the one image the viewer draws from.

``texturize_scene`` bakes each distinct (block, meta, side, state) face into a 16x16 PNG of its
own: several hundred on a large line (500 on ev-nitrobenzene). The viewer merges each layer's blocks
into one mesh, but a mesh still makes a draw call per material, and with a material per texture a
layer cost as many draw calls as it had textures: 680 a frame on ev-nitrobenzene. Packed into one
image, every face shares one material, so a layer is one draw call (97 a frame there), and the
page carries one PNG instead of hundreds of data URIs.

::

    scene["textures"]        (key -> baked PNG)   --+
    scene["texturesActive"]  (running overrides)  --+--> pack_atlas --> scene["atlas"]
    scene["blocks"]          (is there anything?) --+
                                                          image    every idle face, one tile each
                                                          active   the same image with the running
                                                                   faces pasted over, or None
                                                          tiles    key -> [x, y, w, h] in pixels
                                                          missing  the checkerboard tile

**Every tile carries a one-pixel border copied from its own edge.** The viewer samples with nearest
filtering, and a fragment on the very edge of a face can land on the texel just outside its tile.
With the border that texel is the tile's own edge colour, so no sliver of a neighbouring texture
can show along a block edge.

**The running image has the same layout as the idle one**, with only the faces whose running bake
differs pasted over. So the viewer's idle/running toggle swaps one texture on one material, and a
face with no running skin looks the same in both.

**The missing-texture checkerboard is a tile too.** A face whose sprite did not bake is drawn as
Minecraft's own magenta-and-black check (loud on purpose, #98), and as a tile it shares the one
material rather than needing a second.

Pillow comes from the ``preview`` extra, as for the bake. A scene with nothing to draw from an atlas
(no baked face and no block) never imports it.
"""

from __future__ import annotations

import base64
import io
import math
from typing import Any

from .bake import _require_pillow
from .textures import _png_data_uri

#: Minecraft's missing-texture check: black, with magenta top-left and bottom-right quarters.
_MISSING_DARK = (0, 0, 0, 255)
_MISSING_LIGHT = (248, 0, 248, 255)
_MISSING_SIZE = 16


def pack_atlas(scene: dict[str, Any]) -> None:
    """Replace the scene's per-face texture pool with one atlas, in place.

    Pops ``scene["textures"]`` and ``scene["texturesActive"]`` and writes ``scene["atlas"]``, or
    ``None`` when the scene has no baked face and no block to draw. Tiles are laid out in sorted key
    order, so the same scene always packs to the same image.
    """
    pool: dict[str, str] = scene.pop("textures", None) or {}
    running: dict[str, str] = scene.pop("texturesActive", None) or {}
    if not pool and not scene.get("blocks"):
        scene["atlas"] = None
        return
    image_mod = _require_pillow()
    faces: list[tuple[str | None, Any]] = [
        (key, _decode(pool[key], image_mod)) for key in sorted(pool)
    ]
    faces.append((None, _missing(image_mod)))  # None: the checkerboard, placed last

    cell_w = max(img.width for _, img in faces) + 2
    cell_h = max(img.height for _, img in faces) + 2
    cols = math.ceil(math.sqrt(len(faces)))
    rows = math.ceil(len(faces) / cols)
    idle = image_mod.new("RGBA", (cols * cell_w, rows * cell_h), (0, 0, 0, 0))
    rects: dict[str | None, list[int]] = {}
    for i, (key, img) in enumerate(faces):
        x, y = (i % cols) * cell_w + 1, (i // cols) * cell_h + 1
        _paste_bordered(idle, img, x, y)
        rects[key] = [x, y, img.width, img.height]

    active = None
    overrides = [key for key in sorted(running) if key in pool]
    if overrides:
        active = idle.copy()
        for key in overrides:
            x, y, w, h = rects[key]
            img = _decode(running[key], image_mod)
            if img.size != (w, h):  # a bake is always one size; never let a stray one spill over
                img = img.resize((w, h), image_mod.NEAREST)
            _paste_bordered(active, img, x, y)

    scene["atlas"] = {
        "image": _data_uri(idle),
        "active": _data_uri(active) if active is not None else None,
        "size": [idle.width, idle.height],
        "tiles": {key: rect for key, rect in rects.items() if key is not None},
        "missing": rects[None],
    }


def _paste_bordered(atlas: Any, img: Any, x: int, y: int) -> None:
    """Paste ``img`` at ``(x, y)`` with a one-pixel border of its own edge pixels around it.

    Shifted copies go down in order: the four diagonal ones, then the four straight ones, then the
    tile itself. The order is the point. A diagonal copy also covers part of a border side, with the
    edge pixel one along from the right one, so a straight copy has to land after it to put each
    side right; only the corners are left holding a diagonal copy, which is their corner pixel.
    Everything stays inside the tile's cell, which is two pixels wider and taller than the tile.
    """
    for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1), (0, -1), (0, 1), (-1, 0), (1, 0), (0, 0)):
        atlas.paste(img, (x + dx, y + dy))


def _missing(image_mod: Any) -> Any:
    img = image_mod.new("RGBA", (_MISSING_SIZE, _MISSING_SIZE), _MISSING_DARK)
    half = _MISSING_SIZE // 2
    img.paste(_MISSING_LIGHT, (0, 0, half, half))
    img.paste(_MISSING_LIGHT, (half, half, _MISSING_SIZE, _MISSING_SIZE))
    return img


def _decode(uri: str, image_mod: Any) -> Any:
    return image_mod.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))).convert("RGBA")


def _data_uri(img: Any) -> str:
    out = io.BytesIO()
    img.save(out, format="PNG")
    return _png_data_uri(out.getvalue())
