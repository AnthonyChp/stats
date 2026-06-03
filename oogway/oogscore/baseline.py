from __future__ import annotations
import json
import logging
import statistics
from datetime import datetime
from typing import Dict, Optional
from .models import StatDistribution, RoleBaseline
from .weights import (
    ROLE_WEIGHTS, ROLE_CONFIDENCE_THRESHOLD,
    CHAMPION_CONFIDENCE_THRESHOLD, CHAMPION_FULL_CONFIDENCE_N
)

log = logging.getLogger(__name__)

STAT_KEYS = [
    "kda", "kill_participation", "team_damage_pct", "dmg_per_min",
    "gold_per_min", "cs_per_min", "vision_per_min", "heal_shield",
    "cc_score_per_min", "obj_participation", "lane_cs_adv", "damage_taken_pct",
]

def _compute_distribution(values: list[float]) -> StatDistribution:
    n = len(values)
    if n == 0:
        return StatDistribution(0, 1e-6, 0, 0, 0, 0, 0, 0)
    sv = sorted(values)
    mean = statistics.mean(sv)
    std = statistics.pstdev(sv) if n > 1 else 1e-6
    std = max(std, 1e-6)
    def pct(p): return sv[min(n - 1, int(p / 100 * n))]
    return StatDistribution(
        mean=mean, std=std,
        p10=pct(10), p25=pct(25), p50=pct(50), p75=pct(75), p90=pct(90),
        sample_size=n,
    )

def _stat_from_row(row, duration_min: float) -> dict[str, float]:
    ch = {}
    if row.challenges_json:
        try:
            ch = json.loads(row.challenges_json)
        except Exception:
            pass
    dm = max(duration_min, 1.0)
    kda_val = ch.get("kda", (row.kills + row.assists) / max(1, row.deaths))
    kp_val = float(ch.get("killParticipation", 0.0))
    team_dmg_pct = float(ch.get("teamDamagePercentage", 0.0))
    dmg_taken_pct = float(ch.get("damageTakenOnTeamPercentage", 0.0))
    dmg_pm = float(ch.get("damagePerMinute", row.total_damage_champ / dm))
    gold_pm = float(ch.get("goldPerMinute", row.gold_earned / dm))
    cs_pm = row.cs_total / dm
    vis_pm = float(ch.get("visionScorePerMinute", row.vision_score / dm))
    heal_raw = row.heals_on_teammates + row.shields_on_teammates
    heal_shield = float(ch.get("effectiveHealAndShielding", heal_raw)) / dm
    cc_pm = row.time_ccing_others / dm
    obj = (
        float(ch.get("dragonTakedowns", row.dragon_kills)) +
        float(ch.get("baronTakedowns", row.baron_kills)) +
        float(ch.get("riftHeraldTakedowns", 0)) +
        float(ch.get("turretTakedowns", row.turret_kills))
    )
    lane_adv = float(ch.get("maxCsAdvantageOnLaneOpponent", 0.0))
    return {
        "kda": float(kda_val),
        "kill_participation": kp_val,
        "team_damage_pct": team_dmg_pct,
        "damage_taken_pct": dmg_taken_pct,
        "dmg_per_min": dmg_pm,
        "gold_per_min": gold_pm,
        "cs_per_min": cs_pm,
        "vision_per_min": vis_pm,
        "heal_shield": heal_shield,
        "cc_score_per_min": cc_pm,
        "obj_participation": obj,
        "lane_cs_adv": lane_adv,
    }

