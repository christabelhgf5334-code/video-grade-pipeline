#!/usr/bin/env python3
"""Diagnose the local Xiaomi 15 Ultra Log Grade environment."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from resolve_client import ResolveConnectionError, ResolvePaths, connect_resolve
from resolve_powergrades import (
    GradeValidationError,
    load_powergrade_registry,
    user_lut_directory,
    verify_assets,
)
from runtime_config import (
    SKILL_ROOT,
    add_version_argument,
    output_root,
    tool_path,
)

REQUIRED_ASSETS = (
    "assets/looks.json",
    "assets/powergrades.json",
    "assets/presets.json",
    "assets/luts/documentary_clear.cube",
    "assets/luts/clean_filmic.cube",
    "assets/powergrades/runtime_template.drx",
)


def _check(
    name: str,
    status: str,
    detail: str,
    hint: str = "",
) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail, "hint": hint}


def diagnose(
    *,
    offline: bool = False,
    root: Path = SKILL_ROOT,
    env: dict[str, str] | None = None,
    home: Path | None = None,
    system: str | None = None,
    tool_resolver: Callable[[str], str] = tool_path,
    resolve_discover: Callable[..., ResolvePaths] = ResolvePaths.discover,
    resolve_connect: Callable[..., Any] = connect_resolve,
    lut_directory_resolver: Callable[..., Path] = user_lut_directory,
) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    home = Path.home() if home is None else home
    checks: list[dict[str, str]] = []

    version = sys.version_info
    python_ok = version >= (3, 11)
    checks.append(
        _check(
            "Python",
            "ok" if python_ok else "error",
            f"{version.major}.{version.minor}.{version.micro}",
            "" if python_ok else "Install Python 3.11 or newer.",
        )
    )

    for module_name in ("numpy", "PIL"):
        try:
            module = __import__(module_name)
            checks.append(_check(module_name, "ok", getattr(module, "__version__", "installed")))
        except ImportError:
            checks.append(
                _check(
                    module_name,
                    "error",
                    "not importable",
                    "Run: python -m pip install -r requirements.txt",
                )
            )

    for executable in ("ffmpeg", "ffprobe"):
        try:
            resolved = tool_resolver(executable)
            checks.append(_check(executable, "ok", resolved))
        except FileNotFoundError as exc:
            checks.append(
                _check(
                    executable,
                    "error",
                    str(exc),
                    f"Install {executable} or set the matching XIAOMI15U_* variable.",
                )
            )

    missing_assets = [relative for relative in REQUIRED_ASSETS if not (root / relative).exists()]
    checks.append(
        _check(
            "assets",
            "ok" if not missing_assets else "error",
            "all required assets present" if not missing_assets else ", ".join(missing_assets),
            "" if not missing_assets else "Restore the missing files from the repository.",
        )
    )

    try:
        registry = load_powergrade_registry()
        hashes = verify_assets()
        mismatches = [
            look_id
            for look_id, digest in hashes.items()
            if registry["looks"][look_id].get("sha256") != digest
        ]
        checks.append(
            _check(
                "asset hashes",
                "ok" if not mismatches else "error",
                "registry hashes match" if not mismatches else ", ".join(mismatches),
                "" if not mismatches else "Regenerate DRX assets and update their hashes.",
            )
        )
    except (GradeValidationError, KeyError, OSError) as exc:
        checks.append(
            _check(
                "asset hashes",
                "error",
                str(exc),
                "Restore the registered PowerGrade assets.",
            )
        )

    target = output_root(env=env, home=home)
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(_check("output", "ok", str(target)))
    except OSError as exc:
        checks.append(
            _check(
                "output",
                "error",
                f"{target}: {exc}",
                "Set XIAOMI15U_OUTPUT_DIR to a writable directory.",
            )
        )

    if not offline:
        try:
            lut_dir = lut_directory_resolver(env=env, system=system, home=home)
            lut_dir.mkdir(parents=True, exist_ok=True)
            probe = lut_dir / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            checks.append(_check("Resolve LUT directory", "ok", str(lut_dir)))
        except OSError as exc:
            checks.append(
                _check(
                    "Resolve LUT directory",
                    "warning",
                    f"{lut_dir}: {exc}",
                    "Set RESOLVE_LUT_DIR to a writable directory or grant "
                    "Resolve access to its standard LUT directory.",
                )
            )

        try:
            paths = resolve_discover(env=env, system=system)
            checks.append(_check("Resolve paths", "ok", str(paths.api_dir.parent)))
            resolve = resolve_connect(paths=paths)
            project = resolve.GetProjectManager().GetCurrentProject()
            checks.append(
                _check(
                    "Resolve connection",
                    "ok",
                    project.GetName() if project else "connected; no project open",
                )
            )
        except (ResolveConnectionError, ImportError, OSError) as exc:
            checks.append(
                _check(
                    "Resolve",
                    "warning",
                    str(exc),
                    "Resolve is optional. Enable external scripting Local when using it.",
                )
            )

    if any(check["status"] == "error" for check in checks):
        summary = "error"
    elif any(check["status"] == "warning" for check in checks):
        summary = "warning"
    else:
        summary = "ok"
    return {"status": summary, "checks": checks}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    add_version_argument(parser)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = diagnose(offline=args.offline)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for check in report["checks"]:
            print(f"[{check['status'].upper()}] {check['name']}: {check['detail']}")
            if check["hint"]:
                print(f"  {check['hint']}")
        print(f"Overall: {report['status']}")
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
