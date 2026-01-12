# oogway/champ_meta.py
from __future__ import annotations
import json, pathlib, unicodedata

# ────────────────────────────── Chargement unique ─────────────────────────────
DATA_DIR = pathlib.Path(__file__).with_suffix("").parent / "data"
FILES = (
    "champion_meta_top.json",
    "champion_meta_jungle.json",
    "champion_meta_mid.json",
    "champion_meta_adc.json",
    "champion_meta_support.json",
)

_META: dict[str, dict] = {}
for fname in FILES:
    with open(DATA_DIR / fname, encoding="utf-8") as f:
        _META.update(json.load(f))

# ────────────────────────────── API publique ─────────────────────────────────
def _norm(name: str) -> str:
    """Simplifie (minuscules, sans espaces/ponctuation, NFKD) pour les recherches."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return "".join(c for c in n.lower() if c.isalnum())

_LOOKUP = { _norm(k): v for k, v in _META.items() }

def meta(champ: str) -> dict:
    """Retourne le bloc méta du champion, ou `{}` (winrate 50 par défaut ailleurs)."""
    return _LOOKUP.get(_norm(champ), {})
