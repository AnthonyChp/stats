from __future__ import annotations
import difflib
import logging
from io import BytesIO
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from oogway.config import settings
from oogway.database import SessionLocal, User, MatchParticipant
from oogway.oogscore.tasks import get_baseline_cache
from oogway.oogscore.baseline import load_for
from oogway.oogscore.weights import ROLE_WEIGHTS, grade_from_score
from oogway.oogscore.analyse.aggregate import get_player_aggregate, get_player_component_percentiles, get_baseline_component_percentiles
from oogway.oogscore.analyse.categories import visible_axes, visible_categories
from oogway.oogscore.analyse.insights import generate_insights, pct_to_text, COMP_LABELS
from oogway.oogscore.analyse.render import render_radar, render_curve
from oogway.oogscore.analyse.pages import (
    build_page1_baseline, build_page1_joueur,
    build_page2_baseline, build_page2_joueur,
    build_page3_joueur, build_page3_no_data, build_page3_not_linked,
    GRADE_ACCENTS, GRADE_COLORS,
)
from oogway.oogscore.analyse.view import AnalyseView

log = logging.getLogger(__name__)

ROLES = ["TOP", "JUNGLE", "MID", "ADC", "SUPPORT"]
MIN_PLAYER_GAMES = 10
MIN_BASELINE_ROLE = 100

# Champion name normalization cache (built lazily)
_known_champions: list[str] = []

def _normalize_champion(name: str) -> Optional[str]:
    """Normalize champion name using difflib fuzzy match against known list."""
    if not name:
        return None
    # Try exact match first (case-insensitive)
    for c in _known_champions:
        if c.lower() == name.lower():
            return c
    # Fuzzy match
    matches = difflib.get_close_matches(name.lower(), [c.lower() for c in _known_champions], n=1, cutoff=0.6)
    if matches:
        for c in _known_champions:
            if c.lower() == matches[0]:
                return c
    return None

def _populate_known_champions(session):
    """Populate _known_champions from MatchParticipant if empty."""
    global _known_champions
    if _known_champions:
        return
    rows = session.query(MatchParticipant.champion).distinct().all()
    _known_champions = sorted([r[0] for r in rows if r[0]])

def _infer_role(session, champion: str) -> Optional[str]:
    """Infer dominant role for champion from MatchParticipant data."""
    from sqlalchemy import func
    rows = (
        session.query(MatchParticipant.role, func.count(MatchParticipant.id).label("cnt"))
        .filter(MatchParticipant.champion == champion, MatchParticipant.role != None)
        .group_by(MatchParticipant.role)
        .order_by(func.count(MatchParticipant.id).desc())
        .all()
    )
    if not rows:
        return None
    total = sum(r.cnt for r in rows)
    top_role, top_cnt = rows[0].role, rows[0].cnt
    if total > 0 and top_cnt / total >= 0.60:
        return top_role
    return None  # ambiguous


