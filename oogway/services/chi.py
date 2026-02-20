# oogway/services/chi.py
# ============================================================================
# Prédiction de winrate avec système de counters
# OPTIMISÉ: Cache LRU, pre-lookup des winrates, set lookups
# ============================================================================

from __future__ import annotations
from typing import Sequence
from functools import lru_cache

from oogway import champ_meta as cm

# Cache des winrates pour éviter les lookups répétés dans meta()
_WR_CACHE: dict[str, float] = {}
_COUNTERS_CACHE: dict[str, frozenset[str]] = {}


def _get_winrate(champ: str) -> float:
    """Récupère le winrate avec cache."""
    if champ not in _WR_CACHE:
        _WR_CACHE[champ] = cm.meta(champ).get("winrate", 50.0)
    return _WR_CACHE[champ]


def _get_counters(champ: str) -> frozenset[str]:
    """Récupère les counters avec cache."""
    if champ not in _COUNTERS_CACHE:
        counters = cm.meta(champ).get("counters", [])
        _COUNTERS_CACHE[champ] = frozenset(counters)
    return _COUNTERS_CACHE[champ]


def _score(team: Sequence[str], enemies: Sequence[str]) -> float:
    """
    Calcule le score d'une team basé sur:
    - Win-rate moyen de la team
    - +1 point par counter direct contre un ennemi
    """
    if not team:  # Aucun pick → baseline 50%
        return 50.0

    # Calcul du winrate moyen
    total_wr = sum(_get_winrate(c) for c in team)
    base = total_wr / len(team)
    
    # Calcul des bonus de counter (optimisé avec set lookup)
    enemy_set = set(enemies)
    bonus = 0
    
    for champ in team:
        counters = _get_counters(champ)
        # Intersection plus rapide que any() dans une boucle
        if counters & enemy_set:
            bonus += 1
    
    return base + bonus


@lru_cache(maxsize=512)
def predict_cached(picks_a_tuple: tuple[str, ...], picks_b_tuple: tuple[str, ...]) -> tuple[float, float]:
    """
    Version cachée de predict pour éviter les recalculs sur compositions identiques.
    Utilise des tuples (immutables) pour le cache LRU.
    """
    score_a = _score(picks_a_tuple, picks_b_tuple)
    score_b = _score(picks_b_tuple, picks_a_tuple)
    
    total = score_a + score_b
    
    if total == 0:  # Edge case: pas de picks
        return 50.0, 50.0
    
    pct_a = round((score_a / total) * 100, 1)
    pct_b = round((score_b / total) * 100, 1)
    
    return pct_a, pct_b


def predict(picks_a: list[str], picks_b: list[str]) -> tuple[float, float]:
    """
    Interface publique pour prédire les chances de victoire.
    
    Args:
        picks_a: Liste des champions de l'équipe A
        picks_b: Liste des champions de l'équipe B
    
    Returns:
        tuple[float, float]: (pourcentage_A, pourcentage_B)
    """
    return predict_cached(tuple(picks_a), tuple(picks_b))


def bar(blue_pct: float, blocks: int = 20) -> str:
    """
    Génère une barre visuelle représentant le winrate.
    
    Args:
        blue_pct: Pourcentage de winrate blue side (0-100)
        blocks: Nombre de blocs dans la barre
    
    Returns:
        str: Barre visuelle avec émojis
    """
    blue_blocks = round(blocks * blue_pct / 100)
    red_blocks = blocks - blue_blocks
    return "🟦" * blue_blocks + "🟥" * red_blocks


def clear_cache() -> None:
    """Vide tous les caches (utile pour les tests ou mise à jour de meta)."""
    _WR_CACHE.clear()
    _COUNTERS_CACHE.clear()
    predict_cached.cache_clear()


def get_cache_info() -> dict:
    """Retourne des infos sur l'état des caches."""
    return {
        "winrate_cache_size": len(_WR_CACHE),
        "counters_cache_size": len(_COUNTERS_CACHE),
        "predict_cache_info": predict_cached.cache_info()._asdict()
    }
