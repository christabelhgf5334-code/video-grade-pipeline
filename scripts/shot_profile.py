#!/usr/bin/env python3
"""Per-clip metrics, CDL calculation and Resolve shot profiles."""

from __future__ import annotations

import statistics
from typing import Any

from recommend import recommend_looks


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _triplet(red: float, green: float, blue: float) -> str:
    return f"{red:.3f} {green:.3f} {blue:.3f}"


def calculate_cdl(metrics: dict[str, Any]) -> dict[str, str]:
    warm_cool = float(metrics.get("warm_cool", 0.0))
    p50 = float(metrics.get("luminance_p50", 0.32))
    p99 = float(metrics.get("luminance_p99", 0.90))
    mean_sat = float(metrics.get("mean_saturation", 0.30))
    skin = float(metrics.get("skin_fraction", 0.0))
    clips = float(metrics.get("highlight_clip_fraction", 0.0))

    balance = _clamp(warm_cool * 0.35, -0.08, 0.08)
    red_slope = _clamp(1.0 - balance, 0.90, 1.10)
    blue_slope = _clamp(1.0 + balance, 0.90, 1.10)
    green_slope = 1.0

    exposure = _clamp((0.32 - p50) * 0.15 - clips * 0.5, -0.04, 0.04)
    contrast = _clamp(0.92 + (p99 - p50) * 0.08, 0.92, 1.08)
    saturation = _clamp(1.0 + (0.30 - mean_sat) * 0.08 - skin * 0.05, 0.92, 1.08)

    return {
        "Slope": _triplet(red_slope, green_slope, blue_slope),
        "Offset": _triplet(exposure, exposure, exposure),
        "Power": _triplet(1.0 / contrast, 1.0 / contrast, 1.0 / contrast),
        "Saturation": f"{saturation:.3f}",
    }


def build_shot_profile(
    clip_id: str,
    source: dict[str, Any],
    frame_metrics: list[dict[str, Any]],
    scouting: dict[str, Any],
    looks: dict[str, Any],
) -> dict[str, Any]:
    if not frame_metrics:
        raise ValueError("frame_metrics must not be empty")
    numeric_keys = [
        key for key, value in frame_metrics[0].items() if isinstance(value, (int, float))
    ]
    metrics = {
        key: statistics.fmean(float(frame[key]) for frame in frame_metrics) for key in numeric_keys
    }
    luminance_values = [float(frame.get("luminance_p50", 0.0)) for frame in frame_metrics]
    spread = max(luminance_values) - min(luminance_values)
    recommended = (
        recommend_looks(
            metrics,
            scouting,
            looks,
            count=4,
            neutral_id=("documentary" if "documentary" in looks else "documentary_resolve"),
        )
        if looks
        else []
    )
    return {
        "clip_id": clip_id,
        "source": source,
        "scene": scouting.get("scene", []),
        "light": scouting.get("light", []),
        "risks": scouting.get("risks", []),
        "metrics": {key: round(value, 6) for key, value in metrics.items()},
        "cdl": calculate_cdl(metrics),
        "luminance_spread": round(spread, 6),
        "requires_scene_review": spread > 0.18,
        "recommended_looks": [item["id"] for item in recommended],
    }
