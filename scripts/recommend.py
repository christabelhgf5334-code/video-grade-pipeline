#!/usr/bin/env python3
"""Source-aware deterministic look recommendation."""

from __future__ import annotations

from typing import Any

BASE_SCORES = {
    "documentary": 2.0,
    "documentary_resolve": 2.0,
    "filmic": 0.5,
    "natural_warm": 0.4,
    "warm_human": 0.4,
    "cool_editorial": 0.2,
    "market_rich": 0.1,
    "market_resolve": 0.1,
    "soft_portrait": 0.3,
}


def _score_look(
    look_id: str,
    definition: dict[str, Any],
    metrics: dict[str, Any],
    scouting: dict[str, Any],
) -> tuple[float, list[str]]:
    score = BASE_SCORES.get(look_id, 0.0)
    reasons: list[str] = []
    scene = set(scouting.get("scene", []))
    light = set(scouting.get("light", []))
    risks = set(scouting.get("risks", []))

    scene_matches = scene & set(definition.get("scene_tags", []))
    for tag in sorted(scene_matches):
        score += 2.0
        reasons.append(f"scene match: {tag}")

    light_matches = light & set(definition.get("light_tags", []))
    for tag in sorted(light_matches):
        score += 1.25
        reasons.append(f"lighting match: {tag}")

    risk_matches = risks & set(definition.get("risk_tags", []))
    for tag in sorted(risk_matches):
        score += 0.75
        reasons.append(f"protects: {tag}")

    clipping = float(metrics.get("highlight_clip_fraction", 0.0))
    if clipping > 0.01 and look_id in {
        "market_rich",
        "market_resolve",
        "filmic",
        "soft_portrait",
    }:
        score -= 0.5 if look_id not in {"market_rich", "market_resolve"} else 1.0
        reasons.append("lowered because highlights clip easily")

    if look_id == "cool_editorial" and ({"overcast", "mixed", "night"} & light):
        score += 0.3
        reasons.append("supports controlled urban coolness")

    if look_id in {"market_rich", "market_resolve"} and ({"market", "food"} & scene):
        score += 0.5
        reasons.append("lifts market and food color")

    if look_id == "soft_portrait" and ({"portrait", "people"} & scene) and "skin" in risks:
        score += 0.75
        reasons.append("protects skin in a people-led frame")

    if "dominant_color" in risks and look_id in {
        "market_rich",
        "market_resolve",
        "natural_warm",
        "warm_human",
    }:
        score -= 0.5
        reasons.append("lowered because the source already has a dominant color")

    if "neutral_white" in risks and look_id in {"cool_editorial", "filmic"}:
        score -= 0.25
        reasons.append("lowered to protect neutral whites")

    if not reasons:
        reasons.append(
            "neutral reference" if look_id == "documentary" else "general-purpose alternative"
        )
    return round(score, 6), reasons


def recommend_looks(
    metrics: dict[str, Any],
    scouting: dict[str, Any],
    looks: dict[str, dict[str, Any]],
    count: int = 4,
    neutral_id: str = "documentary",
) -> list[dict[str, Any]]:
    if count < 1 or count > len(looks):
        raise ValueError(f"count must be between 1 and {len(looks)}")

    order = {look_id: index for index, look_id in enumerate(looks)}
    scored: list[dict[str, Any]] = []
    for look_id, definition in looks.items():
        score, reasons = _score_look(look_id, definition, metrics, scouting)
        scored.append(
            {
                "id": look_id,
                "name": definition["name"],
                "score": score,
                "reasons": reasons,
            }
        )
    scored.sort(key=lambda item: (-item["score"], order[item["id"]]))

    selected = scored[:count]
    if not any(item["id"] == neutral_id for item in selected):
        neutral = next(
            (item for item in scored if item["id"] == neutral_id),
            scored[0],
        )
        selected = selected[: count - 1] + [neutral]
        selected.sort(key=lambda item: (-item["score"], order[item["id"]]))

    for rank, item in enumerate(selected, start=1):
        item["rank"] = rank
    return selected
