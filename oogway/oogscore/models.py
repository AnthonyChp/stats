from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class RawStats:
    role: str
    champion: str
    win: bool
    duration_min: float
    kda: float
    kill_participation: float
    team_damage_pct: float
    damage_taken_pct: float
    dmg_per_min: float
    gold_per_min: float
    cs_per_min: float
    vision_per_min: float
    heal_shield: float
    cc_score_per_min: float
    obj_participation: float
    lane_cs_adv: float
    penta: int

@dataclass
class StatDistribution:
    mean: float
    std: float
    p10: float
    p25: float
    p50: float
    p75: float
    p90: float
    sample_size: int

@dataclass
class RoleBaseline:
    distributions: dict[str, StatDistribution]
    source: str          # "champion" | "role_fallback" | "blended"
    sample_size: int

@dataclass
class ComponentBreakdown:
    code: str
    raw_value: float
    normalized: float
    weight: float
    contribution: float

@dataclass
class OogScoreResult:
    score: float
    grade: str
    role: str
    components: dict[str, ComponentBreakdown]
    modifiers: dict[str, float]
    baseline_source: str
    sample_size_used: int
    is_scorable: bool
    low_confidence: bool = False
    not_scorable_reason: str = ""
