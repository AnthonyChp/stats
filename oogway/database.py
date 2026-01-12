# database.py – SQLAlchemy setup + User model

from pathlib import Path
from sqlalchemy import (
    create_engine, Column, String, Integer, Boolean, DateTime, ForeignKey, PrimaryKeyConstraint
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from oogway.config import settings


# Si on utilise SQLite, créer le dossier parent du fichier .db
if settings.DB_URL.startswith("sqlite:///"):
    # Extrait le chemin local (après sqlite:///)
    db_file = settings.DB_URL.replace("sqlite:///", "")
    parent_dir = Path(db_file).parent
    parent_dir.mkdir(parents=True, exist_ok=True)

# Engine & session
engine = create_engine(settings.DB_URL, future=True)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
)

# Base pour les modèles
Base = declarative_base()


class User(Base):
    __tablename__ = 'users'
    discord_id = Column(String, primary_key=True, index=True)
    puuid = Column(String, unique=True, nullable=False, index=True)
    summoner_name = Column(String, nullable=False, index=True)
    region = Column(String, nullable=False, index=True)


class Match(Base):
    __tablename__ = 'matches'
    match_id = Column(String, primary_key=True, index=True)
    puuid = Column(String, ForeignKey('users.puuid'),primary_key=True, index=True)
    queue_id = Column(Integer, nullable=False)
    win = Column(Boolean, nullable=False)
    timestamp = Column(DateTime, nullable=False)


def init_db():
    """Créer les tables si elles n'existent pas encore."""
    """Créer les tables si elles n'existent pas encore."""
    # Import local pour éviter le cycle d'import
    from oogway.db import riot_cache  # noqa: F401
    Base.metadata.create_all(bind=engine)
