# riot_cache.py – mini-cache Riot API (SQLite, SQLAlchemy)

from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, JSON, PrimaryKeyConstraint
from oogway.database import Base

class RankedCache(Base):
    """Entrées /league/v4/entries. 1 ligne par queue + summoner_id."""
    __tablename__ = "ranked_cache"
    summoner_id = Column(String, primary_key=True)
    queue_type  = Column(String, primary_key=True)   # RANKED_SOLO_5x5 …
    json        = Column(JSON)                       # réponse brute
    ts          = Column(DateTime, default=datetime.utcnow)

class MatchCache(Base):
    """Réponse brute d’un match-v5 (pour réduire le quota)."""
    __tablename__ = "match_cache"
    match_id  = Column(String, primary_key=True)
    region    = Column(String)
    json      = Column(JSON)
    ts        = Column(DateTime, default=datetime.utcnow)

# appelé dans database.init_db()