def build_baseline(session) -> Dict[str, dict]:
    """
    Build baseline distributions from all scorable MatchParticipant rows.
    Returns a dict keyed by scope string:
      "role:TOP" -> {stat_key: StatDistribution}
      "champ:TOP:Garen" -> {stat_key: StatDistribution}
    """
    from oogway.database import MatchParticipant
    rows = session.query(MatchParticipant).filter(
        MatchParticipant.is_scorable == True,
        MatchParticipant.duration_min >= 5,
        MatchParticipant.role != None,
    ).all()

    by_role: dict[str, dict[str, list[float]]] = {}
    by_champ: dict[tuple[str, str], dict[str, list[float]]] = {}

    for row in rows:
        if not row.role:
            continue
        dm = row.duration_min or 1.0
        stats = _stat_from_row(row, dm)
        role = row.role
        champ = row.champion or ""

        if role not in by_role:
            by_role[role] = {k: [] for k in STAT_KEYS}
        for k, v in stats.items():
            by_role[role][k].append(v)

        key = (role, champ)
        if key not in by_champ:
            by_champ[key] = {k: [] for k in STAT_KEYS}
        for k, v in stats.items():
            by_champ[key][k].append(v)

    result: Dict[str, dict] = {}
    for role, stat_lists in by_role.items():
        scope = f"role:{role}"
        result[scope] = {k: _compute_distribution(v) for k, v in stat_lists.items()}

    for (role, champ), stat_lists in by_champ.items():
        n = len(next(iter(stat_lists.values()), []))
        if n < CHAMPION_CONFIDENCE_THRESHOLD:
            continue
        scope = f"champ:{role}:{champ}"
        result[scope] = {k: _compute_distribution(v) for k, v in stat_lists.items()}

    return result

def save_baseline_cache(session, distributions: Dict[str, dict]):
    from oogway.database import BaselineCache
    import json as _json
    now = datetime.utcnow()
    session.query(BaselineCache).delete()
    for scope, dists in distributions.items():
        serialized = {k: {
            "mean": d.mean, "std": d.std,
            "p10": d.p10, "p25": d.p25, "p50": d.p50, "p75": d.p75, "p90": d.p90,
            "sample_size": d.sample_size,
        } for k, d in dists.items()}
        row = BaselineCache(
            scope=scope,
            distributions_json=_json.dumps(serialized),
            sample_size=next(iter(dists.values())).sample_size if dists else 0,
            computed_at=now,
        )
        session.add(row)
    session.commit()
    log.info("Baseline cache saved: %d scopes", len(distributions))

def load_baseline_cache(session) -> Dict[str, dict[str, StatDistribution]]:
    from oogway.database import BaselineCache
    import json as _json
    rows = session.query(BaselineCache).all()
    result = {}
    for row in rows:
        if not row.distributions_json:
            continue
        dists = _json.loads(row.distributions_json)
        result[row.scope] = {
            k: StatDistribution(**v) for k, v in dists.items()
        }
    return result

def _blended(champ_dists, role_dists, n_champ):
    w = min(1.0, n_champ / CHAMPION_FULL_CONFIDENCE_N)
    blended = {}
    for k in role_dists:
        if k not in champ_dists:
            blended[k] = role_dists[k]
            continue
        cd, rd = champ_dists[k], role_dists[k]
        blended[k] = StatDistribution(
            mean = w * cd.mean + (1-w) * rd.mean,
            std  = max(w * cd.std  + (1-w) * rd.std, 1e-6),
            p10  = w * cd.p10  + (1-w) * rd.p10,
            p25  = w * cd.p25  + (1-w) * rd.p25,
            p50  = w * cd.p50  + (1-w) * rd.p50,
            p75  = w * cd.p75  + (1-w) * rd.p75,
            p90  = w * cd.p90  + (1-w) * rd.p90,
            sample_size = cd.sample_size,
        )
    return blended

def load_for(
    role: str,
    champion: str,
    cache: Dict[str, dict[str, StatDistribution]],
) -> Optional[RoleBaseline]:
    role_key = f"role:{role}"
    champ_key = f"champ:{role}:{champion}"

    role_dists = cache.get(role_key)
    champ_dists = cache.get(champ_key)

    if role_dists is None:
        return RoleBaseline(distributions={}, source="no_baseline", sample_size=0)

    role_n = next(iter(role_dists.values())).sample_size if role_dists else 0

    if champ_dists is None:
        return RoleBaseline(
            distributions=role_dists,
            source="role_fallback",
            sample_size=role_n,
        )

    champ_n = next(iter(champ_dists.values())).sample_size if champ_dists else 0
    blended = _blended(champ_dists, role_dists, champ_n)
    source = "champion" if champ_n >= CHAMPION_FULL_CONFIDENCE_N else "blended"
    return RoleBaseline(distributions=blended, source=source, sample_size=champ_n)
