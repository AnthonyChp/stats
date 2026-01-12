# oogway/services/chi.py
from __future__ import annotations
from typing import Sequence

from oogway import champ_meta as cm

def _score(team: Sequence[str], enemies: Sequence[str]) -> float:
    """Win-rate moyen +1 pt par counter direct."""
    if not team:  # aucun pick → baseline 50 %
        return 50.0

    base = sum(cm.meta(c).get("winrate", 50) for c in team) / len(team)
    bonus = sum(any(c in cm.meta(e).get("counters", []) for e in enemies) for c in team)
    return base + bonus

def predict(picks_a: list[str], picks_b: list[str]) -> tuple[float, float]:
    a, b = _score(picks_a, picks_b), _score(picks_b, picks_a)
    tot = a + b
    return round(a / tot * 100, 1), round(b / tot * 100, 1)

def bar(blue_pct: float, blocks: int = 20) -> str:
    blue = round(blocks * blue_pct / 100)
    red  = blocks - blue
    return "🟦" * blue + "🟥" * red