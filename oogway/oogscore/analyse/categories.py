from __future__ import annotations
from oogway.oogscore.weights import ROLE_WEIGHTS

CATEGORIES: dict[str, list[str]] = {
    "COMBAT":     ["KDA", "KP", "DMG", "CC"],
    "ECONOMIE":   ["ECO", "LANE"],
    "VISION_MAP": ["VIS", "OBJ", "UTL"],
}

CAT_LABELS = {
    "COMBAT":     "⚔️ Combat",
    "ECONOMIE":   "💰 Économie",
    "VISION_MAP": "👁️ Vision & Map",
}

def visible_categories(role: str) -> list[str]:
    """Return categories that have at least one component with non-zero weight for this role."""
    weights = ROLE_WEIGHTS.get(role, {})
    return [
        cat for cat, comps in CATEGORIES.items()
        if any(weights.get(c, 0) > 0 for c in comps)
    ]

def category_score(cat: str, role: str, component_percentiles: dict[str, float]) -> float | None:
    """
    Weighted average percentile of components in this category for this role.
    Uses ROLE_WEIGHTS renormalized within the category.
    Returns None if all components have zero weight (category invisible for this role).
    """
    weights = ROLE_WEIGHTS.get(role, {})
    comps = CATEGORIES[cat]
    active = {c: weights[c] for c in comps if weights.get(c, 0) > 0 and c in component_percentiles}
    if not active:
        return None
    total_w = sum(active.values())
    return sum(component_percentiles[c] * w / total_w for c, w in active.items())

def visible_axes(role: str) -> list[str]:
    """Return component codes that have non-zero weight for this role, in AXIS_ORDER."""
    AXIS_ORDER = ["KDA", "KP", "DMG", "CC", "ECO", "LANE", "OBJ", "VIS", "UTL"]
    weights = ROLE_WEIGHTS.get(role, {})
    return [a for a in AXIS_ORDER if weights.get(a, 0) > 0]
