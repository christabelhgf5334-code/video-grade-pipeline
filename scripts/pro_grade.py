#!/usr/bin/env python3
"""
Per-clip "pro" grade: measure the footage, derive the parameters, bake, render.

    python pro_grade.py CLIP_DIR --out OUT_DIR --base-recipe references/recipes/universal-v1.json \
        --curve slog3 --hdr log

Why this exists: a single fixed LUT cannot serve footage whose measured saturation spans
0.11 to 0.78. Measuring first and deriving the parameters makes one recipe behave sensibly
across subjects — saturated footage gets *less* saturation, flat footage gets more contrast,
dark footage gets exposure, bright footage gets a stronger highlight shoulder.

The rules (all self-clamping):
    exposure = 0.42 / measured key            clamp 0.80 .. 1.90
    sat      = 0.48 / measured sat * K_CAL    clamp 0.75 .. 1.30
    contrast = 0.65 / measured spread         clamp 0.95 .. 1.35
    shoulder = 0.040 + 0.10 * max(0, key-0.55)

K_CAL exists because the derivation uses the 75th-percentile saturation while the check
uses mean HSL saturation; without it the target undershoots by ~25%.
"""
import argparse
import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import auto_grade as AG

# Chasing one absolute saturation target is wrong: a waterfall mist measures 0.09 and a
# fruit close-up 0.37; forcing both to 0.42 either fakes the first or under-serves the
# second. Aim for a RELATIVE lift with sane bounds instead.
SAT_BOOST = 1.55
SAT_MIN, SAT_MAX = 0.16, 0.46
CONTRAST_TARGET = 0.65


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration:stream=avg_frame_rate", "-of", "json", path],
                       capture_output=True, text=True)
    d = json.loads(r.stdout)
    v = next(s for s in d["streams"] if "avg_frame_rate" in s)
    fr = v["avg_frame_rate"]
    num, den = (int(x) for x in fr.split("/")) if "/" in fr else (int(fr), 1)
    return float(d["format"]["duration"]), round(num / den)


def grab(path, ts, vf, dst, cwd=None):
    # lut3d takes a bare filename (a Windows drive letter breaks the filter parser), so the
    # working directory must be the folder holding the .cube
    dst_abs = os.path.abspath(dst)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{ts:.3f}", "-i",
                    os.path.abspath(path), "-frames:v", "1",
                    "-vf", vf + ",format=rgb24", dst_abs], cwd=cwd, check=True)
    return np.asarray(Image.open(dst_abs).convert("RGB")).astype(np.float64) / 255.0


