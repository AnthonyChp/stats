# database.py – SQLAlchemy setup + User model

from pathlib import Path
from sqlalchemy import (
    create_engine, Column, String, Integer, Boolean, DateTime, ForeignKey, PrimaryKeyConstraint,
    Float, Text
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


class LinkedAccount(Base):
    """Comptes secondaires (smurf / alt) rattachés à un compte Discord.

    Le compte *principal* reste dans la table `users` (1 par Discord). Les
    smurfs vivent ici pour ne pas polluer les itérations `query(User).all()`
    (leaderboard, match_alerts, etc.) qui doivent compter une personne une
    seule fois. `puuid` est unique pour empêcher qu'un même compte Riot soit
    lié deux fois (anti-usurpation)."""
    __tablename__ = 'linked_accounts'
    id            = Column(Integer, primary_key=True, autoincrement=True)
    discord_id    = Column(String, ForeignKey('users.discord_id'), nullable=False, index=True)
    puuid         = Column(String, unique=True, nullable=False, index=True)
    summoner_name = Column(String, nullable=False)
    region        = Column(String, nullable=False)


class Match(Base):
    __tablename__ = 'matches'
    match_id = Column(String, primary_key=True, index=True)
    puuid = Column(String, ForeignKey('users.puuid'),primary_key=True, index=True)
    queue_id = Column(Integer, nullable=False)
    win = Column(Boolean, nullable=False)
    timestamp = Column(DateTime, nullable=False)


class MutedUser(Base):
    """Stocke les rôles retirés lors d'un mute pour pouvoir les restaurer."""
    __tablename__ = 'muted_users'
    discord_id = Column(String, primary_key=True, index=True)
    role_ids = Column(String, nullable=False)  # IDs séparés par des virgules
    muted_at = Column(DateTime, nullable=False)
    muted_by = Column(String, nullable=False)  # Discord ID du modérateur
    reason = Column(String, nullable=True)


class MatchParticipant(Base):
    __tablename__ = "match_participants"
    id                   = Column(Integer, primary_key=True, autoincrement=True)
    match_id             = Column(String, ForeignKey("matches.match_id"), index=True, nullable=False)
    puuid                = Column(String, index=True, nullable=False)
    is_linked_member     = Column(Boolean, default=False)
    role                 = Column(String, index=True)       # TOP/JUNGLE/MID/ADC/SUPPORT
    champion             = Column(String, index=True)
    win                  = Column(Boolean)
    kills                = Column(Integer, default=0)
    deaths               = Column(Integer, default=0)
    assists              = Column(Integer, default=0)
    total_damage_champ   = Column(Integer, default=0)
    total_damage_taken   = Column(Integer, default=0)
    gold_earned          = Column(Integer, default=0)
    cs_total             = Column(Integer, default=0)
    vision_score         = Column(Integer, default=0)
    heals_on_teammates   = Column(Integer, default=0)
    shields_on_teammates = Column(Integer, default=0)
    time_ccing_others    = Column(Integer, default=0)
    penta_kills          = Column(Integer, default=0)
    dragon_kills         = Column(Integer, default=0)
    baron_kills          = Column(Integer, default=0)
    turret_kills         = Column(Integer, default=0)
    challenges_json      = Column(Text)
    duration_min         = Column(Float)
    is_scorable          = Column(Boolean, default=True)


class BaselineCache(Base):
    __tablename__ = "baseline_cache"
    id                  = Column(Integer, primary_key=True, autoincrement=True)
    scope               = Column(String, index=True)   # "role:SUPPORT" | "champ:SUPPORT:Rakan"
    distributions_json  = Column(Text)
    sample_size         = Column(Integer)
    computed_at         = Column(DateTime)


class OogScoreRecord(Base):
    __tablename__ = "oogscores"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    match_id        = Column(String, index=True)
    puuid           = Column(String, index=True)
    score           = Column(Float)
    grade           = Column(String)
    role            = Column(String)
    components_json = Column(Text)
    baseline_source = Column(String)
    sample_size_used= Column(Integer)
    computed_at     = Column(DateTime)


def init_db():
    """Créer les tables si elles n'existent pas encore."""
    """Créer les tables si elles n'existent pas encore."""
    # Import local pour éviter le cycle d'import
    from oogway.db import riot_cache  # noqa: F401
    Base.metadata.create_all(bind=engine)


# =============================================================
# Helpers comptes liés (principal + smurfs)
# =============================================================
def find_puuid_owner(session, puuid: str):
    """Retourne le discord_id propriétaire d'un puuid (principal OU smurf), sinon None."""
    user = session.query(User).filter_by(puuid=puuid).first()
    if user:
        return user.discord_id
    smurf = session.query(LinkedAccount).filter_by(puuid=puuid).first()
    return smurf.discord_id if smurf else None


def get_linked_puuids(session, discord_id: str) -> list[str]:
    """Tous les puuids d'un membre : compte principal + smurfs."""
    puuids: list[str] = []
    user = session.get(User, str(discord_id))
    if user:
        puuids.append(user.puuid)
    puuids.extend(
        a.puuid for a in session.query(LinkedAccount).filter_by(discord_id=str(discord_id)).all()
    )
    return puuids


def get_all_accounts(session) -> list:
    """Tous les comptes Riot suivis : principaux (User) + smurfs (LinkedAccount).

    Les deux modèles exposent `discord_id`, `puuid`, `region` et
    `summoner_name`, donc les consommateurs (leaderboard, match_alerts) peuvent
    les traiter de manière interchangeable."""
    return session.query(User).all() + session.query(LinkedAccount).all()


def get_all_puuids(session) -> set[str]:
    """Ensemble de tous les puuids suivis (principaux + smurfs)."""
    return (
        {u.puuid for u in session.query(User).all()}
        | {a.puuid for a in session.query(LinkedAccount).all()}
    )
