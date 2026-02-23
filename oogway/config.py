# config.py – Chargement des paramètres via pydantic-settings
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # — Tokens & API Keys —
    DISCORD_TOKEN: str
    RIOT_API_KEY: str
    LEETIFY_API_KEY: str
    STEAM_API_KEY: Optional[str] = None

    # — Database & Timezone —
    DB_URL: str = "sqlite:///data/oogway.db"
    TIMEZONE: str = "Europe/Paris"

    # — Discord IDs —
    APPLICATION_ID:    int
    ALERT_CHANNEL_ID:  int
    SUMMARY_CHANNEL_ID:int
    LINK_CHANNEL_ID:   int
    LEADERBOARD_CHANNEL_ID: int
    DEBUG_GUILD_ID:    Optional[int] = None
    ORGANIZER_ROLE_ID: int
    CUSTOM_GAME_CHANNEL_ID: int
    JOIN_PING_ROLE_ID: Optional[int] = None

    # — Modération —
    MODERATION_CHANNEL_ID: int
    MUTE_ROLE_ID: int

    # — Oogle (Wordle FR) —
    OOGLE_CHANNEL_ID: int
    OOGLE_LEADERBOARD_CHANNEL_ID: int
    OOGLE_ROLE_ID: Optional[int] = None

    # — CS2 Tracker —
    CS_MATCH_CHANNEL_ID: int
    CS_STEAM_IDS: str = ""
    CS_POLL_INTERVAL: int = 300

    # — Redis —
    REDIS_URL: str

    # — Riot API Configuration —
    DEFAULT_REGION: str = "euw1"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

settings = Settings()
