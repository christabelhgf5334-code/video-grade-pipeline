#!/usr/bin/env python3
"""Preview and grade Xiaomi 15 Ultra Log video with the bundled LUTs."""

from __future__ import annotations

import hashlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from runtime_config import timestamped_output_dir, tool_path

SKILL_DIR = Path(__file__).resolve().parent.parent
PRESETS_PATH = SKILL_DIR / "assets" / "presets.json"


class GradeError(RuntimeError):
    pass


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def require_tool(name: str) -> str:
    try:
        return tool_path(name)
    except FileNotFoundError as exc:
        raise GradeError(str(exc)) from exc


def run_capture(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def run_checked(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = run_capture(cmd, cwd=cwd)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise GradeError(f"Command failed ({result.returncode}): {' '.join(cmd)}\n{detail}")
    return result


def load_presets() -> dict[str, dict[str, Any]]:
    if not PRESETS_PATH.exists():
        raise GradeError(f"Preset file missing: {PRESETS_PATH}")
    presets = json.loads(PRESETS_PATH.read_text(encoding="utf-8"))
    for key, preset in presets.items():
        lut_path = SKILL_DIR / preset["lut"]
        if not lut_path.exists():
            raise GradeError(f"LUT for {key} is missing: {lut_path}")
        preset["lut_path"] = lut_path
    return presets


def ffprobe_json(path: Path) -> dict[str, Any]:
    ffprobe = require_tool("ffprobe")
    result = run_checked(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
    )
    return json.loads(result.stdout)


def first_stream(probe: dict[str, Any], codec_type: str) -> dict[str, Any] | None:
    return next(
        (stream for stream in probe.get("streams", []) if stream.get("codec_type") == codec_type),
        None,
    )


def stream_duration(stream: dict[str, Any], probe: dict[str, Any]) -> float:
    for value in (stream.get("duration"), probe.get("format", {}).get("duration")):
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            return duration
    raise GradeError("Could not determine video duration")


def rotation_degrees(stream: dict[str, Any]) -> int:
    rotate_tag = stream.get("tags", {}).get("rotate")
    if rotate_tag not in (None, ""):
        try:
            return int(float(rotate_tag)) % 360
        except ValueError:
            pass
    for side_data in stream.get("side_data_list", []) or []:
        if "rotation" in side_data:
            try:
                return int(float(side_data["rotation"])) % 360
            except (TypeError, ValueError):
                pass
    return 0


def fraction_value(value: str | None) -> float | None:
    if not value or value in ("0/0", "N/A"):
        return None
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None


def source_summary(path: Path, probe: dict[str, Any]) -> dict[str, Any]:
    video = first_stream(probe, "video")
    if not video:
        raise GradeError(f"No video stream found: {path}")
    audio = first_stream(probe, "audio")
    rotation = rotation_degrees(video)
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    display_width, display_height = (height, width) if rotation in (90, 270) else (width, height)
    avg_fps = fraction_value(video.get("avg_frame_rate"))
    nominal_fps = fraction_value(video.get("r_frame_rate"))
    duration = stream_duration(video, probe)
    format_name = probe.get("format", {}).get("format_name")
    pix_fmt = video.get("pix_fmt")
    primaries = video.get("color_primaries")
    matrix = video.get("color_space")
    transfer = video.get("color_transfer")
    color_range = video.get("color_range")
    signals = {
        "ten_bit": bool(pix_fmt and "10" in pix_fmt),
        "hevc": video.get("codec_name") == "hevc",
        "bt2020_primaries": primaries == "bt2020",
        "bt2020_matrix": matrix in ("bt2020nc", "bt2020c"),
        "full_range": color_range == "pc",
        "transfer_unset": transfer in (None, "", "unknown"),
        "ultra_hd": width * height >= 7_000_000,
    }
    return {
        "path": str(path.resolve()),
        "filename": path.name,
        "format_name": format_name,
        "duration_sec": round(duration, 6),
        "rotation_degrees": rotation,
        "encoded_width": width,
        "encoded_height": height,
        "display_width": display_width,
        "display_height": display_height,
        "codec_name": video.get("codec_name"),
        "profile": video.get("profile"),
        "pix_fmt": pix_fmt,
        "color_range": color_range,
        "color_space": matrix,
        "color_transfer": transfer,
        "color_primaries": primaries,
        "avg_frame_rate": avg_fps,
        "nominal_frame_rate": nominal_fps,
        "nb_frames": int(video["nb_frames"]) if str(video.get("nb_frames", "")).isdigit() else None,
        "audio": {
            "present": audio is not None,
            "codec_name": audio.get("codec_name") if audio else None,
            "sample_rate": audio.get("sample_rate") if audio else None,
            "channels": audio.get("channels") if audio else None,
        },
        "xiaomi_log_signals": signals,
        "matches_tested_profile": all(signals.values()),
    }


def sample_times(duration: float) -> list[float]:
    if duration < 1.0:
        ratios = (0.05, 0.5, 0.9)
    else:
        ratios = (0.08, 0.5, 0.86)
    return [max(0.0, min(duration - 0.03, duration * ratio)) for ratio in ratios]


def extract_preview(
    ffmpeg: str,
    source: Path,
    seconds: float,
    output: Path,
    lut_path: Path | None,
    max_edge: int = 1280,
) -> None:
    resize = (
        f"scale={max_edge}:{max_edge}:force_original_aspect_ratio=decrease:"
        "flags=lanczos,format=rgb24"
    )
    if lut_path:
        vf = f"lut3d={lut_path.name}:interp=tetrahedral,{resize}"
        cwd = lut_path.parent
    else:
        vf = resize
        cwd = None
    run_checked(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{seconds:.3f}",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            vf,
            str(output),
        ],
        cwd=cwd,
    )


def load_font(size: int) -> ImageFont.ImageFont:
    candidates = (
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def fit_on_tile(image_path: Path, size: tuple[int, int]) -> Image.Image:
    image = Image.open(image_path).convert("RGB")
    fitted = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
    tile = Image.new("RGB", size, (16, 16, 16))
    tile.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
    return tile


def build_contact_sheet(
    frames_by_row: dict[str, list[Path]],
    labels: dict[str, str],
    times: list[float],
    output: Path,
    tile_size: tuple[int, int] = (480, 300),
) -> None:
    columns = len(times)
    rows = len(frames_by_row)
    tile_w, tile_h = tile_size
    gap = 8
    label_h = 26
    sheet = Image.new(
        "RGB",
        (columns * tile_w + (columns + 1) * gap, rows * tile_h + (rows + 1) * gap),
        (8, 8, 8),
    )
    font = load_font(17)
    for row, (key, paths) in enumerate(frames_by_row.items()):
        for column, (path, seconds) in enumerate(zip(paths, times)):
            x = gap + column * (tile_w + gap)
            y = gap + row * (tile_h + gap)
            tile = Image.new("RGB", (tile_w, tile_h), (20, 20, 20))
            draw_tile = ImageDraw.Draw(tile)
            draw_tile.rectangle((0, 0, tile_w, label_h), fill=(34, 34, 34))
            label = f"{labels.get(key, key)}  {seconds:.2f}s"
            draw_tile.text((8, 4), label, fill=(242, 242, 242), font=font)
            preview = fit_on_tile(path, (tile_w, tile_h - label_h))
            tile.paste(preview, (0, label_h))
            sheet.paste(tile, (x, y))
    sheet.save(output, quality=92, subsampling=1)


def image_stats(path: Path) -> dict[str, Any]:
    array = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32) / 255.0
    luminance = 0.2126 * array[:, :, 0] + 0.7152 * array[:, :, 1] + 0.0722 * array[:, :, 2]
    return {
        "mean_rgb": [round(float(value), 6) for value in array.mean(axis=(0, 1))],
        "luminance_p01": round(float(np.percentile(luminance, 1)), 6),
        "luminance_p50": round(float(np.percentile(luminance, 50)), 6),
        "luminance_p99": round(float(np.percentile(luminance, 99)), 6),
        "shadow_clip_fraction": round(float((array.max(axis=2) < 0.004).mean()), 8),
        "highlight_clip_fraction": round(float((array.min(axis=2) > 0.996).mean()), 8),
    }


def target_fps(summary: dict[str, Any]) -> str:
    rate = summary.get("avg_frame_rate")
    if rate and 58.0 <= rate <= 61.0:
        return "60000/1001"
    if rate and 23.0 <= rate <= 25.0:
        return "24000/1001"
    if rate and 29.0 <= rate <= 31.0:
        return "30000/1001"
    if rate:
        return f"{rate:.5f}"
    return "60000/1001"


def render_video(
    ffmpeg: str,
    input_path: Path,
    output_path: Path,
    lut_path: Path,
    crf: int,
    preset: str,
    fps_value: str | None,
) -> None:
    filters = [
        f"lut3d={lut_path.name}:interp=tetrahedral",
        "scale=out_color_matrix=bt709:out_range=tv:flags=lanczos",
    ]
    if fps_value:
        filters.append(f"fps={fps_value}")
    filters.append("format=yuv420p10le")
    vf = ",".join(filters)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-stats",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        vf,
        "-c:v",
        "libx265",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p10le",
        "-tag:v",
        "hvc1",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-color_range",
        "tv",
        "-metadata:s:v:0",
        "rotate=0",
        "-c:a",
        "aac",
        "-b:a",
        "320k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    print("Rendering:", " ".join(cmd), flush=True)
    result = subprocess.run(
        cmd,
        cwd=str(lut_path.parent),
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise GradeError(f"ffmpeg render failed with exit code {result.returncode}")


def retag_video(ffmpeg: str, input_path: Path, output_path: Path) -> None:
    """Remux the encoded file to write reliable Rec.709 VUI/container tags."""
    run_checked(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-map",
            "0",
            "-c",
            "copy",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-color_range",
            "tv",
            "-metadata:s:v:0",
            "rotate=0",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def output_qc(
    output_path: Path,
    source_info: dict[str, Any],
    fps_value: str | None,
) -> dict[str, Any]:
    probe = ffprobe_json(output_path)
    summary = source_summary(output_path, probe)
    output_duration = summary["duration_sec"]
    source_duration = source_info["duration_sec"]
    metadata_ok = (
        summary["codec_name"] == "hevc"
        and summary["profile"] == "Main 10"
        and summary["pix_fmt"] == "yuv420p10le"
        and summary["color_primaries"] == "bt709"
        and summary["color_transfer"] == "bt709"
        and summary["color_space"] == "bt709"
        and summary["color_range"] == "tv"
    )
    duration_delta = abs(output_duration - source_duration)
    duration_ok = duration_delta <= max(0.25, source_duration * 0.01)
    frame_count_ok = None
    if fps_value and summary.get("nb_frames"):
        fps_float = fraction_value(fps_value)
        if fps_float:
            expected_frames = round(output_duration * fps_float)
            tolerance = max(3, round(expected_frames * 0.003))
            frame_count_ok = abs(summary["nb_frames"] - expected_frames) <= tolerance
    return {
        "output_path": str(output_path.resolve()),
        "output_summary": summary,
        "checks": {
            "metadata_ok": metadata_ok,
            "duration_ok": duration_ok,
            "duration_delta_sec": round(duration_delta, 6),
            "frame_count_ok": frame_count_ok,
            "audio_preserved": summary["audio"]["present"] == source_info["audio"]["present"],
        },
    }


def default_output_dir() -> Path:
    return timestamped_output_dir("Xiaomi15U_Grade")
