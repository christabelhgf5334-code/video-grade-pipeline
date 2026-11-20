#!/usr/bin/env python3
"""Portable runtime paths and executable configuration."""

from __future__ import annotations

import argparse
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Mapping

# These modules live one level deeper than originally (scripts/resolve/), so walk up until
# the folder that actually holds SKILL.md — hard-coding a parent count breaks on relocation.
def _find_skill_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "SKILL.md").exists():
            return p
    return start.parent.parent


SKILL_ROOT = _find_skill_root(Path(__file__).resolve().parent)
OUTPUT_ENV = "XIAOMI15U_OUTPUT_DIR"
TOOL_ENV_PREFIX = "XIAOMI15U_"


def read_version(root: Path = SKILL_ROOT) -> str:
    version_path = root / "VERSION"
    if not version_path.exists():
        raise FileNotFoundError(f"Version file missing: {version_path}")
    version = version_path.read_text(encoding="utf-8").strip()
    if not version:
        raise ValueError(f"Version file is empty: {version_path}")
    return version


def output_root(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    configured = env.get(OUTPUT_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return (home / "Videos" / "Xiaomi15U").resolve()


def timestamped_output_dir(
    prefix: str,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return output_root(env=env, home=home) / f"{prefix}_{stamp}"


def tool_path(
    name: str,
    env: Mapping[str, str] | None = None,
) -> str:
    env = os.environ if env is None else env
    env_key = f"{TOOL_ENV_PREFIX}{name.upper()}"
    configured = env.get(env_key)
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        discovered = shutil.which(configured)
        if discovered:
            return discovered
        raise FileNotFoundError(f"{env_key} points to an unavailable executable: {configured}")
    discovered = shutil.which(name)
    if discovered:
        return discovered
    raise FileNotFoundError(
        f"Required tool not found: {name}. Install it, add it to PATH, or set {env_key}."
    )


def add_version_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {read_version()}",
    )
