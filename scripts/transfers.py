#!/usr/bin/env python3
"""
Input transfer functions: encoded value -> linear scene light.

Every camera bakes its footage with a different curve. "灰片" (flat footage) can be a true
Log curve, an HDR curve read as SDR, or a flat picture profile. Metadata on phones is often
`transfer=unknown`, so the only reliable way is to decode with each candidate and see which
one produces a physically plausible image.

All decoders accept a normalised 0..1 encoded array and return linear 0..1+.
All encoders are the inverse, gamma-encoded for Rec.709 display.
"""
import numpy as np

EPS = 1e-9
LUM = np.array([0.2126, 0.7152, 0.0722])


# ---------------------------------------------------------------- display
def rec709_encode(lin):
    lin = np.clip(lin, 0.0, None)
    return np.where(lin < 0.018, lin * 4.5, 1.099 * np.power(lin, 1 / 2.4) - 0.099)


def rec709_decode(v):
    v = np.clip(v, 0.0, 1.0)
    return np.where(v < 0.081, v / 4.5, np.power((v + 0.099) / 1.099, 2.4))


def srgb_decode(v):
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.04045, v / 12.92, np.power((v + 0.055) / 1.055, 2.4))


def gamma_decode(v, g):
    return np.power(np.clip(v, 0.0, 1.0), g)


# ---------------------------------------------------------------- HDR
def pq_decode(v, peak_nits=10000.0, ref_nits=100.0):
    """SMPTE ST 2084 EOTF. Returns light normalised so that ref_nits -> 1.0."""
    m1, m2 = 2610 / 16384, 2523 / 4096 * 128
    c1, c2, c3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32
    v = np.clip(v, 0.0, 1.0)
    p = np.power(v, 1 / m2)
    num = np.maximum(p - c1, 0.0)
    den = np.maximum(c2 - c3 * p, EPS)
    y = np.power(num / den, 1 / m1)          # 0..1 of peak_nits
    return y * (peak_nits / ref_nits)


def hlg_decode(v, system_gamma=1.2):
    """ITU-R BT.2100 HLG, scene light, with the display OOTF folded in."""
    a, b, c = 0.17883277, 1 - 4 * 0.17883277, 0.5 - 0.17883277 * np.log(4 * 0.17883277)
    v = np.clip(v, 0.0, 1.0)
    scene = np.where(v <= 0.5, v * v / 3.0, (np.exp((v - c) / a) + b) / 12.0)
    return np.power(np.maximum(scene, 0.0), system_gamma)


# ---------------------------------------------------------------- camera log
def slog3_decode(v):
    """Sony S-Log3 (IRE 0..100 mapped to 0..1)."""
    t = np.clip(v, 0.0, 1.0) * 1023.0
    hi = 10 ** ((t - 420.0) / 261.5) * 0.19 - 0.01
    lo = (t - 95.0) * 0.01125 / (171.2102946929 - 95.0)
    return np.where(t >= 171.2102946929, hi, lo)


def vlog_decode(v):
    """Panasonic V-Log."""
    cut1 = 0.01
    b, c, d = 0.00873, 0.241514, 0.598206
    v = np.clip(v, 0.0, 1.0)
    hi = (np.power(10.0, (v - d) / c) - b) / (1.0 - b)
    lo = (v - 0.125) / 5.6 + cut1
    return np.where(v < 0.181, lo, hi)


def logc3_decode(v):
    """ARRI LogC3 (EI 800)."""
    cut, a, b, c, d, e, f = 0.010591, 5.555556, 0.052272, 0.247190, 0.385537, 5.367655, 0.092809
    v = np.clip(v, 0.0, 1.0)
    hi = (np.power(10.0, (v - d) / c) - b) / a
    lo = (v - f) / e
    return np.where(v > e * cut + f, hi, lo)


def dlog_decode(v):
    """DJI D-Log (approximation of the published curve)."""
    a, b, c, d, e = 0.584555, 0.010591, 5.555556, 0.052272, 0.247190
    v = np.clip(v, 0.0, 1.0)
    return np.where(v >= 0.149658, np.power(10.0, (v - 0.385537) / 0.247190) * 0.117779 - 0.052272,
                    (v - 0.092809) / 5.367655)


def generic_log_decode(v, punch=6.0, centre=0.42):
    """Unknown flat curve: exponential expansion anchored at a mid-grey assumption."""
    v = np.clip(v, 0.0, 1.0)
    return (np.power(2.0, (v - centre) * punch) - np.power(2.0, -centre * punch)) \
        / (1.0 - np.power(2.0, -centre * punch))


# ---------------------------------------------------------------- registry
CANDIDATES = {
    "rec709":  ("Rec.709 / 已经是 SDR", lambda v: rec709_decode(v)),
    "srgb":    ("sRGB", lambda v: srgb_decode(v)),
    "g22":     ("Gamma 2.2", lambda v: gamma_decode(v, 2.2)),
    "pq":      ("PQ / HDR10", lambda v: pq_decode(v) / 20.0),
    "hlg":     ("HLG", lambda v: hlg_decode(v)),
    "slog3":   ("Sony S-Log3", lambda v: slog3_decode(v)),
    "vlog":    ("Panasonic V-Log", lambda v: vlog_decode(v)),
    "logc3":   ("ARRI LogC3", lambda v: logc3_decode(v)),
    "dlog":    ("DJI D-Log", lambda v: dlog_decode(v)),
    "genlog":  ("未知 Log（通用展开）", lambda v: generic_log_decode(v)),
}


def _hable(x):
    a, b, c, d, e, f = 0.15, 0.50, 0.10, 0.20, 0.02, 0.30
    num = x * (a * x + c * b) + d * e
    den = x * (a * x + b) + d * f
    return num / np.maximum(den, EPS) - e / f


def filmic_tonemap(lin, exposure=1.0, white=1.0):
    """Hable-style filmic curve, normalised so scene white lands on display white.

    Without the /_hable(1.0) normalisation the curve crushes everything: 0.18 linear
    comes out at 0.012 instead of ~0.22.
    """
    x = np.clip(lin * exposure, 0.0, None) / white
    return np.clip(_hable(x) / _hable(1.0), 0.0, None)


def to_display(lin, exposure=1.0):
    """linear scene -> Rec.709 gamma-encoded 0..1."""
    t = filmic_tonemap(lin, exposure)
    return np.clip(rec709_encode(t), 0.0, 1.0)


def expose_to_mid(lin, target=0.42):
    """Auto exposure: put the median luminance near a pleasant mid tone."""
    y = np.maximum(lin @ LUM, EPS)
    med = float(np.median(y))
    if med <= 0:
        return lin, 1.0
    gain = 0.18 * (target / 0.42) / med
    return lin * gain, gain
