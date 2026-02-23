# oogway/cogs/cs.py — CS2 Match Tracker via Leetify API
# ============================================================================
# Polle l'API Leetify pour détecter les nouvelles parties CS2 et poster
# un embed détaillé dans le channel défini dans le .env.
#
# Variables .env requises :
#   LEETIFY_API_KEY       — clé API (https://leetify.com/app/developer)
#   CS_MATCH_CHANNEL_ID   — ID du salon Discord où poster les matchs
#   CS_STEAM_IDS          — Steam64 IDs fixes (séparés par des virgules)
#   CS_POLL_INTERVAL      — intervalle de polling en secondes (défaut: 300)
#   STEAM_API_KEY         — clé Steam (pour résoudre les vanity URLs)
#
# Commandes :
#   /cs-link <steam>     — lier son compte Steam (ID ou URL)
#   /cs-unlink           — délier son compte Steam
#   /cs-profile [@user]  — voir le profil Leetify d'un membre
#   /cs-check            — forcer une vérification (organisateur)
# ============================================================================

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import aiohttp
import discord
from discord import app_commands, Interaction
from discord.ext import commands, tasks
from sqlalchemy import Column, String, DateTime
from sqlalchemy.sql import func

from oogway.config import settings
from oogway.database import Base, SessionLocal

logger = logging.getLogger(__name__)

# ─────────────────────────────── Constantes API ───────────────────────────────
LEETIFY_BASE = "https://api-public.cs-prod.leetify.com"

