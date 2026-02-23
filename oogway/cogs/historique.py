# oogway/cogs/historique.py
# ============================================================================
# Historique des séries customs + stats joueurs
#
# Commandes :
#   /historique [page]        — liste des dernières séries
#   /serie <id>               — détail d'une série
#   /stats-joueur [@membre]   — winrate, picks, bans d'un joueur
#   /stats-equipes            — winrates des paires de joueurs
# ============================================================================

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from oogway.cogs.profile import r_get, r_set
from oogway.models.series_state import SeriesState

logger = logging.getLogger(__name__)

# ─── Clés Redis ───────────────────────────────────────────────────────────────
HISTORY_LIST_KEY = "history:series:list"   # liste ordonnée des IDs (du plus récent)
HISTORY_ITEM_KEY = "history:series:{}"     # hash complet d'une série

HISTORY_MAX      = 200   # max séries gardées
HISTORY_TTL      = 365 * 24 * 3600  # 1 an

PAGE_SIZE = 5


# ─── Persistence ──────────────────────────────────────────────────────────────
async def save_series_to_history(series: SeriesState) -> None:
    """Persiste une série terminée dans Redis."""
    try:
        data = series.to_history_dict()
        sid  = data["id"]

        # Sauvegarder le détail
        await r_set(HISTORY_ITEM_KEY.format(sid), json.dumps(data), ttl=HISTORY_TTL)

        # Mettre à jour la liste des IDs
        raw_list = await r_get(HISTORY_LIST_KEY)
        if isinstance(raw_list, list):
            id_list = raw_list
        elif isinstance(raw_list, str):
            try:
                id_list = json.loads(raw_list)
            except Exception:
                id_list = []
        else:
            id_list = []

        # Insérer en tête (plus récent en premier)
        if sid in id_list:
            id_list.remove(sid)
        id_list.insert(0, sid)

        # Limiter la taille
        id_list = id_list[:HISTORY_MAX]
        await r_set(HISTORY_LIST_KEY, json.dumps(id_list), ttl=HISTORY_TTL)

        logger.info(f"✅ Série {sid} sauvegardée dans l'historique ({len(id_list)} total)")

    except Exception as e:
        logger.error(f"❌ Erreur sauvegarde historique série {series.id}: {e}", exc_info=True)


async def load_series(sid: str) -> Optional[SeriesState]:
    """Charge une série depuis Redis."""
    try:
        raw = await r_get(HISTORY_ITEM_KEY.format(sid))
        if not raw:
            return None
        if isinstance(raw, str):
            data = json.loads(raw)
        elif isinstance(raw, dict):
            data = raw
        else:
            return None
        return SeriesState.from_history_dict(data)
    except Exception as e:
        logger.error(f"Erreur chargement série {sid}: {e}")
        return None


async def load_all_series(limit: int = HISTORY_MAX) -> List[SeriesState]:
    """Charge toutes les séries de l'historique."""
    try:
        raw_list = await r_get(HISTORY_LIST_KEY)
        if isinstance(raw_list, list):
            id_list = raw_list
        elif isinstance(raw_list, str):
            id_list = json.loads(raw_list)
        else:
            return []
    except Exception:
        return []

    series_list = []
    for sid in id_list[:limit]:
        s = await load_series(sid)
        if s:
            series_list.append(s)
    return series_list


# ─── Helpers d'affichage ──────────────────────────────────────────────────────
def _duration_str(started_at: float, ended_at: Optional[float]) -> str:
    if not ended_at or not started_at:
        return "?"
    secs = int(ended_at - started_at)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


def _score_str(s: SeriesState) -> str:
    return f"{s.score_a}–{s.score_b}"


def _winner_label(s: SeriesState) -> str:
    w = s.winner_side()
    if w == "A":
        return "🔵 Team A"
    elif w == "B":
        return "🔴 Team B"
    return "🤝 Nul"


def _mentions(ids: List[int]) -> str:
    return " ".join(f"<@{uid}>" for uid in ids) or "—"


