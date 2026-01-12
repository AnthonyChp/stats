# cogs/leaderboard.py

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import time
from typing import List, Tuple, Optional

import discord
from discord.ext import commands, tasks

from oogway.database import SessionLocal, User, init_db
from oogway.riot.client import RiotClient
from oogway.config import settings

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    log.addHandler(handler)

# -------------------------------------------------------------------
# Queue configuration
QUEUE_ORDERS = [420, 440]
QUEUE_NAMES = {420: "Ranked Solo/Duo", 440: "Ranked Flex"}
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
            super().__init__(label='⬅️', style=discord.ButtonStyle.secondary)
        async def callback(self, interaction: discord.Interaction):  # type: ignore
            await interaction.response.defer()
            view: LeaderboardView = self.view  # type: ignore
            view.page = max(view.page - 1, 0)
            embed = await view.cog.build_embed(view.queue_index, view.page, view.sort_by)
            await interaction.edit_original_response(embed=embed, view=view)

    class NextButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label='➡️', style=discord.ButtonStyle.secondary)
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
    """Interactive LP leaderboard with pagination, sorting, queue toggling."""
    CACHE_TTL = 300  # secondes

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()
        self.db = SessionLocal()
        self.riot = RiotClient(settings.RIOT_API_KEY)
        self.sem = asyncio.Semaphore(8)
        self.lb_message: Optional[discord.Message] = None
        self.view: Optional[LeaderboardView] = None
        self._rank_cache: dict[Tuple[str,int], Tuple[float, Tuple[str,str,int,int,int,int]]] = {}

    @staticmethod
    def get_wr_label(wr: int) -> str:
        """Retourne un label humoristique basé sur le winrate."""
        if wr < 40:
            return "**IA ChatGPT**"
        elif wr <= 42:
            return "**Boosted**"
        elif wr <= 45:
            return "**Dans le sac à dos**"
        elif wr <= 48:
            return "**Uber LP**"
        elif wr <= 51:
            return "**All inclusive**"
        elif wr <= 54:
            return "**Semi-boosté**"
        elif wr <= 57:
            return "**Propre**"
        elif wr <= 60:
            return "**Peut jouer seul**"
        elif wr <= 63:
            return "**1v9**"
        elif wr <= 65:
            return "**Illégal en soloQ**"
        else:
            return "**Oogway** 🐢"

    @commands.Cog.listener()
    async def on_ready(self):
        log.info("LeaderboardCog ready, retrieving or sending message")
        channel = self.bot.get_channel(settings.LEADERBOARD_CHANNEL_ID) or await self.bot.fetch_channel(settings.LEADERBOARD_CHANNEL_ID)
        async for msg in channel.history(limit=50):
            if msg.author == self.bot.user and msg.embeds and msg.embeds[0].title.startswith("🏆 Leaderboard —"):
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

    @tasks.loop(minutes=5)
    async def update_loop(self):
        if not self.lb_message:
            return
        embed = await self.build_embed(self.view.queue_index, self.view.page, self.view.sort_by)
        try:
            await self.lb_message.edit(embed=embed)
            log.info("Leaderboard auto-updated")
        except Exception as e:
            log.error(f"Failed auto-update: {e}")

    @update_loop.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()


    async def build_embed(self, queue_idx: int, page: int, sort_by: str) -> discord.Embed:
        queue_id = QUEUE_ORDERS[queue_idx]
        users = self.db.query(User).all()
        # (User, tier, div, lp, wr, wins, losses)
        entries: List[Tuple[User, str, str, int, int, int, int]] = []

        async def fetch(u: User):
            async with self.sem:
                try:
                    tier, div, lp, wr, wins, losses = await self._get_rank(u, queue_id)
                    if tier in TIERS:
                        entries.append((u, tier, div, lp, wr, wins, losses))
                except Exception as e:
                    log.warning(f"Fetch error for {u.discord_id}: {e}")

        await asyncio.gather(*(fetch(u) for u in users))

        if sort_by == "LP":
            key_fn = lambda e: (TIERS.index(e[1]), DIV_WEIGHTS[e[2]], e[3])  # e[3] = lp
        else:
            key_fn = lambda e: (TIERS.index(e[1]), DIV_WEIGHTS[e[2]], e[4])  # e[4] = wr
        entries.sort(key=key_fn, reverse=True)

        per_page = 10
        total_pages = max(math.ceil(len(entries) / per_page), 1)
        page = max(0, min(page, total_pages - 1))
        slice_ = entries[page * per_page:(page + 1) * per_page]

        top_tier = slice_[0][1] if slice_ else "Gold"
        color = TIER_COLORS.get(top_tier, 0x3498db)
        embed = discord.Embed(
            title=f"🏆 Leaderboard — {QUEUE_NAMES[queue_id]}",
            color=color,
            timestamp=dt.datetime.utcnow()
        )
        if getattr(settings, "BOT_ICON_URL", None):
            embed.set_thumbnail(url=settings.BOT_ICON_URL)

        for idx, (u, tier, div, lp, wr, wins, losses) in enumerate(slice_, start=page * per_page + 1):
            medal = MEDALS[idx - 1] + " " if idx <= 3 else ""
            try:
                du = await self.bot.fetch_user(u.discord_id)
                name = du.display_name
                avatar = du.display_avatar.url
            except:
                name = u.puuid[:6]
                avatar = None
            field_name = f"{medal}#{idx} • {name}"
            #        ex: **Platinum II** — 75 LP (54% WR • 120V/102D • **Propre**)
            wr_label = self.get_wr_label(wr)
            field_value = f"**{tier} {div}** — {lp} LP ({wr}% WR • {wins}V/{losses}D • {wr_label})"
            embed.add_field(name=field_name, value=field_value, inline=False)
            if idx == page * per_page + 1 and avatar:
                embed.set_author(name="Leaderboard", icon_url=avatar)

        embed.set_footer(text=f"Page {page + 1}/{total_pages} — Sort: {sort_by} — Mis à jour toutes les 5 minutes")
        return embed

    @with_retry()
    async def _get_rank(self, user: User, queue_id: int) -> Tuple[str, str, int, int, int, int]:
        """Get player rank with caching - now fully async."""
        key = (user.puuid, queue_id)
        now = time.time()
        if key in self._rank_cache:
            ts, data = self._rank_cache[key]
            if now - ts < self.CACHE_TTL:
                return data

        # Now fully async - no run_in_executor needed
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
