# cogs/leaderboard.py

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import time
from typing import List, Tuple, Optional, Dict
from collections import Counter

import discord
from discord.ext import commands, tasks

from oogway.database import SessionLocal, User, init_db
from oogway.riot.client import RiotClient
from oogway.config import settings
from oogway.cogs.profile import r_get, r_set

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    log.addHandler(handler)

# -------------------------------------------------------------------
# Queue configuration
QUEUE_ORDERS = [420, 440]
QUEUE_NAMES = {420: "Solo/Duo", 440: "Flex"}
QUEUE_TYPE = {420: "RANKED_SOLO_5x5", 440: "RANKED_FLEX_SR"}

# Tier ordering and colors
TIERS = [
    "Iron", "Bronze", "Silver", "Gold",
    "Platinum", "Emerald", "Diamond", "Master",
    "Grandmaster", "Challenger",
]
DIV_WEIGHTS = {"I": 4, "II": 3, "III": 2, "IV": 1}
TIER_COLORS = {
    "Iron": 0x4D4D4D,
    "Bronze": 0xCD7F32,
    "Silver": 0xC0C0C0,
    "Gold": 0xFFD700,
    "Platinum": 0x66CDAA,
    "Emerald": 0x50C878,
    "Diamond": 0x8A2BE2,
    "Master": 0xFF4500,
    "Grandmaster": 0x00BFFF,
    "Challenger": 0xFF1493,
}

# Tier emojis (épuré)
TIER_EMOJI = {
    "Iron": "⚫", "Bronze": "🟤", "Silver": "⚪",
    "Gold": "🟡", "Platinum": "🔵", "Emerald": "🟢",
    "Diamond": "💎", "Master": "🔮", 
    "Grandmaster": "⭐", "Challenger": "👑"
}

# Medal emojis for top 3
MEDALS = ["🥇", "🥈", "🥉"]

# Retry decorator
def with_retry(max_attempts: int = 3, base_delay: float = 0.5):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    log.warning(f"[retry {attempt}/{max_attempts}] {func.__name__} failed: {e}")
                    if attempt == max_attempts:
                        log.error(f"Giving up on {func.__name__}")
                        raise
                    await asyncio.sleep(base_delay * 2 ** (attempt - 1))
        return wrapper
    return decorator

# Redis helpers for progression tracking
async def safe_r_get(key: str):
    """Safely get value from Redis and parse JSON if needed."""
    import json
    value = await r_get(key)
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value

async def safe_r_set(key: str, value, ttl: int = None):
    """Safely set value to Redis with JSON serialization if needed."""
    import json
    if isinstance(value, (dict, list)):
        value = json.dumps(value)
    await r_set(key, value, ttl=ttl)

# View for pagination, sorting, queue toggle
class LeaderboardView(discord.ui.View):
    def __init__(self, cog: "LeaderboardCog"):
        super().__init__(timeout=None)
        self.cog = cog
        self.queue_index = 0
        self.page = 0
        self.sort_by = "LP"
        # Buttons
        self.add_item(self.PreviousButton())
        self.add_item(self.NextButton())
        self.add_item(self.QueueToggleButton())

    class PreviousButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label='◀', style=discord.ButtonStyle.secondary)
        async def callback(self, interaction: discord.Interaction):  # type: ignore
            await interaction.response.defer()
            view: LeaderboardView = self.view  # type: ignore
            view.page = max(view.page - 1, 0)
            embed = await view.cog.build_embed(view.queue_index, view.page, view.sort_by)
            await interaction.edit_original_response(embed=embed, view=view)

    class NextButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label='▶', style=discord.ButtonStyle.secondary)
        async def callback(self, interaction: discord.Interaction):  # type: ignore
            await interaction.response.defer()
            view: LeaderboardView = self.view  # type: ignore
            view.page += 1
            embed = await view.cog.build_embed(view.queue_index, view.page, view.sort_by)
            await interaction.edit_original_response(embed=embed, view=view)

    class QueueToggleButton(discord.ui.Button):
        def __init__(self):
            label = QUEUE_NAMES[QUEUE_ORDERS[1]]
            super().__init__(label=label, style=discord.ButtonStyle.primary)
        async def callback(self, interaction: discord.Interaction):  # type: ignore
            await interaction.response.defer()
            view: LeaderboardView = self.view  # type: ignore
            view.queue_index = (view.queue_index + 1) % len(QUEUE_ORDERS)
            view.page = 0
            next_idx = (view.queue_index + 1) % len(QUEUE_ORDERS)
            self.label = QUEUE_NAMES[QUEUE_ORDERS[next_idx]]
            embed = await view.cog.build_embed(view.queue_index, view.page, view.sort_by)
            await interaction.edit_original_response(embed=embed, view=view)

