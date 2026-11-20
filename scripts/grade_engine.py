#!/usr/bin/env python3
"""
A real grading engine: encoded input -> linear -> tone map -> grade -> Rec.709, plus LUT bake.

    python grade_engine.py preview FRAME --curve pq --look appetite_warm --out out.jpg
    python grade_engine.py sheet   FRAME --curve pq --out looks.jpg
    python grade_engine.py bake    --curve pq --look appetite_warm --out look.cube --size 65

Why this instead of ffmpeg filter chains: colorbalance/eq/curves give you three crude knobs
per tonal band. This works in linear light with the primitives a colorist actually uses —
white balance, filmic tone curve, lift/gamma/gain per channel, contrast around a pivot,
luminance-weighted saturation with skin protection, and split tone — then encodes once.

The same function paints the preview and bakes the LUT, so what you see is what ships.
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from transfers import CANDIDATES, LUM, filmic_tonemap, rec709_encode, rec709_decode

FONT = "C:/Windows/Fonts/msyhbd.ttc"
EPS = 1e-9


# ------------------------------------------------------------------ primitives
def rgb2hsl(rgb):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx, mn = rgb.max(-1), rgb.min(-1)
    l = (mx + mn) * 0.5
    d = mx - mn
    s = np.zeros_like(l)
    nz = d > 1e-9
    s[nz] = d[nz] / (1.0 - np.abs(2.0 * l[nz] - 1.0) + 1e-9)
    h = np.zeros_like(l)
    rmax = nz & (mx == r); gmax = nz & (mx == g) & ~rmax
    bmax = nz & (mx == b) & ~rmax & ~gmax
    h[rmax] = ((g[rmax] - b[rmax]) / d[rmax]) % 6
    h[gmax] = (b[gmax] - r[gmax]) / d[gmax] + 2
    h[bmax] = (r[bmax] - g[bmax]) / d[bmax] + 4
    return (h * 60.0) % 360.0, s, l


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def white_balance(lin, temp=0.0, tint=0.0):
    """+temp warmer, +tint magenta. Applied as symmetric gains in linear light."""
    r = 1.0 + 0.28 * temp + 0.10 * tint
    g = 1.0 - 0.05 * temp - 0.16 * tint
    b = 1.0 - 0.30 * temp + 0.10 * tint
    return lin * np.array([r, g, b])


def lgg(lin, lift=(0, 0, 0), gamma=(1, 1, 1), gain=(1, 1, 1)):
    """Lift / gamma / gain, the classic three-way. Operates in linear light."""
    x = np.clip(lin, 0.0, None)
    x = np.power(np.maximum(x + np.array(lift), 0.0), 1.0 / np.array(gamma))
    return x * np.array(gain)


def contrast(lin, amount=1.0, pivot=0.18):
    return np.power(np.maximum(lin / pivot, EPS), amount) * pivot


def saturation(rgb, amount=1.0, skin_protect=0.0):
    y = (rgb @ LUM)[..., None]
    out = y + (rgb - y) * amount
    if skin_protect > 0:
        h, s, l = rgb2hsl(rgb)
        skin = smoothstep(0.06, 0.16, s) * (1 - smoothstep(0.55, 0.72, s))
        skin *= smoothstep(0.10, 0.20, l) * (1 - smoothstep(0.80, 0.95, l))
        band = (1 - smoothstep(14, 26, np.abs((h - 32 + 180) % 360 - 180)))
        w = (skin * band * skin_protect)[..., None]
        out = out * (1 - w) + rgb * w
    return np.clip(out, 0.0, None)


def split_tone(rgb, warm=(0, 0, 0), cool=(0, 0, 0)):
    """Tint highlights warm and shadows cool — the cheapest way to add depth."""
    h, s, l = rgb2hsl(rgb)
    w_hi = smoothstep(0.55, 0.98, l)[..., None]
    w_lo = (1 - smoothstep(0.10, 0.45, l))[..., None]
    return rgb + w_hi * np.array(warm) + w_lo * np.array(cool)


# ------------------------------------------------------------------ looks
LOOKS = {
    "appetite_warm": dict(
        label="食欲暖调", exposure=1.00, temp=0.30, tint=0.03,
        contrast=1.10, sat=1.16, skin_protect=0.45,
        lift=(0.004, 0.002, 0.000), gamma=(1.00, 1.00, 1.02), gain=(1.04, 1.00, 0.96),
        warm=(-0.004, -0.001, 0.008), cool=(0.010, 0.002, -0.012),
        shoulder=1.00,
    ),
    "clean_neutral": dict(
        label="干净中性", exposure=1.00, temp=0.06, tint=0.0,
        contrast=1.05, sat=1.04, skin_protect=0.35,
        lift=(0.002, 0.002, 0.002), gamma=(1.0, 1.0, 1.0), gain=(1.01, 1.0, 0.99),
        warm=(0, 0, 0), cool=(0, 0, 0),
        shoulder=1.00,
    ),
    "market_vivid": dict(
        label="夜市浓烈", exposure=1.03, temp=0.24, tint=0.05,
        contrast=1.20, sat=1.30, skin_protect=0.55,
        lift=(0.0, 0.0, 0.0), gamma=(1.00, 1.00, 1.03), gain=(1.06, 1.00, 0.94),
        warm=(-0.006, -0.002, 0.010), cool=(0.012, 0.002, -0.014),
        shoulder=1.00,
    ),
    "cinematic_teal": dict(
        label="电影青橙", exposure=1.00, temp=0.14, tint=0.0,
        contrast=1.14, sat=1.06, skin_protect=0.60,
        lift=(-0.002, 0.000, 0.006), gamma=(1.00, 1.00, 1.00), gain=(1.05, 1.00, 0.95),
        warm=(-0.004, -0.001, 0.010), cool=(0.004, 0.006, 0.014),
        shoulder=0.94,
    ),
    "film_soft": dict(
        label="胶片柔和", exposure=1.01, temp=0.18, tint=0.02,
        contrast=0.94, sat=0.94, skin_protect=0.40,
        lift=(0.014, 0.012, 0.010), gamma=(1.02, 1.02, 1.02), gain=(1.02, 1.00, 0.98),
        warm=(0.008, 0.004, -0.004), cool=(0.000, 0.002, 0.008),
        shoulder=0.88,
    ),
}


# ------------------------------------------------------------------ pipeline
def auto_exposure(lin, target=0.15):
    y = np.maximum(lin @ LUM, EPS)
    med = float(np.median(y))
    return target / med if med > 0 else 1.0


def grade(enc, curve="pq", look="appetite_warm", auto_exp=True, extra_exposure=1.0):
    decode = CANDIDATES[curve][1]
    lin = np.clip(decode(enc), 0.0, None)

    p = LOOKS[look]
    gain = (auto_exposure(lin) if auto_exp else 1.0) * p["exposure"] * extra_exposure
    lin = lin * gain

    lin = white_balance(lin, p["temp"], p["tint"])
    lin = lgg(lin, p["lift"], p["gamma"], p["gain"])
    lin = contrast(lin, p["contrast"])
    t = filmic_tonemap(lin, 1.0, p["shoulder"])
    rgb = rec709_encode(t)
    rgb = saturation(rgb, p["sat"], p["skin_protect"])
    rgb = split_tone(rgb, p["warm"], p["cool"])
    # Highlight roll-off: pull chroma out of near-whites so specular areas keep texture
    # instead of turning into flat white mush. Also nudges them down a touch.
    h2, s2, l2 = rgb2hsl(rgb)
    w_hi = smoothstep(0.78, 1.00, l2)
    y = (rgb @ LUM)[..., None]
    rgb = rgb * (1 - 0.45 * w_hi[..., None]) + y * (0.45 * w_hi[..., None])
    rgb = rgb - (0.045 * smoothstep(0.86, 1.00, l2))[..., None]
    return np.clip(rgb, 0.0, 1.0)


# ------------------------------------------------------------------ cli
def load_frame(path):
    return np.asarray(Image.open(path).convert("RGB")).astype(np.float64) / 255.0


def cmd_preview(a):
    enc = load_frame(a.frame)
    out = grade(enc, a.curve, a.look, not a.no_auto_exposure)
    Image.fromarray((out * 255).astype(np.uint8)).save(a.out, quality=94)
    print(f"{a.curve} + {a.look} -> {a.out}")


def cmd_sheet(a):
    enc = load_frame(a.frame)
    tiles = []
    tw = a.tile
    for key, p in LOOKS.items():
        out = grade(enc, a.curve, key, not a.no_auto_exposure)
        im = Image.fromarray((out * 255).astype(np.uint8))
        if a.crop:
            im = im.crop(tuple(int(v) for v in a.crop.split(",")))
        im = im.resize((tw, int(tw * im.height / im.width)), Image.LANCZOS)
        tiles.append((im, p["label"], key))
    w, h = tiles[0][0].size
    pad, lbl = 8, 30
    sheet = Image.new("RGB", (len(tiles) * (w + pad) + pad, h + lbl + pad), (16, 16, 16))
    dr = ImageDraw.Draw(sheet)
    f = ImageFont.truetype(FONT, 16) if os.path.exists(FONT) else None
    for i, (im, label, key) in enumerate(tiles):
        x = pad + i * (w + pad)
        dr.text((x, 7), f"{label}  ({key})", font=f, fill=(240, 236, 228))
        sheet.paste(im, (x, lbl))
    sheet.save(a.out, quality=92)
    print(f"{len(tiles)} looks on one frame -> {a.out}")


def cmd_bake(a):
    n = a.size
    idx = np.arange(n ** 3)
    grid = np.stack([idx % n, (idx // n) % n, idx // (n * n)], 1).astype(np.float64) / (n - 1)
    out = grade(grid, a.curve, a.look, not a.no_auto_exposure)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(f'TITLE "{a.look} via {a.curve}"\nLUT_3D_SIZE {n}\n')
        fh.write("DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n")
        for r, g, b in out:
            fh.write(f"{r:.6f} {g:.6f} {b:.6f}\n")
    print(f"wrote {a.out}  ({n}^3 = {n**3} nodes)")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("preview", "sheet"):
        s = sub.add_parser(name)
        s.add_argument("frame")
        s.add_argument("--curve", default="pq", choices=list(CANDIDATES))
        s.add_argument("--look", default="appetite_warm", choices=list(LOOKS))
        s.add_argument("--out", required=True)
        s.add_argument("--tile", type=int, default=340)
        s.add_argument("--crop", default=None)
        s.add_argument("--no-auto-exposure", action="store_true")

    b = sub.add_parser("bake")
    b.add_argument("--curve", default="pq", choices=list(CANDIDATES))
    b.add_argument("--look", default="appetite_warm", choices=list(LOOKS))
    b.add_argument("--out", required=True)
    b.add_argument("--size", type=int, default=65)
    b.add_argument("--no-auto-exposure", action="store_true")

    a = ap.parse_args()
    {"preview": cmd_preview, "sheet": cmd_sheet, "bake": cmd_bake}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
