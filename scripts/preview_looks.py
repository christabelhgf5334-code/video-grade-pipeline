#!/usr/bin/env python3
"""
Render several candidate looks on ONE frame and build a labelled contact sheet.

    python preview_looks.py FRAME_OR_VIDEO --looks looks.json --out sheet.jpg \
        [--cols 3] [--crop 0,300,2160,2500] [--at 0.5] [--tile 360]

Looks file:
[
  {"name": "1 clean_neutral", "chain": "eq=contrast=1.03:saturation=1.02"},
  {"name": "2 warm_appetite", "chain": "colorbalance=rm=0.034:bm=-0.030,eq=contrast=1.09"}
]

Why one frame: comparing looks across different frames is not a comparison — exposure and
content change, so you end up judging the shot instead of the grade. Same frame, same crop,
same size, labelled. Only then does "which one is better" mean anything.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_REG = "C:/Windows/Fonts/msyh.ttc"


def grab_frame(path, at, out):
    if path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
        Image.open(path).convert("RGB").save(out)
        return out
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True).stdout.strip() or 0)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * at:.3f}", "-i", path,
                    "-frames:v", "1", out], check=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--looks", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cols", type=int, default=3)
    ap.add_argument("--crop", default=None, help="x0,y0,x1,y1 in source pixels")
    ap.add_argument("--at", type=float, default=0.5)
    ap.add_argument("--tile", type=int, default=360, help="tile width in the sheet")
    ap.add_argument("--title", default="")
    a = ap.parse_args()

    looks = json.load(open(a.looks, encoding="utf-8"))
    if not isinstance(looks, list) or not looks:
        print("looks file must be a non-empty JSON array")
        return 2

    tmp = tempfile.mkdtemp(prefix="looks_")
    frame = grab_frame(a.source, a.at, os.path.join(tmp, "frame.png"))

    crop = None
    if a.crop:
        crop = tuple(int(float(v)) for v in a.crop.split(","))

    tiles = []
    for i, lk in enumerate(looks):
        name = lk.get("name", f"look{i}")
        chain = lk.get("chain", "null")
        dst = os.path.join(tmp, f"out{i}.png")
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", frame,
                            "-vf", chain, dst], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(dst):
            print(f"  [{name}] chain failed: {r.stderr.strip()[:200]}")
            continue
        im = Image.open(dst).convert("RGB")
        if crop:
            im = im.crop(crop)
        im = im.resize((a.tile, int(a.tile * im.height / im.width)), Image.LANCZOS)
        tiles.append((im, name))

    if not tiles:
        print("no tile rendered")
        return 1

    w, h = tiles[0][0].size
    cols = min(a.cols, len(tiles))
    rows = (len(tiles) + cols - 1) // cols
    pad, lbl = 8, 34
    head = 34 if a.title else 0
    sheet = Image.new("RGB", (cols * (w + pad) + pad, rows * (h + lbl + pad) + pad + head),
                      (16, 16, 16))
    dr = ImageDraw.Draw(sheet)
    try:
        f1 = ImageFont.truetype(FONT_BOLD, 16)
    except OSError:
        f1 = ImageFont.load_default()
    try:
        f2 = ImageFont.truetype(FONT_REG, 13)
    except OSError:
        f2 = ImageFont.load_default()

    if a.title:
        dr.text((pad, 9), a.title, font=f1, fill=(240, 240, 235))

    for i, (im, name) in enumerate(tiles):
        r_, c_ = divmod(i, cols)
        x = pad + c_ * (w + pad)
        y = head + pad + r_ * (h + lbl + pad)
        dr.text((x, y), name, font=f1, fill=(238, 232, 220))
        sheet.paste(im, (x, y + 22))

    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    sheet.save(a.out, quality=92)
    print(f"{len(tiles)} look(s) on one frame -> {a.out}  ({sheet.width}x{sheet.height})")
    print("下一步：判断顺序 皮肤 -> 高光 -> 色彩平衡 -> 风格强度。皮肤失色的方案直接淘汰。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