def _short_date(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%d/%m/%Y")


# ─── Calcul stats joueur ──────────────────────────────────────────────────────
def compute_player_stats(uid: int, all_series: List[SeriesState]) -> Dict[str, Any]:
    """Calcule les stats d'un joueur sur toutes les séries."""
    games_played  = 0
    games_won     = 0
    series_played = 0
    series_won    = 0
    picks: Dict[str, int] = {}
    bans:  Dict[str, int] = {}
    as_captain   = 0
    cap_wins     = 0

    for s in all_series:
        # Déterminer si le joueur est dans cette série
        if uid in s.team_a:
            team = "A"
            opp  = "B"
        elif uid in s.team_b:
            team = "B"
            opp  = "A"
        else:
            continue

        series_played += 1
        if s.winner_side() == team:
            series_won += 1

        is_cap = (uid == s.captain_a and team == "A") or (uid == s.captain_b and team == "B")
        if is_cap:
            as_captain += 1
            if s.winner_side() == team:
                cap_wins += 1

        for g in s.games:
            if g.winner is None:
                continue  # game pas reportée
            games_played += 1
            if g.winner == team:
                games_won += 1

            # Picks du joueur (tous les joueurs de l'équipe ont les mêmes picks dans notre modèle)
            champ_picks = g.picks_a if team == "A" else g.picks_b
            champ_bans  = g.bans_a  if team == "A" else g.bans_b

            for c in champ_picks:
                picks[c] = picks.get(c, 0) + 1
            for c in champ_bans:
                bans[c] = bans.get(c, 0) + 1

    top_picks = sorted(picks.items(), key=lambda x: x[1], reverse=True)[:5]
    top_bans  = sorted(bans.items(),  key=lambda x: x[1], reverse=True)[:5]

    return {
        "games_played":  games_played,
        "games_won":     games_won,
        "series_played": series_played,
        "series_won":    series_won,
        "top_picks":     top_picks,
        "top_bans":      top_bans,
        "as_captain":    as_captain,
        "cap_wins":      cap_wins,
    }


def compute_duo_stats(all_series: List[SeriesState]) -> List[Tuple[int, int, int, int]]:
    """
    Calcule les winrates des paires de joueurs dans la même équipe.
    Retourne une liste de (uid1, uid2, games_together, games_won) triée par winrate desc.
    """
    duo_games: Dict[Tuple[int, int], int] = {}
    duo_wins:  Dict[Tuple[int, int], int] = {}

    for s in all_series:
        for team_tag, team in [("A", s.team_a), ("B", s.team_b)]:
            for i, uid1 in enumerate(team):
                for uid2 in team[i + 1:]:
                    pair = (min(uid1, uid2), max(uid1, uid2))
                    for g in s.games:
                        if g.winner is None:
                            continue
                        duo_games[pair] = duo_games.get(pair, 0) + 1
                        if g.winner == team_tag:
                            duo_wins[pair] = duo_wins.get(pair, 0) + 1

    results = []
    for pair, total in duo_games.items():
        if total >= 3:  # minimum 3 games ensemble pour être significatif
            wins = duo_wins.get(pair, 0)
            results.append((pair[0], pair[1], total, wins))

    results.sort(key=lambda x: x[3] / x[2] if x[2] else 0, reverse=True)
    return results[:10]


# ─── Cog ──────────────────────────────────────────────────────────────────────
class HistoriqueCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /historique ───────────────────────────────────────────────────────────
    @app_commands.command(name="historique", description="Liste des dernières séries customs")
    @app_commands.describe(page="Numéro de page (défaut : 1)")
    async def historique(self, inter: Interaction, page: int = 1):
        await inter.response.defer()

        all_series = await load_all_series()
        if not all_series:
            return await inter.followup.send("📭 Aucune série dans l'historique pour l'instant.")

        # Pagination
        page       = max(1, page)
        total_pages = max(1, (len(all_series) + PAGE_SIZE - 1) // PAGE_SIZE)
        page       = min(page, total_pages)
        start      = (page - 1) * PAGE_SIZE
        page_series = all_series[start:start + PAGE_SIZE]

        embed = discord.Embed(
            title="📜 Historique des séries",
            colour=discord.Colour.dark_teal(),
            description=f"Page **{page}/{total_pages}** — {len(all_series)} série(s) au total",
        )

        for s in page_series:
            dur   = _duration_str(s.started_at, s.ended_at)
            score = _score_str(s)
            win   = _winner_label(s)
            date  = _short_date(s.started_at)
            subs  = f" · {len(s.substitutions)} rempl." if s.substitutions else ""

            embed.add_field(
                name=f"`{s.id}` — Bo{s.bo} · {date}",
                value=(
                    f"{win}  **{score}**  ·  ⏱️ {dur}{subs}\n"
                    f"🔵 <@{s.captain_a}> vs 🔴 <@{s.captain_b}>"
                ),
                inline=False,
            )

        embed.set_footer(text="Utilise /serie <id> pour le détail d'une série")
        await inter.followup.send(embed=embed)

    # ── /serie <id> ───────────────────────────────────────────────────────────
    @app_commands.command(name="serie", description="Détail d'une série custom")
    @app_commands.describe(serie_id="ID de la série (visible dans /historique)")
    async def serie(self, inter: Interaction, serie_id: str):
        await inter.response.defer()

        s = await load_series(serie_id.strip())
        if not s:
            return await inter.followup.send(f"❌ Série `{serie_id}` introuvable.")

        dur  = _duration_str(s.started_at, s.ended_at)
        win  = _winner_label(s)
        date = _short_date(s.started_at)

        embed = discord.Embed(
            title=f"⚔️ Série `{s.id}` — Bo{s.bo}",
            colour=discord.Colour.from_rgb(30, 136, 229),
            description=(
                f"**{win}** · Score final **{_score_str(s)}**\n"
                f"📅 {date} · ⏱️ {dur}"
            ),
        )

        # Équipes
        embed.add_field(
            name="🔵 Team A",
            value=_mentions(s.team_a),
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(
            name="🔴 Team B",
            value=_mentions(s.team_b),
            inline=True,
        )

        # Résultats par game
        for i, g in enumerate(s.games, 1):
            if g.winner is None and not g.picks_a and not g.picks_b:
                continue
            w_str = f"**{'🔵 Team A' if g.winner == 'A' else '🔴 Team B' if g.winner == 'B' else '?'}** gagne"
            pa    = ", ".join(f"`{c}`" for c in g.picks_a) or "—"
            pb    = ", ".join(f"`{c}`" for c in g.picks_b) or "—"
            ba    = ", ".join(f"`{c}`" for c in g.bans_a) or "—"
            bb    = ", ".join(f"`{c}`" for c in g.bans_b) or "—"

            embed.add_field(
                name=f"Game {i} — {w_str}",
                value=(
                    f"**Picks A:** {pa}\n"
                    f"**Picks B:** {pb}\n"
                    f"**Bans A:** {ba} · **Bans B:** {bb}"
                ),
                inline=False,
            )

        # Remplacements
        if s.substitutions:
            lines = []
            for sub in s.substitutions:
                cap_note = " *(capitaine remplacé)*" if sub.was_captain else ""
                new_cap  = f" → nouveau cap <@{sub.new_captain_id}>" if sub.new_captain_id else ""
                lines.append(
                    f"Game {sub.game_number} · Team {sub.team} : "
                    f"<@{sub.out_id}> ➜ <@{sub.in_id}>{cap_note}{new_cap}"
                )
            embed.add_field(name="🔄 Remplacements", value="\n".join(lines), inline=False)

        embed.set_footer(text=f"Fearless pool : {', '.join(sorted(s.fearless_pool)) or '—'}")
        await inter.followup.send(embed=embed)

    # ── /stats-joueur ─────────────────────────────────────────────────────────
    @app_commands.command(name="stats-joueur", description="Stats customs d'un joueur")
    @app_commands.describe(membre="Membre Discord (toi par défaut)")
    async def stats_joueur(self, inter: Interaction, membre: Optional[discord.Member] = None):
        await inter.response.defer()

        target = membre or inter.user
        all_series = await load_all_series()

        if not all_series:
            return await inter.followup.send("📭 Aucune donnée disponible.")

        st = compute_player_stats(target.id, all_series)

        if st["games_played"] == 0:
            return await inter.followup.send(
                f"📭 **{target.display_name}** n'a pas encore joué de série custom."
            )

        games_wr  = st["games_won"] / st["games_played"] * 100 if st["games_played"] else 0
        series_wr = st["series_won"] / st["series_played"] * 100 if st["series_played"] else 0
        cap_wr    = st["cap_wins"] / st["as_captain"] * 100 if st["as_captain"] else 0

        embed = discord.Embed(
            title=f"📊 Stats customs — {target.display_name}",
            colour=discord.Colour.gold(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)

        embed.add_field(
            name="🎮 Parties",
            value=(
                f"**Séries :** {st['series_played']} ({series_wr:.0f}% WR)\n"
                f"**Games :** {st['games_played']} ({games_wr:.0f}% WR)"
            ),
            inline=True,
        )
        embed.add_field(
            name="👑 Capitaine",
            value=(
                f"**Fois cap :** {st['as_captain']}\n"
                f"**WR en tant que cap :** {cap_wr:.0f}%"
                if st["as_captain"] else "Jamais capitaine"
            ),
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)

        top_picks_str = "\n".join(f"`{c}` × {n}" for c, n in st["top_picks"]) or "—"
        top_bans_str  = "\n".join(f"`{c}` × {n}" for c, n in st["top_bans"]) or "—"

        embed.add_field(name="✅ Top Picks", value=top_picks_str, inline=True)
        embed.add_field(name="🚫 Top Bans",  value=top_bans_str,  inline=True)

        embed.set_footer(text=f"Sur {len(all_series)} série(s) dans l'historique")
        await inter.followup.send(embed=embed)

    # ── /stats-equipes ────────────────────────────────────────────────────────
    @app_commands.command(name="stats-equipes", description="Winrates des paires de joueurs (min. 3 games)")
    async def stats_equipes(self, inter: Interaction):
        await inter.response.defer()

        all_series = await load_all_series()
        if not all_series:
            return await inter.followup.send("📭 Aucune donnée disponible.")

        duo_stats = compute_duo_stats(all_series)
        if not duo_stats:
            return await inter.followup.send(
                "📭 Pas assez de données (minimum 3 games ensemble par paire)."
            )

        embed = discord.Embed(
            title="🤝 Synergies — Top paires de joueurs",
            colour=discord.Colour.dark_green(),
            description="Winrate des paires ayant joué **3+ games** dans la même équipe",
        )

        for uid1, uid2, total, wins in duo_stats:
            wr = wins / total * 100
            bar_filled = round(wr / 10)
            bar        = "█" * bar_filled + "░" * (10 - bar_filled)
            embed.add_field(
                name=f"<@{uid1}> + <@{uid2}>",
                value=f"`{bar}` **{wr:.0f}%** ({wins}W / {total - wins}L · {total} games)",
                inline=False,
            )

        embed.set_footer(text=f"Sur {len(all_series)} série(s) dans l'historique")
        await inter.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(HistoriqueCog(bot))
