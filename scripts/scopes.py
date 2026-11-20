#!/usr/bin/env python3
"""
Scopes: waveform, RGB parade, vectorscope — the QA a colourist actually looks at.

    python scopes.py FRAME.png --out scopes.jpg
    python scopes.py CLIP.mp4 --at 0.45 --out scopes.jpg

Why the eyes are not enough: exposure errors, clipped channels and skin-tone drift are
hard to judge on a normal-size image but obvious on a scope. Everything measured here has
a hard target:

  waveform   100% line = clipping. Anything piled on it has lost detail.
  parade     channels separated — a cast shows as one channel sitting higher.
  vectorscope skin runs along the I-line (~+33 deg); pushing it off-line is how faces go
             orange or green without anyone noticing on the image itself.
"""
import argparse
import os
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_B = "C:/Windows/Fonts/msyhbd.ttc"
FONT_R = "C:/Windows/Fonts/msyh.ttc"
LUM = np.array([0.2126, 0.7152, 0.0722])


def load(src, at):
    if src.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".webp")):
        im = Image.open(src).convert("RGB")
    else:
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                    "format=duration", "-of", "csv=p=0", src],
                                   capture_output=True, text=True).stdout.strip() or 0)
        tmp = os.path.join(tempfile.gettempdir(), "vgp_scope_tmp.png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur*at:.3f}", "-i", src,
                        "-frames:v", "1", tmp], check=True)
        im = Image.open(tmp).convert("RGB")
    return im


def waveform(rgb, w, h):
    """Luma waveform: x = image x, y = level (0 bottom, 1 top), brightness = density."""
    img = rgb.resize((w, h), Image.BILINEAR)
    a = np.asarray(img).astype(np.float64) / 255.0
    y = a @ LUM
    out = np.zeros((h * 2, a.shape[1]))
    yy = (y * (h * 2 - 1)).astype(int)
    for x in range(a.shape[1]):
        np.add.at(out[:, x], yy[:, x], 1.0)
    out = np.log1p(out)
    out /= max(out.max(), 1e-6)
    return out


def parade(rgb, w, h):
    img = rgb.resize((w, h), Image.BILINEAR)
    a = np.asarray(img).astype(np.float64) / 255.0
    panels = []
    for c in range(3):
        out = np.zeros((h * 2, a.shape[1]))
        yy = (a[..., c] * (h * 2 - 1)).astype(int)
        for x in range(a.shape[1]):
            np.add.at(out[:, x], yy[:, x], 1.0)
        out = np.log1p(out)
        panels.append(out / max(out.max(), 1e-6))
    return panels


def vectorscope(rgb, size=300):
    img = rgb.resize((size, size), Image.BILINEAR)
    a = np.asarray(img).astype(np.float64) / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    # Rec.709 YCbCr chroma plane
    cb = -0.1146 * r - 0.3854 * g + 0.5000 * b
    cr = 0.5000 * r - 0.4542 * g - 0.0458 * b
    n = size
    hist = np.zeros((n, n))
    xx = ((cb + 0.5) * (n - 1)).astype(int).clip(0, n - 1)
    yy = ((0.5 - cr) * (n - 1)).astype(int).clip(0, n - 1)
    np.add.at(hist, (yy.ravel(), xx.ravel()), 1.0)
    hist = np.log1p(hist)
    hist /= max(hist.max(), 1e-6)
    return hist


