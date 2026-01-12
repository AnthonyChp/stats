# oogway/cogs/draft.py
# ============================================================================
# Draft compétitive – fil public, nom du champion seul, recap + boutons Win
# + Stats méta (pick/ban/win) persistées dans Redis + commande /meta
# + “Capitaines only” partout (Win, side choice, ready-check)
# + Couleur d’embed dynamique (A=bleu, B=rouge) et affichage pseudos capitaines
# ============================================================================

from __future__ import annotations

import asyncio
import difflib
import logging
import random
from typing import Dict, Optional, List, Tuple

import aiohttp
import discord
from discord import Interaction, app_commands
from discord.ext import commands

from oogway.models.series_state import SeriesState
from oogway.services.chi import predict as chi_predict, bar as chi_bar
from oogway.cogs.profile import r_get, r_set   # Redis helpers

BAR_FULL, BAR_EMPTY, BAR_BLOCKS = "▰", "▱", 12
logger = logging.getLogger(__name__)

# ───────────────────────────── Data-Dragon ──────────────────────────────
DD_VERSION_CACHE: Optional[str] = None
CHAMPS_CACHE: Dict[str, dict] = {}
ALIASES: Dict[str, str] = {}


async def ddragon_version() -> str:
    global DD_VERSION_CACHE
    if DD_VERSION_CACHE:
        return DD_VERSION_CACHE

    async with aiohttp.ClientSession() as s:
        async with s.get("https://ddragon.leagueoflegends.com/api/versions.json") as r:
            DD_VERSION_CACHE = (await r.json())[0]
            logger.info("Version Data-Dragon : %s", DD_VERSION_CACHE)
            return DD_VERSION_CACHE


async def load_champs() -> None:
    global CHAMPS_CACHE, ALIASES
    if CHAMPS_CACHE:
        return

    ver = await ddragon_version()
    url = f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/champion.json"
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            CHAMPS_CACHE = {v["id"]: v for v in (await r.json())["data"].values()}
    logger.info("Champions chargés : %d", len(CHAMPS_CACHE))

    manual = {
        "lb": "Leblanc", "mf": "MissFortune", "tf": "TwistedFate",
        "j4": "JarvanIV", "ww": "Warwick", "gp": "Gangplank",
        "wu": "MonkeyKing", "wk": "MonkeyKing", "wukong": "MonkeyKing",
        "mk": "MonkeyKing", "monkey": "MonkeyKing",
        "belv": "Belveth", "ks": "KSante", "cho": "Chogath",
    }

    taken: set[str] = set()
    for cid in CHAMPS_CACHE:
        slug = cid.lower()
        nospace = slug.replace(" ", "")
        ALIASES.update({slug: cid, nospace: cid})
        abbr3 = nospace[:3]
        if abbr3 not in ALIASES and abbr3 not in taken:
            ALIASES[abbr3] = cid
            taken.add(abbr3)

    ALIASES.update(manual)
    logger.info("Alias générés : %d (dont %d manuels)", len(ALIASES), len(manual))


def canonicalize(name: str) -> Optional[str]:
    key = name.lower().replace(" ", "")
    if key in ALIASES:
        return ALIASES[key]
    if match := difflib.get_close_matches(key, ALIASES.keys(), n=1, cutoff=0.8):
        logger.debug("Fuzzy «%s» → %s", name, ALIASES[match[0]])
        return ALIASES[match[0]]
    return None


# ───────────────────────── Draft order ────────────────────────────
DRAFT_ORDER = (
    ["A", "B", "A", "B", "A", "B"]
    + ["A", "B", "B", "A", "A", "B"]
    + ["B", "A", "B", "A"]
    + ["B", "A", "A", "B"]
)
BAN_INDEXES = {0, 1, 2, 3, 4, 5, 12, 13, 14, 15}


def random_champ(series: SeriesState, taken: set[str]) -> str:
    pool = [c for c in CHAMPS_CACHE if c not in taken and c not in series.fearless_pool]
    pick = random.choice(pool)
    logger.info("Pick aléatoire : %s", pick)
    return pick


