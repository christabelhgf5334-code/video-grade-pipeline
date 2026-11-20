#!/usr/bin/env python3
"""
Decide what the source ACTUALLY is before grading it: PQ HDR, HLG HDR, Log, or plain SDR.

    python detect_source.py CLIP_OR_FRAME [CLIP2 ...] [--out DIR] [--json r.json]

The Xiaomi 15 Ultra writes `color_transfer=unknown` with BT.2020 + 10-bit for BOTH its
HDR video mode and its Log video mode. Metadata cannot tell them apart. Applying a
Log-tuned LUT to HDR footage (or the reverse) is the single most common reason a batch
"looks wrong but nobody can say why": highlights clip, saturation runs away.

So this script renders the same frame under each candidate interpretation, scores how
healthy each result is, and names the winner. Always look at the comparison sheet before
committing — the score is a filter, not a verdict.

Routes it distinguishes:
    sdr    no conversion; the file is already Rec.709
    pq     zscale tin=smpte2084 ... tonemap (HDR10 / P010 BT.2020)
    hlg    zscale tin=arib-std-b67 ... tonemap
    log    none of the above score well -> Log curve, needs a source LUT first
"""
import argparse
import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

ROUTES = {
    "sdr":  "format=yuv420p",
    "pq":   ("zscale=tin=smpte2084:min=bt2020nc:pin=bt2020:rin=full:t=linear:npl=100,"
             "format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
             "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"),
    "hlg":  ("zscale=tin=arib-std-b67:min=bt2020nc:pin=bt2020:rin=full:t=linear:npl=100,"
             "format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
             "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"),
}


def probe(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,pix_fmt,width,height,avg_frame_rate,"
         "color_primaries,color_transfer:stream_side_data=rotation",
         "-of", "json", path], capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return None
    v = next((s for s in d.get("streams", []) if s["codec_type"] == "video"), None)
    if not v:
        return None
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    return {
        "duration": float(d["format"].get("duration") or 0),
        "pix_fmt": v.get("pix_fmt"), "w": v.get("width"), "h": v.get("height"),
        "rot": rot, "avg_fps": v.get("avg_frame_rate"),
        "primaries": (v.get("color_primaries") or "?").lower(),
        "transfer": (v.get("color_transfer") or "unknown").lower(),
    }


def grab(path, out, at=0.45):
    if path.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".webp")):
        Image.open(path).convert("RGB").save(out)
        return True
    p = probe(path)
    ts = (p["duration"] * at) if p else 0
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{ts:.3f}", "-i", path,
                        "-frames:v", "1", out], capture_output=True, text=True)
    return os.path.exists(out)


