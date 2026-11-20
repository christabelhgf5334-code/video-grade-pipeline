#!/usr/bin/env python3
"""
Shot matching: make a set of graded clips agree with each other.

    python match_shots.py --in GRADED_DIR --out MATCHED_DIR [--reference auto|NAME]
                          [--src SRC_DIR] [--fps auto]

A set of individually graded clips does not cut together: measured on five real clips the
median luma spanned 0.09-0.63 and the warm/cool balance spanned R-B +0.34 to -0.01. Each
clip was "correct" on its own and wrong as a sequence.

This picks a reference (the clip closest to the set median, or one you name), derives a
per-clip correction, bakes it into a small match LUT, re-renders, then measures again —
because a correction should be verified, not assumed.

Correction model (all applied after the look LUT):
    luma     eq=brightness  (additive, hits the median directly)
    spread   eq=contrast    (ratio of measured ranges)
    cast     colorbalance   rm/bm from the measured R-B gap
    chroma   eq=saturation  (ratio of mean HSL saturation)
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import auto_grade as AG

LUM = np.array([0.2126, 0.7152, 0.0722])


def measure(path, at=0.45):
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", path], capture_output=True,
                               text=True).stdout.strip() or 0)
    tmp = os.path.join(tempfile.gettempdir(), "vgp_match_tmp.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur*at:.3f}", "-i", path,
                    "-frames:v", "1", tmp], check=True)
    a = np.asarray(Image.open(tmp).convert("RGB")).astype(np.float64) / 255.0
    y = a @ LUM
    mx, mn = a.max(-1), a.min(-1)
    s = np.where(mx - mn > 1e-6, (mx - mn) / (1 - np.abs(mx + mn - 1) + 1e-9), 0)
    return {"key": float(np.median(y)),
            "spread": float(np.percentile(y, 95) - np.percentile(y, 5)),
            "sat": float(s.mean()),
            "rb": float(a[..., 0].mean() - a[..., 2].mean())}


def correction(m, ref, want_sat=False):
    bright = float(np.clip(ref["key"] - m["key"], -0.25, 0.25))
    # pushing down can crush shadows: 10434 went from 0.15% to 2.82% dead-black at -0.25.
    # If the clip is already dark, halve the downward correction.
    if bright < 0 and m["key"] < 0.35:
        bright *= 0.5
    con = float(np.clip(ref["spread"] / max(m["spread"], 0.05), 0.80, 1.30))
    # Saturation is a CONTENT property, not a consistency defect: a waterfall should read
    # less saturated than a fruit close-up. Matching it across unrelated shots flattens the
    # grade. Only enable inside one scene/lighting setup (--match-saturation).
    sat = float(np.clip(ref["sat"] / max(m["sat"], 0.03), 0.70, 1.45)) if want_sat else 1.0
    d_rb = float(np.clip(ref["rb"] - m["rb"], -0.30, 0.30))
    # Per-channel GAIN, not colorbalance. colorbalance offsets saturate at a magnitude that
    # could not pull a +0.334 cast back to neutral (measured: only reached +0.235). A gain
    # scales the whole channel, so it can close a large gap; the loop below refines it.
    kr = float(np.clip(1.0 + d_rb * 0.40, 0.80, 1.25))
    kb = float(np.clip(1.0 - d_rb * 0.40, 0.80, 1.25))
    return {"brightness": bright, "contrast": con, "saturation": sat,
            "kr": round(kr, 4), "kb": round(kb, 4), "d_rb": d_rb}


def bake_match(c, out, size=33):
    """Match corrections are gentle and smooth — 33^3 is plenty and keeps files small."""
    chain = (f"eq=brightness={c['brightness']:.4f}:contrast={c['contrast']:.4f}:"
             f"saturation={c['saturation']:.4f},"
             f"colorchannelmixer=rr={c['kr']:.4f}:bb={c['kb']:.4f}")
    spec = {"primary_chain": chain,
            "secondaries": [],
            "filmic": {},
            "size": size}
    p = out.replace(".cube", ".json")
    json.dump(spec, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    subprocess.run([sys.executable, os.path.join(HERE, "build_look_lut.py"), p,
                    "--out", out, "--size", str(size)], check=True, capture_output=True)
    return out


def dispersion(rows):
    k = [r["key"] for r in rows]
    sp = [r["spread"] for r in rows]
    sa = [r["sat"] for r in rows]
    rb = [r["rb"] for r in rows]
    return {"key_span": round(max(k) - min(k), 3), "key_std": round(float(np.std(k)), 3),
            "spread_std": round(float(np.std(sp)), 3),
            "sat_std": round(float(np.std(sa)), 3),
            "rb_span": round(max(rb) - min(rb), 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="indir", required=True, help="folder of graded clips")
    ap.add_argument("--out", required=True, help="folder for matched clips")
    ap.add_argument("--reference", default="auto")
    ap.add_argument("--src", default=None, help="source folder, to re-render from scratch")
    ap.add_argument("--fps", default="auto")
    ap.add_argument("--match-saturation", action="store_true",
                    help="also match saturation — only valid within one scene")
    ap.add_argument("--work", default=None)
    a = ap.parse_args()

    work = os.path.abspath(a.work or os.path.join(a.out, "_work"))
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(work, exist_ok=True)

    clips = sorted(f for f in os.listdir(a.indir) if f.endswith(".mp4"))
    if not clips:
        print("no clips"); return 1

    before = {c: measure(os.path.join(a.indir, c)) for c in clips}
    rows = [{"clip": c, **before[c]} for c in clips]

    if a.reference == "auto":
        med = {k: float(np.median([before[c][k] for c in clips]))
               for k in ("key", "spread", "sat", "rb")}
        ref_name = min(clips, key=lambda c: sum(
            abs(before[c][k] - med[k]) / max(abs(med[k]), 0.05) for k in med))
    else:
        ref_name = next(c for c in clips if a.reference in c)
    ref = before[ref_name]
    print(f"基准镜头: {ref_name}   key={ref['key']:.3f} spread={ref['spread']:.3f} "
          f"sat={ref['sat']:.3f} R-B={ref['rb']:+.3f}")

    d0 = dispersion(rows)
    print(f"\n匹配前离散度: 中间调跨度 {d0['key_span']:.3f}  σ={d0['key_std']:.3f}  "
          f"饱和σ={d0['sat_std']:.3f}  色偏跨度 {d0['rb_span']:.3f}")

    print(f"\n{'clip':<16}{'亮度修正':>9}{'对比':>7}{'饱和':>7}{'R-B补':>8}")
    for c in clips:
        if c == ref_name:
            print(f"{c[:-4]:<16}{'— 基准 —':>31}")
            continue
        corr = correction(before[c], ref, a.match_saturation)
        print(f"{c[:-4]:<16}{corr['brightness']:>+9.3f}{corr['contrast']:>7.2f}"
              f"{corr['saturation']:>7.2f}{corr['d_rb']:>+8.3f}")
        lut = os.path.join(work, c.replace(".mp4", "_match.cube"))
        bake_match(corr, lut)

        # closed loop on the cast: measure what the LUT actually did, then correct again
        for _ in range(2):
            probe = os.path.join(work, "_mkprobe.png")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "3", "-i",
                            os.path.abspath(os.path.join(a.indir, c)), "-frames:v", "1",
                            "-vf", f"lut3d={os.path.basename(lut)}", probe],
                           cwd=work, check=False)
            if not os.path.exists(probe):
                break
            got = np.asarray(Image.open(probe).convert("RGB")).astype(np.float64) / 255.0
            rb_now = float(got[..., 0].mean() - got[..., 2].mean())
            err = ref["rb"] - rb_now
            if abs(err) <= 0.03:
                break
            corr["kr"] = round(float(np.clip(corr["kr"] * (1.0 + err * 0.60), 0.75, 1.35)), 4)
            corr["kb"] = round(float(np.clip(corr["kb"] * (1.0 - err * 0.60), 0.75, 1.35)), 4)
            bake_match(corr, lut)

        # re-render from source when available so the match is baked into one pass,
        # otherwise transcode the graded file (one extra generation, acceptable fallback)
        env = dict(os.environ)
        if a.src:
            fps = a.fps
            if fps == "auto":
                fr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                     "-show_entries", "stream=avg_frame_rate", "-of",
                                     "csv=p=0", os.path.join(a.src, c)],
                                    capture_output=True, text=True).stdout.strip()
                num, den = (fr.split("/") + ["1"])[:2]
                fps = str(round(int(num) / int(den)))
            env.update(SRC_DIR=os.path.abspath(a.src), OUT_DIR=os.path.abspath(a.out),
                       CLIPS=c[:-4], POST_LUT=lut, FPS=str(fps),
                       MARK_DIR=os.path.join(work, "mark"))
            subprocess.run(["bash", os.path.join(HERE, "render_v5.sh")], env=env,
                           capture_output=True)
        else:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i",
                            os.path.abspath(os.path.join(a.indir, c)),
                            "-vf", f"lut3d={os.path.basename(lut)}",
                            "-map", "0:v:0", "-map", "0:a?",
                            "-c:a", "copy",
                            "-c:v", "libx265", "-preset", "veryfast", "-crf", "18",
                            "-x265-params", "log-level=error", "-pix_fmt", "yuv420p10le",
                            os.path.abspath(os.path.join(a.out, c))], cwd=work, check=False)

    # the reference clip still needs to exist in the output folder
    if a.src:
        env = dict(os.environ, SRC_DIR=os.path.abspath(a.src),
                   OUT_DIR=os.path.abspath(a.out), CLIPS=ref_name[:-4],
                   FPS=str(a.fps if a.fps != "auto" else 24),
                   MARK_DIR=os.path.join(work, "mark"))
        subprocess.run(["bash", os.path.join(HERE, "render_v5.sh")], env=env,
                       capture_output=True)
    else:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i",
                        os.path.abspath(os.path.join(a.indir, ref_name)),
                        "-c", "copy", os.path.abspath(os.path.join(a.out, ref_name))], check=False)

    after = {}
    for c in clips:
        p = os.path.join(a.out, c)
        if os.path.exists(p):
            after[c] = measure(p)
    if len(after) == len(clips):
        d1 = dispersion([{"clip": c, **after[c]} for c in clips])
        print(f"\n匹配后离散度: 中间调跨度 {d1['key_span']:.3f}  σ={d1['key_std']:.3f}  "
              f"饱和σ={d1['sat_std']:.3f}  色偏跨度 {d1['rb_span']:.3f}")
        print(f"改善: 中间调跨度 {d0['key_span']:.3f} -> {d1['key_span']:.3f}   "
              f"色偏跨度 {d0['rb_span']:.3f} -> {d1['rb_span']:.3f}")
        json.dump({"before": d0, "after": d1,
                   "reference": ref_name}, open(os.path.join(a.out, "match_report.json"),
                                                "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    else:
        print("\n部分片段未生成，跳过匹配后测量")


if __name__ == "__main__":
    sys.exit(main())
