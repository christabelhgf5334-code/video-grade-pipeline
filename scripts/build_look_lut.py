#!/usr/bin/env python3
"""
Build a portable .cube look: corrective base + creative chain (ffmpeg) -> secondary
qualifiers -> filmic finish. Every stage is a per-pixel point operation, so the result
bakes exactly and behaves identically inside Resolve / CapCut once imported as a LUT.

    python build_look_lut.py spec.json --out look.cube [--size 65] [--emit-stages DIR]

spec.json
{
  "primary_chain": "colorbalance=rm=0.034:bm=-0.030,eq=contrast=1.09:saturation=1.10",
  "size": 65,
  "secondaries": [
    {"name": "cool_bg", "hue": 205, "hue_full": 22, "hue_zero": 40,
     "d_hue": 4.0, "sat_mul": -0.40, "lum_mul": -0.15}
  ],
  "filmic": {
    "curve": 0.20, "shoulder": 0.032, "toe": 0.004, "hi_desat": 0.26,
    "global_sat": 0.90, "sat_exempt_hue": {"centre": 34, "full": 20, "zero": 38},
    "skin_lock": {"centre": 33.5, "full": 10, "zero": 19, "pull": 0.40},
    "split": {"warm": [0.011, 0.003, -0.009], "cool": [-0.007, -0.002, 0.012]}
  }
}

`primary_chain` must contain ONLY per-pixel filters (colorbalance/eq/curves/vibrance/
colorchannelmixer). Do NOT put zscale, gblur, blend, unsharp or any spatial filter here —
it will not bake and the LUT will silently diverge from the video.
"""
import argparse
import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image


# ----------------------------------------------------------------- colour helpers
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


def hsl2rgb(h, s, l):
    c = (1.0 - np.abs(2.0 * l - 1.0)) * s
    hp = (h % 360.0) / 60.0
    x = c * (1.0 - np.abs(hp % 2.0 - 1.0))
    z = np.zeros_like(c)
    cond = [hp < 1, hp < 2, hp < 3, hp < 4, hp < 5, hp >= 5]
    r = np.select(cond, [c, x, z, z, x, c])
    g = np.select(cond, [x, c, c, x, z, z])
    b = np.select(cond, [z, z, x, c, c, x])
    m = l - c * 0.5
    return np.clip(np.stack([r + m, g + m, b + m], -1), 0.0, 1.0)


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def hue_band(h, centre, full, zero):
    """1.0 within +/-full degrees of centre, 0.0 at +/-zero (wraps at 360)."""
    d = np.abs((h - centre + 180.0) % 360.0 - 180.0)
    return 1.0 - smoothstep(full, zero, d)


# ----------------------------------------------------------------- stages
def apply_secondaries(h, s, l, specs):
    """Hue-qualified HSL adjustments. Soft (smoothstep) edges only — hard thresholds
    leave visible contour rings around the selected colour range."""
    h2, s2, l2 = h.copy(), s.copy(), l.copy()
    for sp in specs or []:
        if sp.get("hue") is None:
            # no hue qualifier: a pure luma/saturation gate across all hues
            # (e.g. "lift the top end", "deepen the darkest zone")
            w = np.ones_like(h)
        else:
            w = hue_band(h, sp["hue"], sp.get("hue_full", 12), sp.get("hue_zero", 24))
        if sp.get("sat_min") is not None:
            w = w * smoothstep(sp["sat_min"], sp.get("sat_knee", sp["sat_min"] + 0.12), s)
        if sp.get("sat_max") is not None:
            w = w * (1.0 - smoothstep(sp.get("sat_knee_max", sp["sat_max"] - 0.12),
                                      sp["sat_max"], s))
        if sp.get("lum_min") is not None:
            w = w * smoothstep(sp["lum_min"], sp.get("lum_knee", sp["lum_min"] + 0.10), l)
        if sp.get("lum_max") is not None:
            w = w * (1.0 - smoothstep(sp.get("lum_knee_max", sp["lum_max"] - 0.10),
                                      sp["lum_max"], l))
        h2 = h2 + sp.get("d_hue", 0.0) * w
        s2 = s2 * (1.0 + sp.get("sat_mul", 0.0) * w)
        l2 = l2 * (1.0 + sp.get("lum_mul", 0.0) * w)
    return h2, np.clip(s2, 0.0, 1.0), np.clip(l2, 0.0, 1.0)


