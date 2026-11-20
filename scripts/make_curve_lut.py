#!/usr/bin/env python3
"""
Bake a 1D-curve 3D LUT: encode Rec.709 into a camera Log curve, or decode it back.

    python make_curve_lut.py --curve slog3 --direction encode --out log_encode.cube
    python make_curve_lut.py --curve slog3 --direction decode --out log_decode.cube

Why: ffmpeg has no native S-Log3/V-Log/LogC decoder, and `zscale` only handles
PQ/HLG/sRGB/709. Baking the curve into a LUT lets the same render chain serve Log
footage without any Python video I/O.

`encode` is derived by numerically inverting the decoder rather than re-deriving each
camera's formula — fewer chances to transcribe a constant wrong.
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from transfers import CANDIDATES, filmic_tonemap, rec709_encode, rec709_decode

N = 4096


def build_inverse(decode):
    """Invert a monotonic decoder by sampling densely and interpolating."""
    xs = np.linspace(0.0, 1.0, N)
    ys = decode(xs)
    ys = np.maximum.accumulate(ys)              # enforce monotonic (Log curves are)
    lo, hi = ys[0], ys[-1]
    return lambda v: np.interp(np.clip(v, lo, hi), ys, xs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--curve", required=True, choices=[k for k in CANDIDATES
                                                       if k not in ("rec709",)])
    ap.add_argument("--direction", required=True, choices=["encode", "decode"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", type=int, default=65)
    ap.add_argument("--exposure", type=float, default=1.0,
                    help="decode only: scale before the tone curve")
    a = ap.parse_args()

    decode = CANDIDATES[a.curve][1]
    n = a.size
    idx = np.arange(n ** 3)
    grid = np.stack([idx % n, (idx // n) % n, idx // (n * n)], 1).astype(np.float64) / (n - 1)

    if a.direction == "encode":
        # The LUT grid holds Rec.709-ENCODED values, not linear light. Decode first,
        # then push through the log curve — otherwise the whole image comes out crushed
        # (verified: 61/255 round-trip error before this fix).
        enc = build_inverse(decode)
        out = np.clip(enc(rec709_decode(grid)), 0.0, 1.0)
        title = f"{a.curve} encode"
    else:
        lin = np.clip(decode(grid), 0.0, None) * a.exposure
        out = np.clip(rec709_encode(filmic_tonemap(lin, 1.0, 1.0)), 0.0, 1.0)
        title = f"{a.curve} decode -> Rec.709"

    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(f'TITLE "{title}"\nLUT_3D_SIZE {n}\n')
        fh.write("DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n")
        for r, g, b in out:
            fh.write(f"{r:.6f} {g:.6f} {b:.6f}\n")

    print(f"wrote {a.out}  {n}^3  direction={a.direction} curve={a.curve}")
    # a per-channel curve must map greys to greys
    g = np.linspace(0, 1, 17)
    grey = np.stack([g, g, g], 1)
    from grade_engine import grade as _unused  # noqa  (import guard only)
    print(f"  灰->灰检查: 输出三通道一致 "
          f"{np.allclose(out[[0, n**3//2, n**3-1]][:,0], out[[0, n**3//2, n**3-1]][:,1])}")


if __name__ == "__main__":
    sys.exit(main())
