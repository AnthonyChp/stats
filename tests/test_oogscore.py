"""Unit tests for OogScore v2."""
import pytest
from oogway.oogscore.weights import ROLE_WEIGHTS, grade_from_score
from oogway.oogscore.normalize import normalize_percentile
from oogway.oogscore.models import StatDistribution, RawStats, RoleBaseline
from oogway.oogscore.engine import compute_oogscore
from oogway.oogscore.extract import normalize_role, from_participant

# ── Weights invariant ────────────────────────────────────────────────────────

def test_weights_sum_to_one():
    for role, weights in ROLE_WEIGHTS.items():
        total = sum(weights.values())
        assert abs(total - 1.0) < 1e-9, f"{role} weights sum to {total}"

# ── Grade thresholds ─────────────────────────────────────────────────────────

def test_grade_thresholds():
    assert grade_from_score(90) == "S"
    assert grade_from_score(85) == "S"
    assert grade_from_score(72) == "A"
    assert grade_from_score(58) == "B"
    assert grade_from_score(42) == "C"
    assert grade_from_score(10) == "D"

# ── Normalize percentile ─────────────────────────────────────────────────────

def test_normalize_median():
    dist = StatDistribution(mean=100.0, std=20.0, p10=0, p25=0, p50=100, p75=0, p90=0, sample_size=1000)
    assert abs(normalize_percentile(100.0, dist) - 0.5) < 0.01

def test_normalize_zero_std():
    dist = StatDistribution(mean=50.0, std=0.0, p10=0, p25=0, p50=50, p75=0, p90=0, sample_size=10)
    result = normalize_percentile(50.0, dist)
    assert 0.0 <= result <= 1.0

def test_normalize_clamp():
    dist = StatDistribution(mean=100.0, std=10.0, p10=0, p25=0, p50=100, p75=0, p90=0, sample_size=100)
    assert normalize_percentile(-1000.0, dist) >= 0.0
    assert normalize_percentile(10000.0, dist) <= 1.0

# ── Role normalization ────────────────────────────────────────────────────────

def test_normalize_role_basic():
    assert normalize_role("TOP") == "TOP"
    assert normalize_role("JUNGLE") == "JUNGLE"
    assert normalize_role("MIDDLE") == "MID"
    assert normalize_role("BOTTOM") == "ADC"
    assert normalize_role("UTILITY") == "SUPPORT"

def test_normalize_role_fallback():
    assert normalize_role("", "MIDDLE") == "MID"
    assert normalize_role("", "") is None

# ── from_participant ──────────────────────────────────────────────────────────

SAMPLE_PARTICIPANT = {
    "puuid": "abc",
    "teamPosition": "MIDDLE",
    "individualPosition": "MIDDLE",
    "championName": "Lux",
    "win": True,
    "kills": 8,
    "deaths": 2,
    "assists": 10,
    "totalDamageDealtToChampions": 25000,
    "totalDamageTaken": 12000,
    "goldEarned": 12000,
    "totalMinionsKilled": 180,
    "neutralMinionsKilled": 10,
    "visionScore": 35,
    "totalHealsOnTeammates": 0,
    "totalDamageShieldedOnTeammates": 0,
    "timeCCingOthers": 30,
    "pentaKills": 0,
    "dragonKills": 1,
    "baronKills": 0,
    "turretKills": 2,
    "challenges": {
        "kda": 9.0,
        "killParticipation": 0.72,
        "teamDamagePercentage": 0.30,
        "damageTakenOnTeamPercentage": 0.18,
        "damagePerMinute": 800.0,
        "goldPerMinute": 385.0,
        "visionScorePerMinute": 1.1,
        "effectiveHealAndShielding": 0,
        "maxCsAdvantageOnLaneOpponent": 25.0,
        "dragonTakedowns": 1,
        "baronTakedowns": 0,
        "riftHeraldTakedowns": 0,
        "turretTakedowns": 2,
    }
}

def test_from_participant_basic():
    raw = from_participant(SAMPLE_PARTICIPANT, game_duration_seconds=1950)  # 32.5 min
    assert raw is not None
    assert raw.role == "MID"
    assert raw.champion == "Lux"
    assert raw.win is True
    assert raw.duration_min == pytest.approx(32.5, rel=0.01)

def test_from_participant_remake():
    raw = from_participant(SAMPLE_PARTICIPANT, game_duration_seconds=180)  # 3 min
    assert raw is None

