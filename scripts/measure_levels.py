#!/usr/bin/env python3
"""
Compare two graded frames for REAL subject separation, not for vignette darkening.

    python measure_levels.py BEFORE.png AFTER.png [--mask mask.png] [--band 0.30]

Why this exists: the first version of this measurement compared "subject region" against
"frame edges". A vignette darkens exactly the frame edges, so adding a vignette *looked*
like a 30% separation gain while the region grade itself did nothing. The number moved,
the picture did not get better.

Corrected model:
  subject   the mask area, shrunk inward
  ring      a band immediately around the subject — NOT the frame edge
  separation = luma(subject) - luma(ring)

  bg_detail  std of luma inside the ring. A press that removes background detail shows up
             here even when the mean separation looks good.
  corner/centre ratio  detects a vignette; if it drops a lot, part of any "separation"
             number is just edge darkening.
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image

LUM = np.array([0.2126, 0.7152, 0.0722])


def load(p):
    return np.asarray(Image.open(p).convert("RGB")).astype(np.float64) / 255.0


def regions(shape, mask_path, band):
    h, w = shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    if mask_path:
        m = np.asarray(Image.open(mask_path).convert("L")).astype(np.float64) / 255.0
        subj = m > 0.5
    else:
        subj = (yy > h * band) & (yy < h * 0.95)
    # ring = dilated subject minus subject, clipped away from the frame border
    grow = max(4, int(h * 0.04))
    near = np.zeros_like(subj)
    near[grow:, :] |= subj[:-grow, :]
    near[:-grow, :] |= subj[grow:, :]
    near[:, grow:] |= subj[:, :-grow]
    near[:, :-grow] |= subj[:, grow:]
    ring = near & ~subj
    border = int(h * 0.10)
    valid = np.zeros_like(subj)
    valid[border:h - border, border:w - border] = True
    return subj & valid, ring & valid


def stats(a, subj, ring):
    y = a @ LUM
    return {
        "subj_l": float(y[subj].mean()),
        "ring_l": float(y[ring].mean()),
        "sep": float(y[subj].mean() - y[ring].mean()),
        "bg_detail": float(y[ring].std()),
        "crush": float((y < 0.015).mean() * 100),
    }


def corner_ratio(a):
    h, w = a.shape[:2]
    y = a @ LUM
    c = y[h // 2 - h // 10:h // 2 + h // 10, w // 2 - w // 10:w // 2 + w // 10].mean()
    k = np.concatenate([y[:h // 12, :w // 12].ravel(), y[:h // 12, -w // 12:].ravel(),
                        y[-h // 12:, :w // 12].ravel(), y[-h // 12:, -w // 12:].ravel()]).mean()
    return float(k / max(c, 1e-6))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--mask", default=None)
    ap.add_argument("--band", type=float, default=0.30)
    a = ap.parse_args()

    A, B = load(a.before), load(a.after)
    if A.shape != B.shape:
        print("size mismatch"); return 2
    subj, ring = regions(A.shape, a.mask, a.band)
    if subj.sum() < 1000 or ring.sum() < 1000:
        print("regions too small — check the mask"); return 2

    sa, sb = stats(A, subj, ring), stats(B, subj, ring)
    ka, kb = corner_ratio(A), corner_ratio(B)

    print(f"\n{'':<14}{'主体L':>8}{'紧邻背景L':>10}{'分离':>8}{'背景细节σ':>10}{'死黑%':>7}{'角/中':>7}")
    print(f"{'前':<14}{sa['subj_l']:>8.3f}{sa['ring_l']:>10.3f}{sa['sep']:>+8.3f}"
          f"{sa['bg_detail']:>10.3f}{sa['crush']:>7.2f}{ka:>7.3f}")
    print(f"{'后':<14}{sb['subj_l']:>8.3f}{sb['ring_l']:>10.3f}{sb['sep']:>+8.3f}"
          f"{sb['bg_detail']:>10.3f}{sb['crush']:>7.2f}{kb:>7.3f}")

    d_sep = sb["sep"] - sa["sep"]
    d_det = sb["bg_detail"] - sa["bg_detail"]
    d_vig = kb - ka
    print()
    print(f"分离变化 {d_sep:+.3f}   背景细节变化 {d_det:+.3f}   角/中变化 {d_vig:+.3f}")
    verdict = []
    if d_sep > 0.02 and d_det > -0.02:
        verdict.append("分离真实提升，且背景细节没丢")
    elif d_sep > 0.02 and d_det <= -0.02:
        verdict.append("分离数字涨了，但背景细节在掉——是压出来的，不是分离出来的")
    else:
        verdict.append("分离没有实质提升")
    if d_vig < -0.10:
        verdict.append("暗角明显加重：角/中比值下降，分离数字里有一部分是边缘压暗")
    for v in verdict:
        print("判定:", v)


if __name__ == "__main__":
    sys.exit(main())
