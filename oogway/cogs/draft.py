# oogway/cogs/draft.py
# ============================================================================
# Draft compétitive – fil public, nom du champion seul, recap + boutons Win
# + Stats méta (pick/ban/win) persistées dans Redis + commande /meta
# + "Capitaines only" partout (Win, side choice, ready-check)
# + Couleur d'embed dynamique (A=bleu, B=rouge) et affichage pseudos capitaines
# + /remplacer : substitution d'un joueur entre les games (cap → nouveau cap aléatoire)
# + Historique complet sauvegardé dans Redis à la fin de chaque série
# ============================================================================

from __future__ import annotations

import asyncio
import difflib
import logging
import random
import time
from typing import Dict, Optional, List, Tuple
from collections import deque

import aiohttp
import discord
from discord import Interaction, app_commands
from discord.ext import commands

from oogway.models.series_state import SeriesState
from oogway.services.chi import predict as chi_predict, bar as chi_bar
from oogway.cogs.profile import r_get, r_set
from oogway.cogs.historique import save_series_to_history
from oogway.database import SessionLocal, User

BAR_FULL, BAR_EMPTY, BAR_BLOCKS = "▰", "▱", 12
logger = logging.getLogger(__name__)

# ───────────────────────────── Data-Dragon ──────────────────────────────
DD_VERSION_CACHE: Optional[str] = None
DD_VERSION_CACHE_TIME: float = 0.0
DD_CACHE_TTL: int = 86400

CHAMPS_CACHE: Dict[str, dict] = {}
CHAMPS_CACHE_TIME: float = 0.0
ALIASES: Dict[str, str] = {}

_CHAMP_LIST_CACHE: Dict[tuple, str] = {}
_CHAMP_LIST_CACHE_MAX = 200


async def ddragon_version(force_refresh: bool = False) -> str:
    global DD_VERSION_CACHE, DD_VERSION_CACHE_TIME
    now = time.time()
    if not force_refresh and DD_VERSION_CACHE and (now - DD_VERSION_CACHE_TIME) < DD_CACHE_TTL:
        return DD_VERSION_CACHE
    async with aiohttp.ClientSession() as s:
        async with s.get("https://ddragon.leagueoflegends.com/api/versions.json") as r:
            DD_VERSION_CACHE = (await r.json())[0]
            DD_VERSION_CACHE_TIME = now
            return DD_VERSION_CACHE


async def load_champs(force_refresh: bool = False) -> None:
    global CHAMPS_CACHE, ALIASES, CHAMPS_CACHE_TIME
    now = time.time()
    if not force_refresh and CHAMPS_CACHE and (now - CHAMPS_CACHE_TIME) < DD_CACHE_TTL:
        return
    ver = await ddragon_version(force_refresh)
    url = f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/champion.json"
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            CHAMPS_CACHE = {v["id"]: v for v in (await r.json())["data"].values()}
    CHAMPS_CACHE_TIME = now
    logger.info("Champions chargés : %d", len(CHAMPS_CACHE))

    manual = {
        "lb": "Leblanc", "mf": "MissFortune", "tf": "TwistedFate",
        "j4": "JarvanIV", "ww": "Warwick", "gp": "Gangplank",
        "wu": "MonkeyKing", "wk": "MonkeyKing", "wukong": "MonkeyKing",
        "mk": "MonkeyKing", "monkey": "MonkeyKing",
        "belv": "Belveth", "ks": "KSante", "cho": "Chogath",
    }
    ALIASES.clear()
    taken: set[str] = set()
    for cid in CHAMPS_CACHE:
        slug   = cid.lower()
        nospace = slug.replace(" ", "")
        ALIASES[slug]   = cid
        ALIASES[nospace] = cid
        abbr3 = nospace[:3]
        if abbr3 not in ALIASES and abbr3 not in taken:
            ALIASES[abbr3] = cid
            taken.add(abbr3)
    ALIASES.update(manual)


def canonicalize(name: str) -> Optional[str]:
    key = name.lower().replace(" ", "")
    if key in ALIASES:
        return ALIASES[key]
    if match := difflib.get_close_matches(key, ALIASES.keys(), n=1, cutoff=0.8):
        return ALIASES[match[0]]
    return None


# ─── Draft order ──────────────────────────────────────────────────────────────
DRAFT_ORDER = (
    ["A", "B", "A", "B", "A", "B"]
    + ["A", "B", "B", "A", "A", "B"]
    + ["B", "A", "B", "A"]
    + ["B", "A", "A", "B"]
)
BAN_INDEXES = {0, 1, 2, 3, 4, 5, 12, 13, 14, 15}


def random_champ(series: SeriesState, taken: set[str]) -> str:
    pool = [c for c in CHAMPS_CACHE if c not in taken and c not in series.fearless_pool]
    return random.choice(pool)


def time_bar(seconds_left: int) -> str:
    filled = round(seconds_left / 60 * BAR_BLOCKS)
    return BAR_FULL * filled + BAR_EMPTY * (BAR_BLOCKS - filled)


# ─── Meta helpers ─────────────────────────────────────────────────────────────
META_KEY = "meta:champions"

async def _meta_load() -> dict:
    data = await r_get(META_KEY) or {}
    data.setdefault("picks", {}); data.setdefault("bans", {}); data.setdefault("wins", {})
    data["picks"] = {str(k): int(v) for k, v in data["picks"].items()}
    data["bans"]  = {str(k): int(v) for k, v in data["bans"].items()}
    data["wins"]  = {str(k): int(v) for k, v in data["wins"].items()}
    return data

async def _meta_save(data: dict) -> None:
    await r_set(META_KEY, data, ttl=180*24*3600)