# ─────────────────────────────── Modèle DB ────────────────────────────────────
class SteamLink(Base):
    """Lien Discord ↔ Steam64 pour le tracker CS2."""
    __tablename__ = "steam_links"

    discord_id = Column(String, primary_key=True, index=True)
    steam64_id = Column(String, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"<SteamLink discord={self.discord_id} steam={self.steam64_id}>"


# ─────────────────────────────── Helpers DB ───────────────────────────────────
def _ensure_table():
    try:
        from oogway.database import engine
        Base.metadata.create_all(bind=engine, tables=[SteamLink.__table__])
    except Exception as e:
        logger.error(f"❌ Erreur création table steam_links: {e}")


def get_steam_link(discord_id: str) -> Optional[str]:
    with SessionLocal() as db:
        row = db.query(SteamLink).filter_by(discord_id=discord_id).first()
        return row.steam64_id if row else None


def set_steam_link(discord_id: str, steam64_id: str):
    with SessionLocal() as db:
        row = db.query(SteamLink).filter_by(discord_id=discord_id).first()
        if row:
            row.steam64_id = steam64_id
        else:
            db.add(SteamLink(discord_id=discord_id, steam64_id=steam64_id))
        db.commit()


def delete_steam_link(discord_id: str) -> bool:
    with SessionLocal() as db:
        row = db.query(SteamLink).filter_by(discord_id=discord_id).first()
        if row:
            db.delete(row)
            db.commit()
            return True
        return False


def get_all_linked_steam_ids() -> list[str]:
    with SessionLocal() as db:
        return [row.steam64_id for row in db.query(SteamLink).all()]


# ─────────────────────────────── Résolution Steam ID ─────────────────────────
_STEAM64_RE = re.compile(r"^7656119\d{10}$")


async def resolve_steam_input(raw: str) -> Optional[str]:
    """
    Résout une entrée utilisateur en Steam64 ID.
    Accepte :
      - Steam64 ID direct              : 76561198XXXXXXXXX
      - URL profil numérique           : steamcommunity.com/profiles/76561198XXXXXXXXX
      - URL profil vanity              : steamcommunity.com/id/monpseudo
    """
    raw = raw.strip().rstrip("/")

    # Cas 1 : ID direct
    if _STEAM64_RE.match(raw):
        return raw

    # Cas 2 : URL avec Steam64 dedans
    m = re.search(r"steamcommunity\.com/profiles/(\d{15,})", raw)
    if m and _STEAM64_RE.match(m.group(1)):
        return m.group(1)

    # Cas 3 : URL vanity → Steam Web API
    m = re.search(r"steamcommunity\.com/id/([^/?\s]+)", raw)
    if m:
        vanity = m.group(1)
        steam_api_key = getattr(settings, "STEAM_API_KEY", "")
        if not steam_api_key:
            logger.warning("STEAM_API_KEY manquante — impossible de résoudre les URLs vanity")
            return None
        try:
            url = (
                "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
                f"?key={steam_api_key}&vanityurl={vanity}"
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        r = data.get("response", {})
                        if r.get("success") == 1:
                            return r.get("steamid")
        except Exception as e:
            logger.error(f"Erreur résolution vanity '{vanity}': {e}")

    return None


# ─────────────────────────────── Constantes display ───────────────────────────
COLOR_WIN  = discord.Colour.from_rgb(67, 181, 129)
COLOR_LOSS = discord.Colour.from_rgb(240, 71, 71)
COLOR_DRAW = discord.Colour.from_rgb(250, 166, 26)

MAP_NAMES: dict[str, str] = {
    "de_dust2":    "Dust II",
    "de_mirage":   "Mirage",
    "de_inferno":  "Inferno",
    "de_nuke":     "Nuke",
    "de_overpass": "Overpass",
    "de_vertigo":  "Vertigo",
    "de_ancient":  "Ancient",
    "de_anubis":   "Anubis",
    "de_train":    "Train",
}


# ─────────────────────────────── Client Leetify ───────────────────────────────
class LeetifyClient:
    """
    Wrapper pour l'API publique Leetify v3.
    Auth : Authorization: Bearer <key>  (ou header _leetify_key: <key>)
    Base : https://api-public.cs-prod.leetify.com
    """

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

    def _headers(self) -> dict:
        return {
            "Accept": "application/json",
            "_leetify_key": self._api_key,
        }

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers())
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: Optional[dict] = None):
        """GET vers LEETIFY_BASE + path avec query params optionnels."""
        s = await self._sess()
        url = f"{LEETIFY_BASE}{path}"
        try:
            async with s.get(url, params=params) as r:
                logger.debug(f"Leetify {url} params={params} → HTTP {r.status}")
                if r.status == 200:
                    return await r.json()
                if r.status == 401:
                    logger.error("Leetify: clé API invalide (401)")
                elif r.status != 404:
                    body = await r.text()
                    logger.error(f"Leetify {url}: HTTP {r.status} — {body[:200]}")
        except Exception as e:
            logger.error(f"Leetify GET {url}: {e}")
        return None

    # ── Endpoints ──────────────────────────────────────────────────
    async def validate_key(self) -> bool:
        """Valide la clé API. Retourne True si valide."""
        result = await self._get("/api-key/validate")
        return result is not None

    async def get_profile(self, steam64_id: str) -> Optional[dict]:
        """GET /v3/profile?steam64_id=..."""
        return await self._get("/v3/profile", params={"steam64_id": steam64_id})

    async def get_matches(self, steam64_id: str) -> list[dict]:
        """GET /v3/profile/matches?steam64_id=... (à adapter selon la doc réelle)"""
        data = await self._get("/v3/profile/matches", params={"steam64_id": steam64_id})
        if isinstance(data, list):
            return data
        # Certaines réponses encapsulent dans un objet
        if isinstance(data, dict):
            return data.get("matches") or data.get("data") or []
        return []

    async def get_match(self, match_id: str) -> Optional[dict]:
        """GET /v3/match/{match_id}"""
        return await self._get(f"/v3/match/{match_id}")


# ─────────────────────────────── Helpers formatting ──────────────────────────
def _f(val, d: float = 0.0) -> float:
    try: return float(val) if val is not None else d
    except: return d

def _i(val, d: int = 0) -> int:
    try: return int(val) if val is not None else d
    except: return d

def _score_icon(s: float) -> str:
    r = round(s)
    if r >= 70: return f"🟢 {r}"
    if r >= 45: return f"🟡 {r}"
    return f"🔴 {r}"

def _map_name(raw: str) -> str:
    return MAP_NAMES.get(raw, raw.replace("de_", "").capitalize())

def _rank_str(premier) -> str:
    return (f"{int(premier):,}".replace(",", " ") + " pts") if premier else "—"


