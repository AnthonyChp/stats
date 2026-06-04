from __future__ import annotations
import json
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

STAT_KEYS_MAP = {
    "kda":               lambda ch, row, dm: float(ch.get("kda", (row.kills + row.assists) / max(1, row.deaths))),
    "kill_participation":lambda ch, row, dm: float(ch.get("killParticipation", 0.0)),
    "team_damage_pct":   lambda ch, row, dm: float(ch.get("teamDamagePercentage", 0.0)),
    "damage_taken_pct":  lambda ch, row, dm: float(ch.get("damageTakenOnTeamPercentage", 0.0)),
    "dmg_per_min":       lambda ch, row, dm: float(ch.get("damagePerMinute", row.total_damage_champ / dm)),
    "gold_per_min":      lambda ch, row, dm: float(ch.get("goldPerMinute", row.gold_earned / dm)),
    "cs_per_min":        lambda ch, row, dm: row.cs_total / dm,
    "vision_per_min":    lambda ch, row, dm: float(ch.get("visionScorePerMinute", row.vision_score / dm)),
    "heal_shield":       lambda ch, row, dm: float(ch.get("effectiveHealAndShielding", row.heals_on_teammates + row.shields_on_teammates)) / dm,
    "cc_score_per_min":  lambda ch, row, dm: row.time_ccing_others / dm,
    "obj_participation": lambda ch, row, dm: (
        float(ch.get("dragonTakedowns", row.dragon_kills)) +
        float(ch.get("baronTakedowns", row.baron_kills)) +
        float(ch.get("riftHeraldTakedowns", 0)) +
        float(ch.get("turretTakedowns", row.turret_kills))
    ),
    "lane_cs_adv":       lambda ch, row, dm: float(ch.get("maxCsAdvantageOnLaneOpponent", 0.0)),
}

# Map from OogScore component code to baseline stat key
COMP_TO_STAT = {
    "KDA":  "kda",
    "KP":   "kill_participation",
    "DMG":  "team_damage_pct",   # primary (share); dmg_per_min is secondary
    "ECO":  "gold_per_min",      # primary; cs_per_min is secondary
    "OBJ":  "obj_participation",
    "VIS":  "vision_per_min",
    "UTL":  "heal_shield",
    "LANE": "lane_cs_adv",
    "CC":   "cc_score_per_min",
}

@dataclass
class PlayerAggregate:
    champion: str
    role: str
    puuid: str
    n_games: int
    avg_stats: dict[str, float]   # stat_key -> average raw value
    avg_score: float
    score_history: list[float]    # chronological OogScores (oldest first)


def get_player_aggregate(session, puuid: str, champion: str, role: str) -> PlayerAggregate | None:
    """
    Compute average raw stats for a player on (champion, role) from MatchParticipant.
    Returns None if no scorable games found.
    """
    from oogway.database import MatchParticipant, OogScoreRecord

    rows = session.query(MatchParticipant).filter(
        MatchParticipant.puuid == puuid,
        MatchParticipant.champion == champion,
        MatchParticipant.role == role,
        MatchParticipant.is_scorable == True,
    ).all()

    if not rows:
        return None

    stat_sums: dict[str, float] = {k: 0.0 for k in STAT_KEYS_MAP}
    for row in rows:
        ch = {}
        if row.challenges_json:
            try:
                ch = json.loads(row.challenges_json)
            except Exception:
                pass
        dm = max(row.duration_min or 1.0, 1.0)
        for k, fn in STAT_KEYS_MAP.items():
            try:
                stat_sums[k] += fn(ch, row, dm)
            except Exception:
                pass

    n = len(rows)
    avg_stats = {k: v / n for k, v in stat_sums.items()}

    # OogScore history (chronological)
    score_records = (
        session.query(OogScoreRecord)
        .filter(
            OogScoreRecord.puuid == puuid,
            OogScoreRecord.role == role,
        )
        .order_by(OogScoreRecord.computed_at)
        .all()
    )
    # Filter to this champion via match_id join would be complex; use all role scores for the curve
    score_history = [r.score for r in score_records if r.score is not None]

    avg_score = sum(score_history[-n:]) / len(score_history[-n:]) if score_history else 0.0

    return PlayerAggregate(
        champion=champion,
        role=role,
        puuid=puuid,
        n_games=n,
        avg_stats=avg_stats,
        avg_score=avg_score,
        score_history=score_history[-30:],  # last 30 games for the curve
    )


def get_player_component_percentiles(
    agg: PlayerAggregate,
    baseline_dists: dict,  # stat_key -> StatDistribution
) -> dict[str, float]:
    """
    Convert player's average raw stats to percentiles using baseline distributions.
    For DMG: combines team_damage_pct (60%) + dmg_per_min (40%).
    For ECO: combines gold_per_min (50%) + cs_per_min (50%).
    Anti-Jensen: one percentile call on the average stat, not average of percentile calls.
    """
    from oogway.oogscore.normalize import normalize_percentile

    result = {}
    for code, stat_key in COMP_TO_STAT.items():
        if code == "DMG":
            d_share = baseline_dists.get("team_damage_pct")
            d_abs = baseline_dists.get("dmg_per_min")
            if d_share is None and d_abs is None:
                continue
            n_share = normalize_percentile(agg.avg_stats.get("team_damage_pct", 0), d_share) if d_share else 0.5
            n_abs = normalize_percentile(agg.avg_stats.get("dmg_per_min", 0), d_abs) if d_abs else 0.5
            result[code] = 0.6 * n_share + 0.4 * n_abs
        elif code == "ECO":
            d_gold = baseline_dists.get("gold_per_min")
            d_cs = baseline_dists.get("cs_per_min")
            if d_gold is None and d_cs is None:
                continue
            n_gold = normalize_percentile(agg.avg_stats.get("gold_per_min", 0), d_gold) if d_gold else 0.5
            n_cs = normalize_percentile(agg.avg_stats.get("cs_per_min", 0), d_cs) if d_cs else 0.5
            result[code] = 0.5 * n_gold + 0.5 * n_cs
        else:
            dist = baseline_dists.get(stat_key)
            if dist is None:
                continue
            result[code] = normalize_percentile(agg.avg_stats.get(stat_key, 0), dist)
    return result


def get_baseline_component_percentiles(baseline_dists: dict) -> dict[str, float]:
    """
    Returns p50 (median) for each component as a reference point for the radar.
    """
    result = {}
    for code, stat_key in COMP_TO_STAT.items():
        if code == "DMG":
            d_share = baseline_dists.get("team_damage_pct")
            d_abs = baseline_dists.get("dmg_per_min")
            if d_share or d_abs:
                result[code] = 0.5  # median is always 0.5 by definition
        elif code == "ECO":
            d_gold = baseline_dists.get("gold_per_min")
            d_cs = baseline_dists.get("cs_per_min")
            if d_gold or d_cs:
                result[code] = 0.5
        else:
            dist = baseline_dists.get(stat_key)
            if dist is not None:
                result[code] = 0.5  # median = 0.5 by definition of normalize_percentile
    return result
