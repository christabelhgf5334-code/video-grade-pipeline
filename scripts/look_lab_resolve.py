#!/usr/bin/env python3
"""Resolve-native batch Look Lab for Xiaomi 15 Ultra Log footage."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from grade_core import (
    GradeError,
    build_contact_sheet,
    extract_preview,
    ffprobe_json,
    require_tool,
    sample_times,
    write_json,
)
from look_lab import assign_clip_ids, discover_inputs, read_json, scan_clip
from resolve_client import ResolveConnectionError, connect_resolve
from resolve_grade import (
    create_resolve_project,
    grade_clip_look,
    save_and_export_project,
)
from resolve_powergrades import (
    GradeValidationError,
    install_look_luts,
    load_powergrade_registry,
)
from resolve_render import output_qc_checks, render_timelines
from runtime_config import add_version_argument, timestamped_output_dir
from scout import image_metrics, validate_scouting
from shot_profile import build_shot_profile


def validate_selections(
    payload: dict[str, Any],
    clip_ids: set[str],
    rendered_looks: set[str],
) -> dict[str, list[str]]:
    unknown = set(payload) - clip_ids
    if unknown:
        raise ValueError(f"unknown clips: {sorted(unknown)}")
    missing = clip_ids - set(payload)
    if missing:
        raise ValueError(f"missing clips: {sorted(missing)}")
    result: dict[str, list[str]] = {}
    for clip_id, entry in payload.items():
        looks = entry.get("looks", []) if isinstance(entry, dict) else []
        if not looks:
            raise ValueError(f"{clip_id}: no looks selected")
        if len(looks) != len(set(looks)):
            raise ValueError(f"{clip_id}: duplicate looks")
        unknown_looks = set(looks) - rendered_looks
        if unknown_looks:
            raise ValueError(f"{clip_id}: unrendered looks {sorted(unknown_looks)}")
        result[clip_id] = looks
    return result


def _preview_file(preview_root: Path, timeline_name: str) -> Path:
    candidates = sorted((preview_root / timeline_name).glob("*.mp4"))
    if not candidates:
        raise GradeValidationError(f"Resolve preview output missing: {timeline_name}")
    return candidates[0]


def _make_look_contact(
    ffmpeg: str,
    source_frames: list[Path],
    preview_path: Path,
    output_path: Path,
    label: str,
) -> None:
    probe = ffprobe_json(preview_path)
    duration = float(probe.get("format", {}).get("duration", 5.0))
    grade_frames: list[Path] = []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_").lower()
    for index, seconds in enumerate(sample_times(duration), start=1):
        frame_path = output_path.parent / f"{safe_label}_{index}.png"
        extract_preview(ffmpeg, preview_path, seconds, frame_path, None)
        grade_frames.append(frame_path)
    build_contact_sheet(
        {"source": source_frames, "look": grade_frames},
        {"source": "SOURCE", "look": label.upper()},
        sample_times(duration),
        output_path,
        tile_size=(480, 300),
    )


def command_prepare(args: argparse.Namespace) -> int:
    ffmpeg = require_tool("ffmpeg")
    paths = discover_inputs(args.inputs)
    if not paths:
        raise GradeValidationError("no supported Xiaomi video files found")
    batch_dir = (args.out or timestamped_output_dir("Xiaomi15U_ResolveLab")).resolve()
    manifest_path = batch_dir / "batch_manifest.json"
    if manifest_path.exists() and not args.force:
        raise GradeValidationError(f"batch already exists: {batch_dir}")
    clips_root = batch_dir / "clips"
    profiles_dir = batch_dir / "shot_profiles"
    preview_root = batch_dir / "previews"
    for directory in (clips_root, profiles_dir, preview_root):
        directory.mkdir(parents=True, exist_ok=True)

    assignments = assign_clip_ids(paths)
    clip_ids = {clip_id for clip_id, _ in assignments}
    if args.scouting:
        scouting = validate_scouting(read_json(args.scouting), clip_ids)
    else:
        scouting = {
            clip_id: {"scene": [], "light": [], "risks": [], "notes": ""} for clip_id in clip_ids
        }

    registry = load_powergrade_registry()
    manifest: dict[str, Any] = {
        "version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "batch_dir": str(batch_dir),
        "status": "preparing",
        "clips": {},
    }
    media_paths: dict[str, Path] = {}
    profiles: dict[str, dict[str, Any]] = {}
    for clip_id, source_path in assignments:
        clip_root = clips_root / clip_id
        clip_root.mkdir(parents=True, exist_ok=True)
        scanned = scan_clip(ffmpeg, clip_id, source_path.resolve(), clip_root)
        if not scanned.get("source_matches_tested_profile") and not ALLOW_ANY_SOURCE[0]:
            raise GradeValidationError(
                f"{clip_id} does not match the tested Xiaomi 15 Ultra Log profile"
            )
        frame_metrics = [
            image_metrics(batch_dir / relative) for relative in scanned["source_frames"]
        ]
        profile = build_shot_profile(
            clip_id,
            scanned["source_summary"],
            frame_metrics,
            scouting[clip_id],
            registry["looks"],
        )
        write_json(profiles_dir / f"{clip_id}.json", profile)
        profiles[clip_id] = profile
        media_paths[clip_id] = source_path.resolve()
        manifest["clips"][clip_id] = {
            "filename": source_path.name,
            "source_path": str(source_path.resolve()),
            "status": "scanned",
            "source_contact": scanned["source_contact"],
            "source_frames": scanned["source_frames"],
            "sample_times_sec": scanned["sample_times_sec"],
            "cdl": profile["cdl"],
            "recommended_looks": profile["recommended_looks"],
            "looks": [],
        }

    resolve = connect_resolve()
    project_name = f"Xiaomi15U_ResolveLab_{datetime.now():%Y%m%d_%H%M%S}"
    project = create_resolve_project(resolve, project_name)
    install_look_luts(resolve, project)
    media_pool = project.GetMediaPool()
    imported_by_id: dict[str, Any] = {}
    for clip_id, path in media_paths.items():
        imported = media_pool.ImportMedia([str(path)])
        if not imported:
            raise GradeValidationError(f"Resolve failed to import {clip_id}")
        imported_by_id[clip_id] = imported[0]

    timelines: list[Any] = []
    timeline_map: dict[str, Any] = {}
    for clip_id, profile in profiles.items():
        for look_id in profile["recommended_looks"]:
            look = registry["looks"][look_id]
            grade = grade_clip_look(
                project,
                imported_by_id[clip_id],
                clip_id,
                look_id,
                (Path(__file__).resolve().parent.parent / look["drx"]).resolve(),
                profile["cdl"],
            )
            timeline = project.GetCurrentTimeline()
            timelines.append(timeline)
            timeline_map[grade["timeline"]] = timeline
            manifest["clips"][clip_id]["looks"].append(
                {
                    "id": look_id,
                    "name": look["name"],
                    "timeline": grade["timeline"],
                    "drx": look["drx"],
                    "preview": None,
                }
            )

    project_path = batch_dir / "resolve_project" / f"{project_name}.drp"
    save_and_export_project(resolve, project, project_path)
    manifest["project"] = project_name
    manifest["project_file"] = str(project_path)

    preview_result = render_timelines(project, timelines, preview_root, final=False)
    manifest["preview_render"] = preview_result
    for clip in manifest["clips"].values():
        for look in clip["looks"]:
            preview_path = _preview_file(preview_root, look["timeline"])
            look["preview"] = str(preview_path)
            contact_name = f"{look['timeline']}.jpg"
            source_frames = [batch_dir / path for path in clip["source_frames"]]
            _make_look_contact(
                ffmpeg,
                source_frames,
                preview_path,
                batch_dir / "look_contacts" / contact_name,
                look["name"],
            )
            look["look_contact"] = str(batch_dir / "look_contacts" / contact_name)
        clip["status"] = "previewed"

    manifest["status"] = "preview_complete" if preview_result["status"] == "complete" else "partial"
    write_json(manifest_path, manifest)
    write_json(
        batch_dir / "selection_template.json",
        {
            clip_id: {"filename": clip["filename"], "looks": []}
            for clip_id, clip in manifest["clips"].items()
        },
    )
    print(json.dumps({"batch": str(batch_dir), "status": manifest["status"]}, indent=2))
    return 0 if manifest["status"] == "preview_complete" else 1


def command_render(args: argparse.Namespace) -> int:
    batch_dir = args.batch.resolve()
    manifest = read_json(batch_dir / "batch_manifest.json")
    if not str(manifest.get("status", "")).startswith(("preview_", "render_")):
        raise GradeValidationError("batch previews are not complete")
    clip_ids = set(manifest["clips"])
    rendered_looks = {look["id"] for clip in manifest["clips"].values() for look in clip["looks"]}
    selections = validate_selections(
        read_json(args.selections),
        clip_ids,
        rendered_looks,
    )
    resolve = connect_resolve()
    manager = resolve.GetProjectManager()
    project = manager.LoadProject(manifest["project"])
    if project is None:
        raise GradeValidationError(f"could not load Resolve project: {manifest['project']}")
    timeline_by_name = {
        project.GetTimelineByIndex(index).GetName(): project.GetTimelineByIndex(index)
        for index in range(1, int(project.GetTimelineCount()) + 1)
    }
    selected_timelines: list[Any] = []
    selected_keys: list[str] = []
    for clip_id, looks in selections.items():
        for look_id in looks:
            timeline_name = f"{clip_id}__{look_id}"
            if timeline_name not in timeline_by_name:
                raise GradeValidationError(f"missing Resolve timeline: {timeline_name}")
            selected_timelines.append(timeline_by_name[timeline_name])
            selected_keys.append(timeline_name)
    final_dir = batch_dir / "final"
    render_result = render_timelines(
        project,
        selected_timelines,
        final_dir,
        final=True,
    )
    qc: dict[str, Any] = {
        "status": render_result["status"],
        "rendered": [],
        "failed": [],
        "files": {},
    }
    for job_id, timeline_name in render_result["job_timelines"].items():
        status = render_result["jobs"].get(job_id, {})
        candidates = sorted(final_dir.glob(f"{timeline_name}*.mp4"))
        if not candidates:
            qc["failed"].append(
                {"timeline": timeline_name, "error": "output file missing", "job": status}
            )
            continue
        output = candidates[0]
        probe = ffprobe_json(output)
        clip_id = timeline_name.split("__", 1)[0]
        source_probe = read_json(batch_dir / "clips" / clip_id / "analysis" / "probe.json")
        qc_result = output_qc_checks(source_probe, probe)
        checks = qc_result["checks"]
        qc["files"][timeline_name] = {
            "output": str(output),
            "job": status,
            "checks": checks,
            "source_frames": qc_result["source_frames"],
            "output_frames": qc_result["output_frames"],
            "frame_delta": qc_result["frame_delta"],
            "frame_tolerance": qc_result["frame_tolerance"],
        }
        if all(checks.values()):
            qc["rendered"].append(timeline_name)
        else:
            qc["failed"].append({"timeline": timeline_name, "error": "QC failed"})
    qc["status"] = (
        "complete"
        if qc["rendered"] and not qc["failed"]
        else "partial"
        if qc["rendered"]
        else "failed"
    )
    write_json(batch_dir / "resolve_render_jobs.json", render_result)
    write_json(batch_dir / "qc_report.json", qc)
    manifest["status"] = f"render_{qc['status']}"
    write_json(batch_dir / "batch_manifest.json", manifest)
    print(json.dumps({"status": qc["status"], "files": qc["files"]}, indent=2))
    return 0 if qc["status"] == "complete" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve-native Xiaomi Look Lab")
    add_version_argument(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("inputs", nargs="+", type=Path)
    prepare.add_argument("--out", type=Path)
    prepare.add_argument("--scouting", type=Path)
    prepare.add_argument("--force", action="store_true")
    prepare.add_argument("--allow-any-source", action="store_true",
                         help="accept footage that does not match the Xiaomi Log fingerprint")
    prepare.set_defaults(handler=command_prepare)
    render = subparsers.add_parser("render")
    render.add_argument("--batch", type=Path, required=True)
    render.add_argument("--selections", type=Path, required=True)
    render.set_defaults(handler=command_render)
    return parser.parse_args()


# The upstream pipeline hard-gates on a Xiaomi 15 Ultra Log fingerprint, which makes the
# Resolve route unusable for any other camera. --allow-any-source relaxes it to a warning
# so the node graph / CDL / render machinery can serve arbitrary footage.
ALLOW_ANY_SOURCE = [False]


def main() -> int:
    args = parse_args()
    if getattr(args, "allow_any_source", False):
        ALLOW_ANY_SOURCE[0] = True
        print("WARNING: 已放宽素材指纹校验，按通用 Log 处理")
    return int(args.handler(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GradeError, GradeValidationError, ResolveConnectionError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
