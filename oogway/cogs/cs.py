# oogway/cogs/cs.py — CS2 Match Tracker via Leetify API
# ============================================================================
# Variables .env requises :
#   LEETIFY_API_KEY       — clé API Leetify
#   CS_MATCH_CHANNEL_ID   — salon Discord où poster les matchs
#   CS_STEAM_IDS          — Steam64 IDs fixes (séparés par des virgules)
#   CS_POLL_INTERVAL      — intervalle polling en secondes (défaut: 300)
#   STEAM_API_KEY         — clé Steam pour résoudre les URLs vanity
#
# Endpoints utilisés :
#   GET /v3/profile?steam64_id=...          → profil joueur
#   GET /v3/profile/matches?steam64_id=...  → liste des matchs (avec stats)
#   GET /v2/matches/{id}                    → détail match (tous joueurs)
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

LEETIFY_BASE = "https://api-public.cs-prod.leetify.com"


# ─────────────────────────────── Modèle DB ────────────────────────────────────
class SteamLink(Base):
    __tablename__ = "steam_links"
    discord_id = Column(String, primary_key=True, index=True)
    steam64_id = Column(String, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


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
    raw = raw.strip().rstrip("/")

    if _STEAM64_RE.match(raw):
        return raw

    m = re.search(r"steamcommunity\.com/profiles/(\d{15,})", raw)
    if m and _STEAM64_RE.match(m.group(1)):
        return m.group(1)

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

# data_source → label lisible
SOURCE_LABELS: dict[str, str] = {
    "matchmaking":            "Compétitif Premier",
    "matchmaking_competitive": "Compétitif",
    "matchmaking_wingman":    "Wingman",
    "renown":                 "Renown",
    "faceit":                 "FACEIT",
}


# ─────────────────────────────── Client Leetify ───────────────────────────────
class LeetifyClient:
    """
    Auth : _leetify_key: <key>
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
        s = await self._sess()
        url = f"{LEETIFY_BASE}{path}"
        try:
            async with s.get(url, params=params) as r:
                logger.debug(f"Leetify {url} params={params} → {r.status}")
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

    async def validate_key(self) -> bool:
        result = await self._get("/api-key/validate")
        return result is not None

    async def get_profile(self, steam64_id: str) -> Optional[dict]:
        """GET /v3/profile?steam64_id=... → {name, steam64_id, ranks, rating, stats, recent_matches, ...}"""
        return await self._get("/v3/profile", params={"steam64_id": steam64_id})

    async def get_matches(self, steam64_id: str) -> list[dict]:
        """
        GET /v3/profile/matches?steam64_id=...
        → liste de matchs, chaque match contient déjà stats[] du joueur
        Structure : {id, finished_at, map_name, outcome, score[], data_source, stats[{steam64_id, ...}]}
        """
        data = await self._get("/v3/profile/matches", params={"steam64_id": steam64_id})
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("matches") or data.get("data") or []
        return []

    async def get_match_detail(self, match_id: str) -> Optional[dict]:
        """GET /v2/matches/{id} → même structure mais avec TOUS les joueurs dans stats[]"""
        return await self._get(f"/v2/matches/{match_id}")


# ─────────────────────────────── Helpers formatting ──────────────────────────
def _f(val, d: float = 0.0) -> float:
    try: return float(val) if val is not None else d
    except: return d

def _i(val, d: int = 0) -> int:
    try: return int(val) if val is not None else d
    except: return d

def _pct(val) -> str:
    """Convertit 0.2636 → '26%'  ou  26.3 → '26%' selon le contexte."""
    v = _f(val)
    if v <= 1.0:
        v *= 100
    return f"{v:.0f}%"

def _score_icon(s: float) -> str:
    r = round(s)
    if r >= 70: return f"🟢 {r}"
    if r >= 45: return f"🟡 {r}"
    return f"🔴 {r}"

def _map_name(raw: str) -> str:
    return MAP_NAMES.get(raw, raw.replace("de_", "").capitalize())

def _rank_str(premier) -> str:
    return (f"{int(premier):,}".replace(",", " ") + " pts") if premier else "—"

def _ts(raw: Optional[str]) -> str:
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return f"<t:{int(dt.timestamp())}:R>"
    except Exception:
        return "—"

def _extract_player_stats(match: dict, steam64_id: str) -> Optional[dict]:
    """Trouve les stats du joueur cible dans match['stats'][]."""
    for p in match.get("stats", []):
        if str(p.get("steam64_id", "")) == str(steam64_id):
            return p
    return None

def _match_scores(match: dict) -> tuple[int, int]:
    """
    Retourne (score_team_joueur, score_ennemi) depuis team_scores[].
    On ne connaît pas l'équipe du joueur ici, donc on retourne score[0], score[1]
    depuis recent_matches (format: score: [13, 5]).
    Pour /v2/matches, on utilise team_scores[].
    """
    # Format recent_matches : "score": [13, 5]
    score_list = match.get("score")
    if isinstance(score_list, list) and len(score_list) >= 2:
        return score_list[0], score_list[1]
    # Format /v2/matches : team_scores: [{team_number, score}, ...]
    team_scores = match.get("team_scores", [])
    if len(team_scores) >= 2:
        return team_scores[0]["score"], team_scores[1]["score"]
    return 0, 0


def build_match_embed(
    player_name: str,
    steam_id: str,
    match: dict,
    stats: dict,
) -> discord.Embed:
    # ── Résultat ──────────────────────────────────────────────────
    outcome = match.get("outcome") or ("win" if stats.get("rounds_won", 0) > stats.get("rounds_lost", 0) else "loss")
    color   = COLOR_WIN if outcome == "win" else (COLOR_LOSS if outcome == "loss" else COLOR_DRAW)
    result  = "🏆 Victoire" if outcome == "win" else ("💀 Défaite" if outcome == "loss" else "🤝 Nul")

    # ── Scores ────────────────────────────────────────────────────
    score_a, score_b = _match_scores(match)
    rounds_won  = _i(stats.get("rounds_won"))
    rounds_lost = _i(stats.get("rounds_lost"))
    # Priorité au score du joueur (rounds_won / rounds_lost plus fiable)
    if rounds_won or rounds_lost:
        score_str = f"{rounds_won} – {rounds_lost}"
    else:
        score_str = f"{score_a} – {score_b}"

    # ── Infos match ───────────────────────────────────────────────
    map_name  = _map_name(match.get("map_name", ""))
    mode      = SOURCE_LABELS.get(match.get("data_source", ""), match.get("data_source", "").replace("_", " ").title())
    ts        = _ts(match.get("finished_at"))
    match_id  = match.get("id", "")

    # ── Stats joueur ──────────────────────────────────────────────
    kills      = _i(stats.get("total_kills"))
    deaths     = _i(stats.get("total_deaths"))
    assists    = _i(stats.get("total_assists"))
    kd         = _f(stats.get("kd_ratio"))
    hs_pct     = _pct(stats.get("accuracy_head"))
    adr        = _f(stats.get("dpr"))          # dpr = damage per round
    rating     = _f(stats.get("leetify_rating"))
    ct_rating  = _f(stats.get("ct_leetify_rating"))
    t_rating   = _f(stats.get("t_leetify_rating"))
    mvps       = _i(stats.get("mvps"))

    # ── Scores Leetify (depuis profile.rating ou stats du match) ──
    # Dans /v3/profile/matches les scores Leetify par match ne sont pas présents
    # On affiche le rating du match (leetify_rating, ct, t)
    rating_display = f"{rating:+.4f}"
    ct_display     = f"{ct_rating:+.4f}"
    t_display      = f"{t_rating:+.4f}"

    # ── Précision ─────────────────────────────────────────────────
    accuracy   = _pct(stats.get("accuracy_enemy_spotted"))
    spray      = _pct(stats.get("spray_accuracy"))
    reaction   = _f(stats.get("reaction_time")) * 1000  # secondes → ms
    if reaction == 0:
        reaction = _f(stats.get("reaction_time_ms"))

    embed = discord.Embed(
        title=f"{result}  ·  {map_name}  ·  {score_str}",
        colour=color,
        url=f"https://leetify.com/app/match-details/{match_id}" if match_id else discord.Embed.Empty,
    )
    embed.set_author(
        name=player_name,
        url=f"https://leetify.com/app/profile/{steam_id}",
    )

    embed.add_field(
        name="📊 Stats",
        value=(
            f"**K / D / A** : `{kills} / {deaths} / {assists}`\n"
            f"**K/D** : `{kd:.2f}`\n"
            f"**HS%** : `{hs_pct}`\n"
            f"**ADR** : `{adr:.0f}`\n"
            f"**MVPs** : `{mvps}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="🎯 Leetify Rating",
        value=(
            f"**Global** : `{rating_display}`\n"
            f"**CT Side** : `{ct_display}`\n"
            f"**T Side** : `{t_display}`\n"
            f"**Précision** : `{accuracy}`\n"
            f"**Reaction** : `{reaction:.0f}ms`"
        ),
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(
        name="🗺️ Partie",
        value=(
            f"**Map** : {map_name}\n"
            f"**Mode** : {mode}\n"
            f"**Terminée** : {ts}"
        ),
        inline=True,
    )
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
        return self._static_ids | set(get_all_linked_steam_ids())

    # ─────────────────────────────── Lifecycle ────────────────────
    async def cog_load(self):
        _ensure_table()
        if self._is_configured():
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
            self._seen_matches[sid] = {m["id"] for m in matches if m.get("id")}
        total = sum(len(v) for v in self._seen_matches.values())
        logger.info(f"✅ Init — {total} matchs connus, {len(self._all_ids())} joueur(s)")

    # ─────────────────────────────── Check joueur ─────────────────
    async def _check_player(self, steam_id: str):
        matches = await self.leetify.get_matches(steam_id)
        if not matches:
            return

        seen = self._seen_matches.setdefault(steam_id, set())
        new  = [m for m in matches if m.get("id") and m["id"] not in seen]
        for m in new:
            seen.add(m["id"])

        if not new:
            return

        # Récupérer le nom du joueur via son profil
        profile     = await self.leetify.get_profile(steam_id)
        player_name = profile.get("name", steam_id) if profile else steam_id

        channel = self.bot.get_channel(self.channel_id)
        if not channel:
            logger.warning(f"Channel {self.channel_id} introuvable")
            return

        for match in new:
            # Les stats du joueur sont déjà dans match["stats"][] depuis /v3/profile/matches
            stats = _extract_player_stats(match, steam_id)
            if not stats:
                logger.warning(f"Stats introuvables pour {steam_id} dans match {match['id']}")
                continue

            embed = build_match_embed(player_name, steam_id, match, stats)
            try:
                await channel.send(embed=embed)
                logger.info(f"📬 {player_name} | {match['id']} | {match.get('outcome', '?')}")
            except discord.HTTPException as e:
                logger.error(f"❌ Post {match['id']}: {e}")

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

        profile = await self.leetify.get_profile(steam64)
        if not profile:
            return await inter.followup.send(
                f"⚠️ `{steam64}` introuvable sur Leetify.\n"
                "Assure-toi d'avoir un compte Leetify actif lié à ce Steam.",
                ephemeral=True,
            )

        set_steam_link(str(inter.user.id), steam64)

        # Initialiser le tracker pour ce joueur immédiatement
        if steam64 not in self._seen_matches:
            matches = await self.leetify.get_matches(steam64)
            self._seen_matches[steam64] = {m["id"] for m in matches if m.get("id")}
            logger.info(f"➕ Nouveau joueur tracké: {steam64} ({inter.user.name})")

        player_name = profile.get("name", steam64)
        ranks       = profile.get("ranks", {})
        rating      = profile.get("rating", {})
        premier     = ranks.get("premier")
        aim         = _f(rating.get("aim"))
        total       = _i(profile.get("total_matches"))
        winrate     = _f(profile.get("winrate")) * 100

        embed = discord.Embed(
            title="✅ Compte Steam lié !",
            colour=discord.Colour.green(),
            description=f"{inter.user.mention} → **{player_name}**",
        )
        embed.add_field(name="Steam64 ID",   value=f"`{steam64}`",         inline=True)
        embed.add_field(name="🏅 Premier",   value=_rank_str(premier),     inline=True)
        embed.add_field(name="🎯 Parties",   value=str(total),             inline=True)
        embed.add_field(name="📈 Win Rate",  value=f"{winrate:.1f}%",      inline=True)
        embed.add_field(name="🎯 Aim Score", value=_score_icon(aim),       inline=True)
        embed.add_field(
            name="Profil Leetify",
            value=f"[Voir le profil](https://leetify.com/app/profile/{steam64})",
            inline=True,
        )
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

        # ── Extraction des champs réels de l'API ──────────────────
        player_name = profile.get("name", steam64)
        ranks       = profile.get("ranks", {})
        rating      = profile.get("rating", {})
        stats_prof  = profile.get("stats", {})

        premier     = ranks.get("premier")
        faceit      = ranks.get("faceit")
        wingman     = ranks.get("wingman")
        leetify_r   = _f(ranks.get("leetify"))

        aim         = _f(rating.get("aim"))
        positioning = _f(rating.get("positioning"))
        utility     = _f(rating.get("utility"))
        ct_leetify  = _f(rating.get("ct_leetify")) * 100   # ratio → display
        t_leetify   = _f(rating.get("t_leetify")) * 100

        total_matches = _i(profile.get("total_matches"))
        winrate       = _f(profile.get("winrate")) * 100
        hs_pct        = _f(stats_prof.get("accuracy_head"))
        reaction      = _f(stats_prof.get("reaction_time_ms"))
        preaim        = _f(stats_prof.get("preaim"))

        # Rang compétitif par map
        competitive = ranks.get("competitive", [])
        comp_lines  = "\n".join(
            f"• {_map_name(r['map_name'])} : rang `{r['rank']}`"
            for r in competitive if r.get("rank", 0) > 0
        ) or "—"

        bans = profile.get("bans", [])
        ban_str = "\n".join(
            f"⚠️ Banni sur **{b['platform'].upper()}** ({b['platform_nickname']}) le {b['banned_since'][:10]}"
            for b in bans
        ) if bans else None

        embed = discord.Embed(
            title=f"💥 {player_name}",
            colour=discord.Colour.from_rgb(66, 133, 244),
            url=f"https://leetify.com/app/profile/{steam64}",
        )
        embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)

        embed.add_field(name="Steam64",       value=f"`{steam64}`",          inline=False)
        embed.add_field(name="🏅 Premier",    value=_rank_str(premier),      inline=True)
        embed.add_field(name="⚡ FACEIT",     value=f"lvl {faceit}" if faceit else "—", inline=True)
        embed.add_field(name="🤝 Wingman",    value=f"rang {wingman}" if wingman else "—", inline=True)
        embed.add_field(name="🎯 Parties",    value=str(total_matches),      inline=True)
        embed.add_field(name="📈 Win Rate",   value=f"{winrate:.1f}%",       inline=True)
        embed.add_field(name="🎖️ Leetify",   value=f"{leetify_r:+.2f}",     inline=True)

        embed.add_field(
            name="📊 Scores Leetify",
            value=(
                f"**Aim** : {_score_icon(aim)}  "
                f"**Utility** : {_score_icon(utility)}  "
                f"**Positioning** : {_score_icon(positioning)}\n"
                f"**CT** : `{ct_leetify:+.2f}`  **T** : `{t_leetify:+.2f}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔫 Précision",
            value=(
                f"**HS%** : `{hs_pct:.1f}%`\n"
                f"**Reaction** : `{reaction:.0f}ms`\n"
                f"**Pré-aim** : `{preaim:.1f}°`"
            ),
            inline=True,
        )
        embed.add_field(name="🗺️ Rangs compétitifs", value=comp_lines, inline=True)

        if ban_str:
            embed.add_field(name="🚫 Bans", value=ban_str, inline=False)

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
