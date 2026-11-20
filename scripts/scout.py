#!/usr/bin/env python3
"""Objective clip metrics and scouting-schema validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

SCENES = {
    "people",
    "portrait",
    "market",
    "food",
    "products",
    "indoor",
    "architecture",
    "urban",
    "nature",
    "document",
}
LIGHTS = {"daylight", "overcast", "mixed", "fluorescent", "night", "backlight"}
RISKS = {
    "skin",
    "signage_highlight",
    "neutral_white",
    "dominant_color",
    "clipping",
}


def _rounded(value: float) -> float:
    return round(float(value), 6)


def image_metrics(path: Path) -> dict[str, Any]:
    image = Image.open(path).convert("RGB")
    rgb = np.asarray(image, dtype=np.float32) / 255.0
    max_channel = rgb.max(axis=2)
    min_channel = rgb.min(axis=2)
    chroma = max_channel - min_channel
    saturation = chroma / np.maximum(max_channel, 1e-6)
    luminance = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

    delta = np.maximum(chroma, 1e-6)
    red = rgb[:, :, 0]
    green = rgb[:, :, 1]
    blue = rgb[:, :, 2]
    hue = np.zeros_like(max_channel)
    red_mask = max_channel == red
    green_mask = (~red_mask) & (max_channel == green)
    blue_mask = (~red_mask) & (~green_mask)
    hue[red_mask] = ((green - blue)[red_mask] / delta[red_mask]) % 6.0
    hue[green_mask] = (blue - red)[green_mask] / delta[green_mask] + 2.0
    hue[blue_mask] = (red - green)[blue_mask] / delta[blue_mask] + 4.0
    hue *= 60.0

    neutral_mask = (saturation < 0.12) & (luminance > 0.2) & (luminance < 0.95)
    skin_mask = (
        (hue >= 0.0)
        & (hue <= 50.0)
        & (saturation >= 0.10)
        & (saturation <= 0.75)
        & (luminance >= 0.18)
        & (luminance <= 0.95)
    )
    green_mask_vivid = (green > red * 1.06) & (green > blue * 1.06)
    blue_mask_vivid = (blue > red * 1.06) & (blue > green * 1.06)
    yellow_mask = (hue >= 40.0) & (hue <= 70.0) & (saturation > 0.2)
    red_mask_vivid = ((hue < 20.0) | (hue > 340.0)) & (saturation > 0.2)

    hue_histogram, _ = np.histogram(
        hue[saturation > 0.08],
        bins=np.linspace(0.0, 360.0, 13),
        weights=saturation[saturation > 0.08],
    )
    hue_total = float(hue_histogram.sum())
    hue_distribution = (
        [round(float(value / hue_total), 6) for value in hue_histogram] if hue_total else [0.0] * 12
    )
    p01, p50, p99 = np.percentile(luminance, [1, 50, 99])

    return {
        "mean_rgb": [_rounded(value) for value in rgb.mean(axis=(0, 1))],
        "luminance_p01": _rounded(p01),
        "luminance_p50": _rounded(p50),
        "luminance_p99": _rounded(p99),
        "mean_luminance": _rounded(luminance.mean()),
        "mean_saturation": _rounded(saturation.mean()),
        "warm_cool": _rounded(red.mean() - blue.mean()),
        "shadow_clip_fraction": _rounded((max_channel < 0.004).mean()),
        "highlight_clip_fraction": _rounded((min_channel > 0.996).mean()),
        "neutral_fraction": _rounded(neutral_mask.mean()),
        "skin_fraction": _rounded(skin_mask.mean()),
        "green_fraction": _rounded(green_mask_vivid.mean()),
        "blue_fraction": _rounded(blue_mask_vivid.mean()),
        "yellow_fraction": _rounded(yellow_mask.mean()),
        "red_fraction": _rounded(red_mask_vivid.mean()),
        "hue_distribution": hue_distribution,
    }


def aggregate_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        raise ValueError("at least one frame metric is required")
    scalar_keys = [key for key, value in items[0].items() if isinstance(value, (int, float))]
    result: dict[str, Any] = {
        key: _rounded(np.mean([float(item[key]) for item in items])) for key in scalar_keys
    }
    result["frame_count"] = len(items)
    return result


def scouting_template(clips: dict[str, dict[str, Any]]) -> dict[str, Any]:
    template: dict[str, Any] = {}
    for clip_id, clip in clips.items():
        if clip.get("status") == "scanned":
            template[clip_id] = {
                "scene": [],
                "light": [],
                "risks": [],
                "notes": "",
            }
    return template


def validate_scouting(payload: dict[str, Any], clip_ids: set[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("scouting JSON must be an object")
    unknown_clips = set(payload) - clip_ids
    if unknown_clips:
        raise ValueError(f"unknown clip IDs: {sorted(unknown_clips)}")
    missing_clips = clip_ids - set(payload)
    if missing_clips:
        raise ValueError(f"missing clip IDs: {sorted(missing_clips)}")

    normalized: dict[str, Any] = {}
    for clip_id, entry in payload.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{clip_id}: scouting entry must be an object")
        scene = entry.get("scene", [])
        light = entry.get("light", [])
        risks = entry.get("risks", [])
        for field, values, allowed in (
            ("scene", scene, SCENES),
            ("light", light, LIGHTS),
            ("risks", risks, RISKS),
        ):
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise ValueError(f"{clip_id}: {field} must be a string list")
            unknown = set(values) - allowed
            if unknown:
                raise ValueError(f"{clip_id}: unknown {field} tags {sorted(unknown)}")
        notes = entry.get("notes", "")
        if not isinstance(notes, str):
            raise ValueError(f"{clip_id}: notes must be a string")
        normalized[clip_id] = {
            "scene": sorted(set(scene)),
            "light": sorted(set(light)),
            "risks": sorted(set(risks)),
            "notes": notes.strip(),
        }
    return normalized
