"""The brand mark is centred, and every asset carries the same one.

The chevrons shipped 20% off-centre for the life of the project: laid out on a
30/50/70 pitch running to x=90, which centres the *coordinates* on x=60, not the
50 the plate is centred on. Nothing caught it because a coordinate bbox is not an
ink bbox — `stroke-linecap="round"` puts stroke-width/2 of paint beyond every
endpoint, so the real extent was 25.5..94.5: a 25.5 margin on the left and 5.5 on
the right. On a plain favicon that reads as "slightly off"; blown up to an iOS
home-screen icon it is unmissable, which is how it was finally spotted.

So the invariant is stated in ink, not in coordinates. For round caps and joins
the ink box is exactly the point box inflated by stroke-width/2 — the outer edge
of a round join is an arc of that radius centred on the vertex, same as a cap.
"""
from __future__ import annotations

import re
import struct
import zlib
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parent.parent / "relay" / "static" / "assets"

SVGS = ["relay-favicon.svg", "relay-appicon.svg", "relay-mark.svg", "relay-mark-on-dark.svg"]
RASTERS = [
    "favicon-16.png",
    "favicon-32.png",
    "favicon-64.png",
    "favicon-512.png",
    "relay-mark-512.png",
    "relay-mark-on-dark-512.png",
    "apple-touch-icon-180.png",
]


def _chevrons(svg: str) -> list[tuple[list[tuple[float, float]], float]]:
    """Every polyline as (points, stroke_width)."""
    out = []
    for tag in re.findall(r"<polyline\b[^>]*>", svg):
        pts = re.search(r'points="([^"]+)"', tag).group(1)
        width = float(re.search(r'stroke-width="([\d.]+)"', tag).group(1))
        out.append(([tuple(map(float, p.split(","))) for p in pts.split()], width))
    return out


def _ink_box(svg: str) -> tuple[float, float, float, float]:
    """Painted extent of the chevrons, caps and joins included."""
    xs, ys = [], []
    for points, width in _chevrons(svg):
        for x, y in points:
            xs += [x - width / 2, x + width / 2]
            ys += [y - width / 2, y + width / 2]
    return min(xs), min(ys), max(xs), max(ys)


def _viewbox(svg: str) -> tuple[float, float]:
    _, _, w, h = (float(n) for n in re.search(r'viewBox="([^"]+)"', svg).group(1).split())
    return w, h


@pytest.mark.parametrize("name", SVGS)
def test_the_mark_is_centred_in_every_brand_svg(name):
    svg = (ASSETS / name).read_text()
    width, height = _viewbox(svg)
    x0, y0, x1, y1 = _ink_box(svg)
    assert x0 == pytest.approx(width - x1), f"{name}: left margin {x0} vs right {width - x1}"
    assert y0 == pytest.approx(height - y1), f"{name}: top margin {y0} vs bottom {height - y1}"


def test_every_brand_svg_carries_the_same_chevrons():
    """One shape everywhere. The colours differ per asset; the geometry must not —
    these files are maintained by copy-paste, which is how the offset spread to
    all of them in the first place."""
    shapes = {name: _chevrons((ASSETS / name).read_text()) for name in SVGS}
    reference = shapes[SVGS[0]]
    for name, shape in shapes.items():
        assert shape == reference, f"{name} has drifted from {SVGS[0]}"


def _read_png(path: Path) -> tuple[int, int, bytes]:
    """Minimal RGBA8 PNG decoder — stdlib only, so this holds in CI without
    adding an image dependency for one test."""
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    pos, idat = 8, b""
    width = height = 0
    while pos < len(raw):
        (length,) = struct.unpack(">I", raw[pos : pos + 4])
        kind = raw[pos + 4 : pos + 8]
        body = raw[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", body)
            assert (depth, colour, interlace) == (8, 6, 0), f"{path.name}: expected 8-bit RGBA, non-interlaced"
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + length

    data = zlib.decompress(idat)
    bpp, stride = 4, width * 4
    out, prev = bytearray(), bytes(stride)
    at = 0
    for _ in range(height):
        filt, line = data[at], bytearray(data[at + 1 : at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if filt == 1:
                line[i] = (line[i] + a) & 0xFF
            elif filt == 2:
                line[i] = (line[i] + b) & 0xFF
            elif filt == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif filt == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        out += line
        prev = bytes(line)
    return width, height, bytes(out)


def _ink_margins(path: Path) -> tuple[int, int, int, int]:
    """left, right, top, bottom gaps around the chevrons.

    On a plated icon the chevrons are the pixels brighter than the near-black
    plate; on a bare mark they are simply the opaque ones.
    """
    width, height, px = _read_png(path)
    plated = not path.name.startswith("relay-mark")
    cols, rows = set(), set()
    for y in range(height):
        row = px[y * width * 4 : (y + 1) * width * 4]
        for x in range(width):
            r, g, b, a = row[x * 4 : x * 4 + 4]
            if a > 40 and (r + g + b > 200 if plated else True):
                cols.add(x)
                rows.add(y)
    assert cols, f"{path.name}: found no ink at all"
    return min(cols), width - 1 - max(cols), min(rows), height - 1 - max(rows)


@pytest.mark.parametrize("name", RASTERS)
def test_every_brand_raster_is_centred(name):
    """Catches a raster left stale after an SVG edit, which the SVG tests cannot
    see — these files are generated, and nothing at runtime re-derives them."""
    left, right, top, bottom = _ink_margins(ASSETS / name)
    assert abs(left - right) <= 1, f"{name}: {left}px of margin on the left, {right}px on the right"
    assert abs(top - bottom) <= 1, f"{name}: {top}px of margin on top, {bottom}px below"


def test_the_apple_touch_icon_is_opaque_and_full_bleed():
    """iOS masks the icon with its own squircle and composites transparency away,
    so an alpha corner becomes a dark fringe and our own rx becomes a second
    corner inside Apple's. It is the one brand asset that must be a plain square."""
    path = ASSETS / "apple-touch-icon-180.png"
    width, height, px = _read_png(path)
    assert (width, height) == (180, 180)
    alphas = px[3::4]
    assert min(alphas) == 255, "apple-touch-icon carries transparency"
    for y, x in ((0, 0), (0, width - 1), (height - 1, 0), (height - 1, width - 1)):
        off = (y * width + x) * 4
        assert tuple(px[off : off + 3]) == (0x1A, 0x1C, 0x20), "corner is not the plate colour — rounded?"


def test_the_shell_links_the_apple_touch_icon():
    html = (ASSETS.parent / "index.html").read_text()
    assert 'rel="apple-touch-icon" sizes="180x180" href="/assets/apple-touch-icon-180.png"' in html
    assert "/assets/favicon-512.png" not in html, "the rounded favicon is not an app icon"
