#!/usr/bin/env python3
"""Resolve preview and final render queue helpers."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from resolve_powergrades import GradeValidationError

TERMINAL_FAILURES = {"Failed", "Cancelled", "Error"}


def _first_stream(probe: dict[str, Any], codec_type: str) -> dict[str, Any]:
    return next(
        (stream for stream in probe.get("streams", []) if stream.get("codec_type") == codec_type),
        {},
    )


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def output_qc_checks(
    source_probe: dict[str, Any],
    output_probe: dict[str, Any],
    frame_tolerance: int = 1,
) -> dict[str, Any]:
    source_video = _first_stream(source_probe, "video")
    output_video = _first_stream(output_probe, "video")
    source_audio = _first_stream(source_probe, "audio")
    output_audio = _first_stream(output_probe, "audio")

    source_frames = _integer(source_video.get("nb_frames"))
    output_frames = _integer(output_video.get("nb_frames"))
    frame_delta = (
        abs(output_frames - source_frames)
        if source_frames is not None and output_frames is not None
        else None
    )
    audio_matches = not source_audio or (
        bool(output_audio)
        and output_audio.get("sample_rate") == source_audio.get("sample_rate")
        and output_audio.get("channels") == source_audio.get("channels")
    )

    checks = {
        "codec": output_video.get("codec_name") == "hevc",
        "profile": output_video.get("profile") == "Main 10",
        "pix_fmt": output_video.get("pix_fmt") == "yuv420p10le",
        "resolution": output_video.get("width") == 3840 and output_video.get("height") == 2160,
        "color_range": output_video.get("color_range") == "tv",
        "rec709": output_video.get("color_primaries") == "bt709"
        and output_video.get("color_transfer") == "bt709"
        and output_video.get("color_space") == "bt709",
        "audio": audio_matches,
        "frame_count": frame_delta is not None and frame_delta <= frame_tolerance,
    }
    return {
        "checks": checks,
        "source_frames": source_frames,
        "output_frames": output_frames,
        "frame_delta": frame_delta,
        "frame_tolerance": frame_tolerance,
    }


def preview_settings(
    target_dir: Path,
    name: str,
    mark_in: int = 0,
    mark_out: int = 149,
) -> dict[str, Any]:
    return {
        "SelectAllFrames": False,
        "MarkIn": mark_in,
        "MarkOut": mark_out,
        "TargetDir": str(target_dir),
        "CustomName": name,
        "FormatWidth": 1920,
        "FormatHeight": 1080,
        "FrameRate": "60",
        "ExportVideo": True,
        "ExportAudio": True,
    }


def final_settings(target_dir: Path, name: str) -> dict[str, Any]:
    return {
        "SelectAllFrames": True,
        "TargetDir": str(target_dir),
        "CustomName": name,
        "FormatWidth": 3840,
        "FormatHeight": 2160,
        "FrameRate": "60",
        "ExportVideo": True,
        "ExportAudio": True,
        "EncodingProfile": "Main10",
    }


def wait_for_jobs(
    project: Any,
    job_ids: list[str],
    poll_seconds: float = 2.0,
) -> dict[str, Any]:
    while project.IsRenderingInProgress():
        time.sleep(poll_seconds)
    return {job_id: project.GetRenderJobStatus(job_id) or {} for job_id in job_ids}


def status_summary(statuses: dict[str, Any]) -> str:
    if not statuses:
        return "failed"
    failed = [
        status for status in statuses.values() if status.get("JobStatus") in TERMINAL_FAILURES
    ]
    complete = [status for status in statuses.values() if status.get("JobStatus") == "Complete"]
    if complete and not failed:
        return "complete"
    if complete:
        return "partial"
    return "failed"


def render_timelines(
    project: Any,
    timelines: list[Any],
    target_dir: Path,
    final: bool,
    poll_seconds: float = 2.0,
) -> dict[str, Any]:
    target_dir.mkdir(parents=True, exist_ok=True)
    project.DeleteAllRenderJobs()
    job_ids: list[str] = []
    job_timelines: dict[str, str] = {}
    for timeline in timelines:
        if not project.SetCurrentTimeline(timeline):
            raise GradeValidationError(f"failed to activate timeline: {timeline.GetName()}")
        if final:
            codecs = project.GetRenderCodecs("MP4") or {}
            codec = codecs.get("H.265") or codecs.get("H.265 NVIDIA")
            if not codec:
                raise GradeValidationError("Resolve exposes no H.265 MP4 codec")
            if not project.LoadRenderPreset("H.265 Master"):
                raise GradeValidationError("Resolve H.265 Master preset is unavailable")
            if not project.SetCurrentRenderFormatAndCodec("MP4", codec):
                raise GradeValidationError("failed to set H.265 render codec")
            settings = final_settings(target_dir, timeline.GetName())
        else:
            if not project.SetCurrentRenderFormatAndCodec("MP4", "H264"):
                raise GradeValidationError("failed to set H.264 preview codec")
            start = int(timeline.GetStartFrame())
            end = min(start + 149, int(timeline.GetEndFrame()))
            settings = preview_settings(
                target_dir / timeline.GetName(),
                timeline.GetName(),
                mark_in=start,
                mark_out=end,
            )
        if not project.SetRenderSettings(settings):
            raise GradeValidationError(f"failed to set render settings: {timeline.GetName()}")
        job_id = project.AddRenderJob()
        if not job_id:
            raise GradeValidationError(f"failed to add render job: {timeline.GetName()}")
        job_ids.append(job_id)
        job_timelines[job_id] = timeline.GetName()
    if not project.StartRendering(job_ids):
        raise GradeValidationError("Resolve failed to start rendering")
    statuses = wait_for_jobs(project, job_ids, poll_seconds=poll_seconds)
    return {
        "status": status_summary(statuses),
        "jobs": statuses,
        "job_timelines": job_timelines,
    }