async def _meta_update_for_game(picks_a, picks_b, bans_a, bans_b, winner_side):
    data = await _meta_load()
    P, B, W = data["picks"], data["bans"], data["wins"]
    for cid in picks_a + picks_b: P[cid] = P.get(cid, 0) + 1
    for cid in bans_a  + bans_b:  B[cid] = B.get(cid, 0) + 1
    for cid in (picks_a if winner_side == "A" else picks_b): W[cid] = W.get(cid, 0) + 1
    await _meta_save(data)

def _compute_meta_tables(data: dict, top: int = 10, min_picks_for_wr: int = 10):
    P, B, W = data["picks"], data["bans"], data["wins"]
    presence = sorted([(cid, P.get(cid, 0) + B.get(cid, 0)) for cid in set(P) | set(B)],
                      key=lambda x: x[1], reverse=True)
    top_picks = sorted(P.items(), key=lambda x: x[1], reverse=True)
    top_bans  = sorted(B.items(), key=lambda x: x[1], reverse=True)
    wr_entries = [(cid, (W.get(cid, 0) / pc) * 100.0, pc)
                  for cid, pc in P.items() if pc >= min_picks_for_wr]
    wr_entries.sort(key=lambda x: x[1], reverse=True)
    return {"presence": presence[:top], "picks": top_picks[:top],
            "bans": top_bans[:top], "winrates": wr_entries[:top]}


# ─── Helpers DB (pour vérifier /link du remplaçant) ───────────────────────────
def is_user_linked(uid: int) -> bool:
    with SessionLocal() as db:
        return db.query(User).filter_by(discord_id=str(uid)).first() is not None


