#!/usr/bin/env python3
"""Connection helpers for the DaVinci Resolve scripting API."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class ResolveConnectionError(RuntimeError):
    pass


def _windows_resolve_roots(env: dict[str, str]) -> list[Path]:
    roots = [Path(env.get("ProgramFiles", r"C:\Program Files"))]
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = Path(f"{letter}:/Program Files")
        if candidate not in roots:
            roots.append(candidate)
    return [root / "Blackmagic Design" / "DaVinci Resolve" for root in roots]


def platform_candidates(
    system: str,
    env: dict[str, str],
) -> dict[str, list[Path]]:
    home = Path(env.get("HOME", Path.home()))
    if system == "Windows":
        program_data = Path(env.get("PROGRAMDATA", r"C:\ProgramData"))
        appdata = Path(env.get("APPDATA", home / "AppData" / "Roaming"))
        resolve_roots = _windows_resolve_roots(env)
        return {
            "api": [
                program_data
                / "Blackmagic Design"
                / "DaVinci Resolve"
                / "Support"
                / "Developer"
                / "Scripting"
            ],
            "libraries": [root / "fusionscript.dll" for root in resolve_roots],
            "executables": [root / "Resolve.exe" for root in resolve_roots],
            "legacy_api": [
                appdata
                / "Blackmagic Design"
                / "DaVinci Resolve"
                / "Support"
                / "Developer"
                / "Scripting"
            ],
        }
    if system == "Darwin":
        return {
            "api": [
                Path(
                    "/Library/Application Support/Blackmagic Design/"
                    "DaVinci Resolve/Developer/Scripting"
                )
            ],
            "libraries": [
                Path(
                    "/Applications/DaVinci Resolve/DaVinci Resolve.app/"
                    "Contents/Resources/fusionscript.dylib"
                )
            ],
            "executables": [
                Path(
                    "/Applications/DaVinci Resolve/DaVinci Resolve.app/"
                    "Contents/MacOS/DaVinci Resolve"
                )
            ],
            "legacy_api": [],
        }
    return {
        "api": [Path("/opt/resolve/Developer/Scripting")],
        "libraries": [Path("/opt/resolve/libs/Fusion/fusionscript.so")],
        "executables": [Path("/opt/resolve/bin/resolve")],
        "legacy_api": [],
    }


def _first_existing(paths: list[Path]) -> Path | None:
    return next((path.resolve() for path in paths if path.exists()), None)


@dataclass(frozen=True)
class ResolvePaths:
    api_dir: Path
    library_path: Path
    executable_path: Path

    @classmethod
    def discover(
        cls,
        env: dict[str, str] | None = None,
        system: str | None = None,
    ) -> "ResolvePaths":
        env = dict(os.environ if env is None else env)
        system = platform.system() if system is None else system
        candidates = platform_candidates(system, env)

        api_dir = (
            Path(env["RESOLVE_SCRIPT_API"]).expanduser()
            if env.get("RESOLVE_SCRIPT_API")
            else _first_existing(candidates["api"] + candidates["legacy_api"])
        )
        library_path = (
            Path(env["RESOLVE_SCRIPT_LIB"]).expanduser()
            if env.get("RESOLVE_SCRIPT_LIB")
            else _first_existing(candidates["libraries"])
        )
        executable_path = (
            Path(env["RESOLVE_EXECUTABLE"]).expanduser()
            if env.get("RESOLVE_EXECUTABLE")
            else _first_existing(candidates["executables"])
        )

        missing = [
            name
            for name, value in (
                ("Resolve API directory", api_dir),
                ("Resolve scripting library", library_path),
                ("Resolve executable", executable_path),
            )
            if value is None or not value.exists()
        ]
        if missing:
            raise ResolveConnectionError(
                "Could not discover: "
                + ", ".join(missing)
                + ". Set RESOLVE_SCRIPT_API, RESOLVE_SCRIPT_LIB and "
                "RESOLVE_EXECUTABLE."
            )
        return cls(api_dir.resolve(), library_path.resolve(), executable_path.resolve())

    def apply_environment(self) -> None:
        os.environ["RESOLVE_SCRIPT_API"] = str(self.api_dir)
        os.environ["RESOLVE_SCRIPT_LIB"] = str(self.library_path)
        modules = str(self.api_dir / "Modules")
        current = os.environ.get("PYTHONPATH", "")
        if modules not in current.split(os.pathsep):
            os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [modules, current]))


def _default_loader(paths: ResolvePaths) -> Any:
    modules = str(paths.api_dir / "Modules")
    if modules not in sys.path:
        sys.path.insert(0, modules)
    import DaVinciResolveScript

    return DaVinciResolveScript


def connect_resolve(
    module_loader: Callable[[ResolvePaths], Any] | None = None,
    paths: ResolvePaths | None = None,
) -> Any:
    paths = paths or ResolvePaths.discover()
    paths.apply_environment()
    module = (module_loader or _default_loader)(paths)
    if module is None:
        raise ResolveConnectionError("DaVinciResolveScript module is unavailable")
    resolve = module.scriptapp("Resolve")
    if resolve is None:
        raise ResolveConnectionError(
            "Resolve is not running or external scripting is not set to Local"
        )
    return resolve


def start_headless_resolve(paths: ResolvePaths, timeout: float = 60.0) -> subprocess.Popen:
    process = subprocess.Popen(
        [str(paths.executable_path), "-nogui"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            connect_resolve(paths=paths)
            return process
        except ResolveConnectionError:
            time.sleep(2)
    process.terminate()
    raise ResolveConnectionError("Resolve did not become scriptable before timeout")
