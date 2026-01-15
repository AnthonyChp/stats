# oogway/cogs/draft.py
# ============================================================================
# Draft compétitive – fil public, nom du champion seul, recap + boutons Win
# + Stats méta (pick/ban/win) persistées dans Redis + commande /meta
# + "Capitaines only" partout (Win, side choice, ready-check)
# + Couleur d'embed dynamique (A=bleu, B=rouge) et affichage pseudos capitaines
# OPTIMISÉ: Cache embeds, boucle async optimisée, moins d'allocations mémoire
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
from oogway.cogs.profile import r_get, r_set   # Redis helpers

BAR_FULL, BAR_EMPTY, BAR_BLOCKS = "▰", "▱", 12
logger = logging.getLogger(__name__)

# ───────────────────────────── Data-Dragon ──────────────────────────────
DD_VERSION_CACHE: Optional[str] = None
DD_VERSION_CACHE_TIME: float = 0.0
DD_CACHE_TTL: int = 86400  # 24h

CHAMPS_CACHE: Dict[str, dict] = {}
CHAMPS_CACHE_TIME: float = 0.0
ALIASES: Dict[str, str] = {}

# Cache pour le formatage des listes de champions
_CHAMP_LIST_CACHE: Dict[tuple, str] = {}
_CHAMP_LIST_CACHE_MAX = 200


async def ddragon_version(force_refresh: bool = False) -> str:
    """Récupère la version Data Dragon avec cache."""
    global DD_VERSION_CACHE, DD_VERSION_CACHE_TIME
    
    now = time.time()
    if not force_refresh and DD_VERSION_CACHE and (now - DD_VERSION_CACHE_TIME) < DD_CACHE_TTL:
        return DD_VERSION_CACHE

    async with aiohttp.ClientSession() as s:
        async with s.get("https://ddragon.leagueoflegends.com/api/versions.json") as r:
            DD_VERSION_CACHE = (await r.json())[0]
            DD_VERSION_CACHE_TIME = now
            logger.info("Version Data-Dragon : %s", DD_VERSION_CACHE)
            return DD_VERSION_CACHE


async def load_champs(force_refresh: bool = False) -> None:
    """Charge les champions depuis Data Dragon avec cache."""
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

    # Générer les alias
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
        slug = cid.lower()
        nospace = slug.replace(" ", "")
        ALIASES[slug] = cid
        ALIASES[nospace] = cid
        
        abbr3 = nospace[:3]
        if abbr3 not in ALIASES and abbr3 not in taken:
            ALIASES[abbr3] = cid
            taken.add(abbr3)

    ALIASES.update(manual)
    logger.info("Alias générés : %d (dont %d manuels)", len(ALIASES), len(manual))


def canonicalize(name: str) -> Optional[str]:
    """Convertit un nom de champion en ID canonique."""
    key = name.lower().replace(" ", "")
    if key in ALIASES:
        return ALIASES[key]
    
    # Fuzzy matching
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
    """Sélectionne un champion aléatoire parmi ceux disponibles."""
    pool = [c for c in CHAMPS_CACHE if c not in taken and c not in series.fearless_pool]
    pick = random.choice(pool)
    logger.info("Pick aléatoire : %s", pick)
    return pick


def time_bar(seconds_left: int) -> str:
    """Génère une barre de temps."""
    filled = round(seconds_left / 60 * BAR_BLOCKS)
    return BAR_FULL * filled + BAR_EMPTY * (BAR_BLOCKS - filled)


# ─────────────────────────── Meta helpers (Redis) ─────────────────────────
META_KEY = "meta:champions"  # {"picks": {cid:int}, "bans": {cid:int}, "wins": {cid:int}}

async def _meta_load() -> dict:
    """Charge les stats méta depuis Redis."""
    data = await r_get(META_KEY) or {}
    data.setdefault("picks", {})
    data.setdefault("bans", {})
    data.setdefault("wins", {})
    data["picks"] = {str(k): int(v) for k, v in data["picks"].items()}
    data["bans"]  = {str(k): int(v) for k, v in data["bans"].items()}
    data["wins"]  = {str(k): int(v) for k, v in data["wins"].items()}
    return data

