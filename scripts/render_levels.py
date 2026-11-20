#!/usr/bin/env python3
"""
Multi-level grading orchestrator: level 1 + level 2 + level 3 in one render.

    python render_levels.py CLIP.mp4 --out DIR --style cel_warm
    python render_levels.py CLIP.mp4 --out DIR --style cel_cinema --levels 123 \
        --mask-preset band --mask-param 0.30 --halation 0.20 --grain 5 --vignette PI/12

What each level is, and why they are separate passes:

  L1  primary   exposure / white balance / contrast / filmic curve, GLOBAL.
                Derived per clip from the frame's own statistics -> baked to a LUT.
  L2  secondary REGIONAL. The subject is lifted while the background is pressed and
                desaturated. Needs a mask, so it cannot live in a LUT.
  L3  texture   halation (highlight bloom), grain, vignette. Spatial too, so also
                outside the LUT.

Order matters: L1 normalises the image, L2 separates subject from background, L3 adds the
finish. Doing L3 first buries the separation under bloom; doing L2 before L1 means pushing
pixels that are still wrongly exposed.

Everything is CFR-locked at encode time. Phone footage is VFR; without `-r N -fps_mode cfr`
editors read the file as 30fps.
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
from transfers import LUM

TONEMAP = ("zscale=tin=smpte2084:min=bt2020nc:pin=bt2020:rin=full:t=linear:npl=100,"
           "format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
           "zscale=t=bt709:m=bt709:r=tv")


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration:stream=width,height", "-of", "json", path],
                       capture_output=True, text=True)
    d = json.loads(r.stdout)
    v = next(s for s in d["streams"] if "width" in s)
    return {"dur": float(d["format"]["duration"]), "w": v["width"], "h": v["height"]}


def grab_709(path, ts, dst):
    # The analysis frame MUST enter through the same filter prefix the render uses.
    # Saving through `format=rgb24` instead of `format=gbrpf32le` produced a frame whose
    # levels differed from the render by ~24/255, so every parameter derived from it was
    # computed for an image the renderer never sees.
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{ts:.3f}", "-i", path,
                    "-frames:v", "1", "-vf", TONEMAP + ",format=gbrpf32le", dst], check=True)
    return np.asarray(Image.open(dst).convert("RGB")).astype(np.float64) / 255.0


def bake_clip_lut(style, base, size, dst):
    idx = np.arange(size ** 3)
    grid = np.stack([idx % size, (idx // size) % size, idx // (size * size)], 1)
    grid = grid.astype(np.float64) / (size - 1)
    out = AG.grade_auto(grid, style, base)
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(f'TITLE "{style} L1"\nLUT_3D_SIZE {size}\n')
        fh.write("DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n")
        for r, g, b in out:
            fh.write(f"{r:.6f} {g:.6f} {b:.6f}\n")
    return dst


def build_graph(levels, lut_name, mask_name, w, h, args):
    """CTRL: assemble the filter_complex string for the requested levels."""
    parts = [f"[0:v]{TONEMAP},format=gbrpf32le,lut3d={lut_name}[L1]"]
    last = "[L1]"

    if "2" in levels:
        # 二级：主体提亮 / 背景压缩，用遮罩混合
        subj = (f"eq=contrast={1 + args.subj_contrast:.3f}:"
                f"brightness={args.subj_bright:.3f}:saturation={1 + args.subj_sat:.3f}")
        bgd = (f"eq=contrast={1 - args.bg_contrast:.3f}:"
               f"brightness={-args.bg_bright:.3f}:saturation={1 - args.bg_sat:.3f},"
               f"colorbalance=rm=-0.02:bm=0.04")
        # maskedmerge works cleanly ONLY in RGB. On YUV it just gates luma and the mask's
        # neutral chroma (128) blends 50/50 into U/V, desaturating everything (measured:
        # red+blue+white mask -> magenta). In RGB it selects properly.
        # Operand order matters: a WHITE mask selects the SECOND input, so subject goes
        # second. Verified with the same solid-colour test -> pure blue, no blend.
        parts.append(f"{last}split=2[sa][sb]")
        parts.append(f"[sa]{subj},format=gbrpf32le[subjs]")
        parts.append(f"[sb]{bgd},format=gbrpf32le[bgb]")
        parts.append(f"[1:v]format=gray,scale={w}:{h},format=gbrp[mk]")
        parts.append("[bgb][subjs][mk]maskedmerge,format=gbrpf32le[L2]")
        last = "[L2]"

    if "3" in levels:
        if args.halation > 0:
            parts.append(f"{last}split=2[hm][hs]")
            parts.append("[hs]curves=all='0/0 0.78/0 1/1',scale=iw/4:ih/4,"
                         f"gblur=sigma={args.halation_sigma},scale=iw*4:ih*4,"
                         "colorbalance=rm=0.12:gm=0.04:bm=-0.10[hal]")
            parts.append(f"[hm][hal]blend=all_mode=screen:all_opacity={args.halation}[L3a]")
            last = "[L3a]"
        if args.grain > 0:
            parts.append(f"{last}noise=alls={args.grain}:allf=t[L3b]")
            last = "[L3b]"
        if args.vignette:
            parts.append(f"{last}vignette=angle={args.vignette}[L3c]")
            last = "[L3c]"

    parts.append(f"{last}format=yuv420p10le[v]")
    return ";".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clip")
    ap.add_argument("--out", required=True)
    ap.add_argument("--style", default="cel_warm", choices=list(AG.STYLES),
                    help="ignored when --lut is given (kept for compatibility)")
    ap.add_argument("--lut", default=None,
                    help="look LUT for level 1; defaults to the bundled V5 look")
    ap.add_argument("--levels", default="123", help="any of 1 / 2 / 3, e.g. 123 or 13")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--crf", type=int, default=19)
    ap.add_argument("--lut-size", type=int, default=65)

    g = ap.add_argument_group("level 2 (regional)")
    g.add_argument("--mask-preset", default="band",
                   choices=["band", "ellipse", "left", "right", "top", "bottom"])
    g.add_argument("--mask-param", type=float, default=0.30)
    g.add_argument("--mask-file", default=None, help="use this mask instead of a preset")
    g.add_argument("--mask-feather", type=float, default=0.055)
    g.add_argument("--subj-bright", type=float, default=0.045)
    g.add_argument("--subj-contrast", type=float, default=0.09)
    g.add_argument("--subj-sat", type=float, default=0.08)
    g.add_argument("--bg-bright", type=float, default=0.045)
    g.add_argument("--bg-contrast", type=float, default=0.05)
    g.add_argument("--bg-sat", type=float, default=0.26)
    g.add_argument("--l2-max-coverage", type=float, default=0.55,
                   help="skip level 2 when the mask covers more than this share of the frame")

    g3 = ap.add_argument_group("level 3 (texture)")
    g3.add_argument("--halation", type=float, default=0.20, help="0 disables")
    g3.add_argument("--halation-sigma", type=int, default=9)
    g3.add_argument("--grain", type=int, default=0,
                    help="0 = off (default). Grain is per-frame random and "
                         "nearly incompressible: strength 5 at 4K60 pushed "
                         "bitrate past 300 Mbps. Use 1-3 only if you accept it.")
    g3.add_argument("--vignette", default="PI/12", help="empty disables")

    a = ap.parse_args()
    if not a.lut:
        a.lut = os.path.join(os.path.dirname(HERE), "assets", "luts",
                             "selected-color-look-v3.cube")
    if not os.path.exists(a.lut):
        print(f"LUT not found: {a.lut}"); return 1
    os.makedirs(a.out, exist_ok=True)
    stem = os.path.splitext(os.path.basename(a.clip))[0]

    info = probe(a.clip)
    w, h = info["w"], info["h"]
    if abs(w / h - 9 / 16) > 1e-3 and w > h:
        w, h = h, w                      # portrait storage is sideways

    # ---- level 1: reuse the accepted look LUT verbatim.
    # Deriving a fresh one per clip was tried and rejected: the hand-tuned V5 LUT beat the
    # parameterised engine on the same footage, so L2/L3 must sit ON TOP of it, not replace it.
    import shutil
    lut = os.path.join(a.out, os.path.basename(a.lut))
    shutil.copyfile(a.lut, lut)
    print(f"[L1] {os.path.basename(a.lut)}  (固定 look，不再逐条推导)")

    # ---- level 2 mask
    mask = None
    if "2" in a.levels:
        if a.mask_file:
            mask = a.mask_file
        else:
            from make_mask import build as build_mask
            mask = os.path.join(a.out, f"{stem}_mask.png")
            build_mask(w, h, a.mask_preset, a.mask_param, None,
                       a.mask_feather * h).save(mask)
        cov = float(np.asarray(Image.open(mask).convert("L")).mean() / 255.0)
        # If the mask covers most of the frame there is no background left to press, so the
        # regional pass can only damage the subject. Measured on a close-up food shot: a band
        # at 15% covered 70% of the frame and the "separation" gain came entirely from the
        # vignette, not from the region grade. Skip it and say so.
        if cov > args.l2_max_coverage:
            print(f"[L2] 跳过：遮罩覆盖 {cov*100:.0f}% > {args.l2_max_coverage*100:.0f}%，"
                  f"没有可分离的背景（分区只会在主体上乱压）")
            a_levels = a.levels.replace("2", "") or "13"
            a.levels = a_levels
            mask = None
        else:
            print(f"[L2] mask -> {os.path.basename(mask)}  覆盖 {cov*100:.0f}%")

    graph = build_graph(a.levels, os.path.basename(lut),
                        os.path.basename(mask) if mask else None, w, h, a)
    # lut3d and the mask must be reachable by relative name
    cwd = a.out

    dst = os.path.join(a.out, f"{stem}_graded.mp4")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-i", os.path.abspath(a.clip)]
    if mask:
        # A still mask MUST declare its frame rate and duration. `-loop 1` alone defaults
        # to 25fps and infinite length, which makes maskedmerge wait forever against a
        # 60fps main stream — the render then hangs instead of erroring.
        cmd += ["-loop", "1", "-framerate", str(a.fps), "-t", f"{info['dur']:.3f}",
                "-i", os.path.abspath(mask)]
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
            "-r", str(a.fps), "-fps_mode", "cfr",
            "-filter_threads", "2", "-threads", "6",
            "-c:v", "libx265", "-preset", "veryfast", "-crf", str(a.crf),
            "-x265-params", "log-level=error",
            "-pix_fmt", "yuv420p10le",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-c:a", "copy", "-movflags", "+faststart", os.path.abspath(dst)]

    print(f"[render] levels={a.levels} style={a.style}")
    if os.environ.get("LEVELS_DEBUG"):
        print("GRAPH://n" + graph)
        cmd[cmd.index("-loglevel") + 1] = "warning"
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        print("ffmpeg failed:\n" + r.stderr[-1500:])
        return 1

    # ---- verify
    def p_dur(p):
        return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                     "format=duration", "-of", "csv=p=0", p],
                                    capture_output=True, text=True).stdout.strip() or 0)
    afr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=avg_frame_rate", "-of", "csv=p=0", dst],
                         capture_output=True, text=True).stdout.strip()
    ok = abs(p_dur(a.clip) - p_dur(dst)) < 0.2 and afr == f"{a.fps}/1"
    print(f"[verify] dur {p_dur(dst):.2f}s  avg_fps {afr}  "
          f"{'OK' if ok else 'CHECK'}")
    print(f"[out] {dst}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