def time_bar(seconds_left: int) -> str:
    filled = round(seconds_left / 60 * BAR_BLOCKS)
    return BAR_FULL * filled + BAR_EMPTY * (BAR_BLOCKS - filled)


# ─────────────────────────── Meta helpers (Redis) ─────────────────────────
META_KEY = "meta:champions"  # {"picks": {cid:int}, "bans": {cid:int}, "wins": {cid:int}}

async def _meta_load() -> dict:
    data = await r_get(META_KEY) or {}
    data.setdefault("picks", {})
    data.setdefault("bans", {})
    data.setdefault("wins", {})
    data["picks"] = {str(k): int(v) for k, v in data["picks"].items()}
    data["bans"]  = {str(k): int(v) for k, v in data["bans"].items()}
    data["wins"]  = {str(k): int(v) for k, v in data["wins"].items()}
    return data

async def _meta_save(data: dict) -> None:
    await r_set(META_KEY, data, ttl=180*24*3600)

async def _meta_update_for_game(picks_a: List[str], picks_b: List[str],
                                bans_a: List[str],  bans_b: List[str],
                                winner_side: str) -> None:
    data = await _meta_load()
    P, B, W = data["picks"], data["bans"], data["wins"]

    for cid in picks_a + picks_b:
        P[cid] = P.get(cid, 0) + 1
    for cid in bans_a + bans_b:
        B[cid] = B.get(cid, 0) + 1

    winners = picks_a if winner_side == "A" else picks_b
    for cid in winners:
        W[cid] = W.get(cid, 0) + 1

    await _meta_save(data)

def _compute_meta_tables(data: dict, top: int = 10, min_picks_for_wr: int = 10):
    P, B, W = data["picks"], data["bans"], data["wins"]
    presence: List[Tuple[str, int]] = [(cid, P.get(cid, 0) + B.get(cid, 0)) for cid in set(P) | set(B)]
    presence.sort(key=lambda x: x[1], reverse=True)
    top_picks = sorted(P.items(), key=lambda x: x[1], reverse=True)
    top_bans  = sorted(B.items(), key=lambda x: x[1], reverse=True)

    wr_entries: List[Tuple[str, float, int]] = []
    for cid, pcount in P.items():
        if pcount >= min_picks_for_wr:
            wr = (W.get(cid, 0) / pcount) * 100.0
            wr_entries.append((cid, wr, pcount))
    wr_entries.sort(key=lambda x: x[1], reverse=True)

    return {
        "presence": presence[:top],
        "picks": top_picks[:top],
        "bans": top_bans[:top],
        "winrates": wr_entries[:top],
    }


# ──────────────────────── Vues d’interaction ────────────────────────────────
class ResultView(discord.ui.View):
    """Boutons Win – réservés aux capitaines. Le message est supprimé après report."""
    def __init__(self, cog: "DraftCog", series: SeriesState):
        super().__init__(timeout=None)
        self.cog, self.series = cog, series

    async def _guard(self, inter: Interaction) -> bool:
        if inter.user.id not in (self.series.captain_a, self.series.captain_b):
            await inter.response.send_message("⛔ Capitaines only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Win (capitaine A)", style=discord.ButtonStyle.success)
    async def win_a(self, inter: Interaction, _):
        if not await self._guard(inter): return
        await inter.response.defer()
        await self.cog._report(inter, "A")
        try:
            await inter.message.delete()
        except Exception:
            pass

    @discord.ui.button(label="✅ Win (capitaine B)", style=discord.ButtonStyle.success)
    async def win_b(self, inter: Interaction, _):
        if not await self._guard(inter): return
        await inter.response.defer()
        await self.cog._report(inter, "B")
        try:
            await inter.message.delete()
        except Exception:
            pass


