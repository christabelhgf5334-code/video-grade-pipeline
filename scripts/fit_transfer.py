#!/usr/bin/env python3
"""
Which transfer curve was this footage shot with? Ask the image, not the metadata.

    python fit_transfer.py FRAME.png [--out DIR] [--exposure auto]

Renders the same frame through every candidate curve, auto-exposes each one to the same
mid-grey, and scores how plausible the result looks. Prints a ranked table and writes a
labelled contact sheet — look at it before trusting the ranking.
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from transfers import CANDIDATES, LUM, to_display, filmic_tonemap, rec709_encode

FONT = "C:/Windows/Fonts/msyhbd.ttc"


def load(path):
    im = Image.open(path).convert("RGB")
    a = np.asarray(im).astype(np.float64) / 255.0
    return a


def auto_exposure(lin, target=0.18):
    """Scale linear light so the median luminance lands on middle grey."""
    y = np.maximum(lin @ LUM, 1e-9)
    med = float(np.median(y))
    if med <= 0:
        return 1.0
    return target / med


def score(img):
    mx, mn = img.max(-1), img.min(-1)
    l = (mx + mn) * 0.5
    s = np.where(mx - mn > 1e-6, (mx - mn) / (1 - np.abs(2 * l - 1) + 1e-9), 0)
    mean_l, std = float(l.mean()), float(l.std())
    spread = float(np.percentile(l, 95) - np.percentile(l, 5))
    clip_hi = float((l > 0.985).mean())
    clip_lo = float((l < 0.015).mean())

    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    d = mx - mn
    nz = d > 1e-6
    h = np.zeros_like(l)
    rmax = nz & (mx == r); gmax = nz & (mx == g) & ~rmax; bmax = nz & (mx == b) & ~rmax & ~gmax
    h[rmax] = ((g[rmax] - b[rmax]) / d[rmax]) % 6
    h[gmax] = (b[gmax] - r[gmax]) / d[gmax] + 2
    h[bmax] = (r[bmax] - g[bmax]) / d[bmax] + 4
    h *= 60
    skin = (h > 15) & (h < 48) & (s > 0.12) & (s < 0.62) & (l > 0.20) & (l < 0.72)
    skin_share = float(skin.mean())

    pen = 0.0
    pen += abs(mean_l - 0.46) * 90
    pen += max(0.0, 0.17 - std) * 240
    pen += max(0.0, std - 0.34) * 110
    pen += max(0.0, 0.55 - spread) * 240
    pen += clip_hi * 800 + clip_lo * 400
    pen += max(0.0, 0.015 - skin_share) * 320
    return {"score": round(100 - pen, 1), "mean_l": round(mean_l, 3), "std": round(std, 3),
            "spread": round(spread, 3), "hi%": round(clip_hi * 100, 2),
            "lo%": round(clip_lo * 100, 2), "skin%": round(skin_share * 100, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frame")
    ap.add_argument("--out", default="transfer_fit")
    ap.add_argument("--tile", type=int, default=300)
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    enc = load(a.frame)

    rows, tiles = [], []
    for key, (label, decode) in CANDIDATES.items():
        lin = decode(enc)
        gain = auto_exposure(lin)
        out = np.clip(rec709_encode(filmic_tonemap(lin * gain)), 0.0, 1.0)
        s = score(out)
        rows.append((key, label, s, gain))
        im = Image.fromarray((out * 255).astype(np.uint8))
        im = im.resize((a.tile, int(a.tile * im.height / im.width)), Image.LANCZOS)
        tiles.append((im, f"{label}  {s['score']}"))
        Image.fromarray((out * 255).astype(np.uint8)).save(
            os.path.join(a.out, f"{key}.png"))

    rows.sort(key=lambda r: -r[2]["score"])
    print(f"\n{'曲线':<22}{'得分':>7}{'亮度':>8}{'对比':>8}{'动态范围':>10}"
          f"{'高光%':>8}{'死黑%':>8}{'肤色%':>8}{'曝光增益':>10}")
    print("-" * 92)
    for key, label, s, gain in rows:
        print(f"{label:<22}{s['score']:>7}{s['mean_l']:>8.3f}{s['std']:>8.3f}"
              f"{s['spread']:>10.3f}{s['hi%']:>8}{s['lo%']:>8}{s['skin%']:>8}{gain:>10.2f}")

    best = rows[0]
    runner = rows[1] if len(rows) > 1 else rows[0]
    print(f"\n首选: {best[1]} ({best[2]['score']})   次选: {runner[1]} "
          f"({runner[2]['score']})   差 {best[2]['score'] - runner[2]['score']:.1f} 分")
    print("分数只是排序，务必看对照图确认哪一格黑位、白位、肤色都正常。")

    # contact sheet in score order
    order = {k: i for i, (k, _, _, _) in enumerate(rows)}
    tiles.sort(key=lambda t: order.get(
        next(k for k, lbl, _, _ in rows if t[1].startswith(lbl)), 99))
    tw, th = tiles[0][0].size
    cols = 5
    nrows = (len(tiles) + cols - 1) // cols
    pad, lblh = 6, 28
    sheet = Image.new("RGB", (cols * (tw + pad) + pad, nrows * (th + lblh + pad) + pad),
                      (16, 16, 16))
    dr = ImageDraw.Draw(sheet)
    f = ImageFont.truetype(FONT, 14) if os.path.exists(FONT) else None
    for i, (im, lbl) in enumerate(tiles):
        r_, c_ = divmod(i, cols)
        x = pad + c_ * (tw + pad)
        y = pad + r_ * (th + lblh + pad)
        dr.text((x, y + 4), lbl, font=f, fill=(240, 236, 228))
        sheet.paste(im, (x, y + 22))
    sp = os.path.join(a.out, "transfer_fit.jpg")
    sheet.save(sp, quality=90)
    print(f"对照图 -> {sp}")


if __name__ == "__main__":
    sys.exit(main())