class LeaderboardCog(commands.Cog):
    """Interactive LP leaderboard with progression tracking, streaks, and server stats."""
    CACHE_TTL = 60  # FIX: Réduit à 60s pour éviter les lenteurs de pagination
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()
        self.db = SessionLocal()
        self.riot = RiotClient(settings.RIOT_API_KEY)
        self.sem = asyncio.Semaphore(8)
        self.lb_message: Optional[discord.Message] = None
        self.view: Optional[LeaderboardView] = None
        self._rank_cache: dict[Tuple[str,int], Tuple[float, Tuple[str,str,int,int,int,int]]] = {}
        # FIX: Cache global des entrées pour éviter de refetch à chaque page
        self._entries_cache: Optional[Tuple[float, List]] = None
        self._entries_cache_ttl = 60  # 1 minute

    @staticmethod
    def get_wr_label(wr: int) -> str:
        """Retourne un label humoristique basé sur le winrate."""
        if wr < 40:
            return "IA ChatGPT"
        elif wr <= 42:
            return "Boosted"
        elif wr <= 45:
            return "Dans le sac à dos"
        elif wr <= 48:
            return "Presque en positif"
        elif wr <= 51:
            return "All inclusive"
        elif wr <= 54:
            return "Mouais"
        elif wr <= 57:
            return "Propre"
        elif wr <= 60:
            return "Shifu"
        elif wr <= 63:
            return "1v9"
        elif wr <= 65:
            return "Po"
        else:
            return "Oogway 🐢"

    @commands.Cog.listener()
    async def on_ready(self):
        log.info("LeaderboardCog ready, retrieving or sending message")
        channel = self.bot.get_channel(settings.LEADERBOARD_CHANNEL_ID) or await self.bot.fetch_channel(settings.LEADERBOARD_CHANNEL_ID)
        async for msg in channel.history(limit=50):
            if msg.author == self.bot.user and msg.embeds and "Leaderboard" in msg.embeds[0].title:
                self.lb_message = msg
                break
        if not self.lb_message:
            self.view = LeaderboardView(self)
            embed = await self.build_embed(0, 0, "LP")
            self.lb_message = await channel.send(embed=embed, view=self.view)
        else:
            self.view = LeaderboardView(self)
            await self.lb_message.edit(view=self.view)
        self.update_loop.start()
        self.track_monthly_start.start()

    @tasks.loop(minutes=5)
    async def update_loop(self):
        """Auto-update du leaderboard toutes les 5 minutes."""
        if not self.lb_message:
            return
        # Invalider le cache des entrées pour forcer un refresh
        self._entries_cache = None
        embed = await self.build_embed(self.view.queue_index, self.view.page, self.view.sort_by)
        try:
            await self.lb_message.edit(embed=embed)
            log.info("Leaderboard auto-updated")
        except Exception as e:
            log.error(f"Failed auto-update: {e}")

    @tasks.loop(hours=24)
    async def track_monthly_start(self):
        """Track le LP de début de mois pour chaque joueur."""
        now = dt.datetime.now(dt.timezone.utc)
        # Si on est le 1er du mois, sauvegarder les LP actuels
        if now.day == 1 and now.hour < 6:
            users = self.db.query(User).all()
            for queue_id in QUEUE_ORDERS:
                for user in users:
                    try:
                        tier, div, lp, wr, wins, losses = await self._get_rank(user, queue_id)
                        if tier in TIERS:
                            key = f"monthly_start:{user.puuid}:{queue_id}:{now.year}-{now.month:02d}"
                            await safe_r_set(key, {
                                "tier": tier,
                                "div": div,
                                "lp": lp,
                                "timestamp": int(now.timestamp())
                            }, ttl=90*24*3600)
                    except Exception as e:
                        log.warning(f"Failed to track monthly start for {user.discord_id}: {e}")

    @update_loop.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

    @track_monthly_start.before_loop
    async def before_track(self):
        await self.bot.wait_until_ready()

    async def _get_entries(self, queue_id: int, force_refresh: bool = False) -> List[Tuple]:
        """
        FIX: Cache global des entrées pour éviter de refetch à chaque changement de page.
        Retourne: List[(User, tier, div, lp, wr, wins, losses, delta_lp, streak, prev_pos)]
        """
        now = time.time()
        
        # Utiliser le cache si disponible et pas expiré
        if not force_refresh and self._entries_cache is not None:
            cache_ts, cached_entries = self._entries_cache
            if now - cache_ts < self._entries_cache_ttl:
                return cached_entries
        
        users = self.db.query(User).all()
        entries: List[Tuple] = []

        async def fetch(u: User):
            async with self.sem:
                try:
                    tier, div, lp, wr, wins, losses = await self._get_rank(u, queue_id)
                    if tier in TIERS:
                        # Récupérer la progression mensuelle
                        delta_lp = await self._get_monthly_delta(u, queue_id, tier, div, lp)
                        
                        # Récupérer le streak
                        streak_count, is_win = await self._get_streak(u, queue_id)
                        
                        # Récupérer la position précédente (pour le change indicator)
                        prev_pos = await self._get_previous_position(u, queue_id)
                        
                        entries.append((u, tier, div, lp, wr, wins, losses, delta_lp, streak_count, is_win, prev_pos))
                except Exception as e:
                    log.warning(f"Fetch error for {u.discord_id}: {e}")

        await asyncio.gather(*(fetch(u) for u in users), return_exceptions=True)
        
        # Sauvegarder dans le cache
        self._entries_cache = (now, entries)
        
        return entries

    async def _get_monthly_delta(self, user: User, queue_id: int, current_tier: str, current_div: str, current_lp: int) -> int:
        """Calcule le delta LP depuis le début du mois."""
        now = dt.datetime.now(dt.timezone.utc)
        key = f"monthly_start:{user.puuid}:{queue_id}:{now.year}-{now.month:02d}"
        
        start_data = await safe_r_get(key)
        if not start_data or not isinstance(start_data, dict):
            # Pas de données de début de mois, sauvegarder maintenant
            await safe_r_set(key, {
                "tier": current_tier,
                "div": current_div,
                "lp": current_lp,
                "timestamp": int(now.timestamp())
            }, ttl=90*24*3600)
            return 0
        
        # Calculer le delta
        start_tier = start_data.get("tier", current_tier)
        start_div = start_data.get("div", current_div)
        start_lp = start_data.get("lp", current_lp)
        
        # Simple calculation: si même tier/div, juste la diff de LP
        if start_tier == current_tier and start_div == current_div:
            return current_lp - start_lp
        
        # Si différent, estimation grossière
        start_idx = TIERS.index(start_tier) * 400 + DIV_WEIGHTS.get(start_div, 0) * 100 + start_lp
        current_idx = TIERS.index(current_tier) * 400 + DIV_WEIGHTS.get(current_div, 0) * 100 + current_lp
        
        return current_idx - start_idx

    async def _get_streak(self, user: User, queue_id: int) -> Tuple[int, bool]:
        """Récupère le streak actuel du joueur."""
        key = f"streak:{user.puuid}:{queue_id}"
        raw = await safe_r_get(key)
        
        if not isinstance(raw, list) or not raw:
            return 0, True
        
        # Calculer le streak depuis la fin
        current_result = raw[-1]
        streak_count = 1
        
        for i in range(len(raw) - 2, -1, -1):
            if raw[i] == current_result:
                streak_count += 1
            else:
                break
        
        return streak_count, current_result == "W"

    async def _get_previous_position(self, user: User, queue_id: int) -> Optional[int]:
        """Récupère la position précédente du joueur."""
        key = f"lb_position:{user.puuid}:{queue_id}"
        pos = await safe_r_get(key)
        return int(pos) if pos else None

    async def _save_positions(self, entries: List[Tuple], queue_id: int):
        """Sauvegarde les positions actuelles pour le prochain calcul."""
        for idx, entry in enumerate(entries, start=1):
            user = entry[0]
            key = f"lb_position:{user.puuid}:{queue_id}"
            await safe_r_set(key, idx, ttl=7*24*3600)

    async def build_embed(self, queue_idx: int, page: int, sort_by: str) -> discord.Embed:
        queue_id = QUEUE_ORDERS[queue_idx]
        
        # FIX: Utiliser le cache global des entrées
        entries = await self._get_entries(queue_id)
        
        if not entries:
            # Cas où aucune entrée (serveur vide ou erreurs)
            embed = discord.Embed(
                title=f"Leaderboard — {QUEUE_NAMES[queue_id]}",
                description="Aucun joueur classé pour le moment.",
                color=0x3498db,
                timestamp=dt.datetime.now(dt.timezone.utc)
            )
            return embed

        # Tri
        if sort_by == "LP":
            key_fn = lambda e: (TIERS.index(e[1]), DIV_WEIGHTS[e[2]], e[3])
        else:
            key_fn = lambda e: (TIERS.index(e[1]), DIV_WEIGHTS[e[2]], e[4])
        entries.sort(key=key_fn, reverse=True)
        
        # Sauvegarder les positions actuelles
        await self._save_positions(entries, queue_id)

        per_page = 10
        total_pages = max(math.ceil(len(entries) / per_page), 1)
        page = max(0, min(page, total_pages - 1))
        slice_ = entries[page * per_page:(page + 1) * per_page]

        # Couleur basée sur le top player de la page
        top_tier = slice_[0][1] if slice_ else "Gold"
        color = TIER_COLORS.get(top_tier, 0x3498db)
        
        embed = discord.Embed(
            title=f"Leaderboard — {QUEUE_NAMES[queue_id]}",
            color=color,
            timestamp=dt.datetime.now(dt.timezone.utc)
        )

        # FIX: Vérifier que slice_ n'est pas vide avant d'accéder
        if slice_:
            for idx, entry in enumerate(slice_, start=page * per_page + 1):
                user, tier, div, lp, wr, wins, losses, delta_lp, streak, is_win, prev_pos = entry
                
                medal = MEDALS[idx - 1] if idx <= 3 else f"#{idx}"
                
                try:
                    du = await self.bot.fetch_user(user.discord_id)
                    name = du.display_name
                    avatar = du.display_avatar.url
                except:
                    name = user.puuid[:6]
                    avatar = None
                
                # Position change indicator
                if prev_pos:
                    if prev_pos > idx:
                        pos_change = f"↗ +{prev_pos - idx}"
                    elif prev_pos < idx:
                        pos_change = f"↘ -{idx - prev_pos}"
                    else:
                        pos_change = "━"
                else:
                    pos_change = "NEW"
                
                field_name = f"{medal} {pos_change} • {name}"
                
                # Construction du field_value épuré
                tier_icon = TIER_EMOJI.get(tier, "⚪")
                rank_str = f"{tier_icon} **{tier} {div}** • {lp} LP"
                
                # Delta mensuel
                if delta_lp > 0:
                    delta_str = f"(+{delta_lp} ce mois)"
                elif delta_lp < 0:
                    delta_str = f"({delta_lp} ce mois)"
                else:
                    delta_str = ""
                
                # Streak (seulement si >= 3)
                if streak >= 3:
                    streak_emoji = "🔥" if is_win else "❄️"
                    streak_str = f"{streak_emoji} {streak}"
                else:
                    streak_str = ""
                
                # WR label
                wr_label = self.get_wr_label(wr)
                
                # Ligne 1: Rank + Delta
                line1 = f"{rank_str} {delta_str}".strip()
                
                # Ligne 2: Stats + Streak
                stats_parts = [f"{wr}% WR", f"{wins}V-{losses}D"]
                if streak_str:
                    stats_parts.append(streak_str)
                stats_parts.append(wr_label)
                line2 = " • ".join(stats_parts)
                
                field_value = f"{line1}\n{line2}"
                
                embed.add_field(name=field_name, value=field_value, inline=False)
                
                # FIX: Avatar du top player de la page
                if idx == page * per_page + 1 and avatar:
                    embed.set_author(name="Leaderboard", icon_url=avatar)

        # === STATS DU SERVEUR ===
        server_stats = await self._compute_server_stats(entries)
        
        stats_lines = [
            f"**Joueurs:** {server_stats['total_players']}",
            f"**Rank moyen:** {server_stats['avg_tier']}",
            f"**WR moyen:** {server_stats['avg_wr']}%",
        ]
        
        if server_stats['best_streak_player']:
            stats_lines.append(f"**Meilleure streak:** {server_stats['best_streak_player']} ({server_stats['best_streak']})")
        
        if server_stats['top_climber']:
            stats_lines.append(f"**Progression:** {server_stats['top_climber']} (+{server_stats['top_climb']} LP)")
        
        embed.add_field(
            name="📊 Statistiques du serveur",
            value="\n".join(stats_lines),
            inline=True
        )
        
        # === DISTRIBUTION ===
        distribution = await self._compute_distribution(entries)
        dist_lines = []
        for tier_name, count in distribution.items():
            if count > 0:
                bar_len = min(10, count)
                bar = "▓" * bar_len + "░" * (10 - bar_len)
                dist_lines.append(f"{tier_name:9} {bar} {count}")
        
        embed.add_field(
            name="📈 Distribution",
            value="\n".join(dist_lines) if dist_lines else "Aucune donnée",
            inline=True
        )
        
        # === RECORDS ===
        records = await self._compute_records(entries)
        
        records_lines = []
        if records['highest_rank']:
            records_lines.append(f"**Plus haut:** {records['highest_rank']}")
        if records['best_wr']:
            records_lines.append(f"**Meilleur WR:** {records['best_wr']}")
        if records['most_games']:
            records_lines.append(f"**Plus actif:** {records['most_games']}")
        
        if records_lines:
            embed.add_field(
                name="🏆 Records",
                value="\n".join(records_lines),
                inline=False
            )
        
        # Footer
        embed.set_footer(text=f"Page {page + 1}/{total_pages} • Mise à jour toutes les 5 minutes")
        
        return embed

    async def _compute_server_stats(self, entries: List[Tuple]) -> Dict:
        """Calcule les stats globales du serveur."""
        if not entries:
            return {
                "total_players": 0,
                "avg_tier": "N/A",
                "avg_wr": 0,
                "best_streak_player": None,
                "best_streak": 0,
                "top_climber": None,
                "top_climb": 0
            }
        
        total_wr = sum(e[4] for e in entries)
        avg_wr = int(total_wr / len(entries))
        
        # Tier moyen (approximation)
        tier_indices = [TIERS.index(e[1]) for e in entries]
        avg_tier_idx = int(sum(tier_indices) / len(tier_indices))
        avg_tier = TIERS[avg_tier_idx]
        
        # Meilleure streak
        best_streak = 0
        best_streak_player = None
        for entry in entries:
            user, tier, div, lp, wr, wins, losses, delta_lp, streak, is_win, prev_pos = entry
            if streak > best_streak:
                best_streak = streak
                try:
                    du = await self.bot.fetch_user(user.discord_id)
                    best_streak_player = du.display_name
                except:
                    best_streak_player = user.puuid[:6]
        
        # Top climber du mois
        top_climb = 0
        top_climber = None
        for entry in entries:
            user, tier, div, lp, wr, wins, losses, delta_lp, streak, is_win, prev_pos = entry
            if delta_lp > top_climb:
                top_climb = delta_lp
                try:
                    du = await self.bot.fetch_user(user.discord_id)
                    top_climber = du.display_name
                except:
                    top_climber = user.puuid[:6]
        
        return {
            "total_players": len(entries),
            "avg_tier": avg_tier,
            "avg_wr": avg_wr,
            "best_streak_player": best_streak_player,
            "best_streak": best_streak if best_streak >= 3 else 0,
            "top_climber": top_climber if top_climb > 0 else None,
            "top_climb": top_climb
        }

    async def _compute_distribution(self, entries: List[Tuple]) -> Dict[str, int]:
        """Calcule la distribution des ranks."""
        distribution = {tier: 0 for tier in TIERS}
        for entry in entries:
            tier = entry[1]
            distribution[tier] += 1
        
        # Retourner seulement les tiers avec des joueurs
        return {k: v for k, v in distribution.items() if v > 0}

    async def _compute_records(self, entries: List[Tuple]) -> Dict:
        """Calcule les records du serveur."""
        if not entries:
            return {
                "highest_rank": None,
                "best_wr": None,
                "most_games": None
            }
        
        # Plus haut rank
        highest = entries[0]  # Déjà trié
        try:
            du = await self.bot.fetch_user(highest[0].discord_id)
            highest_name = du.display_name
        except:
            highest_name = highest[0].puuid[:6]
        highest_rank = f"{highest_name} ({highest[1]} {highest[2]})"
        
        # Meilleur WR (minimum 10 games)
        qualified = [e for e in entries if (e[5] + e[6]) >= 10]
        if qualified:
            best_wr_entry = max(qualified, key=lambda e: e[4])
            try:
                du = await self.bot.fetch_user(best_wr_entry[0].discord_id)
                best_wr_name = du.display_name
            except:
                best_wr_name = best_wr_entry[0].puuid[:6]
            best_wr = f"{best_wr_name} ({best_wr_entry[4]}%)"
        else:
            best_wr = None
        
        # Plus de games
        most_games_entry = max(entries, key=lambda e: e[5] + e[6])
        try:
            du = await self.bot.fetch_user(most_games_entry[0].discord_id)
            most_games_name = du.display_name
        except:
            most_games_name = most_games_entry[0].puuid[:6]
        total_games = most_games_entry[5] + most_games_entry[6]
        most_games = f"{most_games_name} ({total_games} games)"
        
        return {
            "highest_rank": highest_rank,
            "best_wr": best_wr,
            "most_games": most_games
        }

    @with_retry()
    async def _get_rank(self, user: User, queue_id: int) -> Tuple[str, str, int, int, int, int]:
        """Get player rank with caching - now fully async."""
        key = (user.puuid, queue_id)
        now = time.time()
        if key in self._rank_cache:
            ts, data = self._rank_cache[key]
            if now - ts < self.CACHE_TTL:
                return data

        # Now fully async
        summ = await self.riot.get_summoner_by_puuid(user.region, user.puuid)
        entries = await self.riot.get_league_entries_by_puuid(user.region, user.puuid)

        entry = next((e for e in entries if e["queueType"] == QUEUE_TYPE[queue_id]), None)
        if not entry:
            result = ("Unranked", "", 0, 0, 0, 0)  # tier, div, lp, wr, wins, losses
        else:
            wins, losses = entry.get("wins", 0), entry.get("losses", 0)
            wr = int(wins / max(1, wins + losses) * 100)
            result = (entry["tier"].title(), entry["rank"], entry["leaguePoints"], wr, wins, losses)

        self._rank_cache[key] = (now, result)
        return result

async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))
