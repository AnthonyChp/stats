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
    CACHE_TTL = 300  # ✅ 5 minutes au lieu de 60s
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()
        self.db = SessionLocal()
        self.riot = RiotClient(settings.RIOT_API_KEY)
        self.sem = asyncio.Semaphore(8)
        self.lb_message: Optional[discord.Message] = None
        self.view: Optional[LeaderboardView] = None
        self._rank_cache: dict[Tuple[str,int], Tuple[float, Tuple[str,str,int,int,int,int]]] = {}
        
        # ✅ Cache global des entrées + stats pré-calculées
        self._entries_cache: Dict[int, Optional[Tuple[float, Dict]]] = {420: None, 440: None}
        self._entries_cache_ttl = 300  # 5 minutes
        
        # ✅ Nouveau: Cache des Discord users
        self._user_cache: Dict[int, Tuple[float, str, str]] = {}
        self._user_cache_ttl = 3600  # 1 heure

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
        self._entries_cache = {420: None, 440: None}
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

    async def _get_discord_user(self, discord_id: int) -> Tuple[str, Optional[str]]:
        """✅ Cache des Discord users pour éviter les fetch répétés."""
        now = time.time()
        
        if discord_id in self._user_cache:
            ts, name, avatar = self._user_cache[discord_id]
            if now - ts < self._user_cache_ttl:
                return name, avatar
        
        try:
            du = await self.bot.fetch_user(discord_id)
            name = du.display_name
            avatar = du.display_avatar.url
            self._user_cache[discord_id] = (now, name, avatar)
            return name, avatar
        except Exception as e:
            log.warning(f"Failed to fetch Discord user {discord_id}: {e}")
            return f"User#{discord_id}", None

    async def _prefetch_discord_users(self, entries: List[Tuple]):
        """✅ Pré-fetch tous les Discord users en parallèle."""
        discord_ids = list(set(entry[0].discord_id for entry in entries))
        
        async def fetch_one(discord_id: int):
            await self._get_discord_user(discord_id)
        
        await asyncio.gather(*(fetch_one(did) for did in discord_ids), return_exceptions=True)

    async def _get_entries(self, queue_id: int, force_refresh: bool = False) -> Dict:
        """
        ✅ Cache global avec stats pré-calculées.
        Retourne: Dict avec 'entries', 'server_stats', 'distribution', 'records'
        """
        now = time.time()
        
        # Utiliser le cache si disponible et pas expiré
        if not force_refresh and self._entries_cache[queue_id] is not None:
            cache_ts, cached_data = self._entries_cache[queue_id]
            if now - cache_ts < self._entries_cache_ttl:
                log.debug(f"Using cached data for queue {queue_id}")
                return cached_data
        
        log.info(f"♻️ Refreshing leaderboard cache for queue {queue_id}")
        
        users = self.db.query(User).all()
        entries: List[Tuple] = []

        async def fetch(u: User):
            async with self.sem:
                try:
                    tier, div, lp, wr, wins, losses = await self._get_rank(u, queue_id)
                    if tier in TIERS:
                        delta_lp = await self._get_monthly_delta(u, queue_id, tier, div, lp)
                        streak_count, is_win = await self._get_streak(u, queue_id)
                        prev_pos = await self._get_previous_position(u, queue_id)
                        entries.append((u, tier, div, lp, wr, wins, losses, delta_lp, streak_count, is_win, prev_pos))
                except Exception as e:
                    log.warning(f"Fetch error for {u.discord_id}: {e}")

        await asyncio.gather(*(fetch(u) for u in users), return_exceptions=True)
        
        if not entries:
            # Retourner des données vides si aucun joueur
            cached_data = {
                'entries': [],
                'server_stats': {
                    "total_players": 0,
                    "avg_tier": "N/A",
                    "avg_wr": 0,
                    "best_streak_player": None,
                    "best_streak": 0,
                    "top_climber": None,
                    "top_climb": 0
                },
                'distribution': {},
                'records': {
                    "highest_rank": None,
                    "best_wr": None,
                    "most_games": None
                }
            }
            self._entries_cache[queue_id] = (now, cached_data)
            return cached_data
        
        # ✅ Trier les entries
        entries.sort(key=lambda e: (TIERS.index(e[1]), DIV_WEIGHTS[e[2]], e[3]), reverse=True)
        
        # ✅ Pré-fetch tous les Discord users en parallèle
        await self._prefetch_discord_users(entries)
        
        # ✅ Pré-calculer toutes les stats UNE SEULE FOIS
        server_stats = await self._compute_server_stats(entries)
        distribution = await self._compute_distribution(entries)
        records = await self._compute_records(entries)
        
        # Sauvegarder les positions pour le prochain calcul
        await self._save_positions(entries, queue_id)
        
        # Sauvegarder dans le cache
        cached_data = {
            'entries': entries,
            'server_stats': server_stats,
            'distribution': distribution,
            'records': records
        }
        
        self._entries_cache[queue_id] = (now, cached_data)
        log.info(f"✅ Cache refreshed with {len(entries)} entries for queue {queue_id}")
        
        return cached_data

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
        try:
            start_idx = TIERS.index(start_tier) * 400 + DIV_WEIGHTS.get(start_div, 0) * 100 + start_lp
            current_idx = TIERS.index(current_tier) * 400 + DIV_WEIGHTS.get(current_div, 0) * 100 + current_lp
            return current_idx - start_idx
        except (ValueError, KeyError):
            return 0

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
        
        # ✅ Récupérer depuis le cache (entries déjà triées + stats pré-calculées)
        cached_data = await self._get_entries(queue_id)
        entries = cached_data['entries']
        server_stats = cached_data['server_stats']
        distribution = cached_data['distribution']
        records = cached_data['records']
        
        if not entries:
            # Cas où aucune entrée (serveur vide ou erreurs)
            embed = discord.Embed(
                title=f"Leaderboard — {QUEUE_NAMES[queue_id]}",
                description="Aucun joueur classé pour le moment.",
                color=0x3498db,
                timestamp=dt.datetime.now(dt.timezone.utc)
            )
            return embed

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

        if slice_:
            for idx, entry in enumerate(slice_, start=page * per_page + 1):
                user, tier, div, lp, wr, wins, losses, delta_lp, streak, is_win, prev_pos = entry
                
                medal = MEDALS[idx - 1] if idx <= 3 else f"#{idx}"
                
                # ✅ Utiliser le cache Discord user (pas de fetch !)
                name, avatar = await self._get_discord_user(user.discord_id)
                
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
                
                # Avatar du top player de la page
                if idx == page * per_page + 1 and avatar:
                    embed.set_author(name="Leaderboard", icon_url=avatar)

        # ✅ Utiliser les stats pré-calculées (pas de recalcul !)
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
                # ✅ Utiliser le cache Discord user
                best_streak_player, _ = await self._get_discord_user(user.discord_id)
        
        # Top climber du mois
        top_climb = 0
        top_climber = None
        for entry in entries:
            user, tier, div, lp, wr, wins, losses, delta_lp, streak, is_win, prev_pos = entry
            if delta_lp > top_climb:
                top_climb = delta_lp
                # ✅ Utiliser le cache Discord user
                top_climber, _ = await self._get_discord_user(user.discord_id)
        
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
        highest = entries[0]  # Déjà trié par rank
        highest_name, _ = await self._get_discord_user(highest[0].discord_id)
        highest_rank = f"{highest_name} ({highest[1]} {highest[2]})"
        
        # Meilleur WR (minimum 10 games)
        qualified = [e for e in entries if (e[5] + e[6]) >= 10]
        if qualified:
            # Trouver le WR maximum
            best_wr_value = max(e[4] for e in qualified)
            # Récupérer l'entrée correspondante (la première si égalité)
            best_wr_entry = next(e for e in qualified if e[4] == best_wr_value)
            best_wr_name, _ = await self._get_discord_user(best_wr_entry[0].discord_id)
            best_wr = f"{best_wr_name} ({best_wr_entry[4]}%)"
        else:
            best_wr = None
        
        # Plus de games
        total_games_values = [e[5] + e[6] for e in entries]
        max_games = max(total_games_values)
        most_games_entry = next(e for e in entries if (e[5] + e[6]) == max_games)
        most_games_name, _ = await self._get_discord_user(most_games_entry[0].discord_id)
        most_games = f"{most_games_name} ({max_games} games)"
        
        return {
            "highest_rank": highest_rank,
            "best_wr": best_wr,
            "most_games": most_games
        }

    @with_retry()
    async def _get_rank(self, user: User, queue_id: int) -> Tuple[str, str, int, int, int, int]:
        """Get player rank with caching - fully async."""
        key = (user.puuid, queue_id)
        now = time.time()
        if key in self._rank_cache:
            ts, data = self._rank_cache[key]
            if now - ts < self.CACHE_TTL:
                return data

        # Fully async
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
