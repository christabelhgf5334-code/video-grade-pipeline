#!/usr/bin/env python3
"""
Prove a grade did not damage skin — measure colour-domain deltas between two frames.

    python check_skin.py BEFORE.png AFTER.png [--json r.json]
    python check_skin.py v1.mp4 v2.mp4 --at 0.5

Bands are decided by hue + saturation + luminance, the same way a qualifier sees the image:

    skin      H 18-45   S 0.22-0.58   L 0.20-0.62
    food      H 20-55   S > 0.75                  (high-chroma warm = the subject)
    cool bg   H 165-245 S > 0.15
    neutral   S < 0.08

Verdict thresholds on skin:
    |dS| <= 0.02 and |dL| <= 0.03   -> protected
    |dS| <= 0.05                    -> visible but acceptable
    beyond that                     -> skin is being pushed, revisit the qualifier

Why measure instead of look: side-by-side comparison exaggerates. A 0.5% shift looks huge
next to the original and invisible on its own. Numbers do not have that problem.
"""
import argparse
import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image

BANDS = {
    "skin":    {"h": (18, 45), "s": (0.22, 0.58), "l": (0.20, 0.62)},
    "food":    {"h": (20, 55), "s": (0.75, 1.01), "l": (0.20, 1.01)},
    "cool_bg": {"h": (165, 245), "s": (0.15, 1.01), "l": (0.10, 1.01)},
    "neutral": {"h": None, "s": (0.0, 0.08), "l": (0.0, 1.01)},
}


def rgb2hsl(rgb):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx, mn = rgb.max(-1), rgb.min(-1)
    l = (mx + mn) * 0.5
    d = mx - mn
    s = np.zeros_like(l)
    nz = d > 1e-9
    s[nz] = d[nz] / (1.0 - np.abs(2.0 * l[nz] - 1.0) + 1e-9)
    h = np.zeros_like(l)
    rmax = nz & (mx == r)
    gmax = nz & (mx == g) & ~rmax
    bmax = nz & (mx == b) & ~rmax & ~gmax
    h[rmax] = ((g[rmax] - b[rmax]) / d[rmax]) % 6.0
    h[gmax] = (b[gmax] - r[gmax]) / d[gmax] + 2.0
    h[bmax] = (r[bmax] - g[bmax]) / d[bmax] + 4.0
    return (h * 60.0) % 360.0, s, l


def load(path, at):
    if path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
        return np.asarray(Image.open(path).convert("RGB")).astype(np.float64) / 255.0
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cs_tmp.png")
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True).stdout.strip() or 0)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * at:.3f}", "-i", path,
                    "-frames:v", "1", tmp], check=True)
    return np.asarray(Image.open(tmp).convert("RGB")).astype(np.float64) / 255.0


def mask_for(h, s, l, spec):
    m = np.ones_like(h, dtype=bool)
    if spec["h"] is not None:
        lo, hi = spec["h"]
        m &= (h >= lo) & (h <= hi)
    m &= (s >= spec["s"][0]) & (s <= spec["s"][1])
    m &= (l >= spec["l"][0]) & (l <= spec["l"][1])
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--at", type=float, default=0.5)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    A, B = load(a.before, a.at), load(a.after, a.at)
    if A.shape != B.shape:
        print(f"size mismatch: {A.shape} vs {B.shape} — same frame required")
        return 2

    ha, sa, la = rgb2hsl(A)
    hb, sb, lb = rgb2hsl(B)
    # the mask comes from BEFORE: that is what the qualifier was aimed at
    d_all = np.abs(A - B).mean() * 255

    print(f"\n整体像素差: {d_all:.2f}/255\n")
    print(f"{'色域':<10}{'像素':>8}{'饱和 前->后':>20}{'亮度 前->后':>20}{'判定':>14}")
    print("-" * 76)

    out = {"overall_mean_delta": round(float(d_all), 2), "bands": {}}
    worst_skin = 0.0

    for name, spec in BANDS.items():
        m = mask_for(ha, sa, la, spec)
        n = int(m.sum())
        if n < 2000:
            print(f"{name:<10}{n:>8}   (样本太少，跳过)")
            continue
        dS = float(sb[m].mean() - sa[m].mean())
        dL = float(lb[m].mean() - la[m].mean())
        if name == "skin":
            worst_skin = max(abs(dS), abs(dL) * 0.6)
            if abs(dS) <= 0.02 and abs(dL) <= 0.03:
                verdict, tag = "守住", "OK"
            elif abs(dS) <= 0.05:
                verdict, tag = "可见但可接受", "WATCH"
            else:
                verdict, tag = "被推过头", "PUSHED"
        else:
            verdict, tag = "-", "-"
        print(f"{name:<10}{n:>8}{f'{sa[m].mean():.3f} -> {sb[m].mean():.3f}':>20}"
              f"{f'{la[m].mean():.3f} -> {lb[m].mean():.3f}':>20}{verdict:>14}")
        out["bands"][name] = {"px": n, "s": [round(float(sa[m].mean()), 3),
                                             round(float(sb[m].mean()), 3)],
                              "l": [round(float(la[m].mean()), 3),
                                    round(float(lb[m].mean()), 3)],
                              "verdict": tag}

    print()
    if d_all > 10:
        print("注意：整体差 {:.1f}/255，说明这两帧之间不止差一个阶段（很可能跨了一级调色）。"
              .format(d_all))
        print("      一级调色本来就会改变皮肤饱和，此处的皮肤判定不能当作「二级泄漏」。")
        print("      要验证二级是否伤皮肤，请比较「一级」和「一级+二级」两帧。")
        print()

    if worst_skin <= 0.02:
        print("结论：皮肤没有被推动。这是可以交付的二级调色。")
    elif worst_skin <= 0.05:
        print("结论：皮肤有轻微变化，肉眼可接受。要更保险就缩小对应限定器的范围。")
    else:
        print("结论：皮肤被明显推动了。缩小限定器范围，或改用「压背景」来做分离。")

    out["skin_worst"] = round(worst_skin, 3)
    if a.json:
        json.dump(out, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"JSON -> {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