def build_match_embed(
    player_name: str,
    player_avatar: Optional[str],
    steam_id: str,
    match: dict,
    stats: dict,
) -> discord.Embed:
    won       = stats.get("won")
    score_s   = _i(stats.get("teamScore"))
    score_o   = _i(stats.get("enemyScore"))
    color     = COLOR_WIN if won is True else (COLOR_LOSS if won is False else COLOR_DRAW)
    result    = "🏆 Victoire" if won is True else ("💀 Défaite" if won is False else "🤝 Nul")
    map_name  = _map_name(match.get("mapName", ""))
    mode      = match.get("gameMode", "Unknown").replace("_", " ").title()

    ts = "—"
    raw_date = match.get("gameFinishedAt") or match.get("finishedAt")
    if raw_date:
        try:
            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            ts = f"<t:{int(dt.timestamp())}:R>"
        except Exception:
            pass

    kills   = _i(stats.get("kills"))
    deaths  = _i(stats.get("deaths"))
    assists = _i(stats.get("assists"))
    hs_pct  = _f(stats.get("headshotPercentage")) * 100
    adr     = _f(stats.get("adr"))
    rating  = _f(stats.get("leetifyRating"))
    aim     = _f(stats.get("aimRating"))
    util    = _f(stats.get("utilityRating"))
    pos     = _f(stats.get("positioningRating"))
    ct      = _f(stats.get("ctRating"))
    t       = _f(stats.get("tRating"))
    premier = stats.get("premierRating")

    embed = discord.Embed(
        title=f"{result}  ·  {map_name}  ·  {score_s} – {score_o}",
        colour=color,
        url=f"https://leetify.com/app/match-details/{match.get('id', '')}",
    )
    embed.set_author(
        name=player_name,
        url=f"https://leetify.com/app/profile/{steam_id}",
        icon_url=player_avatar or discord.Embed.Empty,
    )
    if player_avatar:
        embed.set_thumbnail(url=player_avatar)

    embed.add_field(
        name="📊 Stats",
        value=(
            f"**K / D / A** : `{kills} / {deaths} / {assists}`\n"
            f"**HS%** : `{hs_pct:.0f}%`\n"
            f"**ADR** : `{adr:.0f}`\n"
            f"**Rating** : `{rating:.2f}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="🎯 Scores Leetify",
        value=(
            f"**Aim** : {_score_icon(aim)}\n"
            f"**Utility** : {_score_icon(util)}\n"
            f"**Positioning** : {_score_icon(pos)}\n"
            f"**CT** : {_score_icon(ct)}  |  **T** : {_score_icon(t)}"
        ),
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(
        name="🗺️ Partie",
        value=f"**Map** : {map_name}\n**Mode** : {mode}\n**Terminée** : {ts}",
        inline=True,
    )
    embed.add_field(name="🏅 Premier", value=_rank_str(premier), inline=True)
    embed.set_footer(text="Data Provided by Leetify")
    return embed


# ============================================================================
# Cog principal
# ============================================================================
class CS2TrackerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.api_key: str       = getattr(settings, "LEETIFY_API_KEY", "")
        self.channel_id: int    = int(getattr(settings, "CS_MATCH_CHANNEL_ID", 0))
        self.poll_interval: int = int(getattr(settings, "CS_POLL_INTERVAL", 300))

        raw_ids: str = getattr(settings, "CS_STEAM_IDS", "")
        self._static_ids: set[str] = {s.strip() for s in raw_ids.split(",") if s.strip()}

        self._seen_matches: dict[str, set[str]] = {}
        self.leetify = LeetifyClient(self.api_key)

        if not self.api_key:
            logger.warning("⚠️  LEETIFY_API_KEY manquante — CS tracker désactivé")
        if not self.channel_id:
            logger.warning("⚠️  CS_MATCH_CHANNEL_ID manquant — CS tracker désactivé")

    def _is_configured(self) -> bool:
        return bool(self.api_key and self.channel_id)

    def _all_ids(self) -> set[str]:
        """Fusionne les IDs fixes (.env) et les IDs dynamiques (DB)."""
        return self._static_ids | set(get_all_linked_steam_ids())

    # ─────────────────────────────── Lifecycle ────────────────────
    async def cog_load(self):
        _ensure_table()

        if not self._is_configured():
            return

        self._poll_loop.change_interval(seconds=self.poll_interval)
        self._poll_loop.start()
        logger.info(f"✅ CS2 tracker démarré (poll: {self.poll_interval}s)")

    async def cog_unload(self):
        self._poll_loop.cancel()
        await self.leetify.close()

    # ─────────────────────────────── Polling ──────────────────────
    @tasks.loop(seconds=300)
    async def _poll_loop(self):
        await self.bot.wait_until_ready()
        for sid in self._all_ids():
            try:
                await self._check_player(sid)
            except Exception as e:
                logger.error(f"❌ Poll {sid}: {e}", exc_info=True)

    @_poll_loop.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()
        logger.info("🔄 Init CS2 tracker — chargement des matchs existants…")
        for sid in self._all_ids():
            matches = await self.leetify.get_matches(sid)
            self._seen_matches[sid] = {
                m.get("id") or m.get("matchId", "")
                for m in matches if m.get("id") or m.get("matchId")
            }
        total = sum(len(v) for v in self._seen_matches.values())
        logger.info(f"✅ Init — {total} matchs connus, {len(self._all_ids())} joueur(s)")

    # ─────────────────────────────── Check joueur ─────────────────
    async def _check_player(self, steam_id: str):
        matches = await self.leetify.get_matches(steam_id)
        if not matches:
            return

        seen = self._seen_matches.setdefault(steam_id, set())
        new = [
            (m.get("id") or m.get("matchId", ""), m)
            for m in matches
            if (m.get("id") or m.get("matchId", "")) not in seen
            and (m.get("id") or m.get("matchId"))
        ]
        for mid, _ in new:
            seen.add(mid)

        if not new:
            return

        profile       = await self.leetify.get_profile(steam_id)
        player_name   = profile.get("name") or profile.get("steamName", steam_id) if profile else steam_id
        player_avatar = (profile.get("avatarUrl") or profile.get("avatar")) if profile else None

        channel = self.bot.get_channel(self.channel_id)
        if not channel:
            logger.warning(f"Channel {self.channel_id} introuvable")
            return

        for mid, summary in new:
            detail     = await self.leetify.get_match(mid)
            match_data = detail or summary
            stats      = self._extract_stats(match_data, steam_id) or summary
            embed      = build_match_embed(player_name, player_avatar, steam_id, match_data, stats)
            try:
                await channel.send(embed=embed)
                logger.info(f"📬 {player_name} | {mid}")
            except discord.HTTPException as e:
                logger.error(f"❌ Post {mid}: {e}")

    def _extract_stats(self, match: dict, steam_id: str) -> Optional[dict]:
        """Cherche les stats du joueur cible dans la réponse détaillée du match."""
        for p in match.get("players", []):
            pid = str(p.get("steamId") or p.get("steam64Id") or p.get("id", ""))
            if pid == str(steam_id):
                return p
        for team in match.get("teams", []):
            for p in team.get("players", []):
                pid = str(p.get("steamId") or p.get("steam64Id") or p.get("id", ""))
                if pid == str(steam_id):
                    return p
        if match.get("steamId") or match.get("steam64Id"):
            return match
        return None

    # ============================================================
    # Commandes slash
    # ============================================================

    # ── /cs-link ──────────────────────────────────────────────────
    @app_commands.command(name="cs-link", description="Lier ton compte Steam pour le tracker CS2")
    @app_commands.describe(steam="Steam64 ID (76561198…) ou URL steamcommunity.com")
    async def cs_link(self, inter: Interaction, steam: str):
        await inter.response.defer(ephemeral=True)

        steam64 = await resolve_steam_input(steam)
        if not steam64:
            return await inter.followup.send(
                "❌ Format non reconnu. Exemples acceptés :\n"
                "• `76561198XXXXXXXXX`\n"
                "• `https://steamcommunity.com/id/ton_pseudo`\n"
                "• `https://steamcommunity.com/profiles/76561198XXXXXXXXX`",
                ephemeral=True,
            )

        # Vérifier existence sur Leetify
        profile = await self.leetify.get_profile(steam64)
        if not profile:
            return await inter.followup.send(
                f"⚠️ `{steam64}` introuvable sur Leetify.\n"
                "Assure-toi d'avoir un compte Leetify actif lié à ce Steam.",
                ephemeral=True,
            )

        set_steam_link(str(inter.user.id), steam64)

        # Ajouter au tracker live si nouveau joueur
        if steam64 not in self._seen_matches:
            matches = await self.leetify.get_matches(steam64)
            self._seen_matches[steam64] = {
                m.get("id") or m.get("matchId", "")
                for m in matches if m.get("id") or m.get("matchId")
            }
            logger.info(f"➕ Nouveau joueur tracké: {steam64} ({inter.user.name})")

        player_name   = profile.get("name") or profile.get("steamName", steam64)
        player_avatar = profile.get("avatarUrl") or profile.get("avatar")

        embed = discord.Embed(
            title="✅ Compte Steam lié !",
            colour=discord.Colour.green(),
            description=f"{inter.user.mention} → **{player_name}**",
        )
        embed.add_field(name="Steam64 ID", value=f"`{steam64}`", inline=True)
        embed.add_field(
            name="Profil Leetify",
            value=f"[Voir le profil](https://leetify.com/app/profile/{steam64})",
            inline=True,
        )
        if player_avatar:
            embed.set_thumbnail(url=player_avatar)
        embed.set_footer(text="Tes prochaines parties CS2 seront automatiquement postées.")
        await inter.followup.send(embed=embed, ephemeral=True)

    # ── /cs-unlink ────────────────────────────────────────────────
    @app_commands.command(name="cs-unlink", description="Délier ton compte Steam du tracker CS2")
    async def cs_unlink(self, inter: Interaction):
        if delete_steam_link(str(inter.user.id)):
            await inter.response.send_message(
                "✅ Compte Steam délié. Tu ne seras plus tracké.", ephemeral=True
            )
        else:
            await inter.response.send_message(
                "ℹ️ Aucun compte Steam n'était lié à ton Discord.", ephemeral=True
            )

    # ── /cs-profile ───────────────────────────────────────────────
    @app_commands.command(name="cs-profile", description="Voir le profil CS2 d'un membre")
    @app_commands.describe(member="Membre Discord (toi par défaut)")
    async def cs_profile(self, inter: Interaction, member: Optional[discord.Member] = None):
        target  = member or inter.user
        steam64 = get_steam_link(str(target.id))

        if not steam64:
            return await inter.response.send_message(
                f"ℹ️ **{target.display_name}** n'a pas encore lié son compte Steam.\n"
                "Utilise `/cs-link` pour le faire.",
                ephemeral=True,
            )

        await inter.response.defer(ephemeral=True)

        profile = await self.leetify.get_profile(steam64)
        if not profile:
            return await inter.followup.send(
                f"⚠️ `{steam64}` lié mais introuvable sur Leetify.", ephemeral=True
            )

        player_name   = profile.get("name") or profile.get("steamName", steam64)
        player_avatar = profile.get("avatarUrl") or profile.get("avatar")
        winrate       = _f(profile.get("winrate")) * 100
        matches_count = _i(profile.get("matchCount"))
        aim           = _f(profile.get("aimRating"))
        util          = _f(profile.get("utilityRating"))
        pos           = _f(profile.get("positioningRating"))
        premier       = profile.get("premierRating")

        embed = discord.Embed(
            title=f"💥 {player_name}",
            colour=discord.Colour.from_rgb(66, 133, 244),
            url=f"https://leetify.com/app/profile/{steam64}",
        )
        embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
        if player_avatar:
            embed.set_thumbnail(url=player_avatar)

        embed.add_field(name="Steam64", value=f"`{steam64}`", inline=False)
        embed.add_field(name="🏅 Premier",  value=_rank_str(premier),  inline=True)
        embed.add_field(name="🎯 Parties",  value=str(matches_count),   inline=True)
        embed.add_field(name="📈 Win Rate", value=f"{winrate:.1f}%",    inline=True)
        embed.add_field(
            name="📊 Scores Leetify",
            value=(
                f"**Aim** : {_score_icon(aim)}  "
                f"**Utility** : {_score_icon(util)}  "
                f"**Positioning** : {_score_icon(pos)}"
            ),
            inline=False,
        )
        embed.set_footer(text="Data Provided by Leetify")
        await inter.followup.send(embed=embed, ephemeral=True)

    # ── /cs-check ─────────────────────────────────────────────────
    @app_commands.command(name="cs-check", description="Force la vérification des derniers matchs CS2")
    @app_commands.checks.has_role(settings.ORGANIZER_ROLE_ID)
    async def cs_check(self, inter: Interaction):
        if not self._is_configured():
            return await inter.response.send_message(
                "⚠️ CS2 tracker non configuré.", ephemeral=True
            )
        ids = self._all_ids()
        await inter.response.send_message(
            f"🔄 Vérification de {len(ids)} joueur(s)…", ephemeral=True
        )
        for sid in ids:
            await self._check_player(sid)
        await inter.followup.send("✅ Done.", ephemeral=True)

    # ── Gestion erreurs ───────────────────────────────────────────
    async def cog_app_command_error(self, inter: Interaction, error: app_commands.AppCommandError):
        msg = "⛔ Organisateur uniquement." if isinstance(error, app_commands.MissingRole) else "❌ Une erreur est survenue."
        try:
            if inter.response.is_done():
                await inter.followup.send(msg, ephemeral=True)
            else:
                await inter.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(CS2TrackerCog(bot))
