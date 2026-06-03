from __future__ import annotations
import logging
from typing import Any, Dict, Optional
from .models import RawStats

log = logging.getLogger(__name__)

ROLE_MAP = {
    "TOP": "TOP",
    "JUNGLE": "JUNGLE",
    "MIDDLE": "MID",
    "MID": "MID",
    "BOTTOM": "ADC",
    "ADC": "ADC",
    "UTILITY": "SUPPORT",
    "SUPPORT": "SUPPORT",
}

def normalize_role(team_position: str, individual_position: str = "") -> Optional[str]:
    role = ROLE_MAP.get(team_position.upper())
    if role:
        return role
    role = ROLE_MAP.get(individual_position.upper())
    return role  # None if still unresolved

def from_participant(
    participant: Dict[str, Any],
    game_duration_seconds: int,
    linked_puuids: set[str] | None = None,
) -> Optional[RawStats]:
    """
    Convert a Match-V5 participant dict to RawStats.
    Returns None if the match is not scorable (remake, unknown role).
    """
    # Duration guard
    if game_duration_seconds > 10_000:
        game_duration_seconds = game_duration_seconds // 1000
    duration_min = max(1.0, game_duration_seconds / 60.0)

    if duration_min < 5:
        log.debug("Remake detected (duration_min=%.1f), not scorable", duration_min)
        return None

    # Role resolution
    team_pos = participant.get("teamPosition", "") or ""
    indiv_pos = participant.get("individualPosition", "") or ""
    role = normalize_role(team_pos, indiv_pos)
    if not role:
        log.debug("Cannot determine role for participant %s", participant.get("puuid", "?"))
        return None

    ch = participant.get("challenges", {}) or {}

    # KDA
    kills = participant.get("kills", 0)
    deaths = participant.get("deaths", 0)
    assists = participant.get("assists", 0)
    kda = ch.get("kda", (kills + assists) / max(1, deaths))

    # Kill participation
    kp = float(ch.get("killParticipation", 0.0))

    # Damage
    team_dmg_pct = float(ch.get("teamDamagePercentage", 0.0))
    dmg_taken_pct = float(ch.get("damageTakenOnTeamPercentage", 0.0))
    raw_dmg = participant.get("totalDamageDealtToChampions", 0)
    dmg_per_min = float(ch.get("damagePerMinute", raw_dmg / duration_min))

    # Economy
    gold = participant.get("goldEarned", 0)
    gold_per_min = float(ch.get("goldPerMinute", gold / duration_min))
    cs = participant.get("totalMinionsKilled", 0) + participant.get("neutralMinionsKilled", 0)
    cs_per_min = cs / duration_min

    # Vision
    vision = participant.get("visionScore", 0)
    vision_per_min = float(ch.get("visionScorePerMinute", vision / duration_min))

    # Heal/shield utility
    heal_raw = participant.get("totalHealsOnTeammates", 0) + participant.get("totalDamageShieldedOnTeammates", 0)
    heal_shield = float(ch.get("effectiveHealAndShielding", heal_raw)) / duration_min

    # CC
    cc_raw = participant.get("timeCCingOthers", 0)
    cc_score_per_min = cc_raw / duration_min

    # Objectives participation
    obj = (
        float(ch.get("dragonTakedowns", participant.get("dragonKills", 0))) +
        float(ch.get("baronTakedowns", participant.get("baronKills", 0))) +
        float(ch.get("riftHeraldTakedowns", 0)) +
        float(ch.get("turretTakedowns", participant.get("turretKills", 0)))
    )

    # Lane CS advantage
    lane_cs_adv = float(ch.get("maxCsAdvantageOnLaneOpponent", 0.0))

    penta = participant.get("pentaKills", 0)

    return RawStats(
        role=role,
        champion=participant.get("championName", ""),
        win=bool(participant.get("win", False)),
        duration_min=duration_min,
        kda=float(kda),
        kill_participation=kp,
        team_damage_pct=team_dmg_pct,
        damage_taken_pct=dmg_taken_pct,
        dmg_per_min=dmg_per_min,
        gold_per_min=gold_per_min,
        cs_per_min=cs_per_min,
        vision_per_min=vision_per_min,
        heal_shield=heal_shield,
        cc_score_per_min=cc_score_per_min,
        obj_participation=obj,
        lane_cs_adv=lane_cs_adv,
        penta=penta,
    )

def participant_to_db_fields(
    participant: Dict[str, Any],
    game_duration_seconds: int,
    match_id: str,
    linked_puuids: set[str] | None = None,
) -> Dict[str, Any]:
    """
    Extract all fields needed to insert a MatchParticipant row.
    """
    import json
    if game_duration_seconds > 10_000:
        game_duration_seconds = game_duration_seconds // 1000
    duration_min = max(1.0, game_duration_seconds / 60.0)

    team_pos = participant.get("teamPosition", "") or ""
    indiv_pos = participant.get("individualPosition", "") or ""
    role = normalize_role(team_pos, indiv_pos)

    is_scorable = role is not None and duration_min >= 5
    challenges = participant.get("challenges", {}) or {}

    puuid = participant.get("puuid", "")
    return {
        "match_id": match_id,
        "puuid": puuid,
        "is_linked_member": (puuid in linked_puuids) if linked_puuids else False,
        "role": role,
        "champion": participant.get("championName", ""),
        "win": bool(participant.get("win", False)),
        "kills": participant.get("kills", 0),
        "deaths": participant.get("deaths", 0),
        "assists": participant.get("assists", 0),
        "total_damage_champ": participant.get("totalDamageDealtToChampions", 0),
        "total_damage_taken": participant.get("totalDamageTaken", 0),
        "gold_earned": participant.get("goldEarned", 0),
        "cs_total": participant.get("totalMinionsKilled", 0) + participant.get("neutralMinionsKilled", 0),
        "vision_score": participant.get("visionScore", 0),
        "heals_on_teammates": participant.get("totalHealsOnTeammates", 0),
        "shields_on_teammates": participant.get("totalDamageShieldedOnTeammates", 0),
        "time_ccing_others": participant.get("timeCCingOthers", 0),
        "penta_kills": participant.get("pentaKills", 0),
        "dragon_kills": participant.get("dragonKills", 0),
        "baron_kills": participant.get("baronKills", 0),
        "turret_kills": participant.get("turretKills", 0),
        "challenges_json": json.dumps(challenges),
        "duration_min": duration_min,
        "is_scorable": is_scorable,
    }