# ─── Vues d'interaction ───────────────────────────────────────────────────────
class ResultView(discord.ui.View):
    def __init__(self, cog: "DraftCog", series: SeriesState):
        super().__init__(timeout=None)
        self.cog, self.series = cog, series
        self._lock = asyncio.Lock()

    async def _guard(self, inter: Interaction) -> bool:
        if inter.user.id not in (self.series.captain_a, self.series.captain_b):
            await inter.response.send_message("⛔ Capitaines only.", ephemeral=True)
            return False
        if self._lock.locked():
            await inter.response.send_message("⏳ Vote déjà en cours...", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Team A gagne", emoji="🔵", style=discord.ButtonStyle.success)
    async def win_a(self, inter: Interaction, _):
        if not await self._guard(inter): return
        async with self._lock:
            await inter.response.defer()
            await self.cog._report(inter, "A")
            try: await inter.message.delete()
            except Exception: pass

    @discord.ui.button(label="✅ Team B gagne", emoji="🔴", style=discord.ButtonStyle.danger)
    async def win_b(self, inter: Interaction, _):
        if not await self._guard(inter): return
        async with self._lock:
            await inter.response.defer()
            await self.cog._report(inter, "B")
            try: await inter.message.delete()
            except Exception: pass


class SideChoiceView(discord.ui.View):
    def __init__(self, loser_id: int, captain_a_id: int, captain_b_id: int):
        super().__init__(timeout=60)
        self.loser_id = loser_id
        self.captain_a_id = captain_a_id
        self.captain_b_id = captain_b_id
        self.swap_chosen: Optional[bool] = None
        self._done = asyncio.Event()

    async def _guard(self, inter: Interaction) -> bool:
        if inter.user.id != self.loser_id:
            await inter.response.send_message("⛔ Capitaine perdant uniquement.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🔄 Inverser les sides", style=discord.ButtonStyle.primary)
    async def swap(self, inter: Interaction, _):
        if not await self._guard(inter): return
        self.swap_chosen = True
        for i in self.children: i.disabled = True
        await inter.response.edit_message(
            content=(
                f"🔄 **Sides inversés !**\n\n"
                f"🔵 <@{self.captain_b_id}> → **Team A**\n"
                f"🔴 <@{self.captain_a_id}> → **Team B**"
            ), view=self)
        self._done.set()

    @discord.ui.button(label="➡️ Garder les sides", style=discord.ButtonStyle.secondary)
    async def keep(self, inter: Interaction, _):
        if not await self._guard(inter): return
        self.swap_chosen = False
        for i in self.children: i.disabled = True
        await inter.response.edit_message(
            content=(
                f"✅ **Sides inchangés !**\n\n"
                f"🔵 <@{self.captain_a_id}> → **Team A**\n"
                f"🔴 <@{self.captain_b_id}> → **Team B**"
            ), view=self)
        self._done.set()

    async def on_timeout(self):
        if self.swap_chosen is None:
            self.swap_chosen = False
            self._done.set()


class CaptainsReadyView(discord.ui.View):
    def __init__(self, cap_a: int, cap_b: int):
        super().__init__(timeout=120)
        self.cap_a, self.cap_b = cap_a, cap_b
        self.ready: set[int] = set()
        self._done = asyncio.Event()

    def _label(self, uid: int, name: str) -> str:
        return f"✅ {name} prêt" if uid in self.ready else f"⏳ {name} pas prêt"

    @discord.ui.button(label="⏳ Capitaine A pas prêt", style=discord.ButtonStyle.secondary, row=0)
    async def ready_a(self, inter: Interaction, btn: discord.ui.Button):
        if inter.user.id != self.cap_a:
            return await inter.response.send_message("⛔ Capitaine A uniquement.", ephemeral=True)
        if self.cap_a in self.ready: self.ready.remove(self.cap_a)
        else: self.ready.add(self.cap_a)
        btn.label = self._label(self.cap_a, inter.user.display_name)
        btn.style = discord.ButtonStyle.success if self.cap_a in self.ready else discord.ButtonStyle.secondary
        await inter.response.edit_message(view=self)
        if self.cap_a in self.ready and self.cap_b in self.ready: self._done.set()

    @discord.ui.button(label="⏳ Capitaine B pas prêt", style=discord.ButtonStyle.secondary, row=0)
    async def ready_b(self, inter: Interaction, btn: discord.ui.Button):
        if inter.user.id != self.cap_b:
            return await inter.response.send_message("⛔ Capitaine B uniquement.", ephemeral=True)
        if self.cap_b in self.ready: self.ready.remove(self.cap_b)
        else: self.ready.add(self.cap_b)
        btn.label = self._label(self.cap_b, inter.user.display_name)
        btn.style = discord.ButtonStyle.success if self.cap_b in self.ready else discord.ButtonStyle.secondary
        await inter.response.edit_message(view=self)
        if self.cap_a in self.ready and self.cap_b in self.ready: self._done.set()

    async def on_timeout(self):
        self._done.set()


class ContinueView(discord.ui.View):
    def __init__(self, captains: tuple[int, int], next_bo: int,
                 is_tied: bool = False, current_score: str = ""):
        super().__init__(timeout=60)
        self.captains = captains
        self.next_bo = next_bo
        self.is_tied = is_tied
        self.current_score = current_score
        self.go_next: Optional[bool] = None
        self._done = asyncio.Event()
        if is_tied:
            self.children[0].label = f"✅ Jouer la belle (Bo{next_bo})"
            self.children[1].label = f"🤝 Terminer à {current_score}"

    @discord.ui.button(label="✅ Continuer", style=discord.ButtonStyle.success)
    async def go(self, inter: Interaction, _):
        if inter.user.id not in self.captains:
            return await inter.response.send_message("⛔ Capitaines only.", ephemeral=True)
        self.go_next = True
        for i in self.children: i.disabled = True
        msg = (f"✅ **Belle confirmée !** Passage en **Bo{self.next_bo}**."
               if self.is_tied else f"✅ Passage en **Bo{self.next_bo}** confirmé !")
        await inter.response.edit_message(content=msg, view=self)
        self._done.set()

    @discord.ui.button(label="❌ Terminer", style=discord.ButtonStyle.danger)
    async def stop(self, inter: Interaction, _):
        if inter.user.id not in self.captains:
            return await inter.response.send_message("⛔ Capitaines only.", ephemeral=True)
        self.go_next = False
        for i in self.children: i.disabled = True
        msg = (f"🤝 Série clôturée sur un **match nul {self.current_score}**."
               if self.is_tied else "❌ Série clôturée.")
        await inter.response.edit_message(content=msg, view=self)
        self._done.set()

    async def on_timeout(self):
        if self.go_next is None:
            self.go_next = False
            self._done.set()


# ─── SubstituteView : confirmation visuelle du remplacement ───────────────────
class SubstituteConfirmView(discord.ui.View):
    """Vue de confirmation affichée dans le thread pour valider un remplacement."""
    def __init__(self, requestor_id: int, out_id: int, in_id: int,
                 other_captain_id: int):
        super().__init__(timeout=60)
        self.requestor_id     = requestor_id
        self.out_id           = out_id
        self.in_id            = in_id
        self.other_captain_id = other_captain_id
        self.confirmed: Optional[bool] = None
        self._done = asyncio.Event()

    @discord.ui.button(label="✅ Valider", style=discord.ButtonStyle.success)
    async def confirm(self, inter: Interaction, _):
        # Les deux capitaines doivent valider OU l'organisateur peut forcer
        if inter.user.id not in (self.requestor_id, self.other_captain_id):
            return await inter.response.send_message("⛔ Capitaines uniquement.", ephemeral=True)
        self.confirmed = True
        for i in self.children: i.disabled = True
        await inter.response.edit_message(
            content=f"✅ Remplacement validé : <@{self.out_id}> ➜ <@{self.in_id}>",
            view=self,
        )
        self._done.set()

    @discord.ui.button(label="❌ Annuler", style=discord.ButtonStyle.danger)
    async def cancel(self, inter: Interaction, _):
        if inter.user.id not in (self.requestor_id, self.other_captain_id):
            return await inter.response.send_message("⛔ Capitaines uniquement.", ephemeral=True)
        self.confirmed = False
        for i in self.children: i.disabled = True
        await inter.response.edit_message(content="❌ Remplacement annulé.", view=self)
        self._done.set()

    async def on_timeout(self):
        if self.confirmed is None:
            self.confirmed = False
            self._done.set()


# ─── Cog principal ────────────────────────────────────────────────────────────
class DraftCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.series_by_thread: dict[int, SeriesState] = {}
        # Verrous par thread pour éviter remplacements concurrents
        self._sub_locks: dict[int, asyncio.Lock] = {}

    def _get_sub_lock(self, thread_id: int) -> asyncio.Lock:
        if thread_id not in self._sub_locks:
            self._sub_locks[thread_id] = asyncio.Lock()
        return self._sub_locks[thread_id]

    @staticmethod
    def _format_champ_list(champs: list[str]) -> str:
        if not champs:
            return "—"
        key = tuple(champs)
        if key not in _CHAMP_LIST_CACHE:
            _CHAMP_LIST_CACHE[key] = ", ".join(f"`{c}`" for c in champs)
            if len(_CHAMP_LIST_CACHE) > _CHAMP_LIST_CACHE_MAX:
                for old_key in list(_CHAMP_LIST_CACHE.keys())[:50]:
                    _CHAMP_LIST_CACHE.pop(old_key, None)
        return _CHAMP_LIST_CACHE[key]

    # ─── start_draft listener ──────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_start_draft(self, team_a, team_b, channel: discord.TextChannel,
                             bo: int, captain_a: int, captain_b: int):
        await load_champs()
        series = SeriesState.new(bo, team_a, team_b, captain_a, captain_b)
        series.guild = channel.guild
        member_a = channel.guild.get_member(captain_a)
        member_b = channel.guild.get_member(captain_b)
        series.captain_a_name = member_a.display_name if member_a else "Cap A"
        series.captain_b_name = member_b.display_name if member_b else "Cap B"

        thread = await channel.create_thread(
            name=f"draft-{series.id}",
            type=discord.ChannelType.public_thread,
            auto_archive_duration=1440,
        )
        logger.info("Thread draft créé : #%s (Bo %s)", thread.name, bo)
        self.series_by_thread[thread.id] = series

        status = await thread.send(embed=self._build_embed(series, 60, 0, highlight=True))
        series.status_msg_id = status.id
        await self._draft_loop(thread, series, status)

    # ─── Boucle bans/picks ────────────────────────────────────────────────
    async def _draft_loop(self, thread: discord.Thread, series: SeriesState, status_msg: discord.Message):
        TURN_TIME = 2 if len([uid for uid in series.team_a + series.team_b if uid > 0]) == 1 else 60
        ptr = 0
        taken: set[str] = set()

        while ptr < len(DRAFT_ORDER):
            side    = DRAFT_ORDER[ptr]
            captain = series.captain_a if side == "A" else series.captain_b
            is_ban  = ptr in BAN_INDEXES

            try:
                await thread.send(
                    f"👉 <@{captain}> à toi ({'BAN' if is_ban else 'PICK'})",
                    delete_after=3
                )
            except discord.HTTPException:
                pass

            def make_check(captain_id: int):
                def check(m: discord.Message) -> bool:
                    return m.channel.id == thread.id and m.author.id == captain_id
                return check

            check    = make_check(captain)
            deadline = asyncio.get_event_loop().time() + TURN_TIME
            champ_id = None
            last_update = TURN_TIME
            msg_task = asyncio.create_task(self.bot.wait_for("message", check=check))

            try:
                while True:
                    now       = asyncio.get_event_loop().time()
                    remaining = max(0, int(deadline - now))
                    if remaining == 0:
                        break
                    if remaining != last_update and (remaining % 5 == 0 or remaining <= 10):
                        last_update = remaining
                        try:
                            await status_msg.edit(
                                embed=self._build_embed(series, remaining, ptr, highlight=True)
                            )
                        except discord.HTTPException:
                            pass
                    try:
                        msg = await asyncio.wait_for(asyncio.shield(msg_task), timeout=0.5)
                        raw  = msg.content.strip()
                        name = raw
                        lower = raw.lower()
                        if lower.startswith(("/ban ", "/pick ", "ban ", "pick ")):
                            parts = raw.split(maxsplit=1)
                            if len(parts) == 2:
                                name = parts[1]
                        try:
                            await msg.delete()
                        except (discord.Forbidden, discord.HTTPException):
                            pass
                        cand = canonicalize(name)
                        if not cand:
                            sugg = difflib.get_close_matches(
                                name.lower().replace(" ", ""), ALIASES.keys(), n=3, cutoff=0.6
                            )
                            tip = f" Essaye: {', '.join(ALIASES[s] for s in sugg)}" if sugg else ""
                            await thread.send(f"❓ Champion inconnu: **{name}**.{tip}", delete_after=4)
                            msg_task = asyncio.create_task(self.bot.wait_for("message", check=check))
                            continue
                        if cand in taken or cand in series.fearless_pool:
                            await thread.send("⚠️ Champion déjà pris / interdit.", delete_after=3)
                            msg_task = asyncio.create_task(self.bot.wait_for("message", check=check))
                            continue
                        champ_id = cand
                        break
                    except asyncio.TimeoutError:
                        continue
            finally:
                if not msg_task.done():
                    msg_task.cancel()
                    try: await msg_task
                    except asyncio.CancelledError: pass

            if champ_id is None:
                champ_id = random_champ(series, taken)
                await thread.send(f"⏰ Temps écoulé ! **{champ_id}** sélectionné aléatoirement.")

            game = series.current_game
            if is_ban:
                (game.bans_a if side == "A" else game.bans_b).append(champ_id)
            else:
                (game.picks_a if side == "A" else game.picks_b).append(champ_id)
                series.fearless_pool.add(champ_id)
            taken.add(champ_id)
            ptr += 1

            try:
                await status_msg.edit(embed=self._build_embed(series, TURN_TIME, ptr, highlight=True))
            except discord.HTTPException:
                pass

        logger.info("Draft terminée – série %s", series.id)
        await thread.send(
            embeds=[self._build_recap_embed(series), self._build_chi_embed(series)],
            view=ResultView(self, series)
        )

    # ─── Embeds ───────────────────────────────────────────────────────────
    @staticmethod
    def _turn_color(side: Optional[str]) -> discord.Colour:
        if side == "A": return discord.Colour.from_rgb(30, 136, 229)
        if side == "B": return discord.Colour.from_rgb(229, 57, 53)
        return discord.Colour.blurple()

    def _build_embed(self, series: SeriesState, secs: int, ptr: int, *, highlight=False) -> discord.Embed:
        g = series.current_game
        bar = time_bar(secs)
        capA_mention = f"<@{series.captain_a}>"
        capB_mention = f"<@{series.captain_b}>"
        capA_name    = series.captain_a_name
        capB_name    = series.captain_b_name

        if ptr < len(DRAFT_ORDER):
            side   = DRAFT_ORDER[ptr]
            phase  = "BAN" if ptr in BAN_INDEXES else "PICK"
            who    = capA_mention if side == "A" else capB_mention
            header = (
                f"```\n{bar} {secs:>2}s\n```\n**Tour {who} · {phase}**"
                if highlight else f"```\n{bar} {secs:>2}s\n```{who} · {phase}"
            )
            colour = self._turn_color(side)
        else:
            header = "```\nDraft terminée\n```"
            colour = self._turn_color(None)

        embed = discord.Embed(
            title=f"⚔️ Draft Phase · Game {len(series.games)}",
            colour=colour,
            description=header,
        )
        embed.add_field(name="━━━━━━━━━━━━━━━━━━━━━━", value="", inline=False)
        embed.add_field(name=f"🚫 BANS — 🔵 {capA_name}",
                        value=f"{capA_mention}\n{self._format_champ_list(g.bans_a)}", inline=True)
        embed.add_field(name=f"🚫 BANS — 🔴 {capB_name}",
                        value=f"{capB_mention}\n{self._format_champ_list(g.bans_b)}", inline=True)
        embed.add_field(name="━━━━━━━━━━━━━━━━━━━━━━", value="", inline=False)
        embed.add_field(name=f"✅ PICKS — 🔵 {capA_name}",
                        value=f"{capA_mention}\n{self._format_champ_list(g.picks_a)}", inline=True)
        embed.add_field(name=f"✅ PICKS — 🔴 {capB_name}",
                        value=f"{capB_mention}\n{self._format_champ_list(g.picks_b)}", inline=True)
        embed.set_footer(text="Capitaines only • messages hors capitaines supprimés")
        return embed

    @staticmethod
    def _build_recap_embed(series: SeriesState) -> discord.Embed:
        g = series.current_game
        capA = f"<@{series.captain_a}>"
        capB = f"<@{series.captain_b}>"
        embed = discord.Embed(
            title=f"📊 Récapitulatif · Game {len(series.games)}",
            colour=discord.Colour.dark_gold(),
            description=f"```\nScore : {series.score_a}-{series.score_b}\n```\nVainqueur (Capitaines only).",
        )
        embed.add_field(name=f"🚫 BANS — 🔵 {capA}",
                        value=DraftCog._format_champ_list(g.bans_a), inline=True)
        embed.add_field(name=f"🚫 BANS — 🔴 {capB}",
                        value=DraftCog._format_champ_list(g.bans_b), inline=True)
        embed.add_field(name=f"✅ PICKS — 🔵 {capA}",
                        value=DraftCog._format_champ_list(g.picks_a), inline=True)
        embed.add_field(name=f"✅ PICKS — 🔴 {capB}",
                        value=DraftCog._format_champ_list(g.picks_b), inline=True)
        return embed

    @staticmethod
    def _build_chi_embed(series: SeriesState) -> discord.Embed:
        g = series.current_game
        p_blue, p_red = chi_predict(g.picks_a, g.picks_b)
        advantage = abs(p_blue - p_red)
        adv_side  = "🔵 Blue" if p_blue > p_red else "🔴 Red" if p_red > p_blue else "⚖️ Équilibré"
        embed = discord.Embed(
            title="⚖️ Prédiction Chi · Meta Analysis",
            colour=discord.Colour.from_rgb(0, 176, 255),
            description=(
                f"```\n🔵 Blue: {p_blue:5.1f}%\n🔴 Red:  {p_red:5.1f}%\n```\n"
                f"**Avantage:** {adv_side} ({advantage:.1f}%)"
            ),
        )
        embed.add_field(name="Balance visuelle", value=f"```\n{chi_bar(p_blue)}\n```", inline=False)
        return embed

    # ─── Anti-spam hors capitaines ────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.author.bot: return
        series = self.series_by_thread.get(getattr(msg.channel, "id", 0))
        if series and msg.author.id not in (series.captain_a, series.captain_b):
            try: await msg.delete()
            except (discord.Forbidden, discord.HTTPException): pass

    # ─── Report helper ────────────────────────────────────────────────────
    async def _report(self, inter: Interaction, side: str):
        if not isinstance(inter.channel, discord.Thread) or not inter.channel.name.startswith("draft-"):
            return await inter.followup.send("❌ À utiliser dans le thread draft.", ephemeral=True)
        series = self.series_by_thread.get(inter.channel.id)
        if not series:
            return await inter.followup.send("❌ Série inconnue.", ephemeral=True)
        if series.current_game.winner:
            return await inter.followup.send("⚠️ Partie déjà reportée.", ephemeral=True)

        series.current_game.winner = side
        series.score_a += side == "A"
        series.score_b += side == "B"
        logger.info("Victoire Team %s (score %d-%d)", side, series.score_a, series.score_b)
        await inter.channel.send("🔄 **Besoin d'un remplacement ?** Utilisez `/remplacer` maintenant, avant la prochaine draft.",delete_after=30)

        g = series.current_game
        try:
            await _meta_update_for_game(g.picks_a, g.picks_b, g.bans_a, g.bans_b, side)
        except Exception as e:
            logger.warning(f"[meta] update failed: {e}")

        if series.finished():
            if series.bo == 1:
                await self._handle_bo1_end(inter, series, side); return
            if series.bo == 3 and series.score_a == 1 and series.score_b == 1:
                await self._handle_bo3_tie(inter, series, side); return
            if series.bo == 3:
                await self._handle_bo3_end(inter, series, side); return
            if series.bo == 5 and series.score_a == 2 and series.score_b == 2:
                await self._handle_bo5_tie(inter, series, side); return
            await self._handle_series_victory(inter, series, side)
            return

        await self._continue_series(inter, series, side)

    # ─── Fin de série helpers ─────────────────────────────────────────────
    async def _finalize_series(self, inter: Interaction, series: SeriesState):
        """Sauvegarde l'historique et nettoie le state."""
        import time as _time
        series.ended_at = _time.time()
        try:
            await save_series_to_history(series)
        except Exception as e:
            logger.error(f"Erreur sauvegarde historique: {e}")
        self.series_by_thread.pop(inter.channel.id, None)
        self._sub_locks.pop(inter.channel.id, None)

    async def _handle_bo1_end(self, inter, series, side):
        cont = ContinueView((series.captain_a, series.captain_b), next_bo=3)
        msg  = await inter.channel.send(
            f"🏆 Bo1 terminé (**{series.score_a}-{series.score_b}**). Poursuivre en **Bo3** ?",
            view=cont,
        )
        await cont._done.wait()
        try: await msg.delete()
        except: pass
        if cont.go_next:
            series.bo = 3
            await self._handle_side_choice_and_ready(inter, series, side)
            await self._start_next_game(inter, series)
        else:
            await self._handle_series_victory(inter, series, side)

    async def _handle_bo3_tie(self, inter, series, side):
        cont = ContinueView((series.captain_a, series.captain_b), 3, is_tied=True, current_score="1-1")
        msg  = await inter.channel.send("⚖️ **Match nul 1-1** ! Jouer une belle ?", view=cont)
        await cont._done.wait()
        try: await msg.delete()
        except: pass
        if not cont.go_next:
            embed = discord.Embed(title="🤝 Match nul 1-1", colour=discord.Colour.gold(),
                                  description="Égalité parfaite !").set_footer(text="GG à tous !")
            await inter.channel.send(embed=embed)
            await self._finalize_series(inter, series)
        else:
            await self._handle_side_choice_and_ready(inter, series, side)
            await self._start_next_game(inter, series)

    async def _handle_bo3_end(self, inter, series, side):
        cont = ContinueView((series.captain_a, series.captain_b), next_bo=5)
        msg  = await inter.channel.send(
            f"🏆 Bo3 terminé (**{series.score_a}-{series.score_b}**). Poursuivre en **Bo5** ?",
            view=cont,
        )
        await cont._done.wait()
        try: await msg.delete()
        except: pass
        if cont.go_next:
            series.bo = 5
            await self._handle_side_choice_and_ready(inter, series, side)
            await self._start_next_game(inter, series)
        else:
            await self._handle_series_victory(inter, series, side)

    async def _handle_bo5_tie(self, inter, series, side):
        cont = ContinueView((series.captain_a, series.captain_b), 5, is_tied=True, current_score="2-2")
        msg  = await inter.channel.send("⚖️ **Match nul 2-2** ! Jouer une belle ?", view=cont)
        await cont._done.wait()
        try: await msg.delete()
        except: pass
        if not cont.go_next:
            embed = discord.Embed(title="🤝 Match nul 2-2", colour=discord.Colour.gold(),
                                  description="Égalité parfaite !").set_footer(text="GG à tous !")
            await inter.channel.send(embed=embed)
            await self._finalize_series(inter, series)
        else:
            await self._handle_side_choice_and_ready(inter, series, side)
            await self._start_next_game(inter, series)

    async def _handle_series_victory(self, inter, series, side):
        winners  = series.team_a if side == "A" else series.team_b
        mentions = "\n".join(f"<@{uid}>" for uid in winners)
        embed    = discord.Embed(
            title=f"🏆  Victoire Team {'A' if side=='A' else 'B'}  —  {series.score_a}-{series.score_b}",
            colour=discord.Colour.gold(),
            description=mentions,
        ).set_footer(text="GG à tous !")
        await inter.channel.send(embed=embed)
        await self._finalize_series(inter, series)

    async def _handle_side_choice_and_ready(self, inter, series, last_winner):
        loser = series.captain_b if last_winner == "A" else series.captain_a
        scv   = SideChoiceView(loser, series.captain_a, series.captain_b)
        msg   = await inter.channel.send(f"🧭 <@{loser}> choisit les **sides** :", view=scv)
        await scv._done.wait()
        try: await msg.delete()
        except: pass
        if scv.swap_chosen:
            series.swap_sides()
        rv  = CaptainsReadyView(series.captain_a, series.captain_b)
        msg = await inter.channel.send("⏳ Ready check des capitaines…", view=rv)
        await rv._done.wait()
        try: await msg.delete()
        except: pass

    async def _continue_series(self, inter, series, side):
        await self._handle_side_choice_and_ready(inter, series, side)
        await self._start_next_game(inter, series)

    async def _start_next_game(self, inter, series):
        series.start_new_game()
        status = await inter.channel.send(embed=self._build_embed(series, 60, 0, highlight=True))
        series.status_msg_id = status.id
        if series.fearless_pool:
            await inter.channel.send(embed=discord.Embed(
                title="🔥 Fearless — champions désormais bannis",
                description=", ".join(sorted(series.fearless_pool)),
                colour=discord.Colour.red(),
            ))
        await self._draft_loop(inter.channel, series, status)

    # ─── /remplacer ───────────────────────────────────────────────────────
    @app_commands.command(
        name="remplacer",
        description="Remplacer un joueur entre deux games (capitaines uniquement)"
    )
    @app_commands.describe(
        joueur_sortant="Le joueur qui quitte",
        joueur_entrant="Le remplaçant (doit avoir fait /link)",
    )
    async def remplacer(self, inter: Interaction,
                        joueur_sortant: discord.Member,
                        joueur_entrant: discord.Member):
        await inter.response.defer(ephemeral=True)

        # ── Vérifier que la commande est dans un thread de draft ──────────
        thread = inter.channel
        if not isinstance(thread, discord.Thread) or not thread.name.startswith("draft-"):
            return await inter.followup.send(
                "❌ Cette commande doit être utilisée dans le thread de draft.", ephemeral=True
            )

        series = self.series_by_thread.get(thread.id)
        if not series:
            return await inter.followup.send("❌ Aucune série active dans ce thread.", ephemeral=True)

        # ── Seuls les capitaines peuvent demander un remplacement ─────────
        if inter.user.id not in (series.captain_a, series.captain_b):
            return await inter.followup.send("⛔ Capitaines uniquement.", ephemeral=True)

        out_id = joueur_sortant.id
        in_id  = joueur_entrant.id

        # ── Vérifications de base ─────────────────────────────────────────
        if out_id == in_id:
            return await inter.followup.send("❌ Même joueur entrant et sortant.", ephemeral=True)

        all_players = series.team_a + series.team_b
        if out_id not in all_players:
            return await inter.followup.send(
                f"❌ <@{out_id}> n'est pas dans la série.", ephemeral=True
            )
        if in_id in all_players:
            return await inter.followup.send(
                f"❌ <@{in_id}> est déjà dans la série.", ephemeral=True
            )

        # ── Le remplaçant doit être linked ────────────────────────────────
        if not is_user_linked(in_id):
            return await inter.followup.send(
                f"❌ <@{in_id}> n'a pas encore lié son compte Riot (`/link`).", ephemeral=True
            )

        # ── Vérifier que la draft est terminée (entre deux games) ─────────
        # On interdit le remplacement pendant une draft active
        # La draft est "active" si la game courante n'a pas encore de winner
        # ET qu'elle a déjà des picks/bans
        current = series.current_game
        draft_in_progress = (
            current.winner is None and
            (current.picks_a or current.picks_b or current.bans_a or current.bans_b)
        )
        if draft_in_progress:
            return await inter.followup.send(
                "❌ Impossible de remplacer pendant une draft en cours. "
                "Attends la fin de la game.", ephemeral=True
            )

        # ── Lock anti-concurrence ──────────────────────────────────────────
        lock = self._get_sub_lock(thread.id)
        if lock.locked():
            return await inter.followup.send(
                "⏳ Un remplacement est déjà en cours.", ephemeral=True
            )

        async with lock:
            # ── Confirmation par l'autre capitaine ────────────────────────
            other_cap = series.captain_b if inter.user.id == series.captain_a else series.captain_a
            was_captain_note = ""
            if out_id in (series.captain_a, series.captain_b):
                was_captain_note = "\n⚠️ **Le sortant est capitaine** — un nouveau cap sera tiré aléatoirement dans son équipe."

            confirm_view = SubstituteConfirmView(
                requestor_id     = inter.user.id,
                out_id           = out_id,
                in_id            = in_id,
                other_captain_id = other_cap,
            )
            confirm_msg = await thread.send(
                content=(
                    f"🔄 **Demande de remplacement**\n"
                    f"<@{out_id}> ➜ <@{in_id}>{was_captain_note}\n\n"
                    f"<@{other_cap}> ou <@{inter.user.id}> — confirme ou annule :"
                ),
                view=confirm_view,
            )
            await inter.followup.send("⏳ Demande envoyée dans le thread.", ephemeral=True)
            await confirm_view._done.wait()

            if not confirm_view.confirmed:
                return  # Message déjà édité par la vue

            # ── Appliquer le remplacement ─────────────────────────────────
            try:
                rec = series.substitute(out_id, in_id)
            except ValueError as e:
                await thread.send(f"❌ Erreur remplacement : {e}")
                return

            # ── Mettre à jour les noms des capitaines si nécessaire ───────
            if rec.was_captain and rec.new_captain_id:
                new_cap_member = inter.guild.get_member(rec.new_captain_id) if inter.guild else None
                new_cap_name   = new_cap_member.display_name if new_cap_member else f"Joueur {rec.new_captain_id}"
                if rec.team == "A":
                    series.captain_a_name = new_cap_name
                else:
                    series.captain_b_name = new_cap_name

            # ── Notification dans le thread ───────────────────────────────
            cap_change_str = ""
            if rec.was_captain and rec.new_captain_id:
                cap_change_str = f"\n👑 Nouveau capitaine Team {rec.team} : <@{rec.new_captain_id}>"

            team_a_str = " ".join(f"<@{u}>" for u in series.team_a)
            team_b_str = " ".join(f"<@{u}>" for u in series.team_b)

            embed = discord.Embed(
                title="🔄 Remplacement effectué",
                colour=discord.Colour.orange(),
                description=(
                    f"**<@{out_id}>** ➜ **<@{in_id}>** (Team {rec.team})"
                    f"{cap_change_str}"
                ),
            )
            embed.add_field(name="🔵 Team A", value=team_a_str or "—", inline=True)
            embed.add_field(name="🔴 Team B", value=team_b_str or "—", inline=True)
            embed.set_footer(
                text=f"Game {rec.game_number} · "
                     f"Capitaines : <@{series.captain_a}> vs <@{series.captain_b}>"
            )
            await thread.send(embed=embed)
            logger.info(
                f"🔄 Remplacement {out_id}→{in_id} (Team {rec.team}, "
                f"cap={rec.was_captain}, new_cap={rec.new_captain_id}) "
                f"série {series.id}"
            )
    # ─── /draft-fix ────────────────────────────────────────────────────────────
    @app_commands.command(
    name="draft-fix",
    description="Corriger un pick ou ban après une erreur (admin only)"
    )
    @app_commands.describe(
        type="pick ou ban",
        team="A ou B",
        position="Position dans la liste (1 à 5 pour picks, 1 à 5 pour bans)",
        champion="Nom du champion correct",
    )
    @app_commands.choices(
        type=[
            app_commands.Choice(name="pick", value="pick"),
            app_commands.Choice(name="ban",  value="ban"),
        ],
        team=[
            app_commands.Choice(name="Team A", value="A"),
            app_commands.Choice(name="Team B", value="B"),
        ]
    )
    async def draft_fix(self, inter: Interaction, type: str, team: str,
                        position: int, champion: str):
        await inter.response.defer(ephemeral=True)
    
        # Vérifier thread de draft
        if not isinstance(inter.channel, discord.Thread) or not inter.channel.name.startswith("draft-"):
            return await inter.followup.send("❌ Dans le thread de draft uniquement.", ephemeral=True)
    
        series = self.series_by_thread.get(inter.channel.id)
        if not series:
            return await inter.followup.send("❌ Aucune série active.", ephemeral=True)
    
        # Seul le créateur (captain_a par convention) ou un admin peut corriger
        if inter.user.id not in (series.captain_a, series.captain_b):
            # Vérifier rôle admin
            if not (inter.guild and inter.guild.get_member(inter.user.id) and
                    any(r.id == settings.ORGANIZER_ROLE_ID
                        for r in inter.guild.get_member(inter.user.id).roles)):
                return await inter.followup.send("⛔ Capitaines ou organisateur uniquement.", ephemeral=True)
    
        # Interdire pendant une draft active
        g = series.current_game
        draft_active = (g.winner is None and (g.picks_a or g.picks_b or g.bans_a or g.bans_b))
        if draft_active:
            return await inter.followup.send(
                "❌ Draft en cours — attends la fin de la game.\n"
                "Si c'est une erreur **pendant** la draft, utilise `/draft-undo` à la place.",
                ephemeral=True
            )
    
        # Canonicaliser le champion
        cand = canonicalize(champion)
        if not cand:
            return await inter.followup.send(f"❌ Champion inconnu : `{champion}`", ephemeral=True)
    
        # Récupérer la bonne liste
        if type == "pick":
            lst = g.picks_a if team == "A" else g.picks_b
            max_pos = 5
        else:
            lst = g.bans_a if team == "A" else g.bans_b
            max_pos = 5
    
        if not (1 <= position <= max_pos):
            return await inter.followup.send(
                f"❌ Position invalide (1 à {max_pos}).", ephemeral=True
            )

        if position > len(lst):
            return await inter.followup.send(
                f"❌ La liste Team {team} n'a que {len(lst)} entrée(s) pour l'instant.", ephemeral=True
            )
    
        old_champ = lst[position - 1]
    
        # Vérifier que le nouveau champion n'est pas déjà pris ailleurs
        all_taken = set(g.picks_a + g.picks_b + g.bans_a + g.bans_b) - {old_champ}
        if cand in all_taken:
            return await inter.followup.send(f"❌ `{cand}` est déjà dans la draft.", ephemeral=True)
    
        # Appliquer la correction
        lst[position - 1] = cand
    
        # Mettre à jour le fearless pool si c'était un pick
        if type == "pick":
            series.fearless_pool.discard(old_champ)
            series.fearless_pool.add(cand)
    
        await inter.followup.send(
            f"✅ Corrigé : `{old_champ}` → `{cand}` (Team {team}, {type} #{position})",
            ephemeral=True
        )
        await inter.channel.send(
            f"📝 **Correction draft** : `{old_champ}` → `{cand}` "
            f"(Team {team}, {type} position {position}) par <@{inter.user.id}>"
        )
    
    # ─── /meta ────────────────────────────────────────────────────────────
    @app_commands.command(name="meta", description="Stats méta customs: top picks/bans/presence/winrate")
    @app_commands.describe(top="Taille du top (1-25)", min_picks="Picks minimum pour le WR")
    async def meta(self, inter: Interaction, top: int = 10, min_picks: int = 10):
        await inter.response.defer()
        data   = await _meta_load()
        tables = _compute_meta_tables(data, top=max(1, min(top, 25)), min_picks_for_wr=max(1, min_picks))

        def fmt_presence():
            return "\n".join(
                f"**{cid}** — {cnt} (picks {data['picks'].get(cid,0)} / bans {data['bans'].get(cid,0)})"
                for cid, cnt in tables["presence"]
            ) or "—"

        embed = discord.Embed(
            title="📈 Méta — customs",
            colour=discord.Colour.dark_teal(),
            description="Agrégé sur toutes les games reportées.",
        )
        embed.add_field(name="👀 Presence", value=fmt_presence(), inline=False)
        embed.add_field(name="✅ Top Picks",
                        value="\n".join(f"**{c}** — {n}" for c, n in tables["picks"]) or "—",
                        inline=True)
        embed.add_field(name="🚫 Top Bans",
                        value="\n".join(f"**{c}** — {n}" for c, n in tables["bans"]) or "—",
                        inline=True)
        embed.add_field(name="🏆 Top Winrates",
                        value="\n".join(f"**{c}** — {wr:.1f}% ({pc} picks)"
                                        for c, wr, pc in tables["winrates"]) or "—",
                        inline=False)
        await inter.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(DraftCog(bot))
