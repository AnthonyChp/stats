# oogway/cogs/oogscore_admin.py
from __future__ import annotations

import asyncio
import logging
import datetime as dt
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from oogway.config import settings
from oogway.database import SessionLocal, Match, MatchParticipant, User
from oogway.db.riot_cache import MatchCache
from oogway.oogscore.extract import participant_to_db_fields
from oogway.oogscore.tasks import refresh_baseline, load_baseline_from_db, get_baseline_cache
from oogway.riot.client import RiotClient

log = logging.getLogger(__name__)

# Shared backfill state (module-level for status reporting)
_backfill_state = {
    "running": False,
    "total": 0,
    "done": 0,
    "skipped": 0,
    "errors": 0,
    "api_fetched": 0,
    "cache_hits": 0,
    "started_at": None,
    "finished_at": None,
}

RANKED_QUEUES = {420, 440}


async def _backfill_task(riot_client: RiotClient, region: str, linked_puuids: set[str]):
    """
    Background coroutine that backfills match_participants from match_cache + API.

    Strategy:
    1. Get all distinct match_ids from `matches` table
    2. Subtract those already in `match_participants`
    3. For each remaining match_id:
       a. Check match_cache first (no API call)
       b. If not cached, fetch from Riot API
       c. Filter: only ranked queues (420, 440)
       d. Insert all 10 participants into match_participants
    4. After all done, rebuild baseline
    """
    global _backfill_state
    _backfill_state["running"] = True
    _backfill_state["started_at"] = dt.datetime.utcnow()
    _backfill_state["finished_at"] = None

    try:
        with SessionLocal() as session:
            # Get all match_ids from matches table
            all_match_ids = [row[0] for row in session.query(Match.match_id).distinct().all()]

            # Get match_ids already processed in match_participants
            done_match_ids = set(
                row[0] for row in session.query(MatchParticipant.match_id).distinct().all()
            )

            pending = [mid for mid in all_match_ids if mid not in done_match_ids]
            # Process most recent first (sort descending by match_id — Riot IDs encode timestamp)
            pending.sort(reverse=True)

            _backfill_state["total"] = len(pending)
            _backfill_state["done"] = 0
            _backfill_state["skipped"] = 0
            _backfill_state["errors"] = 0
            _backfill_state["api_fetched"] = 0
            _backfill_state["cache_hits"] = 0

            log.info(f"[backfill] Starting: {len(pending)} matches to process")

        for match_id in pending:
            if not _backfill_state["running"]:
                log.info("[backfill] Stopped by user")
                break
            try:
                match_data = None

                # 1. Try cache first
                with SessionLocal() as session:
                    cached = session.query(MatchCache).filter(MatchCache.match_id == match_id).first()
                    if cached and cached.json:
                        match_data = cached.json
                        _backfill_state["cache_hits"] += 1

                # 2. Fetch from API if not cached
                if match_data is None:
                    match_data = await riot_client.get_match_by_id(region, match_id)
                    if match_data is None:
                        # 404 — match expired or not found
                        _backfill_state["skipped"] += 1
                        _backfill_state["done"] += 1
                        continue
                    _backfill_state["api_fetched"] += 1
                    # Cache it for future use
                    with SessionLocal() as session:
                        existing = session.query(MatchCache).filter(MatchCache.match_id == match_id).first()
                        if not existing:
                            session.add(MatchCache(match_id=match_id, region=region, json=match_data))
                            session.commit()

                # 3. Filter queue
                info = match_data.get("info", {})
                queue_id = info.get("queueId", 0)
                if queue_id not in RANKED_QUEUES:
                    _backfill_state["skipped"] += 1
                    _backfill_state["done"] += 1
                    continue

                # 4. Insert participants
                participants = info.get("participants", [])
                game_duration = info.get("gameDuration", 0)

                with SessionLocal() as session:
                    # Double-check not already inserted (idempotency)
                    existing_count = session.query(MatchParticipant).filter(
                        MatchParticipant.match_id == match_id
                    ).count()
                    if existing_count > 0:
                        _backfill_state["skipped"] += 1
                        _backfill_state["done"] += 1
                        continue

                    for p in participants:
                        fields = participant_to_db_fields(
                            participant=p,
                            game_duration_seconds=game_duration,
                            match_id=match_id,
                            linked_puuids=linked_puuids,
                        )
                        session.add(MatchParticipant(**fields))
                    session.commit()

                _backfill_state["done"] += 1

                # Log progress every 100 matches
                if _backfill_state["done"] % 100 == 0:
                    log.info(
                        f"[backfill] {_backfill_state['done']}/{_backfill_state['total']} "
                        f"(cache: {_backfill_state['cache_hits']}, api: {_backfill_state['api_fetched']}, "
                        f"skip: {_backfill_state['skipped']})"
                    )

            except Exception as e:
                log.error(f"[backfill] Error processing {match_id}: {e}")
                _backfill_state["errors"] += 1
                _backfill_state["done"] += 1
                await asyncio.sleep(1)  # Brief pause on error

        # 5. Rebuild baseline when done
        log.info("[backfill] Backfill complete. Rebuilding baseline...")
        with SessionLocal() as session:
            n = refresh_baseline(session)
        log.info(f"[backfill] Baseline rebuilt: {n} scopes")

    except Exception as e:
        log.error(f"[backfill] Fatal error: {e}", exc_info=True)
    finally:
        _backfill_state["running"] = False
        _backfill_state["finished_at"] = dt.datetime.utcnow()
        log.info("[backfill] Task finished")


