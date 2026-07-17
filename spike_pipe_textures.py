"""Spike (#4): prove the previewer can wrap route bars in a baked pipe/cable sprite.

Not for merge. This stands in a *synthesized* wire sprite for the real GT cable sprites the
manifest does not yet carry (the extractor only walks multiblock member blocks, so pipes and
cables - MetaPipeEntity blocks - are absent). It runs the EXACT bake path a real cable would:
bake_layers() tints a base wire icon by the material RGBA and composites it, the same way the
machine casings are tinted today. The only stand-in is the source PNG; everything downstream
(bake -> data URI -> previewer material wrap -> graceful flat-colour fallback) is the real code.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

from gtnh_solver.adapter import adapt_file
from gtnh_solver.previewer import build_scene, render_html
from gtnh_solver.previewer.bake import bake_layers
from gtnh_solver.solver import solve

ROOT = Path(__file__).resolve().parent
SAND = ROOT / "examples" / "gtnh-sand.json"

# A representative cable material colour per voltage tier. This is the COSMETIC modelling choice
# #4 needs: GT has many cable materials per tier (tin/lead/... all carry LV), so we pin one
# representative per tier. RGBA in GT's [r,g,b,a] convention (a=255 opaque); bake normalises it.
TIER_RGBA: dict[str, list[int]] = {
    "LV": [180, 120, 90, 255],  # tin/bronze-ish
    "MV": [255, 120, 40, 255],  # copper orange
    "HV": [235, 205, 90, 255],  # gold
    "EV": [150, 190, 220, 255],  # aluminium
    "IV": [90, 90, 110, 255],  # tungstensteel
}


def _wire_icon_png() -> bytes:
    """A neutral 16x16 'wire' base sprite: a light horizontal band on a dark casing, transparent
    corners. Stands in for a real GT cable iconset sprite; bake tints THIS by the material colour."""
    img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    px = img.load()
    for y in range(16):
        for x in range(16):
            if 5 <= y <= 10:  # the wire core (gets tinted to the material colour)
                px[x, y] = (200, 200, 200, 255)
            elif 3 <= y <= 12:  # insulation shoulders (dark casing, tint-muted)
                px[x, y] = (70, 70, 70, 255)
    return _png_bytes(img)


def _png_bytes(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _tier_of(net_id: str) -> str | None:
    # Power nets are keyed "power:<TIER>" (e.g. "power:LV"), so the tier is already in the scene.
    return net_id.split(":", 1)[1] if net_id.startswith("power:") else None


def main() -> None:
    ir = adapt_file(SAND)
    scene = build_scene(ir, solve(ir, optimize=False))

    wire = {"WIRE": _wire_icon_png()}
    route_tex: dict[str, str] = {}
    for r in scene["routes"]:
        tier = _tier_of(r["netId"])
        if tier is None:
            continue  # item/fluid pipes: v1 would use one fixed sprite; left flat here on purpose
        rgba = TIER_RGBA.get(tier, [200, 200, 200, 255])
        baked = bake_layers([{"icon": "WIRE", "rgba": rgba, "glow": False}], wire)
        if baked is not None:
            route_tex[r["netId"]] = _data_uri(baked)

    scene["routeTextures"] = route_tex

    out = ROOT / "out" / "sand-pipes-spike.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(render_html(scene), encoding="utf-8")

    powered = [r["netId"] for r in scene["routes"] if r["netId"] in route_tex]
    print(f"routes: {[r['netId'] for r in scene['routes']]}")
    print(f"baked cable sprites for: {powered}")
    print(f"scene.routeTextures keys: {list(route_tex)}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
