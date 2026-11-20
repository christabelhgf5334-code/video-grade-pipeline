#!/usr/bin/env python3
"""PowerGrade registry, installation, export and node validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import time
from pathlib import Path, PureWindowsPath
from typing import Any

import zstandard as zstd
from resolve_client import ResolveConnectionError, connect_resolve
from runtime_config import add_version_argument

from runtime_config import SKILL_ROOT as SKILL_DIR  # relocation-proof
REGISTRY_PATH = SKILL_DIR / "assets" / "powergrades.json"
EXPECTED_NODE_LABELS = [
    "01 SOURCE",
    "02 SHOT BALANCE",
    "03 TONE MAP",
    "04 SKIN PROTECT",
    "05 LOOK",
    "06 OUTPUT TRIM",
]


class GradeValidationError(RuntimeError):
    pass


def drx_lut_stem(path: str) -> str:
    """Return the LUT filename stem from a Windows-style Resolve path."""
    return PureWindowsPath(path).stem


def load_powergrade_registry() -> dict[str, Any]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def save_powergrade_registry(registry: dict[str, Any]) -> None:
    temp = REGISTRY_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(REGISTRY_PATH)


def validate_graph(graph: Any, require_tools: bool = False) -> None:
    count = int(graph.GetNumNodes())
    if count != len(EXPECTED_NODE_LABELS):
        raise GradeValidationError(f"expected {len(EXPECTED_NODE_LABELS)} nodes, found {count}")
    for index, expected in enumerate(EXPECTED_NODE_LABELS, start=1):
        actual = graph.GetNodeLabel(index)
        if actual != expected:
            raise GradeValidationError(
                f"node {index} label mismatch: expected {expected!r}, found {actual!r}"
            )
    source_lut = (graph.GetLUT(1) or "").lower()
    if "documentary_clear" not in source_lut:
        raise GradeValidationError("node 1 does not contain the Xiaomi source LUT")
    if require_tools:
        tone_tools = " ".join(graph.GetToolsInNode(3) or []).lower()
        skin_tools = " ".join(graph.GetToolsInNode(4) or []).lower()
        if "curve" not in tone_tools:
            raise GradeValidationError("node 3 does not contain a curve adjustment")
        if "qualifier" not in skin_tools:
            raise GradeValidationError("node 4 does not contain an HSL qualifier")


def user_lut_directory(
    env: dict[str, str] | None = None,
    system: str | None = None,
    home: Path | None = None,
) -> Path:
    env = dict(os.environ if env is None else env)
    system = platform.system() if system is None else system
    home = Path.home() if home is None else home
    exact = env.get("RESOLVE_LUT_DIR")
    if exact:
        return Path(exact).expanduser().resolve()
    legacy_root = env.get("RESOLVE_LUT_ROOT")
    if legacy_root:
        return (Path(legacy_root).expanduser() / "Xiaomi15U_ResolveLab").resolve()
    if system == "Windows":
        program_data = Path(env.get("PROGRAMDATA", r"C:\ProgramData"))
        master = (
            program_data
            / "Blackmagic Design"
            / "DaVinci Resolve"
            / "Support"
            / "LUT"
            / "Xiaomi15U_ResolveLab"
        )
        if master.parent.exists():
            return master.resolve()
        appdata = Path(env.get("APPDATA", home / "AppData" / "Roaming"))
        return (
            appdata
            / "Blackmagic Design"
            / "DaVinci Resolve"
            / "Support"
            / "LUT"
            / "Xiaomi15U_ResolveLab"
        ).resolve()
    if system == "Darwin":
        return (
            home
            / "Library"
            / "Application Support"
            / "Blackmagic Design"
            / "DaVinci Resolve"
            / "Support"
            / "LUT"
            / "Xiaomi15U_ResolveLab"
        ).resolve()
    return (
        home / ".local" / "share" / "DaVinciResolve" / "Support" / "LUT" / "Xiaomi15U_ResolveLab"
    ).resolve()


def install_look_luts(resolve: Any, project: Any) -> dict[str, Path]:
    target_dir = user_lut_directory()
    target_dir.mkdir(parents=True, exist_ok=True)
    installed: dict[str, Path] = {}
    registry = load_powergrade_registry()
    for key, relative_path in registry.get("pipeline_luts", {}).items():
        source = SKILL_DIR / relative_path
        if not source.exists():
            raise GradeValidationError(f"pipeline LUT missing: {source}")
        target = target_dir / f"pipeline_{source.name}"
        shutil.copy2(source, target)
        installed[f"pipeline_{key}"] = target
    for look_id, definition in registry["looks"].items():
        source = SKILL_DIR / definition["look_lut"]
        if not source.exists():
            raise GradeValidationError(f"look LUT missing: {source}")
        target = target_dir / source.name
        shutil.copy2(source, target)
        installed[look_id] = target
    if hasattr(project, "RefreshLUTList") and not project.RefreshLUTList():
        raise GradeValidationError("Resolve failed to refresh the LUT list")
    return installed


def _current_item(resolve: Any) -> Any:
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        raise GradeValidationError("no current Resolve project")
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        raise GradeValidationError("no current Resolve timeline")
    items = timeline.GetItemListInTrack("video", 1) or []
    if not items:
        raise GradeValidationError("current timeline has no video item")
    return items[0]


def inspect_current(resolve: Any) -> dict[str, Any]:
    item = _current_item(resolve)
    graph = item.GetNodeGraph()
    if graph is None:
        raise GradeValidationError("current clip has no node graph")
    nodes = []
    for index in range(1, int(graph.GetNumNodes()) + 1):
        nodes.append(
            {
                "index": index,
                "label": graph.GetNodeLabel(index),
                "lut": graph.GetLUT(index),
                "tools": graph.GetToolsInNode(index),
            }
        )
    return {"clip": item.GetName(), "nodes": nodes}


def export_current_powergrade(resolve: Any, output_path: Path) -> Path:
    resolve.OpenPage("color")
    project = resolve.GetProjectManager().GetCurrentProject()
    _current_item(resolve)
    timeline = project.GetCurrentTimeline()
    still = timeline.GrabStill()
    if still is None:
        raise GradeValidationError("Resolve failed to grab the current still")
    album = project.GetGallery().GetCurrentStillAlbum()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prefix = output_path.stem
    if not album.ExportStills([still], str(output_path.parent), prefix, "drx"):
        raise GradeValidationError("Resolve failed to export the PowerGrade still")
    candidates = sorted(output_path.parent.glob(f"{prefix}*.drx"))
    if not candidates:
        raise GradeValidationError("Resolve reported success but exported no DRX")
    exported = candidates[-1]
    if exported != output_path:
        exported.replace(output_path)
    return output_path


def build_variants(
    resolve: Any,
    clip_item: Any,
    master_drx: Path,
    registry: dict[str, Any],
    output_dir: Path,
) -> dict[str, Path]:
    resolve.OpenPage("color")
    project = resolve.GetProjectManager().GetCurrentProject()
    installed_luts = install_look_luts(resolve, project)
    album = project.GetGallery().GetCurrentStillAlbum()
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}
    for look_id, definition in registry["looks"].items():
        graph = clip_item.GetNodeGraph()
        if not graph.ApplyGradeFromDRX(str(master_drx), 0):
            raise GradeValidationError(f"failed to apply master PowerGrade: {master_drx}")
        graph = clip_item.GetNodeGraph()
        validate_graph(graph)
        lut_path = installed_luts[look_id]
        if not graph.SetLUT(3, str(installed_luts["pipeline_tone_map"])):
            raise GradeValidationError("failed to set tone-map LUT on node 3")
        if not graph.SetLUT(4, str(installed_luts["pipeline_skin_protect"])):
            raise GradeValidationError("failed to set skin-protect LUT on node 4")
        if not graph.SetLUT(5, str(lut_path)):
            raise GradeValidationError(f"failed to set look LUT on node 5: {lut_path}")
        if not graph.SetLUT(6, str(installed_luts["pipeline_output_trim"])):
            raise GradeValidationError("failed to set output-trim LUT on node 6")
        time.sleep(0.8)
        still = project.GetCurrentTimeline().GrabStill()
        prefix = f"{look_id}_tmp"
        if not album.ExportStills([still], str(output_dir), prefix, "drx"):
            raise GradeValidationError(f"failed to export DRX for {look_id}")
        candidates = sorted(output_dir.glob(f"{prefix}*.drx"))
        if not candidates:
            raise GradeValidationError(f"no DRX exported for {look_id}")
        target = output_dir / f"{look_id}.drx"
        candidates[-1].replace(target)
        outputs[look_id] = target
    return outputs


def verify_assets(write_hashes: bool = False) -> dict[str, str]:
    registry = load_powergrade_registry()
    hashes: dict[str, str] = {}
    for look_id, definition in registry["looks"].items():
        path = SKILL_DIR / definition["drx"]
        if not path.exists():
            raise GradeValidationError(f"PowerGrade missing: {path}")
        data = path.read_bytes().replace(b"\r\n", b"\n")
        if not data.startswith(b"<?xml") or b"<Gallery::GyStill" not in data[:1000]:
            raise GradeValidationError(f"not a Resolve DRX still: {path}")
        digest = hashlib.sha256(data).hexdigest()
        hashes[look_id] = digest
        if write_hashes:
            definition["sha256"] = digest
    if write_hashes:
        save_powergrade_registry(registry)
    return hashes


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("unterminated varint")


def _encode_varint(value: int) -> bytes:
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            byte |= 0x80
        result.append(byte)
        if not value:
            return bytes(result)


def _parse_fields(data: bytes) -> list[tuple[int, int, bytes]]:
    offset = 0
    fields: list[tuple[int, int, bytes]] = []
    while offset < len(data):
        start = offset
        key, offset = _read_varint(data, offset)
        field = key >> 3
        wire_type = key & 7
        if wire_type == 0:
            _, offset = _read_varint(data, offset)
        elif wire_type == 1:
            offset += 8
        elif wire_type == 2:
            length, payload_start = _read_varint(data, offset)
            offset = payload_start + length
        elif wire_type == 5:
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type: {wire_type}")
        fields.append((field, wire_type, data[start:offset]))
    return fields


def _field_payload(raw: bytes) -> bytes:
    _, offset = _read_varint(raw, 0)
    length, payload_start = _read_varint(raw, offset)
    return raw[payload_start : payload_start + length]


def _length_field(field: int, payload: bytes) -> bytes:
    return _encode_varint((field << 3) | 2) + _encode_varint(len(payload)) + payload


def _rewrite_string_recursive(data: bytes, old: bytes, new: bytes) -> bytes:
    try:
        fields = _parse_fields(data)
    except (ValueError, IndexError):
        return data
    rebuilt: list[bytes] = []
    changed = False
    for field, wire_type, raw in fields:
        if wire_type != 2:
            rebuilt.append(raw)
            continue
        payload = _field_payload(raw)
        if payload == old:
            payload = new
            changed = True
        elif old in payload:
            rewritten = _rewrite_string_recursive(payload, old, new)
            if rewritten != payload:
                payload = rewritten
                changed = True
        rebuilt.append(_length_field(field, payload))
    return b"".join(rebuilt) if changed else data


def rewrite_drx_lut(source: Path, target: Path, old_lut: str, new_lut: str) -> Path:
    text = source.read_text(encoding="utf-8")
    bodies = list(re.finditer(r"<Body>([0-9a-fA-F]+)</Body>", text))
    if not bodies:
        raise GradeValidationError(f"DRX contains no Body data: {source}")
    body = bytes.fromhex(bodies[0].group(1))
    if not body.startswith(b"\x81"):
        raise GradeValidationError("unsupported DRX body framing")
    raw = zstd.ZstdDecompressor().decompress(body[1:])
    rewritten = _rewrite_string_recursive(
        raw,
        old_lut.encode("utf-8"),
        new_lut.encode("utf-8"),
    )
    if rewritten == raw:
        raise GradeValidationError(f"LUT path not found in DRX: {old_lut}")
    compressed = zstd.ZstdCompressor(level=3).compress(rewritten)
    new_body = b"\x81" + compressed
    start, end = bodies[0].span(1)
    target.write_text(
        text[:start] + new_body.hex() + text[end:],
        encoding="utf-8",
    )
    return target


def build_offline_variants(template: Path, output_dir: Path) -> dict[str, Path]:
    registry = load_powergrade_registry()
    runtime_dir = "Xiaomi15U_ResolveLab"
    text = template.read_text(encoding="utf-8")
    bodies = list(re.finditer(r"<Body>([0-9a-fA-F]+)</Body>", text))
    if not bodies:
        raise GradeValidationError("runtime template contains no DRX Body")
    body = bytes.fromhex(bodies[0].group(1))
    raw = zstd.ZstdDecompressor().decompress(body[1:])
    matches = [
        match.decode("utf-8")
        for match in re.findall(
            rb"Xiaomi15U_ResolveLab\\[A-Za-z0-9_]+\.cube",
            raw,
        )
        if b"pipeline_" not in match
    ]
    if not matches:
        raise GradeValidationError("runtime template has no look LUT")
    template_name = drx_lut_stem(matches[0])
    outputs: dict[str, Path] = {}
    for look_id, definition in registry["looks"].items():
        target_lut = f"{runtime_dir}\\{Path(definition['look_lut']).stem}.cube"
        source_lut = f"{runtime_dir}\\{template_name}.cube"
        if look_id == "documentary_resolve" and template_name == "documentary":
            source_lut = target_lut
        if source_lut == target_lut:
            target = output_dir / f"{look_id}.drx"
            target.write_bytes(template.read_bytes())
        else:
            target = rewrite_drx_lut(
                template,
                output_dir / f"{look_id}.drx",
                source_lut,
                target_lut,
            )
        outputs[look_id] = target
    return outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve PowerGrade utilities")
    add_version_argument(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("inspect-current")

    export_parser = subparsers.add_parser("export-current")
    export_parser.add_argument("--out", type=Path, required=True)

    variants_parser = subparsers.add_parser("build-variants")
    variants_parser.add_argument("--master", type=Path, required=True)
    variants_parser.add_argument("--out-dir", type=Path, required=True)

    verify_parser = subparsers.add_parser("verify-assets")
    verify_parser.add_argument("--write-hashes", action="store_true")

    offline_parser = subparsers.add_parser("build-offline-variants")
    offline_parser.add_argument("--template", type=Path, required=True)
    offline_parser.add_argument("--out-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "verify-assets":
        print(json.dumps(verify_assets(write_hashes=args.write_hashes), indent=2))
        return 0
    resolve = connect_resolve()
    if args.command == "inspect-current":
        print(json.dumps(inspect_current(resolve), indent=2, ensure_ascii=False))
        return 0
    if args.command == "export-current":
        print(export_current_powergrade(resolve, args.out.resolve()))
        return 0
    if args.command == "build-variants":
        item = _current_item(resolve)
        outputs = build_variants(
            resolve,
            item,
            args.master.resolve(),
            load_powergrade_registry(),
            args.out_dir.resolve(),
        )
        print(json.dumps({key: str(value) for key, value in outputs.items()}, indent=2))
        return 0
    if args.command == "build-offline-variants":
        outputs = build_offline_variants(
            args.template.resolve(),
            args.out_dir.resolve(),
        )
        print(json.dumps({key: str(value) for key, value in outputs.items()}, indent=2))
        return 0
    raise GradeValidationError(f"unknown command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GradeValidationError, ResolveConnectionError) as exc:
        raise SystemExit(f"ERROR: {exc}")
