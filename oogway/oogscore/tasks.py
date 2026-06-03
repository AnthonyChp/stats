from __future__ import annotations
import logging
from datetime import datetime
from .baseline import build_baseline, save_baseline_cache

log = logging.getLogger(__name__)

# In-memory baseline cache (loaded at startup, refreshed by cron)
_baseline_cache: dict = {}

def get_baseline_cache() -> dict:
    return _baseline_cache

def refresh_baseline(session) -> int:
    global _baseline_cache
    log.info("Rebuilding OogScore baseline...")
    distributions = build_baseline(session)
    save_baseline_cache(session, distributions)
    from .baseline import load_baseline_cache
    _baseline_cache = load_baseline_cache(session)
    log.info("Baseline refreshed: %d scopes", len(_baseline_cache))
    return len(_baseline_cache)

def load_baseline_from_db(session):
    global _baseline_cache
    from .baseline import load_baseline_cache
    _baseline_cache = load_baseline_cache(session)
    log.info("Baseline loaded from DB: %d scopes", len(_baseline_cache))
