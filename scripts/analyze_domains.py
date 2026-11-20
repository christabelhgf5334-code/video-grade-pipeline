#!/usr/bin/env python3
"""
Analyse which colour domains a frame is actually made of, before writing any qualifier.

    python analyze_domains.py FRAME|VIDEO [--k 14] [--at 0.5] [--out DIR] [--json d.json]

Video input grabs one frame at `--at` x duration.

Prints each domain's share / RGB / H / S / L and — the part that matters — flags pairs of
domains whose hues overlap. A hue-overlap pair cannot be separated by a hue qualifier no
matter how you tune it; you need a saturation or luminance discriminator, or a spatial
window. Discovering that here saves rewriting the same qualifier five times.
"""
import argparse
import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image

try:
    from PIL import ImageFont
    FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
    FONT_REG = "C:/Windows/Fonts/msyh.ttc"
except Exception:
    FONT_BOLD = FONT_REG = None


# ----------------------------------------------------------------- colour
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


def hue_dist(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


# ----------------------------------------------------------------- input
def load_frame(path, at=0.5):
    if path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
        return np.asarray(Image.open(path).convert("RGB")).astype(np.float32) / 255.0
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True).stdout.strip() or 0)
    ts = max(0.0, dur * at)
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_frame_tmp.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{ts:.3f}", "-i", path,
                    "-frames:v", "1", tmp], check=True)
    return np.asarray(Image.open(tmp).convert("RGB")).astype(np.float32) / 255.0


