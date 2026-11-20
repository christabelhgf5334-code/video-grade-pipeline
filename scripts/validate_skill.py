#!/usr/bin/env python3
"""Validate the public Xiaomi 15 Ultra Log Grade repository structure."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from runtime_config import SKILL_ROOT, add_version_argument

REQUIRED_FILES = (
    "SKILL.md",
    "VERSION",
    "LICENSE",
    "agents/openai.yaml",
    "assets/looks.json",
    "assets/powergrades.json",
)
REQUIRED_DIRECTORIES = ("references", "scripts")
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


def validate_skill(root: Path = SKILL_ROOT) -> list[str]:
    errors: list[str] = []
    for relative in REQUIRED_FILES:
        if not (root / relative).is_file():
            errors.append(f"missing file: {relative}")
    for relative in REQUIRED_DIRECTORIES:
        if not (root / relative).is_dir():
            errors.append(f"missing directory: {relative}")

    skill_path = root / "SKILL.md"
    if skill_path.is_file():
        text = skill_path.read_text(encoding="utf-8")
        match = FRONTMATTER.match(text)
        if not match:
            errors.append("SKILL.md has no valid YAML frontmatter")
        else:
            frontmatter = match.group(1)
            if "name: video-grade-pipeline" not in frontmatter:
                errors.append("SKILL.md name must be video-grade-pipeline")
            if not re.search(r"^description:\s*\S+", frontmatter, re.MULTILINE):
                errors.append("SKILL.md description is missing")
        if "TODO" in text or "TBD" in text:
            errors.append("SKILL.md contains unfinished placeholders")

    for json_path in (root / "assets").glob("*.json"):
        try:
            json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid JSON {json_path.name}: {exc}")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    add_version_argument(parser)
    parser.add_argument("root", nargs="?", type=Path, default=SKILL_ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    errors = validate_skill(args.root.resolve())
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Skill is valid!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