class SideChoiceView(discord.ui.View):
    """Choix des sides par le **capitaine perdant uniquement** avant la prochaine draft."""
    def __init__(self, loser_id: int):
        super().__init__(timeout=60)
        self.loser_id = loser_id
        self.swap_chosen: Optional[bool] = None
        self._done = asyncio.Event()

    async def _guard(self, inter: Interaction) -> bool:
        if inter.user.id != self.loser_id:
            await inter.response.send_message("⛔ Capitaines only (capitaine perdant).", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🔄 Inverser les sides", style=discord.ButtonStyle.primary)
    async def swap(self, inter: Interaction, _):
        if not await self._guard(inter): return
        self.swap_chosen = True
        for i in self.children: i.disabled = True
        await inter.response.edit_message(content="🔄 Sides **inversés** pour la prochaine game.", view=self)
        self._done.set()

    @discord.ui.button(label="➡️ Garder les sides", style=discord.ButtonStyle.secondary)
    async def keep(self, inter: Interaction, _):
        if not await self._guard(inter): return
        self.swap_chosen = False
        for i in self.children: i.disabled = True
        await inter.response.edit_message(content="➡️ Sides **inchangés**.", view=self)
        self._done.set()

    async def on_timeout(self):
        if self.swap_chosen is None:
            self.swap_chosen = False
            self._done.set()


class CaptainsReadyView(discord.ui.View):
    """Ready-check des deux capitaines avant de lancer la prochaine draft."""
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
            return await inter.response.send_message("⛔ Capitaines only (capitaine A).", ephemeral=True)
        name = inter.user.display_name
        if self.cap_a in self.ready: self.ready.remove(self.cap_a)
        else: self.ready.add(self.cap_a)
        btn.label = self._label(self.cap_a, name)
        btn.style = discord.ButtonStyle.success if self.cap_a in self.ready else discord.ButtonStyle.secondary
        await inter.response.edit_message(view=self)
        if self.cap_a in self.ready and self.cap_b in self.ready:
            self._done.set()

    @discord.ui.button(label="⏳ Capitaine B pas prêt", style=discord.ButtonStyle.secondary, row=0)
    async def ready_b(self, inter: Interaction, btn: discord.ui.Button):
        if inter.user.id != self.cap_b:
            return await inter.response.send_message("⛔ Capitaines only (capitaine B).", ephemeral=True)
        name = inter.user.display_name
        if self.cap_b in self.ready: self.ready.remove(self.cap_b)
        else: self.ready.add(self.cap_b)
        btn.label = self._label(self.cap_b, name)
        btn.style = discord.ButtonStyle.success if self.cap_b in self.ready else discord.ButtonStyle.secondary
        await inter.response.edit_message(view=self)
        if self.cap_a in self.ready and self.cap_b in self.ready:
            self._done.set()

    async def on_timeout(self):
        self._done.set()


class ContinueView(discord.ui.View):
    """Propose de prolonger une série (Bo1→Bo3 ou Bo3→Bo5)."""
    def __init__(self, captains: tuple[int, int], next_bo: int):
        super().__init__(timeout=60)
        self.captains = captains
        self.next_bo = next_bo
        self.go_next: Optional[bool] = None
        self._done = asyncio.Event()

    @discord.ui.button(label="✅ Continuer", style=discord.ButtonStyle.success)
    async def go(self, inter: Interaction, _):
        if inter.user.id not in self.captains:
            return await inter.response.send_message("⛔ Capitaines only.", ephemeral=True)
        self.go_next = True
        for i in self.children: i.disabled = True
        await inter.response.edit_message(content=f"✅ Passage en **Bo{self.next_bo}** confirmé !", view=self)
        self._done.set()

    @discord.ui.button(label="❌ Terminer", style=discord.ButtonStyle.danger)
    async def stop(self, inter: Interaction, _):
        if inter.user.id not in self.captains:
            return await inter.response.send_message("⛔ Capitaines only.", ephemeral=True)
        self.go_next = False
        for i in self.children: i.disabled = True
        await inter.response.edit_message(content="❌ Série clôturée.", view=self)
        self._done.set()

    async def on_timeout(self):
        if self.go_next is None:
            self.go_next = False
            self._done.set()


# ──────────────────────────── Cog principal ───────────────────────
class DraftCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.series_by_thread: dict[int, SeriesState] = {}

    # ─── start_draft ───────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_start_draft(self, team_a, team_b, channel: discord.TextChannel,
                             bo: int, captain_a: int, captain_b: int):
        await load_champs()
        series = SeriesState.new(bo, team_a, team_b, captain_a, captain_b)

        thread = await channel.create_thread(
            name=f"draft-{series.id}",
            type=discord.ChannelType.public_thread,
            auto_archive_duration=1440,  # 24h
        )
        logger.info("Thread draft créé : #%s (Bo %s)", thread.name, bo)
        self.series_by_thread[thread.id] = series

        status = await thread.send(embed=self._build_embed(series, 60, 0, highlight=True))
        series.status_msg_id = status.id
        await self._draft_loop(thread, series, status)

    # ─── boucle bans/picks ─────────────────────────────────────────
    async def _draft_loop(self, thread: discord.Thread, series: SeriesState, status_msg: discord.Message):
        TURN_TIME = 2 if len([uid for uid in series.team_a + series.team_b if uid > 0]) == 1 else 60
        ptr, taken = 0, set[str]()
        logger.info("Début draft %s (turn=%ds)", series.id, TURN_TIME)

        while ptr < len(DRAFT_ORDER):
            side = DRAFT_ORDER[ptr]
            captain = series.captain_a if side == "A" else series.captain_b
            is_ban, secs, champ_id = ptr in BAN_INDEXES, TURN_TIME, None

            # ping capitaine au début du tour
            try:
                await thread.send(f"👉 <@{captain}> à toi ({'BAN' if is_ban else 'PICK'})", delete_after=3)
            except discord.HTTPException:
                pass

            def check(m: discord.Message) -> bool:
                return m.channel.id == thread.id and m.author.id == captain

            while secs > 0:
                try:
                    msg = await asyncio.wait_for(self.bot.wait_for("message", check=check), timeout=1)
                    raw = msg.content.strip()
                    # accepter "/ban aatrox", "/pick aatrox", "ban aatrox", "pick aatrox" ou juste "aatrox"
                    name = raw
                    if raw.lower().startswith(("/ban", "/pick", "ban ", "pick ")):
                        parts = raw.split(maxsplit=1)
                        if len(parts) == 2:
                            name = parts[1]

                    cand = canonicalize(name)
                    try:
                        await msg.delete()
                    except discord.Forbidden:
                        pass

                    if not cand:
                        sugg = difflib.get_close_matches(name.lower().replace(" ", ""), ALIASES.keys(), n=3, cutoff=0.6)
                        tip = f" Essaye: {', '.join(ALIASES[s] for s in sugg)}" if sugg else ""
                        await thread.send(f"❓ Champion inconnu: **{name}**.{tip}", delete_after=4)
                        continue
                    if cand in taken or cand in series.fearless_pool:
                        await thread.send("⚠️ Champion déjà pris / interdit.", delete_after=3)
                        continue
                    champ_id = cand
                    break
                except asyncio.TimeoutError:
                    secs -= 1
                    # maj plus “vivante” : toutes les 5s, puis chaque seconde sous 10s
                    if secs % 5 == 0 or secs <= 10:
                        try:
                            await status_msg.edit(embed=self._build_embed(series, secs, ptr, highlight=True))
                        except discord.HTTPException:
                            pass

            if champ_id is None:
                champ_id = random_champ(series, taken)
                await thread.send(f"⏰ Temps écoulé ! **{champ_id}** sélectionné aléatoirement.")

            game = series.current_game
            target = (game.bans_a if side == "A" else game.bans_b) if is_ban else (game.picks_a if side == "A" else game.picks_b)
            target.append(champ_id)
            if not is_ban:
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

    # ─── Embeds helpers (pseudos capitaines + couleur dynamique) ─────────────
    @staticmethod
    def _turn_color(side: Optional[str]) -> discord.Colour:
        if side == "A":
            return discord.Colour.from_rgb(30, 136, 229)   # bleu vif
        if side == "B":
            return discord.Colour.from_rgb(229, 57, 53)    # rouge vif
        return discord.Colour.blurple()                    # neutre

    @staticmethod
    def _build_embed(series: SeriesState, secs: int, ptr: int, *, highlight=False) -> discord.Embed:
        g = series.current_game
        bar = time_bar(secs)
        guild = getattr(series, "guild", None)  # si tu stockes le guild; sinon passe-le en param

        capA_id, capB_id = series.captain_a, series.captain_b
        capA_mention, capB_mention = f"<@{capA_id}>", f"<@{capB_id}>"

        # (optionnel) noms lisibles pour les noms de champs
        capA_name = getattr(getattr(guild, "get_member", lambda _: _)(capA_id), "display_name", f"Cap A")
        capB_name = getattr(getattr(guild, "get_member", lambda _: _)(capB_id), "display_name", f"Cap B")

        if ptr < len(DRAFT_ORDER):
            side, phase = DRAFT_ORDER[ptr], ("BAN" if ptr in BAN_INDEXES else "PICK")
            who = capA_mention if side == "A" else capB_mention
            header = f"{bar} **{secs:>2}s**  ·  Tour **{who} · {phase}**" if highlight else f"{bar} {secs:>2}s · {who} · {phase}"
            colour = DraftCog._turn_color(side)
        else:
            header, colour = "Draft terminée", DraftCog._turn_color(None)

        join = lambda L: ", ".join(L) if L else "—"
        embed = discord.Embed(title=f"🛡️ Draft · Game {len(series.games)}",
                              colour=colour, description=header)

        # 🟥 NOMS DE CHAMPS SANS MENTION ; MENTION EN 1re LIGNE DE LA VALUE
        embed.add_field(name=f"🚫  BANS — {capA_name}", value=f"{capA_mention}\n{join(g.bans_a)}", inline=True)
        embed.add_field(name=f"🚫  BANS — {capB_name}", value=f"{capB_mention}\n{join(g.bans_b)}", inline=True)
        embed.add_field(name=f"✅  PICKS — {capA_name}", value=f"{capA_mention}\n{join(g.picks_a)}", inline=True)
        embed.add_field(name=f"✅  PICKS — {capB_name}", value=f"{capB_mention}\n{join(g.picks_b)}", inline=True)

        embed.set_footer(text="Capitaines only • messages hors capitaines supprimés")
        return embed

    @staticmethod
    def _build_recap_embed(series: SeriesState) -> discord.Embed:
        g = series.current_game
        capA, capB = f"<@{series.captain_a}>", f"<@{series.captain_b}>"
        join = lambda L: ", ".join(L) if L else "—"
        embed = discord.Embed(
            title=f"📊  Récap — Game {len(series.games)}",
            colour=discord.Colour.dark_gold(),
            description=(f"**Score : {series.score_a}-{series.score_b}**\n"
                         "Sélectionnez le vainqueur (Capitaines only)."),
        )
        embed.add_field(name=f"🚫  BANS  {capA}", value=join(g.bans_a), inline=True)
        embed.add_field(name=f"🚫  BANS  {capB}", value=join(g.bans_b), inline=True)
        embed.add_field(name=f"✅  PICKS  {capA}", value=join(g.picks_a), inline=True)
        embed.add_field(name=f"✅  PICKS  {capB}", value=join(g.picks_b), inline=True)
        return embed

    @staticmethod
    def _build_chi_embed(series: SeriesState) -> discord.Embed:
        g = series.current_game
        p_blue, p_red = chi_predict(g.picks_a, g.picks_b)
        embed = discord.Embed(
            title="⚖️  Balance du chi",
            colour=discord.Colour.from_rgb(0, 176, 255),
            description=f"🟦 **{p_blue:4.1f} %** vs **{p_red:4.1f} %** 🟥"
        )
        embed.add_field(name="", value=f"```\n{chi_bar(p_blue)}\n```", inline=False)
        return embed

    # ─── anti-spam hors capitaines ────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.author.bot:
            return
        series = self.series_by_thread.get(getattr(msg.channel, "id", 0))
        if series and msg.author.id not in (series.captain_a, series.captain_b):
            try:
                await msg.delete()
            except discord.Forbidden:
                pass

    # ─── Report helper ────────────────────────────────────────────
    async def _report(self, inter: Interaction, side: str):
        if not isinstance(inter.channel, discord.Thread) or not inter.channel.name.startswith("draft-"):
            return await inter.response.send_message("❌ À utiliser dans le thread draft.", ephemeral=True)
        series = self.series_by_thread.get(inter.channel.id)
        if not series:
            return await inter.response.send_message("❌ Série inconnue.", ephemeral=True)
        if series.current_game.winner:
            return await inter.response.send_message("⚠️ Partie déjà reportée.", ephemeral=True)

        # enregistre le résultat
        series.current_game.winner = side
        series.score_a += side == "A"
        series.score_b += side == "B"
        logger.info("Victoire Team %s (score %d-%d)", side, series.score_a, series.score_b)

        # ─── UPDATE MÉTA : picks/bans/wins
        g = series.current_game
        try:
            await _meta_update_for_game(g.picks_a, g.picks_b, g.bans_a, g.bans_b, side)
        except Exception as e:
            logger.warning(f"[meta] update failed: {e}")

        # ─── série terminée ? ──────────────────────────────────
        if series.finished():
            # Bo1 → proposer Bo3
            if series.bo == 1:
                next_bo = 3
                cont_view = ContinueView((series.captain_a, series.captain_b), next_bo=next_bo)
                msg = await inter.channel.send(
                    f"🏆 Bo1 terminé (**{series.score_a}-{series.score_b}**).\n"
                    f"Voulez-vous poursuivre en **Bo{next_bo}** ?",
                    view=cont_view,
                )
                await cont_view._done.wait()
                await msg.delete()

                if cont_view.go_next:
                    series.bo = next_bo
                    # choix side par le capitaine perdant de la dernière game
                    loser = series.captain_b if side == "A" else series.captain_a
                    scv = SideChoiceView(loser_id=loser)
                    msg_sides = await inter.channel.send(f"🧭 <@{loser}> choisit les **sides** :", view=scv)
                    await scv._done.wait()
                    await msg_sides.delete()
                    if scv.swap_chosen:
                        series.team_a, series.team_b = series.team_b, series.team_a
                        series.captain_a, series.captain_b = series.captain_b, series.captain_a
                        series.score_a, series.score_b = series.score_b, series.score_a

                    rv = CaptainsReadyView(series.captain_a, series.captain_b)
                    msg_ready = await inter.channel.send("⏳ Ready check des capitaines…", view=rv)
                    await rv._done.wait()
                    await msg_ready.delete()

                    series.start_new_game()
                    status = await inter.channel.send(embed=self._build_embed(series, 60, 0, highlight=True))
                    series.status_msg_id = status.id
                    if series.fearless_pool:
                        await inter.channel.send(embed=discord.Embed(
                            title="🔥 Fearless — champions désormais bannis",
                            description=", ".join(series.fearless_pool),
                            colour=discord.Colour.red()))
                    return await self._draft_loop(inter.channel, series, status)

            # Bo3 terminé → proposer Bo5
            if series.bo == 3:
                next_bo = 5
                cont_view = ContinueView((series.captain_a, series.captain_b), next_bo=next_bo)
                msg = await inter.channel.send(
                    f"🏆 Bo3 terminé (**{series.score_a}-{series.score_b}**).\n"
                    f"Voulez-vous poursuivre en **Bo{next_bo}** ?",
                    view=cont_view,
                )
                await cont_view._done.wait()
                await msg.delete()

                if cont_view.go_next:
                    series.bo = next_bo
                    loser = series.captain_b if side == "A" else series.captain_a
                    scv = SideChoiceView(loser_id=loser)
                    msg_sides = await inter.channel.send(f"🧭 <@{loser}> choisit les **sides** :", view=scv)
                    await scv._done.wait()
                    await msg_sides.delete()
                    if scv.swap_chosen:
                        series.team_a, series.team_b = series.team_b, series.team_a
                        series.captain_a, series.captain_b = series.captain_b, series.captain_a
                        series.score_a, series.score_b = series.score_b, series.score_a

                    rv = CaptainsReadyView(series.captain_a, series.captain_b)
                    msg_ready = await inter.channel.send("⏳ Ready check des capitaines…", view=rv)
                    await rv._done.wait()
                    await msg_ready.delete()

                    series.start_new_game()
                    status = await inter.channel.send(embed=self._build_embed(series, 60, 0, highlight=True))
                    series.status_msg_id = status.id
                    if series.fearless_pool:
                        await inter.channel.send(embed=discord.Embed(
                            title="🔥 Fearless — champions désormais bannis",
                            description=", ".join(series.fearless_pool),
                            colour=discord.Colour.red()))
                    return await self._draft_loop(inter.channel, series, status)

            # victoire finale : embed doré
            winners = series.team_a if side == "A" else series.team_b
            mentions = "\n".join(f"<@{uid}>" for uid in winners)
            embed_end = discord.Embed(
                title=f"🏆  Victoire Team {'A' if side=='A' else 'B'}  —  {series.score_a}-{series.score_b}",
                colour=discord.Colour.gold(),
                description=mentions,
            ).set_footer(text="GG à tous !")
            await inter.channel.send(embed=embed_end)
            self.series_by_thread.pop(inter.channel.id, None)
            return

        # ─── série continue : choix des sides par le capitaine perdant ───
        loser = series.captain_b if side == "A" else series.captain_a
        scv = SideChoiceView(loser_id=loser)
        msg_sides = await inter.channel.send(f"🧭 <@{loser}> choisit les **sides** :", view=scv)
        await scv._done.wait()
        await msg_sides.delete()
        if scv.swap_chosen:
            series.team_a, series.team_b = series.team_b, series.team_a
            series.captain_a, series.captain_b = series.captain_b, series.captain_a
            series.score_a, series.score_b = series.score_b, series.score_a

        # Ready-check capitaines
        rv = CaptainsReadyView(series.captain_a, series.captain_b)
        msg_ready = await inter.channel.send("⏳ Ready check des capitaines…", view=rv)
        await rv._done.wait()
        await msg_ready.delete()

        # nouvelle game
        series.start_new_game()
        status = await inter.channel.send(embed=self._build_embed(series, 60, 0, highlight=True))
        series.status_msg_id = status.id
        if series.fearless_pool:
            await inter.channel.send(embed=discord.Embed(
                title="🔥 Fearless — champions désormais bannis",
                description=", ".join(series.fearless_pool),
                colour=discord.Colour.red()))
        await self._draft_loop(inter.channel, series, status)

    # ─── /meta : aperçu méta dans Discord ─────────────────────────
    @app_commands.command(name="meta", description="Stats méta customs: top picks/bans/presence/winrate")
    @app_commands.describe(top="Taille du top (1-25)", min_picks="Nombre minimum de picks pour le WR")
    async def meta(self, inter: Interaction, top: int = 10, min_picks: int = 10):
        await inter.response.defer()
        data = await _meta_load()
        tables = _compute_meta_tables(
            data,
            top=max(1, min(top, 25)),
            min_picks_for_wr=max(1, min_picks)
        )

        def fmt_presence():
            if not tables["presence"]: return "—"
            return "\n".join(f"**{cid}** — {cnt} (picks {data['picks'].get(cid,0)} / bans {data['bans'].get(cid,0)})"
                             for cid, cnt in tables["presence"])

        def fmt_picks():
            if not tables["picks"]: return "—"
            return "\n".join(f"**{cid}** — {cnt}" for cid, cnt in tables["picks"])

        def fmt_bans():
            if not tables["bans"]: return "—"
            return "\n".join(f"**{cid}** — {cnt}" for cid, cnt in tables["bans"])

        def fmt_wr():
            if not tables["winrates"]: return "—"
            return "\n".join(f"**{cid}** — {wr:.1f}%  ({pc} picks)" for cid, wr, pc in tables["winrates"])

        embed = discord.Embed(
            title="📈 Méta — customs",
            colour=discord.Colour.dark_teal(),
            description="Agrégé sur toutes les games reportées (bouton ✅)."
        )
        embed.add_field(name="👀 Presence (picks + bans)", value=fmt_presence(), inline=False)
        embed.add_field(name="✅ Top Picks", value=fmt_picks(), inline=True)
        embed.add_field(name="🚫 Top Bans", value=fmt_bans(), inline=True)
        embed.add_field(name="🏆 Top Winrates", value=fmt_wr(), inline=False)
        await inter.followup.send(embed=embed)


# ─────────────────────────── setup ────────────────────────────────
async def setup(bot: commands.Bot):
    await bot.add_cog(DraftCog(bot))
