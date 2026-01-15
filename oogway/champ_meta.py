# oogway/champ_meta.py
# ============================================================================
# Métadonnées des champions (winrate, counters, rôle)
# OPTIMISÉ: Frozensets pour counters, helpers pour accès direct
# ============================================================================

from __future__ import annotations
import json
import pathlib
import unicodedata
from typing import Dict, FrozenSet, Optional
import logging

logger = logging.getLogger(__name__)

# ────────────────────────────── Chargement unique ─────────────────────────────
DATA_DIR = pathlib.Path(__file__).with_suffix("").parent / "data"
FILES = (
    "champion_meta_top.json",
    "champion_meta_jungle.json",
    "champion_meta_mid.json",
    "champion_meta_adc.json",
    "champion_meta_support.json",
)

_META: Dict[str, dict] = {}
_COUNTERS_CACHE: Dict[str, FrozenSet[str]] = {}  # Cache des counters en frozenset
_WINRATES_CACHE: Dict[str, float] = {}  # Cache des winrates

# Chargement des fichiers JSON
for fname in FILES:
    fpath = DATA_DIR / fname
    if not fpath.exists():
        logger.warning(f"Fichier meta manquant: {fpath}")
        continue
    
    try:
        with open(fpath, encoding="utf-8") as f:
            data = json.load(f)
            for champ, stats in data.items():
                _META[champ] = stats
                
                # Préconvertir les counters en frozenset pour des lookups O(1)
                if "counters" in stats:
                    _COUNTERS_CACHE[champ] = frozenset(stats["counters"])
                else:
                    _COUNTERS_CACHE[champ] = frozenset()
                
                # Précalculer les winrates
                _WINRATES_CACHE[champ] = stats.get("winrate", 50.0)
        
        logger.info(f"Chargé: {fname} ({len(data)} champions)")
    except Exception as e:
        logger.error(f"Erreur chargement {fname}: {e}")

logger.info(f"Total champions chargés: {len(_META)}")

# ────────────────────────────── Normalisation ─────────────────────────────────
def _norm(name: str) -> str:
    """
    Simplifie un nom de champion pour les recherches.
    - Minuscules
    - Sans espaces/ponctuation
    - Normalisation NFKD (retire accents)
    """
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return "".join(c for c in normalized.lower() if c.isalnum())

# Index de recherche normalisé
_LOOKUP = {_norm(k): v for k, v in _META.items()}
_LOOKUP_TO_CANONICAL = {_norm(k): k for k in _META.keys()}

# ────────────────────────────── API publique ─────────────────────────────────
def meta(champ: str) -> dict:
    """
    Retourne le bloc méta complet du champion.
    
    Args:
        champ: Nom du champion (case-insensitive, accent-insensitive)
    
    Returns:
        dict: {'role': str, 'winrate': float, 'counters': list, 'badges': list}
              ou {} si champion inconnu (winrate 50 par défaut)
    """
    return _LOOKUP.get(_norm(champ), {})


def get_counters(champ: str) -> FrozenSet[str]:
    """
    Retourne les counters d'un champion (optimisé avec frozenset).
    
    Args:
        champ: Nom du champion
    
    Returns:
        FrozenSet[str]: Ensemble des champions qui counter ce champion
    """
    canonical = _LOOKUP_TO_CANONICAL.get(_norm(champ))
    if canonical:
        return _COUNTERS_CACHE.get(canonical, frozenset())
    return frozenset()


def get_winrate(champ: str) -> float:
    """
    Retourne le winrate d'un champion.
    
    Args:
        champ: Nom du champion
    
    Returns:
        float: Winrate du champion (50.0 par défaut si inconnu)
    """
    canonical = _LOOKUP_TO_CANONICAL.get(_norm(champ))
    if canonical:
        return _WINRATES_CACHE.get(canonical, 50.0)
    return 50.0


def get_role(champ: str) -> Optional[str]:
    """
    Retourne le rôle principal d'un champion.
    
    Args:
        champ: Nom du champion
    
    Returns:
        Optional[str]: Rôle (TOP, JUNGLE, MID, ADC, SUPPORT) ou None
    """
    data = meta(champ)
    return data.get("role")


def get_badges(champ: str) -> list[str]:
    """
    Retourne les badges d'un champion (ex: S-tier, A-tier, etc.).
    
    Args:
        champ: Nom du champion
    
    Returns:
        list[str]: Liste des badges
    """
    data = meta(champ)
    return data.get("badges", [])


def champion_exists(champ: str) -> bool:
    """
    Vérifie si un champion existe dans la base de données.
    
    Args:
        champ: Nom du champion
    
    Returns:
        bool: True si le champion existe
    """
    return _norm(champ) in _LOOKUP


def get_all_champions() -> list[str]:
    """
    Retourne la liste de tous les champions disponibles.
    
    Returns:
        list[str]: Noms canoniques des champions
    """
    return list(_META.keys())


def get_champions_by_role(role: str) -> list[str]:
    """
    Retourne tous les champions d'un rôle spécifique.
    
    Args:
        role: Rôle à filtrer (TOP, JUNGLE, MID, ADC, SUPPORT)
    
    Returns:
        list[str]: Liste des champions du rôle
    """
    role_upper = role.upper()
    return [champ for champ, data in _META.items() if data.get("role") == role_upper]


def get_top_winrates(n: int = 10, role: Optional[str] = None) -> list[tuple[str, float]]:
    """
    Retourne les N champions avec les meilleurs winrates.
    
    Args:
        n: Nombre de champions à retourner
        role: Filtrer par rôle (optionnel)
    
    Returns:
        list[tuple[str, float]]: Liste de (champion, winrate) triée
    """
    if role:
        filtered = [(c, wr) for c, wr in _WINRATES_CACHE.items() 
                    if _META[c].get("role") == role.upper()]
    else:
        filtered = list(_WINRATES_CACHE.items())
    
    filtered.sort(key=lambda x: x[1], reverse=True)
    return filtered[:n]


def get_stats() -> dict:
    """
    Retourne des statistiques sur la base de données.
    
    Returns:
        dict: Statistiques diverses
    """
    roles = {}
    total_wr = 0.0
    
    for champ, data in _META.items():
        role = data.get("role", "UNKNOWN")
        roles[role] = roles.get(role, 0) + 1
        total_wr += data.get("winrate", 50.0)
    
    return {
        "total_champions": len(_META),
        "champions_by_role": roles,
        "average_winrate": round(total_wr / len(_META), 2) if _META else 0.0,
        "cached_counters": len(_COUNTERS_CACHE),
        "cached_winrates": len(_WINRATES_CACHE)
    }


# ────────────────────────────── Export ─────────────────────────────────────
__all__ = [
    'meta',
    'get_counters',
    'get_winrate',
    'get_role',
    'get_badges',
    'champion_exists',
    'get_all_champions',
    'get_champions_by_role',
    'get_top_winrates',
    'get_stats'
]