def score(img):
    """How plausible does this look as a correctly-interpreted Rec.709 frame?"""
    a = np.asarray(img).astype(np.float64) / 255.0
    mx, mn = a.max(-1), a.min(-1)
    l = (mx + mn) * 0.5
    s = np.where(mx - mn > 1e-6, (mx - mn) / (1 - np.abs(2 * l - 1) + 1e-9), 0)

    mean_l = float(l.mean())
    std = float(l.std())
    clip_hi = float((l > 0.985).mean())
    clip_lo = float((l < 0.015).mean())
    # A correctly interpreted frame EXPANDS the tonal range: a PQ signal read as SDR
    # keeps everything squeezed in the middle. This spread is a stronger discriminator
    # than std, which low-contrast content also depresses.
    spread = float(np.percentile(l, 95) - np.percentile(l, 5))

    # skin plausibility: warm mid-sat pixels, the usual subject in phone footage
    h = np.zeros_like(l)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    d = mx - mn
    nz = d > 1e-6
    rmax = nz & (mx == r); gmax = nz & (mx == g) & ~rmax; bmax = nz & (mx == b) & ~rmax & ~gmax
    h[rmax] = ((g[rmax] - b[rmax]) / d[rmax]) % 6
    h[gmax] = (b[gmax] - r[gmax]) / d[gmax] + 2
    h[bmax] = (r[bmax] - g[bmax]) / d[bmax] + 4
    h = h * 60
    skin = (h > 18) & (h < 45) & (s > 0.15) & (s < 0.60) & (l > 0.22) & (l < 0.68)
    skin_share = float(skin.mean())

    pen = 0.0
    pen += max(0.0, 0.32 - mean_l) * 120       # too dark
    pen += max(0.0, mean_l - 0.58) * 120       # too bright
    pen += max(0.0, 0.17 - std) * 220          # flat / no contrast
    pen += max(0.0, std - 0.36) * 120          # harsh
    pen += max(0.0, 0.52 - spread) * 260       # tones still bunched = unconverted HDR
    pen += clip_hi * 700                        # blown highlights
    pen += clip_lo * 400                        # crushed blacks
    # a natural frame has a meaningful share of warm mid-tone pixels
    pen += max(0.0, 0.02 - skin_share) * 300

    return {
        "score": round(100 - pen, 1),
        "mean_l": round(mean_l, 3), "contrast": round(std, 3),
        "spread": round(spread, 3),
        "clip_hi_pct": round(clip_hi * 100, 2), "clip_lo_pct": round(clip_lo * 100, 2),
        "skin_share_pct": round(skin_share * 100, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--out", default="source_check")
    ap.add_argument("--json", default=None)
    ap.add_argument("--sheet", default=None, help="comparison sheet output path")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    report = {}

    for path in a.inputs:
        meta = probe(path)
        if not meta:
            print(f"{path}: cannot probe")
            continue
        name = os.path.basename(path)
        print(f"\n{'='*66}\n{name}")
        print(f"  {meta['w']}x{meta['h']}  {meta['pix_fmt']}  transfer={meta['transfer']}"
              f"  primaries={meta['primaries']}  fps={meta['avg_fps']}"
              f"  rotation={meta['rot']}")

        frame = os.path.join(a.out, "_frame.png")
        if not grab(path, frame):
            print("  cannot grab a frame")
            continue

        results = {}
        for route, vf in ROUTES.items():
            dst = os.path.join(a.out, f"_{route}.png")
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", frame,
                                "-vf", vf, dst], capture_output=True, text=True)
            if r.returncode != 0 or not os.path.exists(dst):
                continue
            results[route] = (dst, score(Image.open(dst)))

        if not results:
            print("  every route failed")
            continue

        best = max(results, key=lambda k: results[k][1]["score"])
        print(f"  {'路线':<6}{'得分':>7}{'亮度':>8}{'对比':>8}{'动态范围':>10}"
              f"{'高光溢出%':>11}{'死黑%':>8}{'肤色%':>8}")
        for k, (_, sc) in sorted(results.items(), key=lambda x: -x[1][1]["score"]):
            mark = " <<<" if k == best else ""
            print(f"  {k:<6}{sc['score']:>7}{sc['mean_l']:>8.3f}{sc['contrast']:>8.3f}"
                  f"{sc['spread']:>10.3f}{sc['clip_hi_pct']:>11}{sc['clip_lo_pct']:>8}"
                  f"{sc['skin_share_pct']:>8}{mark}")

        top = results[best][1]["score"]
        second = sorted((v[1]["score"] for v in results.values()), reverse=True)
        margin = top - (second[1] if len(second) > 1 else 0)
        verdict = best if top >= 55 else "log"
        note = ""
        if top < 55:
            note = "所有路线都不健康 -> 大概率是 Log 曲线，需要先过源 LUT"
        elif margin < 4:
            note = f"领先第二名只有 {margin:.1f} 分，务必看对比图确认"
        print(f"  => 判定: {verdict.upper()}" + (f"   ({note})" if note else ""))

        report[name] = {"meta": meta, "routes": {k: v[1] for k, v in results.items()},
                        "verdict": verdict, "margin": round(margin, 1), "note": note}

        # 3-up sheet so the choice can be eyeballed
        tiles = []
        for k, (p, sc) in sorted(results.items(), key=lambda x: -x[1][1]["score"]):
            im = Image.open(p).convert("RGB")
            w = 330
            im = im.resize((w, int(w * im.height / im.width)), Image.LANCZOS)
            tiles.append((im, f"{k}  score {sc['score']}"))
        if tiles:
            tw, th = tiles[0][0].size
            sheet = Image.new("RGB", (len(tiles) * (tw + 6) + 6, th + 30), (16, 16, 16))
            dr = ImageDraw.Draw(sheet)
            f = ImageFont.truetype(FONT_BOLD, 14) if os.path.exists(FONT_BOLD) else None
            for i, (im, lbl) in enumerate(tiles):
                x = 6 + i * (tw + 6)
                dr.text((x, 6), lbl, font=f, fill=(235, 235, 230))
                sheet.paste(im, (x, 24))
            sp = a.sheet or os.path.join(a.out, "source_check.jpg")
            sheet.save(sp, quality=90)
            print(f"  对比图 -> {sp}")

    if a.json:
        json.dump(report, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\nJSON -> {a.json}")


if __name__ == "__main__":
    sys.exit(main())
