from __future__ import annotations
from typing import Optional, Tuple
from .models import RawStats, RoleBaseline, StatDistribution
from .normalize import normalize_percentile

def _get_dist(baseline: RoleBaseline, key: str) -> Optional[StatDistribution]:
    return baseline.distributions.get(key)

def resolve_component(
    code: str, raw: RawStats, baseline: RoleBaseline
) -> Tuple[Optional[float], Optional[StatDistribution], Optional[float]]:
    """
    Return (raw_value, distribution, pre_normalized).
    - If pre_normalized is not None, use it directly (skip normalize_percentile).
    - If distribution is None and pre_normalized is None, component is unavailable.
    """
    if code == "KDA":
        return raw.kda, _get_dist(baseline, "kda"), None
    if code == "KP":
        return raw.kill_participation, _get_dist(baseline, "kill_participation"), None
    if code == "DMG":
        d_share = _get_dist(baseline, "team_damage_pct")
        d_abs = _get_dist(baseline, "dmg_per_min")
        if d_share is None and d_abs is None:
            return raw.team_damage_pct, None, None
        n_share = normalize_percentile(raw.team_damage_pct, d_share) if d_share else 0.5
        n_abs = normalize_percentile(raw.dmg_per_min, d_abs) if d_abs else 0.5
        return raw.team_damage_pct, None, 0.6 * n_share + 0.4 * n_abs
    if code == "ECO":
        d_gold = _get_dist(baseline, "gold_per_min")
        d_cs = _get_dist(baseline, "cs_per_min")
        if d_gold is None and d_cs is None:
            return raw.gold_per_min, None, None
        n_gold = normalize_percentile(raw.gold_per_min, d_gold) if d_gold else 0.5
        n_cs = normalize_percentile(raw.cs_per_min, d_cs) if d_cs else 0.5
        return raw.gold_per_min, None, 0.5 * n_gold + 0.5 * n_cs
    if code == "OBJ":
        return raw.obj_participation, _get_dist(baseline, "obj_participation"), None
    if code == "VIS":
        return raw.vision_per_min, _get_dist(baseline, "vision_per_min"), None
    if code == "UTL":
        return raw.heal_shield, _get_dist(baseline, "heal_shield"), None
    if code == "LANE":
        return raw.lane_cs_adv, _get_dist(baseline, "lane_cs_adv"), None
    if code == "CC":
        return raw.cc_score_per_min, _get_dist(baseline, "cc_score_per_min"), None
    return None, None, None