class OogScoreAnalyse(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="analyse", description="Analyse de performance par champion/rôle (baseline + stats perso)")
    @app_commands.describe(
        champion="Nom du champion (ex: Lux, Rakan, LeeSin)",
        role="Rôle (TOP/JUNGLE/MID/ADC/SUPPORT)",
        joueur="Joueur à analyser (toi par défaut)",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name=r, value=r) for r in ROLES
    ])
    async def analyse(
        self,
        interaction: discord.Interaction,
        champion: Optional[str] = None,
        role: Optional[str] = None,
        joueur: Optional[discord.Member] = None,
    ):
        await interaction.response.defer(ephemeral=False)

        target = joueur or interaction.user

        with SessionLocal() as session:
            _populate_known_champions(session)

            # Resolve champion name
            resolved_champion = None
            if champion:
                resolved_champion = _normalize_champion(champion)
                if resolved_champion is None:
                    close = difflib.get_close_matches(champion, _known_champions, n=3, cutoff=0.4)
                    hint = f"\n💡 Tu voulais dire : {', '.join(close)} ?" if close else ""
                    await interaction.followup.send(
                        f"❌ Champion **{champion}** introuvable en base.{hint}", ephemeral=True
                    )
                    return

            # Resolve role
            resolved_role = role
            if resolved_champion and not resolved_role:
                resolved_role = _infer_role(session, resolved_champion)
                if resolved_role is None:
                    await interaction.followup.send(
                        f"⚠️ Le rôle dominant de **{resolved_champion}** est ambigu en base. "
                        "Précise le rôle avec le paramètre `role`.", ephemeral=True
                    )
                    return

            # No champion and no role: try to infer from linked user's main
            if not resolved_champion and not resolved_role:
                linked = session.query(User).filter(User.discord_id == str(target.id)).first()
                if linked:
                    from sqlalchemy import func
                    row = (
                        session.query(MatchParticipant.champion, MatchParticipant.role, func.count().label("cnt"))
                        .filter(MatchParticipant.puuid == linked.puuid, MatchParticipant.is_scorable == True)
                        .group_by(MatchParticipant.champion, MatchParticipant.role)
                        .order_by(func.count().desc())
                        .first()
                    )
                    if row:
                        resolved_champion = row.champion
                        resolved_role = row.role
                    else:
                        await interaction.followup.send(
                            "Tu n'as pas encore de games enregistrées. Joue quelques ranked d'abord !", ephemeral=True
                        )
                        return
                else:
                    await interaction.followup.send(
                        "Précise un champion et/ou un rôle, ou utilise `/link` pour que la commande détecte ton main automatiquement.",
                        ephemeral=True,
                    )
                    return

            # No champion but role given: role-wide baseline
            if not resolved_champion and resolved_role:
                resolved_champion = "Tous champions"

            # Load baseline
            cache = get_baseline_cache()
            if resolved_champion == "Tous champions":
                baseline = load_for(resolved_role, "", cache)
            else:
                baseline = load_for(resolved_role, resolved_champion, cache)

            if not baseline or baseline.source == "no_baseline":
                await interaction.followup.send(
                    f"❌ Pas de baseline disponible pour **{resolved_role}**. Lance `/oogscore rebuild` d'abord.",
                    ephemeral=True,
                )
                return

            is_low_confidence = baseline.sample_size < MIN_BASELINE_ROLE
            dists = baseline.distributions

            # Check if target is linked
            linked_user = session.query(User).filter(User.discord_id == str(target.id)).first()
            player_agg = None
            player_percentiles = None
            insights = None
            mode = "baseline"

            if linked_user and resolved_champion != "Tous champions":
                player_agg = get_player_aggregate(session, linked_user.puuid, resolved_champion, resolved_role)
                if player_agg and player_agg.n_games >= 1:
                    mode = "joueur"
                    player_percentiles = get_player_component_percentiles(player_agg, dists)
                    insights = generate_insights(player_percentiles, resolved_role)

            axes = visible_axes(resolved_role)
            baseline_p50 = get_baseline_component_percentiles(dists)

            # Build pages
            if mode == "joueur":
                is_indicative = player_agg.n_games < MIN_PLAYER_GAMES
                grade = grade_from_score(player_agg.avg_score)
                accent = GRADE_ACCENTS.get(grade, "#5865F2")

                p1 = build_page1_joueur(
                    resolved_champion, resolved_role, player_percentiles,
                    player_agg.n_games, player_agg.avg_score, insights,
                    is_indicative, baseline.sample_size, baseline.source,
                )
                p2 = build_page2_joueur(
                    resolved_champion, resolved_role, player_percentiles,
                    dists, player_agg.n_games, baseline.sample_size,
                )
                # Page 3
                if len(player_agg.score_history) >= 3:
                    p3 = build_page3_joueur(resolved_champion, resolved_role, player_agg.score_history)
                else:
                    p3 = build_page3_no_data(resolved_champion, resolved_role)

                # Render images
                radar_buf = render_radar(player_percentiles, axes, accent)
                curve_buf = render_curve(player_agg.score_history, resolved_champion, resolved_role) if len(player_agg.score_history) >= 3 else None

            else:
                p1 = build_page1_baseline(
                    resolved_champion, resolved_role, dists, baseline_p50,
                    baseline.sample_size, is_low_confidence,
                )
                p2 = build_page2_baseline(resolved_champion, resolved_role, dists, baseline.sample_size)
                p3 = build_page3_not_linked() if not linked_user else build_page3_no_data(resolved_champion, resolved_role)
                accent = "#5865F2"
                radar_buf = render_radar(None, axes, accent)
                curve_buf = None

        embeds = [p1, p2, p3]

        # Collect image bytes (store raw bytes to allow re-attach on page flip)
        image_bytes: dict[int, bytes] = {}
        image_names: dict[int, str] = {}
        if radar_buf:
            image_bytes[0] = radar_buf.read()
            image_names[0] = "radar.png"
            p1.set_image(url="attachment://radar.png")
        if curve_buf:
            image_bytes[2] = curve_buf.read()
            image_names[2] = "curve.png"

        view = AnalyseView(
            author_id=interaction.user.id,
            embeds=embeds,
            image_bytes=image_bytes,
            image_names=image_names,
        )

        # Send initial message
        initial_file = None
        if 0 in image_bytes:
            initial_file = discord.File(BytesIO(image_bytes[0]), filename="radar.png")

        msg = await interaction.followup.send(
            embed=p1,
            file=initial_file,
            view=view,
        )
        view.message = msg


async def setup(bot: commands.Bot):
    await bot.add_cog(OogScoreAnalyse(bot))
