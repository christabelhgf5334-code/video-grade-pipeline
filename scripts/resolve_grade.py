#!/usr/bin/env python3
"""Create Resolve projects, timelines and apply per-shot PowerGrade CDLs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from resolve_powergrades import GradeValidationError, validate_graph

PROJECT_SETTINGS = {
    "timelineResolutionWidth": "3840",
    "timelineResolutionHeight": "2160",
    "timelineFrameRate": "60",
    "timelineOutputResolutionWidth": "3840",
    "timelineOutputResolutionHeight": "2160",
    "colorScienceMode": "davinciYRGB",
    "colorSpaceTimeline": "Rec.709 (Scene)",
    "colorSpaceOutput": "Rec.709 (Scene)",
}


def create_resolve_project(resolve: Any, name: str) -> Any:
    manager = resolve.GetProjectManager()
    current = manager.GetCurrentProject()
    if current is not None and current.GetName() == name:
        return current
    existing = manager.GetProjectListInCurrentFolder() or []
    if name in existing:
        project = manager.LoadProject(name)
    else:
        project = manager.CreateProject(name)
    if project is None:
        raise GradeValidationError(f"could not create or load Resolve project: {name}")
    configure_project(project)
    return project


def configure_project(project: Any) -> None:
    if not project.SetSettings(PROJECT_SETTINGS):
        raise GradeValidationError("Resolve rejected one or more project settings")


def import_sources(project: Any, paths: list[Path]) -> list[Any]:
    media_pool = project.GetMediaPool()
    imported = media_pool.ImportMedia([str(path.resolve()) for path in paths])
    if not imported or len(imported) != len(paths):
        raise GradeValidationError(
            f"Resolve imported {len(imported or [])} of {len(paths)} source files"
        )
    return imported


def create_look_timeline(project: Any, media_item: Any, timeline_name: str) -> Any:
    media_pool = project.GetMediaPool()
    timeline = media_pool.CreateTimelineFromClips(timeline_name, [media_item])
    if timeline is None:
        raise GradeValidationError(f"could not create Resolve timeline: {timeline_name}")
    if not project.SetCurrentTimeline(timeline):
        raise GradeValidationError(f"could not activate Resolve timeline: {timeline_name}")
    return timeline


def apply_grade_to_item(
    item: Any,
    drx_path: Path,
    cdl: dict[str, str],
) -> None:
    graph = item.GetNodeGraph()
    if graph is None:
        raise GradeValidationError("timeline item has no node graph")
    if not graph.ApplyGradeFromDRX(str(drx_path), 0):
        raise GradeValidationError(f"failed to apply PowerGrade: {drx_path}")
    graph = item.GetNodeGraph()
    validate_graph(graph)
    payload = dict(cdl)
    payload["NodeIndex"] = "2"
    if not item.SetCDL(payload):
        raise GradeValidationError("failed to set shot CDL on node 2")


def save_and_export_project(
    resolve: Any,
    project: Any,
    output_path: Path,
) -> None:
    manager = resolve.GetProjectManager()
    if not manager.SaveProject():
        raise GradeValidationError("Resolve failed to save the current project")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not manager.ExportProject(project.GetName(), str(output_path)):
        raise GradeValidationError(f"Resolve failed to export project: {output_path}")


def grade_clip_look(
    project: Any,
    media_item: Any,
    clip_id: str,
    look_id: str,
    drx_path: Path,
    cdl: dict[str, str],
) -> dict[str, Any]:
    timeline_name = f"{clip_id}__{look_id}"
    timeline = create_look_timeline(project, media_item, timeline_name)
    items = timeline.GetItemListInTrack("video", 1) or []
    if not items:
        raise GradeValidationError(f"timeline has no video item: {timeline_name}")
    apply_grade_to_item(items[0], drx_path, cdl)
    return {
        "timeline": timeline_name,
        "look": look_id,
        "drx": str(drx_path),
        "cdl": cdl,
        "node_count": int(items[0].GetNodeGraph().GetNumNodes()),
    }
