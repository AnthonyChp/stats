from __future__ import annotations
import logging
from .models import RawStats, RoleBaseline, ComponentBreakdown, OogScoreResult
from .normalize import normalize_percentile
from .components import resolve_component
from .weights import ROLE_WEIGHTS, WIN_BONUS, LOSS_PENALTY, CLUTCH_BONUS, grade_from_score, ROLE_CONFIDENCE_THRESHOLD

log = logging.getLogger(__name__)

def compute_oogscore(raw: RawStats, baseline: RoleBaseline) -> OogScoreResult:
    if raw.duration_min < 5:
        return OogScoreResult(
            score=0, grade="D", role=raw.role, components={}, modifiers={},
            baseline_source="none", sample_size_used=0,
            is_scorable=False, not_scorable_reason="remake",
        )

    if baseline.source == "no_baseline":
        return OogScoreResult(
            score=0, grade="D", role=raw.role, components={}, modifiers={},
            baseline_source="none", sample_size_used=0,
            is_scorable=False, not_scorable_reason="no_baseline",
        )

    weights = ROLE_WEIGHTS.get(raw.role, {})
    components: dict[str, ComponentBreakdown] = {}
    active_weights: dict[str, float] = {}

    for code, weight in weights.items():
        if weight == 0.0:
            continue
        raw_value, dist, pre_normalized = resolve_component(code, raw, baseline)
        if raw_value is None and pre_normalized is None:
            continue
        if pre_normalized is not None:
            normalized = max(0.0, min(1.0, pre_normalized))
        elif dist is not None:
            normalized = normalize_percentile(raw_value, dist)
        else:
            continue
        components[code] = ComponentBreakdown(
            code=code, raw_value=raw_value, normalized=normalized, weight=weight, contribution=0.0
        )
        active_weights[code] = weight

    total_w = sum(active_weights.values()) or 1.0
    base_score = 0.0
    for code, cb in components.items():
        eff_weight = active_weights[code] / total_w
        cb.weight = eff_weight
        cb.contribution = cb.normalized * eff_weight * 100.0
        base_score += cb.contribution

    modifiers: dict[str, float] = {}
    modifiers["win"] = WIN_BONUS if raw.win else LOSS_PENALTY
    if raw.penta >= 1:
        modifiers["clutch"] = CLUTCH_BONUS

    score = max(0.0, min(100.0, base_score + sum(modifiers.values())))
    low_conf = baseline.sample_size < ROLE_CONFIDENCE_THRESHOLD

    return OogScoreResult(
        score=round(score, 1),
        grade=grade_from_score(score),
        role=raw.role,
        components=components,
        modifiers=modifiers,
        baseline_source=baseline.source,
        sample_size_used=baseline.sample_size,
        is_scorable=True,
        low_confidence=low_conf,
    )