async def _meta_save(data: dict) -> None:
    """Sauvegarde les stats méta dans Redis."""
    await r_set(META_KEY, data, ttl=180*24*3600)

async def _meta_update_for_game(picks_a: List[str], picks_b: List[str],
                                bans_a: List[str],  bans_b: List[str],
                                winner_side: str) -> None:
    """Met à jour les statistiques méta après une game."""
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
    """Calcule les tableaux de méta (top picks/bans/presence/winrates)."""
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


# ──────────────────────── Vues d'interaction ────────────────────────────────
class ResultView(discord.ui.View):
    """Boutons Win – réservés aux capitaines. Le message est supprimé après report."""
    def __init__(self, cog: "DraftCog", series: SeriesState):
        super().__init__(timeout=None)
        self.cog, self.series = cog, series
        self._lock = asyncio.Lock()

    async def _guard(self, inter: Interaction) -> bool:
        """Vérifie si l'utilisateur est un capitaine."""
        if inter.user.id not in (self.series.captain_a, self.series.captain_b):
            await inter.response.send_message("⛔ Capitaines only.", ephemeral=True)
            return False
        if self._lock.locked():
            await inter.response.send_message("⏳ Vote déjà en cours...", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Team A gagne", emoji="🔵", style=discord.ButtonStyle.success)
    async def win_a(self, inter: Interaction, _):
        if not await self._guard(inter): 
            return
        async with self._lock:
            await inter.response.defer()
            await self.cog._report(inter, "A")
            try:
                await inter.message.delete()
            except Exception as e:
                logger.debug(f"Could not delete message: {e}")

    @discord.ui.button(label="✅ Team B gagne", emoji="🔴", style=discord.ButtonStyle.danger)
    async def win_b(self, inter: Interaction, _):
        if not await self._guard(inter): 
            return
        async with self._lock:
            await inter.response.defer()
            await self.cog._report(inter, "B")
            try:
                await inter.message.delete()
            except Exception as e:
                logger.debug(f"Could not delete message: {e}")


class SideChoiceView(discord.ui.View):
    """Choix des sides par le **capitaine perdant uniquement** avant la prochaine draft."""
    def __init__(self, loser_id: int, captain_a_id: int, captain_b_id: int):
        super().__init__(timeout=60)
        self.loser_id = loser_id
        self.captain_a_id = captain_a_id
        self.captain_b_id = captain_b_id
        self.swap_chosen: Optional[bool] = None
        self._done = asyncio.Event()

    async def _guard(self, inter: Interaction) -> bool:
        if inter.user.id != self.loser_id:
            await inter.response.send_message("⛔ Capitaines only (capitaine perdant).", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🔄 Inverser les sides", style=discord.ButtonStyle.primary)
    async def swap(self, inter: Interaction, _):
        if not await self._guard(inter): 
            return
        self.swap_chosen = True
        for i in self.children: 
            i.disabled = True
        msg = (
            f"🔄 **Sides inversés !**\n\n"
            f"🔵 <@{self.captain_b_id}> → **Team A** (Blue side)\n"
            f"🔴 <@{self.captain_a_id}> → **Team B** (Red side)"
        )
        await inter.response.edit_message(content=msg, view=self)
        self._done.set()

    @discord.ui.button(label="➡️ Garder les sides", style=discord.ButtonStyle.secondary)
    async def keep(self, inter: Interaction, _):
        if not await self._guard(inter): 
            return
        self.swap_chosen = False
        for i in self.children: 
            i.disabled = True
        msg = (
            f"✅ **Sides inchangés !**\n\n"
            f"🔵 <@{self.captain_a_id}> → **Team A** (Blue side)\n"
            f"🔴 <@{self.captain_b_id}> → **Team B** (Red side)"
        )
        await inter.response.edit_message(content=msg, view=self)
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
        if self.cap_a in self.ready: 
            self.ready.remove(self.cap_a)
        else: 
            self.ready.add(self.cap_a)
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
        if self.cap_b in self.ready: 
            self.ready.remove(self.cap_b)
        else: 
            self.ready.add(self.cap_b)
        btn.label = self._label(self.cap_b, name)
        btn.style = discord.ButtonStyle.success if self.cap_b in self.ready else discord.ButtonStyle.secondary
        await inter.response.edit_message(view=self)
        if self.cap_a in self.ready and self.cap_b in self.ready:
            self._done.set()

    async def on_timeout(self):
        self._done.set()


class ContinueView(discord.ui.View):
    """Propose de prolonger une série (Bo1→Bo3 ou Bo3→Bo5)."""
    def __init__(self, captains: tuple[int, int], next_bo: int, is_tied: bool = False, current_score: str = ""):
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
        for i in self.children: 
            i.disabled = True
        if self.is_tied:
            msg = f"✅ **Belle confirmée !** Passage en **Bo{self.next_bo}** pour départager."
        else:
            msg = f"✅ Passage en **Bo{self.next_bo}** confirmé !"
        await inter.response.edit_message(content=msg, view=self)
        self._done.set()

    @discord.ui.button(label="❌ Terminer", style=discord.ButtonStyle.danger)
    async def stop(self, inter: Interaction, _):
        if inter.user.id not in self.captains:
            return await inter.response.send_message("⛔ Capitaines only.", ephemeral=True)
        self.go_next = False
        for i in self.children: 
            i.disabled = True
        if self.is_tied:
            msg = f"🤝 Série clôturée sur un **match nul {self.current_score}**."
        else:
            msg = "❌ Série clôturée."
        await inter.response.edit_message(content=msg, view=self)
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
        # Précharger les champions au démarrage
        self.bot.loop.create_task(self._preload())
    
    async def _preload(self):
        """Précharge les données au démarrage."""
        try:
            await load_champs()
            logger.info("Champions préchargés avec succès")
        except Exception as e:
            logger.error(f"Erreur préchargement: {e}")

    # ─── Helpers pour formatter les listes ─────────────────────────
    @staticmethod
    def _format_champ_list(champs: list[str]) -> str:
        """Cache le formatage des listes de champions."""
        if not champs:
            return "—"
        
        key = tuple(champs)
        if key not in _CHAMP_LIST_CACHE:
            _CHAMP_LIST_CACHE[key] = ", ".join(f"`{c}`" for c in champs)
            
            # Limiter la taille du cache
            if len(_CHAMP_LIST_CACHE) > _CHAMP_LIST_CACHE_MAX:
                # Supprimer les 50 plus anciennes entrées
                for old_key in list(_CHAMP_LIST_CACHE.keys())[:50]:
                    _CHAMP_LIST_CACHE.pop(old_key, None)
        
        return _CHAMP_LIST_CACHE[key]

    # ─── start_draft ───────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_start_draft(self, team_a, team_b, channel: discord.TextChannel,
                             bo: int, captain_a: int, captain_b: int):
        await load_champs()
        series = SeriesState.new(bo, team_a, team_b, captain_a, captain_b)
        
        # Cache guild et noms des capitaines
        series.guild = channel.guild
        member_a = channel.guild.get_member(captain_a)
        member_b = channel.guild.get_member(captain_b)
        series.captain_a_name = member_a.display_name if member_a else "Cap A"
        series.captain_b_name = member_b.display_name if member_b else "Cap B"

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

    # ─── boucle bans/picks OPTIMISÉE ───────────────────────────────
    async def _draft_loop(self, thread: discord.Thread, series: SeriesState, status_msg: discord.Message):
        """Boucle de draft optimisée avec moins de context switches."""
        TURN_TIME = 2 if len([uid for uid in series.team_a + series.team_b if uid > 0]) == 1 else 60
        ptr = 0
        taken: set[str] = set()
        
        logger.info("Début draft %s (turn=%ds)", series.id, TURN_TIME)

        while ptr < len(DRAFT_ORDER):
            side = DRAFT_ORDER[ptr]
            captain = series.captain_a if side == "A" else series.captain_b
            is_ban = ptr in BAN_INDEXES
            
            # Ping capitaine au début du tour
            try:
                await thread.send(
                    f"👉 <@{captain}> à toi ({'BAN' if is_ban else 'PICK'})", 
                    delete_after=3
                )
            except discord.HTTPException:
                pass

            # Fonction check optimisée
            def make_check(captain_id: int):
                def check(m: discord.Message) -> bool:
                    return m.channel.id == thread.id and m.author.id == captain_id
                return check
            
            check = make_check(captain)
            deadline = asyncio.get_event_loop().time() + TURN_TIME
            champ_id = None
            last_update = TURN_TIME
            
            # Créer la tâche d'attente de message
            msg_task = asyncio.create_task(self.bot.wait_for("message", check=check))
            
            try:
                while True:
                    now = asyncio.get_event_loop().time()
                    remaining = max(0, int(deadline - now))
                    
                    if remaining == 0:
                        break
                    
                    # Update embed seulement quand nécessaire
                    if remaining != last_update and (remaining % 5 == 0 or remaining <= 10):
                        last_update = remaining
                        try:
                            await status_msg.edit(
                                embed=self._build_embed(series, remaining, ptr, highlight=True)
                            )
                        except discord.HTTPException:
                            pass
                    
                    # Attendre message avec timeout court
                    try:
                        msg = await asyncio.wait_for(asyncio.shield(msg_task), timeout=0.5)
                        
                        # Parser le message
                        raw = msg.content.strip()
                        name = raw
                        
                        # Accepter différents formats
                        lower = raw.lower()
                        if lower.startswith(("/ban ", "/pick ", "ban ", "pick ")):
                            parts = raw.split(maxsplit=1)
                            if len(parts) == 2:
                                name = parts[1]
                        
                        # Supprimer le message immédiatement pour feedback instantané
                        try:
                            await msg.delete()
                        except (discord.Forbidden, discord.HTTPException):
                            pass
                        
                        # Canonicaliser
                        cand = canonicalize(name)
                        
                        # Valider
                        if not cand:
                            sugg = difflib.get_close_matches(
                                name.lower().replace(" ", ""), 
                                ALIASES.keys(), 
                                n=3, 
                                cutoff=0.6
                            )
                            tip = f" Essaye: {', '.join(ALIASES[s] for s in sugg)}" if sugg else ""
                            await thread.send(f"❓ Champion inconnu: **{name}**.{tip}", delete_after=4)
                            msg_task = asyncio.create_task(self.bot.wait_for("message", check=check))
                            continue
                        
                        if cand in taken or cand in series.fearless_pool:
                            await thread.send("⚠️ Champion déjà pris / interdit.", delete_after=3)
                            msg_task = asyncio.create_task(self.bot.wait_for("message", check=check))
                            continue
                        
                        # Champion valide !
                        champ_id = cand
                        break
                        
                    except asyncio.TimeoutError:
                        continue
                        
            finally:
                # Cleanup
                if not msg_task.done():
                    msg_task.cancel()
                    try:
                        await msg_task
                    except asyncio.CancelledError:
                        pass

            # Pick aléatoire si timeout
            if champ_id is None:
                champ_id = random_champ(series, taken)
                await thread.send(f"⏰ Temps écoulé ! **{champ_id}** sélectionné aléatoirement.")

            # Enregistrer le pick/ban
            game = series.current_game
            if is_ban:
                (game.bans_a if side == "A" else game.bans_b).append(champ_id)
            else:
                (game.picks_a if side == "A" else game.picks_b).append(champ_id)
                series.fearless_pool.add(champ_id)
            
            taken.add(champ_id)
            ptr += 1
            
            # Update final
            try:
                await status_msg.edit(embed=self._build_embed(series, TURN_TIME, ptr, highlight=True))
            except discord.HTTPException:
                pass

        # Draft terminée
        logger.info("Draft terminée – série %s", series.id)
        await thread.send(
            embeds=[self._build_recap_embed(series), self._build_chi_embed(series)],
            view=ResultView(self, series)
        )

    # ─── Embeds helpers (avec cache des noms) ─────────────────────
    @staticmethod
    def _turn_color(side: Optional[str]) -> discord.Colour:
        """Retourne la couleur d'embed selon le side."""
        if side == "A":
            return discord.Colour.from_rgb(30, 136, 229)   # bleu vif
        if side == "B":
            return discord.Colour.from_rgb(229, 57, 53)    # rouge vif
        return discord.Colour.blurple()                    # neutre

    def _build_embed(self, series: SeriesState, secs: int, ptr: int, *, highlight=False) -> discord.Embed:
        """Construit l'embed de draft avec cache des noms."""
        g = series.current_game
        bar = time_bar(secs)

        # Utiliser les noms cachés
        capA_mention = f"<@{series.captain_a}>"
        capB_mention = f"<@{series.captain_b}>"
        capA_name = series.captain_a_name
        capB_name = series.captain_b_name

        if ptr < len(DRAFT_ORDER):
            side = DRAFT_ORDER[ptr]
            phase = "BAN" if ptr in BAN_INDEXES else "PICK"
            who = capA_mention if side == "A" else capB_mention
            header = (
                f"```\n{bar} {secs:>2}s\n```\n**Tour {who} · {phase}**" 
                if highlight 
                else f"```\n{bar} {secs:>2}s\n```{who} · {phase}"
            )
            colour = self._turn_color(side)
        else:
            header = "```\nDraft terminée\n```"
            colour = self._turn_color(None)

        embed = discord.Embed(
            title=f"⚔️ Draft Phase · Game {len(series.games)}",
            colour=colour,
            description=header
        )

        # Utiliser le cache de formatage
        embed.add_field(name="━━━━━━━━━━━━━━━━━━━━━━", value="", inline=False)
        embed.add_field(
            name=f"🚫 BANS — 🔵 {capA_name}", 
            value=f"{capA_mention}\n{self._format_champ_list(g.bans_a)}", 
            inline=True
        )
        embed.add_field(
            name=f"🚫 BANS — 🔴 {capB_name}", 
            value=f"{capB_mention}\n{self._format_champ_list(g.bans_b)}", 
            inline=True
        )
        embed.add_field(name="━━━━━━━━━━━━━━━━━━━━━━", value="", inline=False)
        embed.add_field(
            name=f"✅ PICKS — 🔵 {capA_name}", 
            value=f"{capA_mention}\n{self._format_champ_list(g.picks_a)}", 
            inline=True
        )
        embed.add_field(
            name=f"✅ PICKS — 🔴 {capB_name}", 
            value=f"{capB_mention}\n{self._format_champ_list(g.picks_b)}", 
            inline=True
        )

        embed.set_footer(text="Capitaines only • messages hors capitaines supprimés")
        return embed

    @staticmethod
    def _build_recap_embed(series: SeriesState) -> discord.Embed:
        """Construit l'embed de récapitulatif."""
        g = series.current_game
        capA = f"<@{series.captain_a}>"
        capB = f"<@{series.captain_b}>"
        
        embed = discord.Embed(
            title=f"📊 Récapitulatif · Game {len(series.games)}",
            colour=discord.Colour.dark_gold(),
            description=(
                f"```\nScore : {series.score_a}-{series.score_b}\n```\n"
                "Sélectionnez le vainqueur (Capitaines only)."
            ),
        )
        embed.add_field(
            name=f"🚫 BANS — 🔵 {capA}", 
            value=DraftCog._format_champ_list(g.bans_a), 
            inline=True
        )
        embed.add_field(
            name=f"🚫 BANS — 🔴 {capB}", 
            value=DraftCog._format_champ_list(g.bans_b), 
            inline=True
        )
        embed.add_field(
            name=f"✅ PICKS — 🔵 {capA}", 
            value=DraftCog._format_champ_list(g.picks_a), 
            inline=True
        )
        embed.add_field(
            name=f"✅ PICKS — 🔴 {capB}", 
            value=DraftCog._format_champ_list(g.picks_b), 
            inline=True
        )
        return embed

    @staticmethod
    def _build_chi_embed(series: SeriesState) -> discord.Embed:
        """Construit l'embed de prédiction Chi."""
        g = series.current_game
        p_blue, p_red = chi_predict(g.picks_a, g.picks_b)
        advantage = abs(p_blue - p_red)
        adv_side = "🔵 Blue" if p_blue > p_red else "🔴 Red" if p_red > p_blue else "⚖️ Équilibré"

        embed = discord.Embed(
            title="⚖️ Prédiction Chi · Meta Analysis",
            colour=discord.Colour.from_rgb(0, 176, 255),
            description=(
                f"```\n"
                f"🔵 Blue: {p_blue:5.1f}%\n"
                f"🔴 Red:  {p_red:5.1f}%\n"
                f"```\n"
                f"**Avantage:** {adv_side} ({advantage:.1f}%)"
            )
        )
        embed.add_field(
            name="Balance visuelle", 
            value=f"```\n{chi_bar(p_blue)}\n```", 
            inline=False
        )
        return embed
    
    # ─── anti-spam hors capitaines ────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        """Supprime les messages des non-capitaines dans les threads de draft."""
        if msg.author.bot:
            return
        series = self.series_by_thread.get(getattr(msg.channel, "id", 0))
        if series and msg.author.id not in (series.captain_a, series.captain_b):
            try:
                await msg.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ─── Report helper ────────────────────────────────────────────
    async def _report(self, inter: Interaction, side: str):
        """Gère le report d'une victoire et la progression de la série."""
        if not isinstance(inter.channel, discord.Thread) or not inter.channel.name.startswith("draft-"):
            return await inter.followup.send("❌ À utiliser dans le thread draft.", ephemeral=True)
        
        series = self.series_by_thread.get(inter.channel.id)
        if not series:
            return await inter.followup.send("❌ Série inconnue.", ephemeral=True)
        
        if series.current_game.winner:
            return await inter.followup.send("⚠️ Partie déjà reportée.", ephemeral=True)

        # Enregistre le résultat
        series.current_game.winner = side
        series.score_a += side == "A"
        series.score_b += side == "B"
        logger.info("Victoire Team %s (score %d-%d)", side, series.score_a, series.score_b)

        # UPDATE MÉTA : picks/bans/wins
        g = series.current_game
        try:
            await _meta_update_for_game(g.picks_a, g.picks_b, g.bans_a, g.bans_b, side)
        except Exception as e:
            logger.warning(f"[meta] update failed: {e}")

        # ─── série terminée ? ──────────────────────────────────
        if series.finished():
            # Bo1 → proposer Bo3
            if series.bo == 1:
                await self._handle_bo1_end(inter, series, side)
                return

            # Bo3 à 1-1 : proposer belle ou terminer sur match nul
            if series.bo == 3 and series.score_a == 1 and series.score_b == 1:
                await self._handle_bo3_tie(inter, series, side)
                return

            # Bo3 terminé → proposer Bo5
            if series.bo == 3:
                await self._handle_bo3_end(inter, series, side)
                return

            # Bo5 à 2-2 : proposer belle ou terminer sur match nul
            if series.bo == 5 and series.score_a == 2 and series.score_b == 2:
                await self._handle_bo5_tie(inter, series, side)
                return

            # Victoire finale
            await self._handle_series_victory(inter, series, side)
            return

        # ─── série continue : nouvelle game ───────────────────
        await self._continue_series(inter, series, side)

    async def _handle_bo1_end(self, inter: Interaction, series: SeriesState, side: str):
        """Gère la fin d'un Bo1."""
        next_bo = 3
        cont_view = ContinueView((series.captain_a, series.captain_b), next_bo=next_bo)
        msg = await inter.channel.send(
            f"🏆 Bo1 terminé (**{series.score_a}-{series.score_b}**).\n"
            f"Voulez-vous poursuivre en **Bo{next_bo}** ?",
            view=cont_view,
        )
        await cont_view._done.wait()
        try:
            await msg.delete()
        except:
            pass

        if cont_view.go_next:
            series.bo = next_bo
            await self._handle_side_choice_and_ready(inter, series, side)
            await self._start_next_game(inter, series)
        else:
            await self._handle_series_victory(inter, series, side)

    async def _handle_bo3_tie(self, inter: Interaction, series: SeriesState, side: str):
        """Gère un Bo3 à 1-1 (match nul)."""
        next_bo = 3
        cont_view = ContinueView(
            (series.captain_a, series.captain_b), 
            next_bo=next_bo, 
            is_tied=True, 
            current_score="1-1"
        )
        msg = await inter.channel.send(
            f"⚖️ **Match nul 1-1** !\n"
            f"Voulez-vous jouer une **belle** pour départager ?",
            view=cont_view,
        )
        await cont_view._done.wait()
        try:
            await msg.delete()
        except:
            pass

        if not cont_view.go_next:
            # Terminer sur match nul
            embed_tie = discord.Embed(
                title=f"🤝 Match nul 1-1",
                colour=discord.Colour.gold(),
                description="Les deux équipes se quittent sur une égalité parfaite !",
            ).set_footer(text="GG à tous !")
            await inter.channel.send(embed=embed_tie)
            self.series_by_thread.pop(inter.channel.id, None)
        else:
            await self._handle_side_choice_and_ready(inter, series, side)
            await self._start_next_game(inter, series)

    async def _handle_bo3_end(self, inter: Interaction, series: SeriesState, side: str):
        """Gère la fin d'un Bo3."""
        next_bo = 5
        cont_view = ContinueView((series.captain_a, series.captain_b), next_bo=next_bo)
        msg = await inter.channel.send(
            f"🏆 Bo3 terminé (**{series.score_a}-{series.score_b}**).\n"
            f"Voulez-vous poursuivre en **Bo{next_bo}** ?",
            view=cont_view,
        )
        await cont_view._done.wait()
        try:
            await msg.delete()
        except:
            pass

        if cont_view.go_next:
            series.bo = next_bo
            await self._handle_side_choice_and_ready(inter, series, side)
            await self._start_next_game(inter, series)
        else:
            await self._handle_series_victory(inter, series, side)

    async def _handle_bo5_tie(self, inter: Interaction, series: SeriesState, side: str):
        """Gère un Bo5 à 2-2 (match nul)."""
        next_bo = 5
        cont_view = ContinueView(
            (series.captain_a, series.captain_b), 
            next_bo=next_bo, 
            is_tied=True, 
            current_score="2-2"
        )
        msg = await inter.channel.send(
            f"⚖️ **Match nul 2-2** !\n"
            f"Voulez-vous jouer une **belle** pour départager ?",
            view=cont_view,
        )
        await cont_view._done.wait()
        try:
            await msg.delete()
        except:
            pass

        if not cont_view.go_next:
            # Terminer sur match nul
            embed_tie = discord.Embed(
                title=f"🤝 Match nul 2-2",
                colour=discord.Colour.gold(),
                description="Les deux équipes se quittent sur une égalité parfaite !",
            ).set_footer(text="GG à tous !")
            await inter.channel.send(embed=embed_tie)
            self.series_by_thread.pop(inter.channel.id, None)
        else:
            await self._handle_side_choice_and_ready(inter, series, side)
            await self._start_next_game(inter, series)

    async def _handle_series_victory(self, inter: Interaction, series: SeriesState, side: str):
        """Gère la victoire finale d'une série."""
        winners = series.team_a if side == "A" else series.team_b
        mentions = "\n".join(f"<@{uid}>" for uid in winners)
        embed_end = discord.Embed(
            title=f"🏆  Victoire Team {'A' if side=='A' else 'B'}  —  {series.score_a}-{series.score_b}",
            colour=discord.Colour.gold(),
            description=mentions,
        ).set_footer(text="GG à tous !")
        await inter.channel.send(embed=embed_end)
        self.series_by_thread.pop(inter.channel.id, None)

    async def _handle_side_choice_and_ready(self, inter: Interaction, series: SeriesState, last_winner: str):
        """Gère le choix des sides et le ready-check."""
        loser = series.captain_b if last_winner == "A" else series.captain_a
        
        # Choix des sides
        scv = SideChoiceView(
            loser_id=loser, 
            captain_a_id=series.captain_a, 
            captain_b_id=series.captain_b
        )
        msg_sides = await inter.channel.send(f"🧭 <@{loser}> choisit les **sides** :", view=scv)
        await scv._done.wait()
        try:
            await msg_sides.delete()
        except:
            pass
        
        if scv.swap_chosen:
            series.swap_sides()

        # Ready-check
        rv = CaptainsReadyView(series.captain_a, series.captain_b)
        msg_ready = await inter.channel.send("⏳ Ready check des capitaines…", view=rv)
        await rv._done.wait()
        try:
            await msg_ready.delete()
        except:
            pass

    async def _continue_series(self, inter: Interaction, series: SeriesState, side: str):
        """Continue la série avec une nouvelle game."""
        await self._handle_side_choice_and_ready(inter, series, side)
        await self._start_next_game(inter, series)

    async def _start_next_game(self, inter: Interaction, series: SeriesState):
        """Démarre la prochaine game de la série."""
        series.start_new_game()
        status = await inter.channel.send(embed=self._build_embed(series, 60, 0, highlight=True))
        series.status_msg_id = status.id
        
        if series.fearless_pool:
            await inter.channel.send(embed=discord.Embed(
                title="🔥 Fearless — champions désormais bannis",
                description=", ".join(sorted(series.fearless_pool)),
                colour=discord.Colour.red()
            ))
        
        await self._draft_loop(inter.channel, series, status)

    # ─── /meta : aperçu méta dans Discord ─────────────────────────
    @app_commands.command(name="meta", description="Stats méta customs: top picks/bans/presence/winrate")
    @app_commands.describe(top="Taille du top (1-25)", min_picks="Nombre minimum de picks pour le WR")
    async def meta(self, inter: Interaction, top: int = 10, min_picks: int = 10):
        """Affiche les statistiques méta des customs."""
        await inter.response.defer()
        data = await _meta_load()
        tables = _compute_meta_tables(
            data,
            top=max(1, min(top, 25)),
            min_picks_for_wr=max(1, min_picks)
        )

        def fmt_presence():
            if not tables["presence"]: 
                return "—"
            return "\n".join(
                f"**{cid}** — {cnt} (picks {data['picks'].get(cid,0)} / bans {data['bans'].get(cid,0)})"
                for cid, cnt in tables["presence"]
            )

        def fmt_picks():
            if not tables["picks"]: 
                return "—"
            return "\n".join(f"**{cid}** — {cnt}" for cid, cnt in tables["picks"])

        def fmt_bans():
            if not tables["bans"]: 
                return "—"
            return "\n".join(f"**{cid}** — {cnt}" for cid, cnt in tables["bans"])

        def fmt_wr():
            if not tables["winrates"]: 
                return "—"
            return "\n".join(
                f"**{cid}** — {wr:.1f}%  ({pc} picks)" 
                for cid, wr, pc in tables["winrates"]
            )

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
