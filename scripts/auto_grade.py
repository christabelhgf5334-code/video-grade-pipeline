#!/usr/bin/env python3
"""
Scene-adaptive grading: derive the correction from the image, then apply a bounded style.

Fixed presets only work for the scene they were tuned on. This module measures the frame
and computes the base correction itself, so the same skill works on night markets, daylight
portraits, landscapes and product shots. The "look" is then a *bounded delta* on top:

    base correction (from measurements)  +  style (clamped)  =  final

Every style delta is clamped against what the image already has, so a warm look on an
already-warm scene cannot run away, and a saturation boost on an already-vivid scene is
automatically reduced.

    python auto_grade.py analyze FRAME.png
    python auto_grade.py sheet   FRAME.png --out looks.jpg
    python auto_grade.py bake    --look cel_warm --out look.cube
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from transfers import LUM, filmic_tonemap, rec709_decode, rec709_encode
from grade_engine import (rgb2hsl, smoothstep, white_balance, lgg, contrast,
                          saturation, split_tone)

FONT = "C:/Windows/Fonts/msyhbd.ttc"
EPS = 1e-9


# ---------------------------------------------------------------- analysis
def analyze(enc):
    """Profile a Rec.709-encoded frame: key, range, cast, chroma, subject hints."""
    mx, mn = enc.max(-1), enc.min(-1)
    l = (mx + mn) * 0.5
    y = enc @ LUM
    s = np.where(mx - mn > 1e-6, (mx - mn) / (1 - np.abs(2 * l - 1) + 1e-9), 0)
    h, _, _ = rgb2hsl(enc)

    p5, p50, p95, p99 = (float(np.percentile(y, q)) for q in (5, 50, 95, 99))

    # neutral reference: the brightest 10% of pixels are usually specular or white goods
    bright = y > np.percentile(y, 90)
    ref = enc[bright].mean(0) if bright.sum() > 100 else enc.reshape(-1, 3).mean(0)
    ref = np.maximum(ref, 0.02)
    cast = (ref / ref.mean()) - 1.0          # +red / +blue deviation from neutral

    # 窄色相 + 中等饱和 + 中间亮度：过于宽松会把美食的暖色误判成皮肤
    skin = ((h > 20) & (h < 42) & (s > 0.18) & (s < 0.55) & (l > 0.25) & (l < 0.70))
    sky = ((h > 185) & (h < 255) & (s > 0.10) & (y > 0.45))
    green = ((h > 70) & (h < 165) & (s > 0.12))

    return {
        "key": round(p50, 3),
        "black": round(p5, 3),
        "white": round(p95, 3),
        "spread": round(p95 - p5, 3),
        "clip_hi": round(float((y > 0.99).mean() * 100), 2),
        "crush_lo": round(float((y < 0.01).mean() * 100), 2),
        "sat": round(float(np.percentile(s, 75)), 3),
        # mean HSL saturation — the SAME metric the verification step reports. Deriving a
        # gain from the 75th percentile and checking it with the mean caused a systematic
        # 25% undershoot and pushed three clips into the clamp.
        "sat_mean": round(float(s.mean()), 3),
        "cast_r": round(float(cast[0]), 3),
        "cast_b": round(float(cast[2]), 3),
        "skin": round(float(skin.mean() * 100), 1),
        "sky": round(float(sky.mean() * 100), 1),
        "green": round(float(green.mean() * 100), 1),
        "night": bool(p50 < 0.30),
    }


def describe(p):
    tags = []
    if p["night"]:
        tags.append("夜景/暗调")
    elif p["key"] > 0.55:
        tags.append("高调/日景")
    else:
        tags.append("中间调")
    if p["skin"] > 8:
        tags.append("有人物")
    if p["sky"] > 8:
        tags.append("有天空")
    if p["green"] > 15:
        tags.append("有植被")
    if p["sat"] > 0.55:
        tags.append("本身已浓艳")
    if p["spread"] < 0.45:
        tags.append("画面偏平")
    if abs(p["cast_r"]) > 0.06 or abs(p["cast_b"]) > 0.06:
        tags.append("有明显偏色")
    return "、".join(tags) if tags else "常规"


# ---------------------------------------------------------------- base correction
def derive_base(p):
    """Turn measurements into exposure / white balance / contrast, all self-limiting."""
    # exposure: aim the median at 0.44 (a touch above mid, reads well on phones)
    target = 0.40 if p["night"] else 0.46
    gain = float(np.clip(rec709_decode(target) / max(rec709_decode(p["key"]), EPS), 0.55, 2.2))

    # Highlight guard. A frame with a large dark area and a few bright objects pulls the
    # median down, so a median-only exposure blows the bright part (a neon sign + pale
    # pancakes is exactly this case). Back the gain off until the top end survives.
    if p["white"] > 0.90:
        gain *= 0.86
    if p["clip_hi"] > 2.0:
        gain *= 0.86
    if p["clip_hi"] > 8.0:
        gain *= 0.88

    # white balance with a deadband: a night market IS warm on purpose. Only correct the
    # part of the cast that exceeds 0.06, and only 60% of that, so intentional colour
    # survives while gross casts get fixed.
    cast = np.array([p["cast_r"], 0.0, p["cast_b"]])
    excess = np.sign(cast) * np.maximum(np.abs(cast) - 0.06, 0.0)
    wb = -np.clip(excess, -0.12, 0.12) * 0.60

    # contrast: expand a flat image, leave a punchy one alone
    want = 0.60 if not p["night"] else 0.55
    cur = max(p["spread"], 0.05)
    k = float(np.clip(np.log(want) / np.log(cur), 0.92, 1.35))

    return {"exposure": round(gain, 3), "wb": wb.round(3).tolist(), "contrast": round(k, 3)}


# ---------------------------------------------------------------- styles
# Every value is a DELTA on top of the base correction, and is clamped later.
STYLES = {
    "cel_natural": dict(label="自然还原", temp=0.00, tint=0.00, sat=1.00,
                        clutch=1.00, lift=0.000, hi_roll=0.25, split_w=(0, 0, 0),
                        split_c=(0, 0, 0)),
    "cel_warm":    dict(label="暖调", temp=0.16, tint=0.02, sat=1.06,
                        clutch=1.04, lift=0.002, hi_roll=0.30, split_w=(-0.003, -0.001, 0.006),
                        split_c=(0.008, 0.002, -0.010)),
    "cel_cool":    dict(label="冷调", temp=-0.14, tint=0.00, sat=1.00,
                        clutch=1.00, lift=0.004, hi_roll=0.25, split_w=(0.004, 0.001, -0.005),
                        split_c=(-0.006, 0.001, 0.012)),
    "cel_cinema":  dict(label="电影感", temp=0.10, tint=0.01, sat=1.04,
                        clutch=1.10, lift=0.006, hi_roll=0.42, split_w=(-0.004, -0.001, 0.010),
                        split_c=(0.004, 0.006, 0.014)),
    "cel_vivid":   dict(label="浓艳", temp=0.12, tint=0.03, sat=1.22,
                        clutch=1.14, lift=0.000, hi_roll=0.30, split_w=(-0.005, -0.002, 0.008),
                        split_c=(0.010, 0.002, -0.012)),
    "cel_film":    dict(label="胶片", temp=0.08, tint=0.02, sat=0.94,
                        clutch=0.94, lift=0.012, hi_roll=0.50, split_w=(0.007, 0.003, -0.004),
                        split_c=(0.000, 0.002, 0.008)),
}


def apply_style_params(p, style_name):
    """Clamp a style against what the image already is."""
    st = dict(STYLES[style_name])

    # already-saturated footage gets less saturation, not more of the same
    if p["sat"] > 0.50:
        st["sat"] = 1.0 + (st["sat"] - 1.0) * 0.45
    # a warm look on a warm scene compounds: shrink it
    if p["cast_r"] > 0.05 and st["temp"] > 0:
        st["temp"] *= 0.55
    # heavy skin presence: protect harder, warm less
    if p["skin"] > 12:
        st["temp"] *= 0.75
        st["clutch"] = max(st["clutch"], 1.02)
    # flat input can take more contrast, punchy input less
    if p["spread"] > 0.72:
        st["clutch"] = 1.0 + (st["clutch"] - 1.0) * 0.5
    return st


def grade_auto(enc, style="cel_warm", base=None):
    p = analyze(enc)
    b = base or derive_base(p)
    st = apply_style_params(p, style)

    lin = rec709_decode(np.clip(enc, 0, 1)) * b["exposure"]
    lin = lin * (1.0 + np.array(b["wb"]))
    lin = white_balance(lin, st["temp"], st["tint"])
    lin = lgg(lin, (st["lift"],) * 3, (1, 1, 1), (1, 1, 1))
    t = filmic_tonemap(lin, 1.0, 1.0)
    rgb = rec709_encode(t)
    # Contrast in DISPLAY space as a bounded S-curve. Doing it in linear light with a
    # power law exploded the highlights: a 1.35 exponent drove 0.98 to 1.9 and 14% of the
    # frame clipped. smoothstep keeps the endpoints pinned at 0 and 1.
    s_str = float(np.clip((b["contrast"] * st["clutch"] - 1.0) * 1.8, 0.0, 0.55))
    if s_str > 0:
        rgb = rgb + s_str * (smoothstep(0.03, 0.97, rgb) - rgb)
    rgb = saturation(rgb, st["sat"], skin_protect=0.4)
    rgb = split_tone(rgb, st["split_w"], st["split_c"])

    # highlight roll-off so specular never turns into paper white
    h2, s2, l2 = rgb2hsl(rgb)
    w_hi = smoothstep(0.80, 1.0, l2) * st["hi_roll"]
    y = (rgb @ LUM)[..., None]
    rgb = rgb * (1 - w_hi[..., None]) + y * w_hi[..., None]
    return np.clip(rgb, 0.0, 1.0)


# ---------------------------------------------------------------- cli
def load(path):
    return np.asarray(Image.open(path).convert("RGB")).astype(np.float64) / 255.0


def cmd_analyze(a):
    p = analyze(load(a.frame))
    print(json.dumps(p, ensure_ascii=False, indent=2))
    print(f"\n场景: {describe(p)}")
    b = derive_base(p)
    print(f"自动校正: 曝光 x{b['exposure']}  白平衡 {b['wb']}  对比 x{b['contrast']}")


def cmd_sheet(a):
    enc = load(a.frame)
    p = analyze(enc)
    print(f"场景: {describe(p)}")
    b = derive_base(p)
    print(f"自动校正: 曝光 x{b['exposure']}  白平衡 {b['wb']}  对比 x{b['contrast']}\n")

    tiles, tw = [], a.tile
    for key in STYLES:
        out = grade_auto(enc, key, b)
        im = Image.fromarray((out * 255).astype(np.uint8))
        if a.crop:
            im = im.crop(tuple(int(v) for v in a.crop.split(",")))
        im = im.resize((tw, int(tw * im.height / im.width)), Image.LANCZOS)
        tiles.append((im, STYLES[key]["label"]))
    # original first
    o = Image.fromarray((np.clip(enc, 0, 1) * 255).astype(np.uint8))
    if a.crop:
        o = o.crop(tuple(int(v) for v in a.crop.split(",")))
    o = o.resize((tw, int(tw * o.height / o.width)), Image.LANCZOS)
    tiles.insert(0, (o, "原片"))

    w, h = tiles[0][0].size
    cols = a.cols
    rows = (len(tiles) + cols - 1) // cols
    pad, lbl = 6, 26
    sheet = Image.new("RGB", (cols * (w + pad) + pad, rows * (h + lbl + pad) + pad),
                      (16, 16, 16))
    dr = ImageDraw.Draw(sheet)
    f = ImageFont.truetype(FONT, 15) if os.path.exists(FONT) else None
    for i, (im, lb) in enumerate(tiles):
        r_, c_ = divmod(i, cols)
        x = pad + c_ * (w + pad)
        y = pad + r_ * (h + lbl + pad)
        dr.text((x, y + 4), lb, font=f, fill=(238, 232, 220) if i else (150, 150, 145))
        sheet.paste(im, (x, y + 22))
    sheet.save(a.out, quality=92)
    print(f"{len(tiles)-1} 个风格 + 原片 -> {a.out}")


def cmd_bake(a):
    n = a.size
    idx = np.arange(n ** 3)
    grid = np.stack([idx % n, (idx // n) % n, idx // (n * n)], 1).astype(np.float64) / (n - 1)
    out = grade_auto(grid, a.look)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(f'TITLE "{a.look} (scene-adaptive)"\nLUT_3D_SIZE {n}\n')
        fh.write("DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n")
        for r, g, b in out:
            fh.write(f"{r:.6f} {g:.6f} {b:.6f}\n")
    print(f"wrote {a.out} ({n}^3)")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("analyze")
    s1.add_argument("frame")

    s2 = sub.add_parser("sheet")
    s2.add_argument("frame")
    s2.add_argument("--out", required=True)
    s2.add_argument("--tile", type=int, default=330)
    s2.add_argument("--cols", type=int, default=4)
    s2.add_argument("--crop", default=None)

    s3 = sub.add_parser("bake")
    s3.add_argument("--look", default="cel_warm", choices=list(STYLES))
    s3.add_argument("--out", required=True)
    s3.add_argument("--size", type=int, default=65)

    a = ap.parse_args()
    {"analyze": cmd_analyze, "sheet": cmd_sheet, "bake": cmd_bake}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
