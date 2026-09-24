"""previewer.atlas - the baked face textures packed into the one image the viewer draws from.

The viewer's side of the atlas (UVs, the one shared material, the state swap) runs in the browser
and is compared against the per-texture page there (GitHub #94). What is pinned here is the image
itself: every face lands in a tile of its own, pixel for pixel, with a border that cannot bleed a
neighbour into it, and the running image differs from the idle one only where a face's running skin
does.
"""

from __future__ import annotations

import base64
import io
from typing import Any

import pytest

from gtnh_solver.previewer.atlas import pack_atlas

Image = pytest.importorskip("PIL.Image")


def _png(color: tuple[int, int, int, int], size: int = 16, *, dot: bool = False) -> str:
    """A flat ``color`` tile as a data URI; ``dot`` marks its top-left pixel white, so orientation
    and edges are checkable."""
    img = Image.new("RGBA", (size, size), color)
    if dot:
        img.putpixel((0, 0), (255, 255, 255, 255))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def _image(uri: str) -> Any:
    return Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))).convert("RGBA")


def _tile(atlas_uri: str, rect: list[int], border: int = 0) -> Any:
    x, y, w, h = rect
    return _image(atlas_uri).crop((x - border, y - border, x + w + border, y + h + border))


def _scene(textures: dict[str, str], active: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "blocks": [{"cell": [0, 0, 0], "texture": list(textures)[:6]}] if textures else [],
        "textures": dict(textures),
        "texturesActive": dict(active or {}),
    }


_RED, _GREEN, _BLUE = (200, 30, 30, 255), (30, 200, 30, 255), (30, 30, 200, 255)


def test_every_face_is_its_own_tile_pixel_for_pixel() -> None:
    pool = {"a": _png(_RED, dot=True), "b": _png(_GREEN), "c": _png(_BLUE, dot=True)}
    scene = _scene(pool)
    pack_atlas(scene)
    atlas = scene["atlas"]
    assert set(atlas["tiles"]) == set(pool)
    for key, uri in pool.items():
        assert list(_tile(atlas["image"], atlas["tiles"][key]).getdata()) == list(
            _image(uri).getdata()
        ), key


def test_the_pool_is_replaced_not_carried_twice() -> None:
    # The page embeds the scene, so a pool left beside the atlas would ship every face twice.
    scene = _scene({"a": _png(_RED)}, {"a": _png(_GREEN)})
    pack_atlas(scene)
    assert "textures" not in scene
    assert "texturesActive" not in scene


def test_a_tiles_border_is_its_own_edge_so_nothing_bleeds_in() -> None:
    # Nearest sampling on a face's very edge can land one texel outside its tile. That texel must be
    # the tile's own edge colour, never a neighbour's.
    pool = {"a": _png(_RED, dot=True), "b": _png(_GREEN), "c": _png(_BLUE)}
    scene = _scene(pool)
    pack_atlas(scene)
    atlas = scene["atlas"]
    for key, uri in pool.items():
        framed = _tile(atlas["image"], atlas["tiles"][key], border=1)
        inner = _image(uri)
        w, h = inner.size
        for i in range(w):
            assert framed.getpixel((i + 1, 0)) == inner.getpixel((i, 0))  # top border
            assert framed.getpixel((i + 1, h + 1)) == inner.getpixel((i, h - 1))  # bottom
        for j in range(h):
            assert framed.getpixel((0, j + 1)) == inner.getpixel((0, j))  # left
            assert framed.getpixel((w + 1, j + 1)) == inner.getpixel((w - 1, j))  # right
        assert framed.getpixel((0, 0)) == inner.getpixel((0, 0))  # a corner takes the corner pixel


def test_tiles_are_inside_the_image_and_their_frames_never_overlap() -> None:
    pool = {f"k{i}": _png((i * 20 % 256, 40, 90, 255)) for i in range(11)}
    scene = _scene(pool)
    pack_atlas(scene)
    atlas = scene["atlas"]
    width, height = atlas["size"]
    assert _image(atlas["image"]).size == (width, height)
    rects = [*atlas["tiles"].values(), atlas["missing"]]
    frames = [(x - 1, y - 1, x + w + 1, y + h + 1) for x, y, w, h in rects]  # tile plus its border
    for x0, y0, x1, y1 in frames:
        assert min(x0, y0) >= 0
        assert x1 <= width
        assert y1 <= height
    for i, a in enumerate(frames):
        for b in frames[i + 1 :]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], (a, b)


def test_the_running_image_differs_only_where_a_running_skin_does() -> None:
    pool = {"idle-only": _png(_RED), "runs": _png(_GREEN)}
    scene = _scene(pool, {"runs": _png(_BLUE, dot=True)})
    pack_atlas(scene)
    atlas = scene["atlas"]
    tiles = atlas["tiles"]
    running = atlas["active"]
    assert running is not None
    assert list(_tile(running, tiles["runs"]).getdata()) == list(
        _image(_png(_BLUE, dot=True)).getdata()
    )
    assert list(_tile(running, tiles["idle-only"], border=1).getdata()) == list(
        _tile(atlas["image"], tiles["idle-only"], border=1).getdata()
    )


def test_no_running_skin_means_no_running_image() -> None:
    # The viewer disables its state toggle on a null running image rather than show a dead button.
    scene = _scene({"a": _png(_RED)})
    pack_atlas(scene)
    assert scene["atlas"]["active"] is None


def test_the_missing_texture_is_minecrafts_checkerboard() -> None:
    scene = _scene({"a": _png(_RED)})
    pack_atlas(scene)
    check = _tile(scene["atlas"]["image"], scene["atlas"]["missing"])
    magenta, black = (248, 0, 248, 255), (0, 0, 0, 255)
    assert check.size == (16, 16)
    assert [check.getpixel(p) for p in ((0, 0), (15, 15))] == [magenta, magenta]
    assert [check.getpixel(p) for p in ((15, 0), (0, 15))] == [black, black]


def test_blocks_with_no_baked_face_still_get_the_checkerboard() -> None:
    # A machine can expand into blocks none of whose sprites baked; they must still draw, loudly.
    scene: dict[str, Any] = {"blocks": [{"cell": [0, 0, 0]}], "textures": {}, "texturesActive": {}}
    pack_atlas(scene)
    assert scene["atlas"] is not None
    assert scene["atlas"]["tiles"] == {}
    assert scene["atlas"]["missing"]


def test_nothing_to_draw_is_no_atlas() -> None:
    scene: dict[str, Any] = {"blocks": [], "textures": {}, "texturesActive": {}}
    pack_atlas(scene)
    assert scene["atlas"] is None


def test_packing_is_deterministic() -> None:
    pool = {"b": _png(_GREEN), "a": _png(_RED), "c": _png(_BLUE)}
    first, second = _scene(pool), _scene(dict(reversed(list(pool.items()))))
    pack_atlas(first)
    pack_atlas(second)
    assert first["atlas"] == second["atlas"]
