from __future__ import annotations
from dataclasses import dataclass, field
from oogway.oogscore.weights import ROLE_WEIGHTS

ADVICE_TEXT: dict[tuple[str, str], str] = {
    # TOP
    ("TOP", "KDA"): "Travaille ta survie : évite les duels perdants et sors des trades défavorables.",
    ("TOP", "DMG"): "Sois plus agressif dans tes trades. Cherche à all-in quand tu as l'avantage de niveau.",
    ("TOP", "LANE"): "Focus ton CS early : chaque CS manqué est un écart de gold. Priorise les vagues.",
    ("TOP", "OBJ"): "Rejoins tes mates pour Héraut et Dragon dès que ta lane est push.",
    ("TOP", "CC"): "Utilise ton CC dans les teamfights pour lock les cibles prioritaires.",
    # JUNGLE
    ("JUNGLE", "KDA"): "Choisis mieux tes combats : évite les invades risquées et les ganks en sous-nombre.",
    ("JUNGLE", "KP"): "Gank plus : un gank réussi même sans kill peut créer une pression décisive.",
    ("JUNGLE", "DMG"): "Pense à faire des dégâts en teamfight, pas seulement à contester les objectifs.",
    ("JUNGLE", "OBJ"): "Priorise Dragon et Baron dès qu'ils spawn. Track les timers adverses.",
    ("JUNGLE", "VIS"): "Place des wards river avant chaque objectif pour anticiper le counter-engage.",
    ("JUNGLE", "CC"): "Lance tes CC sur les carries adverses en teamfight pour les neutraliser.",
    # MID
    ("MID", "KDA"): "Sois moins impulsif en 1v1. Attends les cooldowns adverses avant d'engager.",
    ("MID", "KP"): "Roam plus : après avoir push ta vague, aide tes lanes latérales.",
    ("MID", "DMG"): "Cible les carries en teamfight. Tes dégâts sont ta principale valeur ajoutée.",
    ("MID", "LANE"): "Focus le CS sous pression : dernier hit malgré les trades.",
    ("MID", "OBJ"): "Anticipe les rotations Dragon/Baron : tu es le mieux placé géographiquement.",
    # ADC
    ("ADC", "KDA"): "Reste en position arrière. Un ADC mort ne fait aucun dégât.",
    ("ADC", "DMG"): "Attaque en continu en teamfight. Chaque seconde d'auto-attaque compte.",
    ("ADC", "ECO"): "Améliore ton CS : vise 8+ CS/min. Les items font tout ton kit.",
    ("ADC", "LANE"): "Sois plus agressif en lane quand ton support engage. Punish les mistakes.",
    ("ADC", "KP"): "Suis les rotations de ton support. Un ADC isolé est une cible facile.",
    # SUPPORT
    ("SUPPORT", "KP"): "Roam avec ton jungler. Ta présence sur la map crée de la pression.",
    ("SUPPORT", "VIS"): "Place tes wards river et tri-bush en priorité. La vision gagne les games.",
    ("SUPPORT", "UTL"): "Timing tes heals/shields sur les teammates qui engagent, pas après.",
    ("SUPPORT", "OBJ"): "Sois présent à chaque dragon et baron. Ton CC est décisif sur les objectifs.",
    ("SUPPORT", "KDA"): "Reste en vie : un support mort ne peut ni healer ni CC.",
}

@dataclass
class Insights:
    strengths: list[str]
    weaknesses: list[str]
    focus: str | None
    advice: str | None

def pct_to_text(p: float) -> str:
    top = round((1 - p) * 100)
    bottom = round(p * 100)
    if p >= 0.90: return f"top {top}% 🟢"
    if p >= 0.75: return f"top {top}%"
    if p >= 0.45: return "dans la moyenne"
    if p >= 0.25: return f"bottom {bottom}%"
    return f"bottom {bottom}% 🔴"

def generate_insights(player_percentiles: dict[str, float], role: str) -> Insights:
    weights = ROLE_WEIGHTS.get(role, {})
    strengths = [
        c for c, p in player_percentiles.items()
        if p >= 0.75 and weights.get(c, 0) >= 0.10
    ]
    weaknesses = [
        c for c, p in player_percentiles.items()
        if p <= 0.30 and weights.get(c, 0) >= 0.10
    ]
    focus = max(weaknesses, key=lambda c: weights.get(c, 0), default=None)
    advice = ADVICE_TEXT.get((role, focus)) if focus else None
    return Insights(strengths=strengths, weaknesses=weaknesses, focus=focus, advice=advice)

COMP_LABELS = {
    "KDA": "KDA", "KP": "Kill Part.", "DMG": "Dégâts",
    "ECO": "Éco", "OBJ": "Objectifs", "VIS": "Vision",
    "UTL": "Utilité", "LANE": "Lane", "CC": "CC",
}
