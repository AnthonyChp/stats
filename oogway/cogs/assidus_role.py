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


async def _compute_scores(guild: discord.Guild) -> tuple[list[tuple[int, int]], dict]:
    """
    Retourne ([(discord_id, score)] top 10, debug_info).
    debug_info contient les détails de chaque étape pour /debug-assidus.
    """
    # SQLite stocke des datetimes naïfs (UTC) — on compare sans timezone
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=WINDOW)
    debug: dict = {"cutoff": cutoff.isoformat(), "ranked": {}, "customs": {}, "scores_final": {}, "not_in_guild": []}

    # ── Ranked matches ────────────────────────────────────────────────────────
    ranked: dict[str, int] = {}
    with SessionLocal() as db:
        total_matches = db.query(func.count(Match.match_id)).scalar()
        debug["total_matches_db"] = total_matches

        rows = (
            db.query(Match.puuid, func.count(Match.match_id))
            .filter(Match.timestamp >= cutoff)
            .group_by(Match.puuid)
            .all()
        )
        for puuid, cnt in rows:
            ranked[puuid] = cnt

        debug["ranked_puuids_count"] = len(ranked)

        puuid_to_discord: dict[str, int] = {}
        all_users = db.query(User).all()
        debug["total_users_linked"] = len(all_users)
        for u in all_users:
            puuid_to_discord[u.puuid] = int(u.discord_id)

    scores: dict[int, int] = {}
    for puuid, cnt in ranked.items():
        did = puuid_to_discord.get(puuid)
        if did:
            scores[did] = scores.get(did, 0) + cnt
            debug["ranked"][str(did)] = cnt
        else:
            debug["ranked"][f"puuid:{puuid[:12]}…"] = f"{cnt} (non lié)"

    # ── Customs (historique Redis) ────────────────────────────────────────────
    series_list = await load_all_series()
    debug["total_series_redis"] = len(series_list)
    debug["series_detail"] = []
    series_in_window = 0
    for s in series_list:
        in_window = not s.started_at or s.started_at >= cutoff.timestamp()
        players = [did for did in (s.team_a + s.team_b) if did and did < 9_000_000_000_000_000]
        debug["series_detail"].append({
            "id": s.id,
            "started_at": s.started_at,
            "in_window": in_window,
            "team_a": s.team_a,
            "team_b": s.team_b,
            "players_count": len(players),
        })
        if not in_window:
            continue
        series_in_window += 1
        for did in players:
            scores[did] = scores.get(did, 0) + 10
            debug["customs"][str(did)] = debug["customs"].get(str(did), 0) + 10
    debug["series_in_window"] = series_in_window

    # ── Résoudre les membres via API (get_member utilise le cache, fetch_member fait un appel API) ──
    top_all = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    result = []
    for did, sc in top_all:
        debug["scores_final"][str(did)] = sc
        member = guild.get_member(did)
        if member is None:
            try:
                member = await guild.fetch_member(did)
            except discord.NotFound:
                debug["not_in_guild"].append(did)
                continue
            except discord.HTTPException:
                debug["not_in_guild"].append(did)
                continue
        result.append((did, sc))
        if len(result) == TOP_N:
            break

    return result, debug


async def _apply_role(guild: discord.Guild, role: discord.Role) -> tuple[list[str], list[str], dict]:
    top, debug = await _compute_scores(guild)
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
        if member is None:
            try:
                member = await guild.fetch_member(did)
            except discord.HTTPException:
                continue
        if role not in member.roles:
            try:
                await member.add_roles(role, reason=f"Assidus: top 10 ({sc} parties/30 j)")
                added.append(f"{member.display_name} ({sc})")
            except discord.HTTPException as e:
                logger.warning("Impossible d'ajouter le rôle à %s : %s", member, e)

    return added, removed, debug


class AssidusRoleCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._update_assidus.start()

    def cog_unload(self) -> None:
        self._update_assidus.cancel()

    @tasks.loop(time=dt.time(hour=6, minute=0, tzinfo=TZ_PARIS))
    async def _update_assidus(self) -> None:
        if dt.datetime.now(TZ_PARIS).weekday() != 0:
            return
        await self._run_update()

    @_update_assidus.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _run_update(self, guild: discord.Guild | None = None) -> tuple[list[str], list[str]]:
        if not settings.ASSIDUS_ROLE_ID:
            logger.warning("ASSIDUS_ROLE_ID non configuré — skip.")
            return [], []

        guild = guild or discord.utils.get(self.bot.guilds)
        if guild is None:
            return [], []

        role = guild.get_role(settings.ASSIDUS_ROLE_ID)
        if role is None:
            logger.error("Rôle ASSIDUS_ROLE_ID %s introuvable.", settings.ASSIDUS_ROLE_ID)
            return [], []

        added, removed, _ = await _apply_role(guild, role)
        logger.info("✅ Assidus — ajoutés: %s | retirés: %s", added or "aucun", removed or "aucun")
        return added, removed

    # ── /update-assidus ───────────────────────────────────────────────────────
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
            lines.append("**Nouveau(x) :**\n" + "\n".join(f"+ {n}" for n in added))
        if removed:
            lines.append("**Retiré(s) :**\n" + "\n".join(f"- {n}" for n in removed))
        if not lines:
            lines.append("Aucun changement (tout le monde a déjà le bon rôle, ou personne n'a de score).\nUtilise `/debug-assidus` pour voir le détail.")

        await inter.followup.send("\n\n".join(lines), ephemeral=True)

    # ── /debug-assidus ────────────────────────────────────────────────────────
    @app_commands.command(
        name="debug-assidus",
        description="Affiche le détail du calcul des scores Assidu (organisateurs)"
    )
    @app_commands.checks.has_role(settings.ORGANIZER_ROLE_ID)
    async def debug_assidus(self, inter: Interaction) -> None:
        await inter.response.defer(ephemeral=True)

        if not inter.guild:
            return await inter.followup.send("Pas de guild.", ephemeral=True)

        top, dbg = await _compute_scores(inter.guild)

        lines = [
            f"**Fenêtre :** 30 jours (depuis `{dbg['cutoff'][:10]}`)",
            f"**Matches total en DB :** {dbg.get('total_matches_db', '?')}",
            f"**Matches dans la fenêtre :** {dbg['ranked_puuids_count']} puuids",
            f"**Users linkés :** {dbg['total_users_linked']}",
            f"**Séries Redis total :** {dbg['total_series_redis']} | dans la fenêtre : {dbg['series_in_window']}",
            "",
        ]

        # Scores finaux — résoudre les noms via fetch_member
        member_cache: dict[int, str] = {}
        for did_str in dbg["scores_final"]:
            did = int(did_str)
            m = inter.guild.get_member(did)
            if m is None:
                try:
                    m = await inter.guild.fetch_member(did)
                except discord.HTTPException:
                    pass
            member_cache[did] = m.display_name if m else f"❓ absent ({did_str})"

        if dbg["scores_final"]:
            lines.append("**Scores calculés :**")
            for did_str, sc in sorted(dbg["scores_final"].items(), key=lambda x: -x[1]):
                name = member_cache.get(int(did_str), f"❓ ({did_str})")
                ranked_pts = dbg["ranked"].get(did_str, 0)
                custom_pts = dbg["customs"].get(did_str, 0)
                rank_str = " ✅" if any(d == int(did_str) for d, _ in top) else ""
                lines.append(f"  `{name}` — {sc} pts (ranked:{ranked_pts} + customs:{custom_pts}){rank_str}")
        else:
            lines.append("⚠️ **Aucun score calculé.**")

        if dbg["not_in_guild"]:
            lines.append(f"\n⚠️ {len(dbg['not_in_guild'])} joueur(s) absent(s) du serveur.")

        # Détail des séries
        lines.append("\n**Séries Redis (détail) :**")
        for sd in dbg.get("series_detail", []):
            import datetime as _dt
            ts = sd["started_at"]
            date_str = _dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?"
            flag = "✅" if sd["in_window"] else "❌ hors fenêtre"
            lines.append(f"  `{sd['id']}` {date_str} {flag} — {sd['players_count']} joueurs (A:{sd['team_a']} B:{sd['team_b']})")

        # Rôle
        role_id = settings.ASSIDUS_ROLE_ID
        role = inter.guild.get_role(role_id) if role_id else None
        if role:
            lines.append(f"\n**Rôle `{role.name}` :** {len(role.members)} membre(s) actuels")
        else:
            lines.append(f"\n❌ **Rôle ASSIDUS_ROLE_ID `{role_id}` introuvable dans le serveur !**")

        # Découper en blocs ≤ 1900 caractères pour rester sous la limite Discord
        content = "\n".join(lines)
        chunks = []
        while len(content) > 1900:
            cut = content.rfind("\n", 0, 1900)
            if cut == -1:
                cut = 1900
            chunks.append(content[:cut])
            content = content[cut:].lstrip("\n")
        chunks.append(content)

        for chunk in chunks:
            await inter.followup.send(chunk, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AssidusRoleCog(bot))