def derive(p, sat_scale=1.0):
    expo = float(np.clip(0.42 / max(p["key"], 0.10), 0.80, 1.90))
    # same metric in and out (mean HSL saturation) -> no calibration fudge needed
    base_sat = p.get("sat_mean", p["sat"])
    target = float(np.clip(base_sat * SAT_BOOST, SAT_MIN, SAT_MAX))
    sat = float(np.clip(target / max(base_sat, 0.06) * sat_scale, 0.60, 2.20))
    con = float(np.clip(CONTRAST_TARGET / max(p["spread"], 0.20), 0.95, 1.35))
    sho = round(0.040 + 0.10 * max(0.0, p["key"] - 0.55), 3)
    return {"exposure": round(expo, 3), "saturation": round(sat, 3),
            "contrast": round(con, 3), "shoulder": sho}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clip_dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--base-recipe", required=True)
    ap.add_argument("--curve", default="slog3")
    ap.add_argument("--hdr", default="log")
    ap.add_argument("--work", default=None)
    a = ap.parse_args()

    # every path handed to render_v5.sh must be absolute: that script cd's into the
    # LUT folder before it writes logs and markers, so relative paths silently break
    work = os.path.abspath(a.work or os.path.join(a.out, "_work"))
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(work, exist_ok=True)
    base = json.load(open(a.base_recipe, encoding="utf-8"))

    clips = sorted(f for f in os.listdir(a.clip_dir) if f.lower().endswith(".mp4"))
    summary = []

    for cf in clips:
        src = os.path.join(a.clip_dir, cf)
        stem = os.path.splitext(cf)[0]
        dur, fps = probe(src)
        print(f"\n=== {stem}  ({dur:.1f}s @ {fps}fps)")

        # pass 1: neutral decode, measure exposure
        dec0 = os.path.join(work, f"{stem}_dec0.cube")
        subprocess.run([sys.executable, os.path.join(HERE, "make_curve_lut.py"),
                        "--curve", a.curve, "--direction", "decode", "--out", dec0],
                       check=True, capture_output=True)
        f0 = grab(src, dur * 0.45, f"lut3d={os.path.basename(dec0)}",
                  os.path.join(work, "f0.png"), cwd=work)
        p0 = AG.analyze(f0)
        d = derive(p0)

        # pass 2: decode with the derived exposure, re-measure for sat/contrast
        dec1 = os.path.join(work, f"{stem}_decode.cube")
        subprocess.run([sys.executable, os.path.join(HERE, "make_curve_lut.py"),
                        "--curve", a.curve, "--direction", "decode",
                        "--exposure", str(d["exposure"]), "--out", dec1],
                       check=True, capture_output=True)
        f1 = grab(src, dur * 0.45, f"lut3d={os.path.basename(dec1)}",
                  os.path.join(work, "f1.png"), cwd=work)
        p1 = AG.analyze(f1)
        d = derive(p1)
        d["shoulder"] = round(0.040 + 0.10 * max(0.0, p1["key"] - 0.55), 3)

        # bake the look LUT from the base recipe with this clip's numbers
        rec = json.loads(json.dumps(base))
        rec["_note"] = f"pro@{stem} exposure x{d['exposure']} sat x{d['saturation']} contrast x{d['contrast']}"
        rec["primary_chain"] = (rec["primary_chain"]
                                .replace("saturation=1.02", f"saturation={d['saturation']:.3f}")
                                .replace("contrast=1.050", f"contrast={d['contrast']:.3f}"))
        rec["filmic"]["shoulder"] = d["shoulder"]
        spec = os.path.join(work, f"{stem}_recipe.json")
        json.dump(rec, open(spec, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

        look = os.path.join(work, f"{stem}_look.cube")
        subprocess.run([sys.executable, os.path.join(HERE, "build_look_lut.py"), spec,
                        "--out", look], check=True, capture_output=True)

        # closed loop: apply the LUT to the probe frame and measure what it actually did.
        # Trusting the formula alone is how three clips ended up pinned at the clamp.
        sat_scale = 1.0
        achieved = None
        for attempt in range(3):
            probe_out = os.path.join(work, "probe_out.png")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", os.path.join(work, "f1.png"),
                            "-vf", f"lut3d={os.path.basename(look)},format=rgb24", probe_out],
                           cwd=work, check=True)
            got = AG.analyze(np.asarray(Image.open(probe_out).convert("RGB")).astype(np.float64) / 255.0)
            achieved = got.get("sat_mean", got["sat"])
            target = float(np.clip(p1.get("sat_mean", p1["sat"]) * SAT_BOOST, SAT_MIN, SAT_MAX))
            if abs(achieved - target) <= 0.035 or attempt == 2:
                break
            sat_scale *= target / max(achieved, 0.04)
            d = derive(p1, sat_scale)
            rec = json.loads(json.dumps(base))
            rec["_note"] = f"pro@{stem} pass{attempt+2} sat_scale {sat_scale:.2f}"
            rec["primary_chain"] = (rec["primary_chain"]
                                    .replace("saturation=1.02", f"saturation={d['saturation']:.3f}")
                                    .replace("contrast=1.050", f"contrast={d['contrast']:.3f}"))
            rec["filmic"]["shoulder"] = d["shoulder"]
            json.dump(rec, open(spec, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            subprocess.run([sys.executable, os.path.join(HERE, "build_look_lut.py"), spec,
                            "--out", look], check=True, capture_output=True)

        print(f"  实测 key={p1['key']:.3f} spread={p1['spread']:.3f} sat_mean={p1.get('sat_mean', p1['sat']):.3f} "
              f"-> 曝光x{d['exposure']} 饱和x{d['saturation']:.3f} 对比x{d['contrast']:.3f} 肩部{d['shoulder']}")
        tgt = float(np.clip(p1.get("sat_mean", p1["sat"]) * SAT_BOOST, SAT_MIN, SAT_MAX))
        print(f"  闭环: 目标饱和 {tgt:.3f}  实测 {achieved:.3f}  "
              f"{'达标' if abs(achieved-tgt)<=0.035 else '未完全达标'}  重烘 {attempt} 次")

        env = dict(os.environ, SRC_DIR=os.path.abspath(a.clip_dir), OUT_DIR=os.path.abspath(a.out),
                   CLIPS=stem, HDR=a.hdr, FPS=str(fps),
                   PRE_LUT=os.path.abspath(dec1), LUT=os.path.abspath(look),
                   MARK_DIR=os.path.join(work, "mark"))
        subprocess.run(["bash", os.path.join(HERE, "render_v5.sh")], env=env,
                       capture_output=True)
        got = os.path.join(a.out, f"{stem}_graded.mp4")
        ok = os.path.exists(got)
        print(f"  -> {'OK' if ok else 'FAILED'}  {got if ok else ''}")
        summary.append({"clip": stem, **d, **{k: round(v, 3) for k, v in
                       {"key": p1["key"], "spread": p1["spread"], "sat": p1["sat"]}.items()},
                        "rendered": ok})

    js = os.path.join(a.out, "pro_params.json")
    json.dump(summary, open(js, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n参数表 -> {js}")
    print(f"{'clip':<16}{'曝光':>7}{'饱和':>7}{'对比':>7}{'肩部':>7}")
    for s in summary:
        print(f"{s['clip']:<16}{s['exposure']:>7}{s['saturation']:>7}{s['contrast']:>7}{s['shoulder']:>7}")


if __name__ == "__main__":
    sys.exit(main())
