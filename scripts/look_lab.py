#!/usr/bin/env python3
"""Batch scan and shortlist workflow for Xiaomi 15 Ultra Log clips."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from build_derived_luts import build_all, load_look_library
from grade_core import (
    GradeError,
    build_contact_sheet,
    extract_preview,
    ffprobe_json,
    output_qc,
    render_video,
    require_tool,
    retag_video,
    sample_times,
    sha256,
    source_summary,
    target_fps,
    write_json,
)
from recommend import recommend_looks
from runtime_config import add_version_argument, timestamped_output_dir
from scout import (
    aggregate_metrics,
    image_metrics,
    scouting_template,
    validate_scouting,
)

SKILL_DIR = Path(__file__).resolve().parent.parent
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}


def discover_inputs(inputs: list[Path]) -> list[Path]:
    found: list[Path] = []
    for item in inputs:
        if item.is_dir():
            found.extend(
                path
                for path in item.rglob("*")
                if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
            )
        elif item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS:
            found.append(item)
    unique = {path.resolve() for path in found}
    return sorted(unique, key=lambda path: (path.name.lower(), str(path).lower()))


def assign_clip_ids(paths: list[Path]) -> list[tuple[str, Path]]:
    return [(f"clip_{index:03d}", path) for index, path in enumerate(paths, start=1)]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def relative_path(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def default_batch_dir() -> Path:
    return timestamped_output_dir("Xiaomi15U_LookLab")


def scan_clip(
    ffmpeg: str,
    clip_id: str,
    source_path: Path,
    clip_root: Path,
) -> dict[str, Any]:
    analysis_dir = clip_root / "analysis"
    frames_dir = analysis_dir / "source_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    probe = ffprobe_json(source_path)
    source = source_summary(source_path, probe)
    times = sample_times(source["duration_sec"])
    frames: list[Path] = []
    for index, seconds in enumerate(times, start=1):
        frame_path = frames_dir / f"{index:02d}.png"
        extract_preview(ffmpeg, source_path, seconds, frame_path, None)
        frames.append(frame_path)

    contact_path = analysis_dir / "source_contact.jpg"
    build_contact_sheet(
        {"source": frames},
        {"source": "SOURCE"},
        times,
        contact_path,
        tile_size=(480, 300),
    )
    metrics = aggregate_metrics([image_metrics(path) for path in frames])
    write_json(analysis_dir / "probe.json", probe)
    write_json(analysis_dir / "metrics.json", metrics)

    return {
        "filename": source_path.name,
        "source_path": str(source_path.resolve()),
        "status": "scanned",
        "duration_sec": source["duration_sec"],
        "source_matches_tested_profile": source["matches_tested_profile"],
        "source_summary": source,
        "sample_times_sec": [round(value, 6) for value in times],
        "source_frames": [relative_path(path, clip_root.parent.parent) for path in frames],
        "source_contact": relative_path(contact_path, clip_root.parent.parent),
        "analysis_dir": relative_path(analysis_dir, clip_root.parent.parent),
        "looks": [],
        "look_contact": None,
        "error": None,
    }


def command_scan(args: argparse.Namespace) -> int:
    ffmpeg = require_tool("ffmpeg")
    paths = discover_inputs(args.inputs)
    if not paths:
        raise GradeError("No supported video files found")
    batch_dir = (args.out or default_batch_dir()).resolve()
    manifest_path = batch_dir / "batch_manifest.json"
    if manifest_path.exists() and not args.force:
        raise GradeError(f"Batch already exists: {batch_dir}. Use --force to rebuild previews.")
    clips_root = batch_dir / "clips"
    clips_root.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "batch_dir": str(batch_dir),
        "status": "scanning",
        "clips": {},
    }
    for clip_id, source_path in assign_clip_ids(paths):
        clip_root = clips_root / clip_id
        clip_root.mkdir(parents=True, exist_ok=True)
        try:
            entry = scan_clip(ffmpeg, clip_id, source_path.resolve(), clip_root)
            manifest["clips"][clip_id] = entry
            print(f"SCANNED {clip_id}: {source_path.name}")
        except Exception as exc:
            manifest["clips"][clip_id] = {
                "filename": source_path.name,
                "source_path": str(source_path.resolve()),
                "status": "failed",
                "error": str(exc),
            }
            print(f"FAILED {clip_id}: {source_path.name}: {exc}", file=sys.stderr)

    scanned_count = sum(1 for entry in manifest["clips"].values() if entry["status"] == "scanned")
    manifest["status"] = (
        "scanned"
        if scanned_count == len(manifest["clips"])
        else "partial"
        if scanned_count
        else "failed"
    )
    write_json(manifest_path, manifest)
    write_json(
        batch_dir / "scouting_template.json",
        scouting_template(manifest["clips"]),
    )
    print(f"Batch manifest: {manifest_path}")
    return 0 if scanned_count else 1


def render_preview_clip(
    ffmpeg: str,
    source_path: Path,
    output_path: Path,
    lut_path: Path,
    duration: float,
    fps_value: str,
) -> None:
    segment_duration = min(5.0, duration)
    start = max(0.0, min(duration - segment_duration, duration * 0.35))
    filters = (
        f"lut3d={lut_path.name}:interp=tetrahedral,"
        f"fps={fps_value},"
        "scale=1920:1080:force_original_aspect_ratio=decrease:"
        "force_divisible_by=2:flags=lanczos,format=yuv420p"
    )
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(source_path),
        "-t",
        f"{segment_duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        filters,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-color_range",
        "tv",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = subprocess.run(
        cmd,
        cwd=str(lut_path.parent),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise GradeError(f"Preview render failed for {output_path.name}: {detail}")


def shortlist_clip(
    ffmpeg: str,
    batch_dir: Path,
    clip_id: str,
    clip: dict[str, Any],
    metrics: dict[str, Any],
    scouting: dict[str, Any],
    library: dict[str, Any],
    lut_paths: dict[str, Path],
) -> dict[str, Any]:
    recommendations = recommend_looks(
        metrics=metrics,
        scouting=scouting,
        looks=library["looks"],
        count=4,
    )
    clip_root = batch_dir / "clips" / clip_id
    preview_root = clip_root / "previews"
    preview_root.mkdir(parents=True, exist_ok=True)
    source_frames = [batch_dir / path for path in clip["source_frames"]]
    rows: dict[str, list[Path]] = {"source": source_frames}
    labels = {"source": "SOURCE"}
    times = clip["sample_times_sec"]
    duration = float(clip["duration_sec"])
    fps_value = target_fps(clip["source_summary"])

    for recommendation in recommendations:
        look_id = recommendation["id"]
        look_dir = preview_root / look_id
        look_dir.mkdir(parents=True, exist_ok=True)
        look_frames: list[Path] = []
        for index, seconds in enumerate(times, start=1):
            frame_path = look_dir / f"{index:02d}.png"
            extract_preview(
                ffmpeg,
                Path(clip["source_path"]),
                float(seconds),
                frame_path,
                lut_paths[look_id],
            )
            look_frames.append(frame_path)
        rows[look_id] = look_frames
        labels[look_id] = library["looks"][look_id]["name"].upper()
        preview_path = look_dir / "preview.mp4"
        render_preview_clip(
            ffmpeg,
            Path(clip["source_path"]),
            preview_path,
            lut_paths[look_id],
            duration,
            fps_value,
        )
        recommendation["preview"] = relative_path(preview_path, batch_dir)

    look_contact = clip_root / "look_contact.jpg"
    build_contact_sheet(
        rows,
        labels,
        times,
        look_contact,
        tile_size=(360, 230),
    )
    clip["status"] = "shortlisted"
    clip["looks"] = recommendations
    clip["look_contact"] = relative_path(look_contact, batch_dir)
    clip["error"] = None
    write_json(clip_root / "preview_manifest.json", clip)
    return clip


def command_shortlist(args: argparse.Namespace) -> int:
    ffmpeg = require_tool("ffmpeg")
    batch_dir = args.batch.resolve()
    manifest_path = batch_dir / "batch_manifest.json"
    if not manifest_path.exists():
        raise GradeError(f"Batch manifest not found: {manifest_path}")
    if (batch_dir / "selection_template.json").exists() and not args.force:
        raise GradeError("Selection template already exists. Use --force to rebuild shortlists.")

    manifest = read_json(manifest_path)
    scanned = {
        clip_id: clip
        for clip_id, clip in manifest["clips"].items()
        if clip.get("status") in {"scanned", "shortlisted"}
    }
    if not scanned:
        raise GradeError("No scanned clips available for shortlist")
    scouting = validate_scouting(read_json(args.scouting), set(scanned))

    lut_paths = build_all(force=False)
    library = load_look_library()
    for clip_id, clip in scanned.items():
        try:
            metrics = read_json(batch_dir / clip["analysis_dir"] / "metrics.json")
            manifest["clips"][clip_id] = shortlist_clip(
                ffmpeg,
                batch_dir,
                clip_id,
                clip,
                metrics,
                scouting[clip_id],
                library,
                lut_paths,
            )
            print(f"SHORTLISTED {clip_id}: {[item['id'] for item in clip['looks']]}")
        except Exception as exc:
            clip["status"] = "failed"
            clip["error"] = str(exc)
            print(f"FAILED {clip_id}: {exc}", file=sys.stderr)

    shortlisted_count = sum(
        1 for clip in manifest["clips"].values() if clip["status"] == "shortlisted"
    )
    manifest["status"] = (
        "shortlisted"
        if shortlisted_count == len(manifest["clips"])
        else "partial"
        if shortlisted_count
        else "failed"
    )
    write_json(manifest_path, manifest)
    selection_template = {
        clip_id: {
            "filename": clip["filename"],
            "looks": [],
        }
        for clip_id, clip in manifest["clips"].items()
        if clip["status"] == "shortlisted"
    }
    write_json(batch_dir / "selection_template.json", selection_template)
    print(f"Selection template: {batch_dir / 'selection_template.json'}")
    return 0 if shortlisted_count else 1


def validate_selections(
    payload: dict[str, Any],
    clip_ids: set[str],
    allowed_looks: set[str],
) -> dict[str, list[str]]:
    if not payload:
        raise ValueError("selections must not be empty")
    unknown_clips = set(payload) - clip_ids
    if unknown_clips:
        raise ValueError(f"unknown clips: {sorted(unknown_clips)}")
    missing_clips = clip_ids - set(payload)
    if missing_clips:
        raise ValueError(f"missing clips: {sorted(missing_clips)}")
    result: dict[str, list[str]] = {}
    for clip_id, entry in payload.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{clip_id}: selection must be an object")
        looks = entry.get("looks", [])
        if not isinstance(looks, list) or not looks:
            raise ValueError(f"{clip_id}: no looks selected")
        if not all(isinstance(look, str) for look in looks):
            raise ValueError(f"{clip_id}: looks must be strings")
        if len(looks) != len(set(looks)):
            raise ValueError(f"{clip_id}: duplicate looks")
        unknown_looks = set(looks) - allowed_looks
        if unknown_looks:
            raise ValueError(f"{clip_id}: unknown looks {sorted(unknown_looks)}")
        result[clip_id] = looks
    return result


def command_render(args: argparse.Namespace) -> int:
    ffmpeg = require_tool("ffmpeg")
    batch_dir = args.batch.resolve()
    manifest_path = batch_dir / "batch_manifest.json"
    if not manifest_path.exists():
        raise GradeError(f"Batch manifest not found: {manifest_path}")
    manifest = read_json(manifest_path)
    shortlisted = {
        clip_id: clip
        for clip_id, clip in manifest["clips"].items()
        if clip.get("status") == "shortlisted"
    }
    if not shortlisted:
        raise GradeError("No shortlisted clips available for rendering")

    library = load_look_library()
    allowed_looks = set(library["looks"])
    selections = validate_selections(
        read_json(args.selections),
        set(shortlisted),
        allowed_looks,
    )
    lut_paths = build_all(force=False)

    final_dir = batch_dir / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for clip_id, looks in selections.items():
        for look_id in looks:
            outputs.append(final_dir / f"{clip_id}__{look_id}.mp4")
    if not args.force:
        existing = [path for path in outputs if path.exists()]
        if existing:
            raise GradeError(
                "Final output already exists; re-run with --force: "
                + ", ".join(path.name for path in existing)
            )

    report: dict[str, Any] = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "rendering",
        "rendered": [],
        "failed": [],
        "clips": {},
    }
    for clip_id, looks in selections.items():
        clip = shortlisted[clip_id]
        if not clip.get("source_matches_tested_profile"):
            for look_id in looks:
                message = "source does not match the tested Xiaomi 15 Ultra Log profile"
                report["failed"].append({"clip": clip_id, "look": look_id, "error": message})
                print(f"FAILED {clip_id}/{look_id}: {message}", file=sys.stderr)
            continue

        source_path = Path(clip["source_path"])
        source_info = clip["source_summary"]
        fps_value = target_fps(source_info)
        for look_id in looks:
            output_path = final_dir / f"{clip_id}__{look_id}.mp4"
            raw_path = final_dir / f"{clip_id}__{look_id}.raw.mp4"
            key = f"{clip_id}__{look_id}"
            try:
                print(f"RENDERING {key}")
                render_video(
                    ffmpeg,
                    source_path,
                    raw_path,
                    lut_paths[look_id],
                    args.crf,
                    args.preset,
                    fps_value,
                )
                retag_video(ffmpeg, raw_path, output_path)
                raw_path.unlink(missing_ok=True)
                qc = output_qc(output_path, source_info, fps_value)
                if not qc["checks"]["metadata_ok"] or not qc["checks"]["duration_ok"]:
                    raise GradeError("final QC failed; inspect qc_report.json")
                report["clips"][key] = {
                    "clip": clip_id,
                    "look": look_id,
                    "source": str(source_path),
                    "output": str(output_path),
                    "bytes": output_path.stat().st_size,
                    "sha256": sha256(output_path),
                    "qc": qc,
                }
                report["rendered"].append(key)
                print(f"RENDERED {key}")
            except Exception as exc:
                report["failed"].append({"clip": clip_id, "look": look_id, "error": str(exc)})
                print(f"FAILED {key}: {exc}", file=sys.stderr)

    rendered_count = len(report["rendered"])
    failed_count = len(report["failed"])
    report["status"] = (
        "complete"
        if rendered_count and not failed_count
        else "partial"
        if rendered_count
        else "failed"
    )
    write_json(batch_dir / "final_manifest.json", report)
    write_json(batch_dir / "qc_report.json", report)
    manifest["status"] = f"render_{report['status']}"
    manifest["render_report"] = "qc_report.json"
    write_json(manifest_path, manifest)
    print(
        json.dumps(
            {
                "status": report["status"],
                "rendered": report["rendered"],
                "failed": report["failed"],
                "report": str(batch_dir / "qc_report.json"),
            },
            indent=2,
        )
    )
    return 0 if report["status"] == "complete" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Xiaomi 15 Ultra Log batch Look Lab")
    add_version_argument(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Probe clips and write source contact sheets")
    scan_parser.add_argument("inputs", nargs="+", type=Path, help="Video files or folders")
    scan_parser.add_argument("--out", type=Path, help="Batch output directory")
    scan_parser.add_argument("--force", action="store_true", help="Rebuild an existing batch")
    scan_parser.set_defaults(handler=command_scan)

    shortlist_parser = subparsers.add_parser(
        "shortlist", help="Recommend four looks and render review previews"
    )
    shortlist_parser.add_argument("--batch", type=Path, required=True)
    shortlist_parser.add_argument("--scouting", type=Path, required=True)
    shortlist_parser.add_argument("--force", action="store_true")
    shortlist_parser.set_defaults(handler=command_shortlist)

    render_parser = subparsers.add_parser("render", help="Render selected clip/look combinations")
    render_parser.add_argument("--batch", type=Path, required=True)
    render_parser.add_argument("--selections", type=Path, required=True)
    render_parser.add_argument("--crf", type=int, default=19)
    render_parser.add_argument(
        "--preset",
        default="medium",
        choices=("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"),
    )
    render_parser.add_argument("--force", action="store_true")
    render_parser.set_defaults(handler=command_render)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GradeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