def kmeans(px, k, iters=25, seed=7):
    rng = np.random.default_rng(seed)
    C = px[rng.choice(len(px), k, replace=False)].copy()
    lab = np.zeros(len(px), dtype=int)
    for _ in range(iters):
        d = ((px[:, None, :] - C[None, :, :]) ** 2).sum(2)
        lab = d.argmin(1)
        for i in range(k):
            m = lab == i
            if m.sum():
                C[i] = px[m].mean(0)
    return C, lab


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--k", type=int, default=14)
    ap.add_argument("--at", type=float, default=0.5)
    ap.add_argument("--width", type=int, default=432, help="analysis width")
    ap.add_argument("--out", default=None, help="dir for the swatch image")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    frame = load_frame(a.input, a.at)
    h0, w0 = frame.shape[:2]
    small = np.asarray(Image.fromarray((frame * 255).astype(np.uint8))
                       .resize((a.width, int(a.width * h0 / w0))))
    px = (small.astype(np.float32) / 255.0).reshape(-1, 3)

    C, lab = kmeans(px, a.k)
    cnt = np.bincount(lab, minlength=a.k)
    total = len(px)
    order = np.argsort(-cnt)

    rows = []
    for i in order:
        if cnt[i] == 0:
            continue
        r, g, b = (C[i] * 255).round().astype(int)
        hh, ss, ll = rgb2hsl(C[i].reshape(1, 1, 3))
        rows.append({
            "share": float(cnt[i] / total),
            "rgb": [int(r), int(g), int(b)],
            "h": float(hh.ravel()[0]), "s": float(ss.ravel()[0]), "l": float(ll.ravel()[0]),
        })

    print(f"\n{'占比':>6}  {'RGB':<18}{'色相':>7}{'饱和':>7}{'亮度':>7}")
    print("-" * 50)
    for r in rows:
        if r["share"] < 0.005:
            continue
        print(f"{r['share']*100:5.1f}%  ({r['rgb'][0]:3d},{r['rgb'][1]:3d},{r['rgb'][2]:3d})"
              f"{r['h']:>10.1f}{r['s']:>7.3f}{r['l']:>7.3f}")

    # ---- group into hue families: 14 clusters are too granular to act on ----
    def family_of(r):
        """Coarse hue family in 30-degree buckets; near-neutrals get no hue at all."""
        if r["s"] < 0.06:
            return None          # hue is meaningless here: dark / grey / white
        return int((r["h"] + 15) // 30 * 30) % 360

    fams = {}
    for r in rows:
        if r["share"] < 0.01:
            continue
        k = family_of(r)
        f = fams.setdefault(k, {"share": 0.0, "s_min": 1.0, "s_max": 0.0,
                                "l_min": 1.0, "l_max": 0.0, "n": 0, "rgb": r["rgb"]})
        f["share"] += r["share"]
        f["s_min"] = min(f["s_min"], r["s"])
        f["s_max"] = max(f["s_max"], r["s"])
        f["l_min"] = min(f["l_min"], r["l"])
        f["l_max"] = max(f["l_max"], r["l"])
        f["n"] += 1

    print("\n色相家族（按 30° 聚合，这才是限定器真正能切分的最小单位）")
    print("-" * 62)
    print(f"{'色相':>8}{'占比':>8}{'饱和范围':>16}{'亮度范围':>16}")
    for k in sorted(fams, key=lambda x: (x is not None, -(fams[x]["share"]))):
        f = fams[k]
        if f["share"] < 0.02:
            continue
        s_rng = f"{f['s_min']:.2f}-{f['s_max']:.2f}"
        l_rng = f"{f['l_min']:.2f}-{f['l_max']:.2f}"
        label = "中性/暗部" if k is None else f"H{k}deg"
        print(f"{label:>8}{f['share']*100:7.1f}%{s_rng:>16}{l_rng:>16}")

    print("\n判定 / 可用维度")
    print("-" * 62)
    warned = False
    for k in sorted(fams, key=lambda x: (x is not None, -(fams[x]["share"]))):
        f = fams[k]
        if f["share"] < 0.05 or k is None:
            continue
        s_span = f["s_max"] - f["s_min"]
        l_span = f["l_max"] - f["l_min"]
        if s_span > 0.25:
            warned = True
            print(f"  ! H{k} 家族占 {f['share']*100:.0f}%，饱和跨度 {s_span:.2f}"
                  f"（{f['s_min']:.2f}-{f['s_max']:.2f}）")
            print(f"    -> 同一色相里混着高饱和和低饱和两种东西。想只动其中一个，"
                  f"用 sat_min/sat_max 门槛，不要指望色相。")
        if l_span > 0.45:
            warned = True
            print(f"  ! H{k} 家族亮度跨度 {l_span:.2f}，里面既有主体也有背景，"
                  f"必要时用 lum 门槛或空间窗口")
    if not warned:
        print("  各家族都比较干净，色相可以直接切分")
    print("\n提示：真正的分离判据是「收紧门槛后，该选的还在、不该选的掉了」。"
          "如果两者一起掉，说明这个维度没区分度，改用背景做分离。")

    if a.out:
        os.makedirs(a.out, exist_ok=True)
        sw_h, sw_w = 90, 720
        sheet = Image.new("RGB", (sw_w, sw_h * min(len(rows), 12) + 8), (18, 18, 18))
        dr = None
        try:
            from PIL import ImageDraw
            dr = ImageDraw.Draw(sheet)
            f1 = ImageFont.truetype(FONT_BOLD, 15) if FONT_BOLD else None
        except Exception:
            f1 = None
        for idx, r in enumerate([x for x in rows if x["share"] >= 0.005][:12]):
            y = idx * sw_h + 4
            sheet.paste(Image.new("RGB", (86, sw_h - 8), tuple(r["rgb"])), (4, y))
            if dr:
                txt = (f"{r['share']*100:4.1f}%   H{r['h']:5.1f}  S{r['s']:.2f}  L{r['l']:.2f}"
                       f"   RGB({r['rgb'][0]},{r['rgb'][1]},{r['rgb'][2]})")
                dr.text((100, y + sw_h // 2 - 10), txt, font=f1, fill=(235, 235, 230))
        p = os.path.join(a.out, "domains.png")
        sheet.save(p)
        print(f"\n色卡 -> {p}")

    if a.json:
        json.dump(rows, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"JSON -> {a.json}")


if __name__ == "__main__":
    sys.exit(main())
