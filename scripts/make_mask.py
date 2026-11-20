#!/usr/bin/env python3
"""
Make a per-clip subject mask (grayscale PNG) for region-based grading.

    python make_mask.py --size 2160x3840 --preset band --param 0.32 --feather 0.05 --out mask.png
    python make_mask.py --size 2160x3840 --poly "0,0.30 1,0.30 1,1.0 0,1.0" \
        --feather 0.06 --out mask.png

White = subject (gets lifted / sharpened), black = background (gets pressed / cooled).

Why a still mask, not tracking: these are locked-off or gently handheld shots of a static
scene. A still mask feathered generously is indistinguishable from tracking for grading
purposes, and costs nothing. It is NOT good enough for object removal or cutouts.

Presets:
    band     horizontal split; --param is the split point (0..1 from top)
    ellipse  centred ellipse; --param is the vertical coverage (0..1)
    left/right/top/bottom   half frame
"""
import argparse
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def build(w, h, preset, param, poly, feather_px):
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)

    if poly:
        pts = []
        for tok in poly.replace(";", " ").split():
            x, y = tok.split(",")
            pts.append((float(x) * w, float(y) * h))
        d.polygon(pts, fill=255)
    elif preset == "band":
        y0 = param * h
        d.rectangle([0, y0, w, h], fill=255)
    elif preset == "ellipse":
        ry = param * h * 0.5
        rx = ry * w / h * 1.25
        cx, cy = w * 0.5, h * 0.55
        d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255)
    elif preset == "left":
        d.rectangle([0, 0, w * param, h], fill=255)
    elif preset == "right":
        d.rectangle([w * (1 - param), 0, w, h], fill=255)
    elif preset == "top":
        d.rectangle([0, 0, w, h * param], fill=255)
    elif preset == "bottom":
        d.rectangle([0, h * (1 - param), w, h], fill=255)
    else:
        raise SystemExit(f"unknown preset {preset}")

    if feather_px > 0:
        m = m.filter(ImageFilter.GaussianBlur(feather_px))
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", required=True, help="WxH, must match the video frame")
    ap.add_argument("--preset", default="band")
    ap.add_argument("--param", type=float, default=0.32)
    ap.add_argument("--poly", default=None)
    ap.add_argument("--feather", type=float, default=0.05,
                    help="feather radius as a fraction of frame height")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    w, h = (int(v) for v in a.size.lower().split("x"))
    m = build(w, h, a.preset, a.param, a.poly, a.feather * h)
    m.save(a.out)
    cov = np.asarray(m).astype(np.float64).mean() / 255.0
    print(f"mask {w}x{h}  preset={a.preset}  feather={a.feather*h:.0f}px  "
          f"subject covers {cov*100:.1f}% -> {a.out}")


if __name__ == "__main__":
    sys.exit(main())