class OogScoreAdmin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._backfill_task_handle: Optional[asyncio.Task] = None
        self._riot_client = RiotClient(settings.RIOT_API_KEY)

    async def cog_unload(self):
        if self._backfill_task_handle and not self._backfill_task_handle.done():
            _backfill_state["running"] = False
            self._backfill_task_handle.cancel()
        await self._riot_client.close()

    def _get_linked_puuids(self) -> set[str]:
        with SessionLocal() as session:
            return {row[0] for row in session.query(User.puuid).all()}

    oogscore_group = app_commands.Group(name="oogscore", description="OogScore v2 administration")

    @oogscore_group.command(name="backfill", description="Lance le backfill des participants historiques")
    @app_commands.default_permissions(administrator=True)
    async def backfill(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if _backfill_state["running"]:
            await interaction.followup.send(
                f"⚠️ Backfill déjà en cours : {_backfill_state['done']}/{_backfill_state['total']} matches traités.",
                ephemeral=True,
            )
            return

        linked_puuids = self._get_linked_puuids()
        region = settings.DEFAULT_REGION

        self._backfill_task_handle = asyncio.create_task(
            _backfill_task(self._riot_client, region, linked_puuids)
        )

        await interaction.followup.send(
            "🚀 **Backfill OogScore v2 lancé !**\n"
            "Il tourne en arrière-plan. Utilise `/oogscore status` pour suivre l'avancement.\n"
            "Les matches en cache local seront traités sans appel API.",
            ephemeral=True,
        )

    @oogscore_group.command(name="status", description="Affiche l'état du backfill et de la baseline")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        s = _backfill_state
        cache = get_baseline_cache()

        # Baseline stats
        role_scopes = sum(1 for k in cache if k.startswith("role:"))
        champ_scopes = sum(1 for k in cache if k.startswith("champ:"))

        with SessionLocal() as session:
            participant_count = session.query(MatchParticipant).count()
            scorable_count = session.query(MatchParticipant).filter(
                MatchParticipant.is_scorable == True
            ).count()

        embed = discord.Embed(title="OogScore v2 — Status", color=0x5865F2)

        # Backfill status
        if s["running"]:
            pct = (s["done"] / max(1, s["total"])) * 100
            elapsed = (dt.datetime.utcnow() - s["started_at"]).seconds if s["started_at"] else 0
            embed.add_field(
                name="🔄 Backfill en cours",
                value=(
                    f"**{s['done']}/{s['total']}** ({pct:.1f}%)\n"
                    f"Cache: {s['cache_hits']} · API: {s['api_fetched']} · Skip: {s['skipped']} · Erreurs: {s['errors']}\n"
                    f"Temps écoulé: {elapsed//60}m{elapsed%60}s"
                ),
                inline=False,
            )
        elif s["finished_at"]:
            duration = (s["finished_at"] - s["started_at"]).seconds if s["started_at"] else 0
            embed.add_field(
                name="✅ Dernier backfill terminé",
                value=(
                    f"**{s['done']}/{s['total']}** traités\n"
                    f"Cache: {s['cache_hits']} · API: {s['api_fetched']} · Skip: {s['skipped']} · Erreurs: {s['errors']}\n"
                    f"Durée: {duration//60}m{duration%60}s"
                ),
                inline=False,
            )
        else:
            embed.add_field(name="⏸️ Backfill", value="Pas encore lancé. `/oogscore backfill`", inline=False)

        # DB stats
        embed.add_field(
            name="📊 Base de données",
            value=f"**{participant_count}** participants · **{scorable_count}** scorables",
            inline=False,
        )

        # Baseline stats
        embed.add_field(
            name="🎯 Baseline en mémoire",
            value=f"**{role_scopes}** rôles · **{champ_scopes}** champions" if cache else "Vide (pas encore calculée)",
            inline=False,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @oogscore_group.command(name="rebuild", description="Recalcule la baseline depuis les données existantes")
    @app_commands.default_permissions(administrator=True)
    async def rebuild(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        with SessionLocal() as session:
            participant_count = session.query(MatchParticipant).filter(
                MatchParticipant.is_scorable == True
            ).count()

        if participant_count == 0:
            await interaction.followup.send(
                "❌ Aucun participant scorable en base. Lance d'abord `/oogscore backfill`.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"⏳ Reconstruction de la baseline depuis {participant_count} participants...",
            ephemeral=True,
        )

        with SessionLocal() as session:
            n = refresh_baseline(session)

        await interaction.channel.send(
            f"✅ **Baseline OogScore v2 reconstruite** — {n} scopes (rôles + champions)."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(OogScoreAdmin(bot))
