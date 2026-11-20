#!/usr/bin/env python3
"""Build the six runtime Xiaomi 15 Ultra look LUTs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

SKILL_DIR = Path(__file__).resolve().parent.parent
LOOKS_PATH = SKILL_DIR / "assets" / "looks.json"
OUTPUT_DIR = SKILL_DIR / "assets" / "luts" / "generated"


def load_look_library() -> dict[str, Any]:
    return json.loads(LOOKS_PATH.read_text(encoding="utf-8"))


def load_look_definitions() -> dict[str, dict[str, Any]]:
    return load_look_library()["looks"]


def read_cube(path: Path) -> tuple[int, list[list[float]]]:
    size: int | None = None
    rows: list[list[float]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "TITLE")):
            continue
        if stripped.startswith("DOMAIN_"):
            continue
        if stripped.startswith("LUT_3D_SIZE"):
            size = int(stripped.split()[-1])
            continue
        parts = stripped.split()
        if len(parts) != 3:
            raise ValueError(f"Invalid LUT row in {path}: {line}")
        rows.append([float(value) for value in parts])
    if size is None:
        raise ValueError(f"LUT size is missing: {path}")
    if len(rows) != size**3:
        raise ValueError(f"Expected {size**3} rows in {path}, found {len(rows)}")
    return size, rows


def apply_ops(rgb: np.ndarray, ops: dict[str, Any]) -> np.ndarray:
    result = np.clip(np.asarray(rgb, dtype=np.float64), 0.0, 1.0).copy()

    temperature = float(ops.get("temperature", 0.0))
    result[:, 0] *= 1.0 + temperature
    result[:, 2] *= 1.0 - temperature

    result += float(ops.get("brightness", 0.0))
    result = (result - 0.5) * float(ops.get("contrast", 1.0)) + 0.5

    weights = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)
    luma = result @ weights
    result = luma[:, None] + (result - luma[:, None]) * float(ops.get("saturation", 1.0))

    shadow_tint = np.asarray(ops.get("shadow_tint", [0.0, 0.0, 0.0]), dtype=np.float64)
    if shadow_tint.shape != (3,):
        raise ValueError("shadow_tint must contain three channels")
    shadow_weight = np.clip(1.0 - luma * 2.0, 0.0, 1.0)
    result += shadow_tint[None, :] * shadow_weight[:, None]

    rolloff = float(ops.get("highlight_rolloff", 0.0))
    if rolloff > 0:
        knee = 0.78
        compressed_luma = np.where(
            luma > knee,
            knee + (1.0 - knee) * np.tanh((luma - knee) / (1.0 - knee)),
            luma,
        )
        scale = np.divide(
            compressed_luma,
            luma,
            out=np.ones_like(luma),
            where=luma > 1e-8,
        )
        rolled = result * scale[:, None]
        result = result * (1.0 - rolloff) + rolled * rolloff

    return np.clip(result, 0.0, 1.0)


def write_cube(path: Path, title: str, rgb: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f'TITLE "{title}"\n')
        handle.write("LUT_3D_SIZE 33\n")
        for red, green, blue in np.clip(rgb, 0.0, 1.0):
            handle.write(f"{red:.6f} {green:.6f} {blue:.6f}\n")
    temp.replace(path)


def identity_grid(size: int = 33) -> np.ndarray:
    values = np.linspace(0.0, 1.0, size, dtype=np.float64)
    rows = [[red, green, blue] for blue in values for green in values for red in values]
    return np.asarray(rows, dtype=np.float64)


def _smoothstep(edge0: float, edge1: float, value: np.ndarray) -> np.ndarray:
    x = np.clip((value - edge0) / (edge1 - edge0), 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def build_pipeline_luts(force: bool = False) -> dict[str, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    identity = identity_grid(33)
    outputs: dict[str, Path] = {}

    definitions = {
        "tone_map": {
            "title": "Xiaomi 15 Ultra Tone Map",
            "ops": {
                "brightness": 0.002,
                "contrast": 1.035,
                "highlight_rolloff": 0.04,
            },
        },
        "output_trim": {
            "title": "Xiaomi 15 Ultra Output Trim",
            "ops": {
                "contrast": 1.005,
                "saturation": 0.995,
                "highlight_rolloff": 0.015,
            },
        },
    }
    for key, definition in definitions.items():
        path = OUTPUT_DIR / f"{key}.cube"
        if force or not path.exists():
            write_cube(path, definition["title"], apply_ops(identity, definition["ops"]))
        outputs[key] = path

    skin_path = OUTPUT_DIR / "skin_protect.cube"
    if force or not skin_path.exists():
        rgb = identity.copy()
        max_channel = rgb.max(axis=1)
        min_channel = rgb.min(axis=1)
        delta = np.maximum(max_channel - min_channel, 1e-8)
        sat = delta / np.maximum(max_channel, 1e-8)
        red, green, blue = rgb[:, 0], rgb[:, 1], rgb[:, 2]
        hue = np.zeros_like(max_channel)
        red_mask = max_channel == red
        green_mask = (~red_mask) & (max_channel == green)
        blue_mask = (~red_mask) & (~green_mask)
        hue[red_mask] = ((green - blue)[red_mask] / delta[red_mask]) % 6.0
        hue[green_mask] = (blue - red)[green_mask] / delta[green_mask] + 2.0
        hue[blue_mask] = (red - green)[blue_mask] / delta[blue_mask] + 4.0
        hue *= 60.0
        luma = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)
        hue_distance = np.abs((hue - 30.0 + 180.0) % 360.0 - 180.0)
        hue_mask = np.exp(-0.5 * (hue_distance / 16.0) ** 2)
        sat_mask = _smoothstep(0.08, 0.22, sat) * (1.0 - _smoothstep(0.68, 0.82, sat))
        luma_mask = _smoothstep(0.12, 0.25, luma) * (1.0 - _smoothstep(0.88, 0.98, luma))
        protect = np.clip(hue_mask * sat_mask * luma_mask, 0.0, 1.0)
        factor = 1.0 - 0.05 * protect
        protected = luma[:, None] + (rgb - luma[:, None]) * factor[:, None]
        write_cube(skin_path, "Xiaomi 15 Ultra Skin Protect", protected)
    outputs["skin_protect"] = skin_path
    return outputs


def build_all(force: bool = False) -> dict[str, Path]:
    outputs: dict[str, Path] = {}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for look_id, definition in load_look_definitions().items():
        output_path = OUTPUT_DIR / f"{look_id}.cube"
        if output_path.exists() and not force:
            outputs[look_id] = output_path
            continue
        base_path = SKILL_DIR / definition["base_lut"]
        size, rows = read_cube(base_path)
        if size != 33:
            raise ValueError(f"Expected a 33^3 base LUT, found {size}: {base_path}")
        rgb = apply_ops(np.asarray(rows, dtype=np.float64), definition.get("ops", {}))
        write_cube(output_path, definition["name"], rgb)
        outputs[look_id] = output_path
    return outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build runtime Xiaomi 15 Ultra look LUTs.")
    parser.add_argument("--force", action="store_true", help="Rebuild existing LUTs")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    outputs = build_all(force=args.force)
    outputs.update(build_pipeline_luts(force=args.force))
    for look_id, path in outputs.items():
        print(f"{look_id}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