def to_img(arr, tint=(120, 235, 180)):
    a = np.clip(arr, 0, 1)
    img = np.zeros((*a.shape, 3), dtype=np.uint8)
    for i, c in enumerate(tint):
        img[..., i] = (a * c).astype(np.uint8)
    return Image.fromarray(img)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--at", type=float, default=0.45)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    rgb = load(a.source, a.at)
    W, H = 420, 220
    wf = to_img(waveform(rgb, W, H))
    par = parade(rgb, W, H)
    par_imgs = [to_img(p, tint) for p, tint in zip(par, [(235, 120, 120), (120, 235, 120), (130, 160, 245)])]
    vec = to_img(vectorscope(rgb, 300), (150, 220, 255))

    # ---- report
    arr = np.asarray(rgb).astype(np.float64) / 255.0
    y = arr @ LUM
    clip_pct = float((y > 0.995).mean() * 100)
    crush_pct = float((y < 0.005).mean() * 100)
    ch_clip = [float((arr[..., c] > 0.995).mean() * 100) for c in range(3)]
    r_b = float(arr[..., 0].mean() - arr[..., 2].mean())

    # skin hue from the same band the graders use
    mx, mn = arr.max(-1), arr.min(-1)
    s = np.where(mx - mn > 1e-6, (mx - mn) / (1 - np.abs(mx + mn - 1) + 1e-9), 0)
    h = np.zeros_like(mx)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    d = mx - mn
    nz = d > 1e-6
    for mask, val in (((nz) & (mx == r), ((g - b) / np.maximum(d, 1e-9)) % 6),
                      ((nz) & (mx == g) & ~(mx == r), (b - r) / np.maximum(d, 1e-9) + 2),
                      ((nz) & (mx == b) & ~(mx == r) & ~(mx == g), (r - g) / np.maximum(d, 1e-9) + 4)):
        h[mask] = val[mask]
    h = h * 60
    skin = (h > 15) & (h < 48) & (s > 0.12) & (s < 0.62) & (y > 0.18) & (y < 0.78)
    skin_hue = float(np.median(h[skin])) if skin.sum() > 500 else None

    print(f"波形: 高光溢出 {clip_pct:.2f}%   死黑 {crush_pct:.2f}%   "
          f"通道溢出 R{ch_clip[0]:.2f}% G{ch_clip[1]:.2f}% B{ch_clip[2]:.2f}%")
    print(f"色偏: R-B {r_b:+.3f}   (>|0.10| 明显偏色)")
    if skin_hue is not None:
        print(f"肤色: 色相中位 {skin_hue:.1f}deg   (目标 30-37deg，>42 偏黄，<26 偏红)   "
              f"占比 {float(skin.mean()*100):.1f}%")
    else:
        print("肤色: 画面中无明显肤色区")

    # ---- compose
    pad, lab = 10, 26
    sheet = Image.new("RGB", (3 * W + 4 * pad, H * 2 + lab * 2 + 4 * pad + 40), (12, 12, 12))
    d = ImageDraw.Draw(sheet)
    fb = ImageFont.truetype(FONT_B, 16) if os.path.exists(FONT_B) else None
    fr = ImageFont.truetype(FONT_R, 12) if os.path.exists(FONT_R) else None
    d.text((pad, 8), os.path.basename(a.source), font=fb, fill=(238, 234, 226))
    d.text((pad, 28), f"溢出 {clip_pct:.2f}%  |  死黑 {crush_pct:.2f}%  |  R-B {r_b:+.3f}  |  "
                      f"肤色 {skin_hue:.1f}deg" if skin_hue else
                      f"溢出 {clip_pct:.2f}%  |  死黑 {crush_pct:.2f}%  |  R-B {r_b:+.3f}",
           font=fr, fill=(160, 160, 155))
    y0 = 48
    sheet.paste(wf, (pad, y0 + lab)); d.text((pad, y0 + 4), "波形 Waveform", font=fr, fill=(150, 200, 175))
    for i, p in enumerate(par_imgs):
        x = pad + (i + 1) * (W + pad)
        d.text((x, y0 + 4), ["R", "G", "B"][i], font=fr, fill=[(235, 140, 140), (140, 235, 140), (150, 170, 245)][i])
        sheet.paste(p, (x, y0 + lab))
    y1 = y0 + lab + H + pad
    sheet.paste(vec, (pad, y1 + lab)); d.text((pad, y1 + 4), "矢量图 Vectorscope",
                                              font=fr, fill=(150, 200, 175))
    prev = rgb.resize((W * 2 + pad, H), Image.LANCZOS)
    sheet.paste(prev, (pad + 300 + pad, y1 + lab))
    d.text((pad + 300 + pad, y1 + 4), "画面", font=fr, fill=(160, 160, 155))
    sheet.save(a.out, quality=92)
    print(f"-> {a.out}")


if __name__ == "__main__":
    sys.exit(main())
