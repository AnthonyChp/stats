# oogway/cogs/assidus_role.py — Rôle @LoL Assidu — top 10 joueurs actifs (30 j)
# Mise à jour automatique chaque lundi à 06h00 (Europe/Paris).

from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

import discord
from discord import Interaction, app_commands
from discord.ext import commands, tasks
from sqlalchemy import func

from oogway.config import settings
from oogway.database import SessionLocal, Match, User
from oogway.cogs.historique import load_all_series

logger = logging.getLogger(__name__)
TZ_PARIS = ZoneInfo("Europe/Paris")

TOP_N   = 10
WINDOW  = 30  # jours glissants


async def _compute_scores(guild: discord.Guild) -> list[tuple[int, int]]:
    """Retourne [(discord_id, score)] triés par score décroissant (top 10)."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=WINDOW)

    # ── Ranked matches ────────────────────────────────────────────────────────
    ranked: dict[str, int] = {}          # puuid → nb parties ranked
    with SessionLocal() as db:
        rows = (
            db.query(Match.puuid, func.count(Match.match_id))
            .filter(Match.timestamp >= cutoff)
            .group_by(Match.puuid)
            .all()
        )
        for puuid, cnt in rows:
            ranked[puuid] = cnt

        # Convertir puuid → discord_id
        puuid_to_discord: dict[str, int] = {}
        if ranked:
            users = db.query(User).filter(User.puuid.in_(ranked.keys())).all()
            for u in users:
                puuid_to_discord[u.puuid] = int(u.discord_id)

    scores: dict[int, int] = {}  # discord_id → score total
    for puuid, cnt in ranked.items():
        did = puuid_to_discord.get(puuid)
        if did:
            scores[did] = scores.get(did, 0) + cnt

    # ── Customs (historique Redis) ────────────────────────────────────────────
    series_list = await load_all_series()
    for s in series_list:
        if s.started_at and s.started_at < cutoff.timestamp():
            continue
        for did in s.team_a + s.team_b:
            if did and did < 9_000_000_000_000_000:  # exclure bots factices
                scores[did] = scores.get(did, 0) + 1

    # ── Top N membres présents dans le guild ──────────────────────────────────
    top = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    result = []
    for did, sc in top:
        if guild.get_member(did):
            result.append((did, sc))
        if len(result) == TOP_N:
            break
    return result


async def _apply_role(guild: discord.Guild, role: discord.Role) -> tuple[list[str], list[str]]:
    """Attribue/retire le rôle selon le top 10 actuel. Retourne (added, removed)."""
    top = await _compute_scores(guild)
    top_ids = {did for did, _ in top}
    added, removed = [], []

    for member in role.members:
        if member.id not in top_ids:
            try:
                await member.remove_roles(role, reason="Assidus: sorti du top 10")
                removed.append(member.display_name)
            except discord.HTTPException as e:
                logger.warning("Impossible de retirer le rôle à %s : %s", member, e)

    for did, sc in top:
        member = guild.get_member(did)
        if member and role not in member.roles:
            try:
                await member.add_roles(role, reason=f"Assidus: top 10 ({sc} parties/30 j)")
                added.append(f"{member.display_name} ({sc})")
            except discord.HTTPException as e:
                logger.warning("Impossible d'ajouter le rôle à %s : %s", member, e)

    return added, removed


class AssidusRoleCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._update_assidus.start()

    def cog_unload(self) -> None:
        self._update_assidus.cancel()

    # ── Task automatique — chaque lundi à 06h00 ───────────────────────────────
    @tasks.loop(time=dt.time(hour=6, minute=0, tzinfo=TZ_PARIS))
    async def _update_assidus(self) -> None:
        if dt.datetime.now(TZ_PARIS).weekday() != 0:
            return
        await self._run_update()

    @_update_assidus.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # ── Logique partagée ──────────────────────────────────────────────────────
    async def _run_update(self, guild: discord.Guild | None = None) -> tuple[list[str], list[str]]:
        if not settings.ASSIDUS_ROLE_ID:
            logger.warning("ASSIDUS_ROLE_ID non configuré — skip mise à jour.")
            return [], []

        guild = guild or discord.utils.get(self.bot.guilds)
        if guild is None:
            return [], []

        role = guild.get_role(settings.ASSIDUS_ROLE_ID)
        if role is None:
            logger.error("Rôle ASSIDUS_ROLE_ID %s introuvable.", settings.ASSIDUS_ROLE_ID)
            return [], []

        added, removed = await _apply_role(guild, role)
        logger.info(
            "✅ Assidus mis à jour — ajoutés: %s | retirés: %s",
            added or "aucun", removed or "aucun"
        )
        return added, removed

    # ── /update-assidus — déclenchement manuel ────────────────────────────────
    @app_commands.command(
        name="update-assidus",
        description="Recalcule et met à jour le rôle @LoL Assidu maintenant (organisateurs)"
    )
    @app_commands.checks.has_role(settings.ORGANIZER_ROLE_ID)
    async def update_assidus(self, inter: Interaction) -> None:
        await inter.response.defer(ephemeral=True)

        added, removed = await self._run_update(inter.guild)

        lines = []
        if added:
            lines.append("**Nouveau(x) assidu(s) :**\n" + "\n".join(f"+ {n}" for n in added))
        if removed:
            lines.append("**Retiré(s) du top 10 :**\n" + "\n".join(f"- {n}" for n in removed))
        if not lines:
            lines.append("Aucun changement.")

        await inter.followup.send("\n\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AssidusRoleCog(bot))