def apply_filmic(rgb, f):
    """Film-style finish: soft S-curve with a rolled shoulder, highlight desaturation,
    restrained palette with the subject exempted, skin-hue lock, split tone."""
    if not f:
        return rgb
    h, s, l = rgb2hsl(rgb)

    curve = f.get("curve", 0.0)
    if curve:
        sc = smoothstep(0.045, 0.955, l)
        l = l + curve * (sc - l)
    l = l - f.get("shoulder", 0.0) * smoothstep(0.82, 1.00, l)
    l = l + f.get("toe", 0.0) * (1.0 - smoothstep(0.00, 0.22, l))
    l = np.clip(l, 0.0, 1.0)

    s = s * (1.0 - f.get("hi_desat", 0.0) * smoothstep(0.72, 1.00, l))

    gs = f.get("global_sat")
    if gs is not None and gs < 1.0:
        ex = f.get("sat_exempt_hue")
        if ex:
            # only trim the hues the subject does NOT own -> 整体克制 / 主体浓
            w = hue_band(h, ex["centre"], ex.get("full", 20), ex.get("zero", 38))
            s = s * (1.0 - (1.0 - gs) * (1.0 - w))
        else:
            s = s * gs

    sk = f.get("skin_lock")
    if sk:
        w = (hue_band(h, sk["centre"], sk.get("full", 10), sk.get("zero", 19))
             * smoothstep(0.16, 0.28, s)
             * (1.0 - smoothstep(0.58, 0.74, s)))
        delta = ((sk["centre"] - h + 180.0) % 360.0) - 180.0
        h = h + sk.get("pull", 0.4) * w * delta

    out = hsl2rgb(h, np.clip(s, 0.0, 1.0), l)

    split = f.get("split")
    if split:
        warm = smoothstep(0.55, 0.98, l)[..., None]
        cool = (1.0 - smoothstep(0.08, 0.42, l))[..., None]
        out = out + warm * np.array(split.get("warm", [0, 0, 0])) \
                  + cool * np.array(split.get("cool", [0, 0, 0]))
    return np.clip(out, 0.0, 1.0)


# ----------------------------------------------------------------- cube io
def write_cube(path, size, arr, title):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f'TITLE "{title}"\nLUT_3D_SIZE {size}\n')
        fh.write("DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n")
        for r, g, b in arr:
            fh.write(f"{r:.6f} {g:.6f} {b:.6f}\n")


def identity_grid(size):
    idx = np.arange(size ** 3)
    g = np.stack([idx % size, (idx // size) % size, idx // (size * size)], 1)
    return g.astype(np.float64) / (size - 1)


def bake_primary(chain, size, workdir):
    """Push the identity grid through the ffmpeg chain to capture the exact pixel mapping."""
    os.makedirs(workdir, exist_ok=True)
    grid = identity_grid(size)
    img = (grid * 255).round().astype(np.uint8).reshape(size, size * size, 3)
    src = os.path.join(workdir, "_grid_id.png")
    dst = os.path.join(workdir, "_grid_out.png")
    Image.fromarray(img, "RGB").save(src)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", src, "-vf", chain, dst], check=True)
    out = np.asarray(Image.open(dst).convert("RGB")).reshape(size ** 3, 3)
    return out.astype(np.float64) / 255.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", type=int, default=None)
    ap.add_argument("--emit-stages", default=None,
                    help="directory to also write primary-only / +secondary LUTs")
    ap.add_argument("--workdir", default=".lutwork")
    a = ap.parse_args()

    spec = json.load(open(a.spec, encoding="utf-8"))
    size = a.size or int(spec.get("size", 65))
    chain = spec.get("primary_chain", "null")

    primary = bake_primary(chain, size, a.workdir)
    rgb = primary

    h, s, l = rgb2hsl(rgb)
    h2, s2, l2 = apply_secondaries(h, s, l, spec.get("secondaries"))
    with_sec = hsl2rgb(h2, s2, l2)
    final = apply_filmic(with_sec, spec.get("filmic"))

    def report(name, arr):
        d = np.abs(arr - primary) * 255
        print(f"  {name:<16} delta vs primary: mean={d.mean():5.2f}  max={d.max():5.0f}")

    print(f"LUT {size}^3 = {size ** 3} nodes")
    report("+secondaries", with_sec)
    report("+filmic (final)", final)

    write_cube(a.out, size, final, os.path.basename(a.out))
    print(f"wrote {a.out}")

    if a.emit_stages:
        os.makedirs(a.emit_stages, exist_ok=True)
        write_cube(os.path.join(a.emit_stages, "stage1-primary.cube"), size, primary, "primary")
        write_cube(os.path.join(a.emit_stages, "stage2-primary+secondary.cube"), size,
                   with_sec, "primary+secondary")
        print(f"  stage LUTs -> {a.emit_stages}")


if __name__ == "__main__":
    sys.exit(main())
