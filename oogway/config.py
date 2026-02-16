# config.py – Chargement des paramètres via pydantic-settings

from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # — Tokens & API Keys —
    DISCORD_TOKEN: str
    RIOT_API_KEY: str

    # — Database & Timezone —
    DB_URL: str = "sqlite:///data/oogway.db"
    TIMEZONE: str = "Europe/Paris"

    # — Discord IDs —
    APPLICATION_ID:    int  # ID de l'application Discord
    ALERT_CHANNEL_ID:  int  # où poster les alerts de parties
    SUMMARY_CHANNEL_ID:int  # où poster les résumés périodiques
    LINK_CHANNEL_ID:   int  # canal où les users "link" leur compte
    LEADERBOARD_CHANNEL_ID: int  # canal du leaderboard
    DEBUG_GUILD_ID:    Optional[int] = None  # pour les slash-commands en dev
    ORGANIZER_ROLE_ID: int  # rôle autorisé à lancer /5v5
    CUSTOM_GAME_CHANNEL_ID: int  # salon où la commande est utilisable
    JOIN_PING_ROLE_ID: Optional[int] = None  # rôle à ping pour rejoindre les customs

    # — Modération —
    MODERATION_CHANNEL_ID: int  # salon où s'affichent les reports/mutes
    MUTE_ROLE_ID: int  # rôle "mute" à attribuer

    # — Oogle (Wordle FR) —
    OOGLE_CHANNEL_ID: int  # salon où poster les notifications quotidiennes
    OOGLE_LEADERBOARD_CHANNEL_ID: int  # salon du leaderboard OOGLE (nouveau)

    # — Redis —
    REDIS_URL: str

    # — Riot API Configuration —
    DEFAULT_REGION: str = "euw1"  # Région par défaut pour les comptes

    #MTXSERV_CLIENT_ID: str
    #MTXSERV_CLIENT_SECRET: str
    #MTXSERV_API_KEY: str
    #MTXSERV_SERVER_ID: str


    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

settings = Settings()