def test_from_participant_ms_duration():
    raw = from_participant(SAMPLE_PARTICIPANT, game_duration_seconds=1950000)  # in ms
    assert raw is not None
    assert raw.duration_min == pytest.approx(32.5, rel=0.01)

# ── Engine ────────────────────────────────────────────────────────────────────

def _make_baseline(role: str = "MID") -> RoleBaseline:
    from oogway.oogscore.models import StatDistribution as SD
    neutral = SD(mean=5.0, std=2.0, p10=2, p25=3, p50=5, p75=7, p90=8, sample_size=500)
    return RoleBaseline(
        distributions={
            "kda": neutral,
            "kill_participation": SD(mean=0.6, std=0.15, p10=0.35, p25=0.5, p50=0.6, p75=0.72, p90=0.82, sample_size=500),
            "team_damage_pct": SD(mean=0.22, std=0.08, p10=0.10, p25=0.16, p50=0.22, p75=0.28, p90=0.34, sample_size=500),
            "dmg_per_min": SD(mean=600.0, std=200.0, p10=300, p25=450, p50=600, p75=750, p90=900, sample_size=500),
            "gold_per_min": SD(mean=350.0, std=60.0, p10=260, p25=310, p50=350, p75=390, p90=430, sample_size=500),
            "cs_per_min": SD(mean=6.0, std=1.5, p10=4, p25=5, p50=6, p75=7, p90=8, sample_size=500),
            "vision_per_min": SD(mean=0.9, std=0.3, p10=0.5, p25=0.7, p50=0.9, p75=1.1, p90=1.4, sample_size=500),
            "heal_shield": SD(mean=10.0, std=8.0, p10=0, p25=5, p50=10, p75=15, p90=25, sample_size=500),
            "cc_score_per_min": SD(mean=0.5, std=0.4, p10=0, p25=0.1, p50=0.5, p75=0.8, p90=1.2, sample_size=500),
            "obj_participation": SD(mean=3.0, std=2.0, p10=0, p25=1, p50=3, p75=5, p90=7, sample_size=500),
            "lane_cs_adv": SD(mean=5.0, std=20.0, p10=-20, p25=-5, p50=5, p75=15, p90=30, sample_size=500),
            "damage_taken_pct": SD(mean=0.2, std=0.08, p10=0.1, p25=0.15, p50=0.2, p75=0.25, p90=0.32, sample_size=500),
        },
        source="role_fallback",
        sample_size=500,
    )

def test_engine_deterministic():
    raw = from_participant(SAMPLE_PARTICIPANT, game_duration_seconds=1950)
    baseline = _make_baseline("MID")
    r1 = compute_oogscore(raw, baseline)
    r2 = compute_oogscore(raw, baseline)
    assert r1.score == r2.score

def test_engine_score_range():
    raw = from_participant(SAMPLE_PARTICIPANT, game_duration_seconds=1950)
    baseline = _make_baseline("MID")
    result = compute_oogscore(raw, baseline)
    assert result.is_scorable
    assert 0.0 <= result.score <= 100.0

def test_engine_grade_set():
    raw = from_participant(SAMPLE_PARTICIPANT, game_duration_seconds=1950)
    baseline = _make_baseline("MID")
    result = compute_oogscore(raw, baseline)
    assert result.grade in ("S", "A", "B", "C", "D")

def test_engine_remake_not_scorable():
    from oogway.oogscore.models import RawStats
    raw = RawStats(
        role="MID", champion="Lux", win=True, duration_min=3.0,
        kda=5.0, kill_participation=0.5, team_damage_pct=0.25,
        damage_taken_pct=0.2, dmg_per_min=500, gold_per_min=300,
        cs_per_min=5, vision_per_min=0.8, heal_shield=0, cc_score_per_min=0.3,
        obj_participation=2, lane_cs_adv=10, penta=0,
    )
    baseline = _make_baseline("MID")
    result = compute_oogscore(raw, baseline)
    assert not result.is_scorable

def test_engine_missing_component_renormalizes():
    """If a component's distribution is missing, weights must still sum."""
    raw = from_participant(SAMPLE_PARTICIPANT, game_duration_seconds=1950)
    baseline = _make_baseline("MID")
    # Remove LANE distribution to simulate missing component
    del baseline.distributions["lane_cs_adv"]
    result = compute_oogscore(raw, baseline)
    assert result.is_scorable
    total_w = sum(cb.weight for cb in result.components.values())
    assert abs(total_w - 1.0) < 1e-6
