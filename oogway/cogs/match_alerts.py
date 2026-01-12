from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image
from sqlalchemy.exc import IntegrityError

from oogway.database import Match, SessionLocal, User, init_db
from oogway.riot.client import RiotClient
from oogway.config import settings
from oogway.cogs.profile import r_get, r_set
import time

# ─── Logging setup ───────────────────────────────────────────────────────────
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    log.addHandler(h)

# ─── Constants & Caches ──────────────────────────────────────────────────────
RANKED_QUEUES = {420: "Ranked Solo/Duo", 440: "Ranked Flex"}
QUEUE_TYPE = {420: "RANKED_SOLO_5x5", 440: "RANKED_FLEX_SR"}
DIV_NUM = {"I": 1, "II": 2, "III": 3, "IV": 4}
TIERS = [
    "Iron", "Bronze", "Silver", "Gold",
    "Platinum", "Emerald", "Diamond", "Master",
    "Grandmaster", "Challenger",
]
TIER_INDEX = {t: i for i, t in enumerate(TIERS)}

D_DRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
SPRITE_SIZE = 32
LP_BAR_LEN = 10

# Throttle (doucement entre users pour éviter 429)
PER_USER_SLEEP = 0.15  # secondes

EM_GOLD, EM_KDA, EM_VISION, EM_CS = "🟡", "⚔️", "👁️", "🌾"
ROLE_EMOJI = {
    "TOP": "<:top:1384144618404315197>",
    "JUNGLE": "<:jungle:1384144488988938323>",
    "MIDDLE": "<:mid:1384144551467417671>",
    "BOTTOM": "<:bot:1384144643150577807>",
    "UTILITY": "<:sup:1384144577832685668>",
    "FILL": "<:fill:1384144523944267978>",
    "UNKNOWN": "❔",
}
BADGE_INFO = {
    "🏆 Skadoosh": "Top dégâts + meilleur KP",
    "🔪 Lightning Lotus": "First Blood ≤ 3 min",
    "💣 Bélier de Jade": "Première tour détruite",
    "🔥 Poing du Panda": "Dégâts > 140 % équipe",
    "🛡️ Oogway Insight": "Tank > 150 % équipe",
    "👁️ Œil de Grue": "Vision ≥ 45 ou top 1",
    "💰 Banquier de Jade": "+1 000 po sur le laner",
    "⚡ Parchemin Express": "Mythique ≤ 9 min",
    "🧹 Maître kung-fu": "≤ 2 morts & KDA ≥ 5",
    "🐉 Cinq Doigts du Wuxi": "Pentakill",
}
ROLE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "TOP":     dict(KDA=.20, DMG=.25, ECO=.15, OBJ=.15, VIS=.05, UTL=.05, CLT=.10),
    "MIDDLE":  dict(KDA=.20, DMG=.25, ECO=.15, OBJ=.10, VIS=.05, UTL=.05, CLT=.15),
    "JUNGLE":  dict(KDA=.15, DMG=.20, ECO=.10, OBJ=.25, VIS=.10, UTL=.05, CLT=.15),
    "BOTTOM":  dict(KDA=.20, DMG=.30, ECO=.15, OBJ=.10, VIS=.05, UTL=.05, CLT=.10),
    "UTILITY": dict(KDA=.15, DMG=.05, ECO=.05, OBJ=.15, VIS=.30, UTL=.25, CLT=.05),
    "UNKNOWN": dict(KDA=.20, DMG=.25, ECO=.15, OBJ=.15, VIS=.10, UTL=.05, CLT=.10),
}

class DDragon:
    version: Optional[str] = None
    icon_cache: Dict[str, Image.Image] = {}

# ─── Retry decorator ─────────────────────────────────────────────────────────
def with_retry(max_attempts: int = 3, base_delay: float = 0.7):
    def deco(func):
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts+1):
                try:
                    return await func(*args, **kwargs)
                except aiohttp.ClientResponseError as e:
                    # 429 et 5xx => retry
                    if e.status not in (429,) and e.status < 500:
                        raise
                    log.warning(f"[retry {attempt}/{max_attempts}] {func.__name__}: {e}")
                except aiohttp.ClientError as e:
                    log.warning(f"[retry {attempt}/{max_attempts}] network {func.__name__}: {e}")
                if attempt == max_attempts:
                    log.error(f"{func.__name__} failed after {max_attempts} attempts")
                    raise
                await asyncio.sleep(base_delay * 2**(attempt-1))
        return wrapper
    return deco

# ─── DDragon helpers ─────────────────────────────────────────────────────────
async def ensure_ddragon_version(session: aiohttp.ClientSession):
    if DDragon.version is None:
        resp = await session.get(D_DRAGON_VERSIONS_URL)
        resp.raise_for_status()
        versions = await resp.json()
        DDragon.version = versions[0]
        log.info(f"DDragon version {DDragon.version} loaded")

async def fetch_icon(url: str, session: aiohttp.ClientSession) -> Image.Image:
    if url in DDragon.icon_cache:
        return DDragon.icon_cache[url]
    resp = await session.get(url)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(await resp.read())).convert("RGBA")
    DDragon.icon_cache[url] = img
    return img

async def make_sprite(item_ids: List[int], session: aiohttp.ClientSession) -> Optional[discord.File]:
    icons: List[Image.Image] = []
    for iid in item_ids:
        if not iid:
            continue
        url = f"https://ddragon.leagueoflegends.com/cdn/{DDragon.version}/img/item/{iid}.png"
        try:
            img = await fetch_icon(url, session)
            icons.append(img.resize((SPRITE_SIZE, SPRITE_SIZE)))
        except Exception as e:
            log.warning(f"Item icon {iid} failed: {e}")
    if not icons:
        return None
    sprite = Image.new("RGBA", (SPRITE_SIZE * len(icons), SPRITE_SIZE))
    for idx, ic in enumerate(icons):
        sprite.paste(ic, (idx*SPRITE_SIZE, 0), ic)
    buf = io.BytesIO()
    sprite.save(buf, "PNG")
    buf.seek(0)
    return discord.File(buf, filename="build.png")

# ─── Stats & badges ──────────────────────────────────────────────────────────
def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))

def norm(v: float, mean: float, std: float) -> float:
    return clamp01(0.5 + (v - mean) / (2*(std or 1)))

def compute_oogscore(part, participants):
    lane = part.get("teamPosition", "UNKNOWN")
    w = ROLE_WEIGHTS.get(lane, ROLE_WEIGHTS["UNKNOWN"])
    team = [p for p in participants if p["teamId"] == part["teamId"]]

    μ = lambda k: sum(p[k] for p in team)/len(team)
    σ = lambda k: math.sqrt(sum((p[k]-μ(k))**2 for p in team)/len(team)) or 1.0

    for p in team:
        p["kda_p"] = (p["kills"] + p["assists"]) / max(1, p["deaths"])
        p["cs_p"]  = p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0)
        p["obj_p"] = p.get("dragonKills", 0) + p.get("baronKills", 0) + p.get("towerKills", 0)
        p["util_p"]= p.get("totalHealOnTeammates", 0) + p.get("totalDamageShieldedOnTeammates", 0)

    kda_n = norm(part["kda_p"], μ("kda_p"), σ("kda_p"))
    dmg_n = 0.6*norm(part.get("totalDamageDealtToChampions", 0), μ("totalDamageDealtToChampions"), σ("totalDamageDealtToChampions")) \
          + 0.4*norm(part.get("totalDamageTaken", 0),             μ("totalDamageTaken"),             σ("totalDamageTaken"))
    eco_n = 0.5*norm(part.get("goldEarned", 0), μ("goldEarned"), σ("goldEarned")) \
          + 0.5*norm(part["cs_p"],              μ("cs_p"),       σ("cs_p"))
    obj_n = norm(part.get("obj_p",  0), μ("obj_p"),  σ("obj_p"))
    vis_n = norm(part.get("visionScore", 0), μ("visionScore"), σ("visionScore"))
    utl_n = norm(part.get("util_p", 0), μ("util_p"), σ("util_p"))
    clt_n = clamp01(part.get("pentaKills", 0))

    scores = {"KDA": kda_n, "DMG": dmg_n, "ECO": eco_n, "OBJ": obj_n, "VIS": vis_n, "UTL": utl_n, "CLT": clt_n}

    total = 0.0
    breakdown = {}
    for k, v in scores.items():
        pts = v * w[k] * 100
        total += pts
        breakdown[k] = (v, w[k])

    total = min(100.0, total)
    return round(total), breakdown

def compute_badges(
    part: Dict[str, Any],
    info: Dict[str, Any],
    opponent: Optional[Dict[str, Any]],
    timeline: Dict[str, Optional[int]],
) -> List[str]:
    team = [p for p in info["participants"] if p["teamId"] == part["teamId"]]
    avg_dmg = sum(p["totalDamageDealtToChampions"] for p in team)/len(team)
    avg_tank = sum(p["totalDamageTaken"] for p in team)/len(team)
    badges: List[str] = []
    if part["totalDamageDealtToChampions"] == max(p["totalDamageDealtToChampions"] for p in team):
        badges.append("🏆 Skadoosh")
    if timeline.get("fb") is not None and timeline["fb"] <= 3 and part["kills"] > 0:
        badges.append("🔪 Lightning Lotus")
    if timeline.get("ft") is not None and part.get("towerKills",0)>0:
        badges.append("💣 Bélier de Jade")
    if part["totalDamageDealtToChampions"] > 1.4*avg_dmg:
        badges.append("🔥 Poing du Panda")
    if part["totalDamageTaken"] > 1.5*avg_tank:
        badges.append("🛡️ Oogway Insight")
    vis = part.get("visionScore",0)
    if vis>=45 or vis == max(p.get("visionScore",0) for p in info["participants"]):
        badges.append("👁️ Œil de Grue")
    if opponent and part["goldEarned"] - opponent["goldEarned"]>1000:
        badges.append("💰 Banquier de Jade")
    if timeline.get("mythic") is not None and timeline["mythic"]<=9:
        badges.append("⚡ Parchemin Express")
    if part.get("pentaKills",0)>0:
        badges.append("🐉 Cinq Doigts du Wuxi")
    if part["deaths"]<=2 and (part["kills"]+part["assists"])/max(1,part["deaths"])>=5:
        badges.append("🧹 Maître kung-fu")
    return badges

def find_opponent(part: Dict[str, Any], parts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    lane = part.get("teamPosition","")
    return next((p for p in parts if p["teamId"]!=part["teamId"] and p.get("teamPosition")==lane), None)

def parse_timeline(raw: Dict[str, Any]) -> Dict[str, Optional[int]]:
    fb = ft = mythic = None
    mythics = {3031,6671,6672,6673,6675,6691,6692,6693,6694,6695,3078,3084,3124,3137,3156,3190,3504,4005,4401,4628}
    for fr in raw.get("info",{}).get("frames",[]):
        for ev in fr.get("events",[]):
            t = ev.get("timestamp",0)//60000
            et = ev.get("type")
            if et=="CHAMPION_KILL" and fb is None:
                fb = t
            elif et=="BUILDING_KILL" and ev.get("buildingType")=="TOWER" and ft is None:
                ft = t
            elif et=="ITEM_PURCHASED" and ev.get("itemId") in mythics and mythic is None:
                mythic = t
            if fb and ft and mythic:
                return {"fb":fb,"ft":ft,"mythic":mythic}
    return {"fb":fb,"ft":ft,"mythic":mythic}

# ─── UI View ─────────────────────────────────────────────────────────────────
class HelpView(discord.ui.View):
    def __init__(self, badges: List[str], lane: str, oog: int, breakdown: Dict[str, Tuple[float,float]]):
        super().__init__(timeout=None)
        self.badges = badges
        self.oog = oog
        self.breakdown = breakdown

    @staticmethod
    def format_breakdown(bd: Dict[str, Tuple[float,float]]) -> List[str]:
        labels = {"KDA":"KDA","DMG":"Dégâts","ECO":"Éco","OBJ":"Obj","VIS":"Vis","UTL":"Util","CLT":"Clt"}
        lines = []
        total = 0.0
        for k,(v,w) in bd.items():
            pts = v*w*100
            total += pts
            lines.append(f"• {labels[k]:5}: {v:.2f} × {int(w*100)}% = {pts:.1f}")
        lines.append("─"*20)
        lines.append(f"Total: **{total:.1f} pts**")
        return lines

    @discord.ui.button(label="ℹ️ Badges ?", style=discord.ButtonStyle.secondary)
    async def show_badges(self, i: discord.Interaction, b: discord.ui.Button):
        txt = "\n".join(f"{x} — {BADGE_INFO[x]}" for x in self.badges) or "Aucun badge"
        await i.response.send_message(txt, ephemeral=True, delete_after=10)

    @discord.ui.button(label="ℹ️ OogScore ?", style=discord.ButtonStyle.primary)
    async def show_oog(self, i: discord.Interaction, b: discord.ui.Button):
        header = f"OogScore {self.oog}/100"
        lines = [header, *self.format_breakdown(self.breakdown)]
        await i.response.send_message("\n".join(lines), ephemeral=True, delete_after=10)

# ─── Delta LP ────────────────────────────────────────────────────────────────
def lp_delta_between(prev: Tuple[str, str, int], cur: Tuple[str, str, int]) -> int:
    prev_t, prev_d, prev_lp = prev
    cur_t, cur_d, cur_lp = cur
    if not prev_t or prev_t == "Unranked":
        return 0
    if prev_t == cur_t:
        od = DIV_NUM.get(prev_d, 4)
        nd = DIV_NUM.get(cur_d, 4)
        if nd == od:
            return cur_lp - prev_lp
        elif nd < od:
            return (100 - prev_lp) + cur_lp
        else:
            return -(prev_lp + (100 - cur_lp))
    else:
        if TIER_INDEX.get(cur_t, 0) > TIER_INDEX.get(prev_t, 0):
            return (100 - prev_lp) + cur_lp
        else:
            return -(prev_lp + (100 - cur_lp))

# ─── Cog ─────────────────────────────────────────────────────────────────────
class MatchAlertsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()
        self.db = SessionLocal()
        self.riot = RiotClient(settings.RIOT_API_KEY)
        self.http = aiohttp.ClientSession()
        # cache par utilisateur ET par file: {puuid: {queueId: (tier, div, lp)}}
        self.lp_cache: Dict[str, Dict[int, Tuple[str, str, int]]] = {}
        # limiter global pour match/timeline (évite burst)
        self.sem = asyncio.Semaphore(2)

    # ─── Persistance (Redis) ──────────────────────────────────────────
    async def _get_last_state(self, puuid: str, queue_id: int) -> Optional[Tuple[str, str, int]]:
        key = f"lp_last_state:{puuid}:{queue_id}"
        raw = await r_get(key)
        if isinstance(raw, dict) and "tier" in raw and "div" in raw and "lp" in raw:
            try:
                return str(raw["tier"]), str(raw["div"]), int(raw["lp"])
            except Exception:
                return None
        return None

    async def _set_last_state(self, puuid: str, queue_id: int, state: Tuple[str, str, int]):
        key = f"lp_last_state:{puuid}:{queue_id}"
        payload = {"tier": state[0], "div": state[1], "lp": int(state[2])}
        await r_set(key, payload, ttl=90*24*3600)

    async def _get_last_seen_match(self, puuid: str) -> Optional[str]:
        return await r_get(f"last_seen_match:{puuid}") or None

    async def _set_last_seen_match(self, puuid: str, mid: str):
        await r_set(f"last_seen_match:{puuid}", mid, ttl=90*24*3600)

    @commands.Cog.listener()
    async def on_ready(self):
        log.info("Bot ready, fetching DDragon & starting poll")
        await ensure_ddragon_version(self.http)
        self.poll_matches.start()

    @tasks.loop(minutes=5)
    async def poll_matches(self):
        users = self.db.query(User).all()

        # Traite séquentiellement avec petite pause pour lisser la charge
        for u in users:
            try:
                await self.handle_user(u)
            except Exception as e:
                log.error(f"Error for user {u.discord_id}: {e}")
                self.db.rollback()
            await asyncio.sleep(PER_USER_SLEEP)

    @with_retry()
    async def _get_match_ids(self, user: User, n: int):
        """Get match IDs for user - now fully async."""
        return await self.riot.get_match_ids(user.region, user.puuid, n)

    @with_retry()
    async def _get_match(self, user: User, mid: str):
        """Get match details - now fully async."""
        return await self.riot.get_match_by_id(user.region, mid)

    @with_retry()
    async def _get_timeline(self, user: User, mid: str):
        """Get match timeline - now fully async."""
        return await self.riot.get_match_timeline_by_id(user.region, mid)

    async def handle_user(self, user: User):
        """
        Stratégie anti-429 :
          1) On tire juste le dernier ID (count=1) -> 1 appel léger.
          2) Si identique au dernier vu (Redis) => on s'arrête (0 appel match).
          3) Si différent => on backfill jusqu'à 5 IDs (2e appel), et on traite
             seulement ceux plus récents que 'last_seen'.
        """
        last_seen = await self._get_last_seen_match(user.puuid)

        # Étape 1: un seul ID
        try:
            latest_ids = await self._get_match_ids(user, 1) or []
        except Exception as e:
            log.warning(f"IDs(1) fail for {user.discord_id}: {e}")
            return

        if not latest_ids:
            return

        newest = latest_ids[0]
        if newest == last_seen:
            # Rien de nouveau, on ne charge rien d'autre
            return

        # Étape 2: backfill jusqu'à 5 si on détecte du nouveau
        try:
            ids = await self._get_match_ids(user, 5) or []
        except Exception as e:
            log.warning(f"IDs(5) fail for {user.discord_id}: {e}")
            ids = latest_ids  # fallback

        # On arrête à l'ancien last_seen s'il apparaît
        if last_seen in ids:
            cutoff_index = ids.index(last_seen)
            to_process = ids[:cutoff_index]  # plus récents uniquement
        else:
            to_process = ids  # tous (jusqu'à 5), on a peut-être manqué plusieurs games

        # Traite du plus ancien au plus récent pour cohérence
        for mid in reversed(to_process):
            # Double garde DB (évite re-fetch si déjà traité)
            exists = self.db.query(Match).filter_by(match_id=mid, puuid=user.puuid).first()
            if exists:
                continue
            await self.process_match(user, mid)
            # MAJ last_seen après chaque succès (évite retraiter si crash en plein lot)
            await self._set_last_seen_match(user.puuid, mid)

    async def process_match(self, user: User, mid: str):
        async with self.sem:  # évite burst match+timeline
            info = (await self._get_match(user, mid))["info"]

        part = next(p for p in info["participants"] if p["puuid"] == user.puuid)
        if info["queueId"] not in RANKED_QUEUES:
            # Même si non-ranked, on a déjà mis à jour last_seen dans handle_user
            return

        queue_id = info["queueId"]

        # Rang/LP/WR de la bonne file
        tier, div, lp_now, wr = await self._get_rank(user, queue_id)

        # 1) Cache mémoire
        prev_state = self.lp_cache.get(user.puuid, {}).get(queue_id)
        # 2) Redis si vide (reboot)
        if prev_state is None:
            prev_state = await self._get_last_state(user.puuid, queue_id)
        # 3) Si rien (1re fois), base = courante
        if prev_state is None:
            prev_state = (tier, div, lp_now)

        cur_state = (tier, div, lp_now)
        lp_delta = lp_delta_between(prev_state, cur_state)

        # MAJ cache + Redis
        self.lp_cache.setdefault(user.puuid, {})[queue_id] = cur_state
        await self._set_last_state(user.puuid, queue_id, cur_state)

        # Historique LP par file 30j
        now = int(time.time())
        hist_key = f"lp_hist:{user.puuid}:{queue_id}"
        raw = await r_get(hist_key) or {}
        try:
            hist: Dict[int, int] = {int(k): int(v) for k, v in raw.items()}
        except AttributeError:
            hist = {}
        hist[now] = int(lp_now)
        thirty_days = 30 * 24 * 3600
        hist = {t: v for t, v in hist.items() if (now - t) <= thirty_days}
        await r_set(hist_key, {str(t): v for t, v in hist.items()}, ttl=thirty_days)

        # Opposant lane, diffs
        opponent = find_opponent(part, info["participants"])
        gold_diff = part["goldEarned"] - (opponent["goldEarned"] if opponent else 0)
        exp_diff  = part.get("champExperience",0) - (opponent.get("champExperience",0) if opponent else 0)

        # Timeline (protégé par le sem)
        async with self.sem:
            timeline = parse_timeline((await self._get_timeline(user, mid)).get("info", {}))
        badges   = compute_badges(part, info, opponent, timeline)

        # ── Détection DuoQ : même team, autre joueur link dans DB (queue 420)
        duo_names: List[str] = []
        if queue_id == 420:
            same_team_puuids = {p["puuid"] for p in info["participants"] if p["teamId"] == part["teamId"] and p["puuid"] != user.puuid}
            if same_team_puuids:
                linked_teammates = self.db.query(User).filter(User.puuid.in_(same_team_puuids)).all()
                for mate in linked_teammates:
                    try:
                        du = await self.bot.fetch_user(mate.discord_id)
                        duo_names.append(du.display_name)
                    except Exception:
                        duo_names.append(mate.puuid[:6])

        # persist match
        match = Match(
            match_id=mid,
            puuid=user.puuid,
            queue_id=info["queueId"],
            win=part["win"],
            timestamp=dt.datetime.fromtimestamp(info["gameStartTimestamp"]/1000),
        )
        self.db.add(match)
        try:
            self.db.commit()
            log.debug(f"Persisted match {mid}")
        except IntegrityError:
            self.db.rollback()
            log.info(f"Match {mid} already in database, skipping persist")
            return

        await self._send_embed(
            user, info, part,
            tier, div, lp_now, lp_delta, wr,
            gold_diff, exp_diff, badges,
            opponent["championName"] if opponent else "?",
            duo_names
        )

    async def _get_rank(self, user: User, queue_id: int) -> Tuple[str, str, int, int]:
        """Get player rank information for specific queue - now fully async."""
        entries = await self.riot.get_league_entries_by_puuid(user.region, user.puuid)

        # Tente correspondance exacte, sinon fallback si Flex
        qtype = QUEUE_TYPE.get(queue_id)
        ent = next((e for e in entries if e["queueType"] == qtype), None)

        if ent is None:
            log.warning(
                f"[RANK] Pas trouvé d'entrée pour queue_id={queue_id} (qtype={qtype}), entries={entries}"
            )
            return "Unranked", "", 0, 0

        wins, losses = ent.get("wins", 0), ent.get("losses", 0)
        wr = int(wins / max(1, wins + losses) * 100)
        return ent["tier"].title(), ent["rank"], int(ent["leaguePoints"]), wr

    async def _send_embed(
        self, user: User, info: Any, part: Any,
        tier: str, div: str, lp: int, lp_delta: int, wr: int,
        gold_diff: int, exp_diff: int,
        badges: List[str], opp_champ: str,
        duo_names: List[str]
    ):
        log.info(f"Sending embed for {user.discord_id}")
        channel = self.bot.get_channel(settings.ALERT_CHANNEL_ID) or await self.bot.fetch_channel(settings.ALERT_CHANNEL_ID)
        embed = discord.Embed(
            color=0x2ECC71 if part["win"] else 0xE74C3C,
            description=(
                f"**{RANKED_QUEUES[info['queueId']]}** · "
                f"{dt.timedelta(seconds=info['gameDuration'])} · "
                f"{ROLE_EMOJI.get(part.get('teamPosition','UNKNOWN'))}"
            ),
            timestamp=dt.datetime.fromtimestamp(info["gameEndTimestamp"]//1000)
        )
        try:
            du = await self.bot.fetch_user(user.discord_id)
            name = du.display_name
        except Exception:
            name = user.puuid[:6]
        champ_icon = f"https://ddragon.leagueoflegends.com/cdn/{DDragon.version}/img/champion/{part['championName']}.png"
        prof_icon  = f"https://ddragon.leagueoflegends.com/cdn/{DDragon.version}/img/profileicon/{part.get('profileIcon',0)}.png"

        outcome = "Victoire" if part["win"] else "Défaite"
        if duo_names:
            outcome += f" (Duo avec {', '.join(duo_names)})"

        embed.set_author(name=f"{name} — {outcome}", icon_url=champ_icon)
        embed.set_thumbnail(url=prof_icon)

        pct = lp % 100
        filled = int(pct/(100/LP_BAR_LEN))
        bar = "█"*filled + "░"*(LP_BAR_LEN-filled)
        embed.add_field(name="Rank", value=f"{tier} {div}\n{lp} LP ({lp_delta:+})\n{bar}\n{wr}% WR", inline=True)
        embed.add_field(name="Stats", value=f"{EM_KDA} **{part['kills']}/{part['deaths']}/{part['assists']}**\n{EM_GOLD} ΔGold **{gold_diff:+}** · ΔXP **{exp_diff:+}**", inline=True)
        embed.add_field(name="Vision", value=f"{EM_VISION} {part.get('visionScore',0)}", inline=True)
        cs = part.get("totalMinionsKilled",0)+part.get("neutralMinionsKilled",0)
        embed.add_field(name="CS", value=f"{EM_CS} {cs} ({cs/(info['gameDuration']/60):.1f}/min)", inline=True)
        oog, breakdown = compute_oogscore(part, info["participants"])
        if oog < 40:
            emo,label = "🟥","Grue bancale"
        elif oog < 70:
            emo,label = "🟨","Apprenti"
        elif oog < 90:
            emo,label = "🟩","Maître du Jade"
        else:
            emo,label = "🟦","Skadoosh"
        embed.add_field(name="OogScore", value=f"{emo} **{oog}/100** — {label}", inline=True)
        embed.add_field(name="Badges", value=" · ".join(badges) or "—", inline=False)

        sprite = await make_sprite([part.get(f"item{i}",0) for i in range(7)], self.http)
        if sprite:
            embed.set_image(url="attachment://build.png")

        view = HelpView(badges, part.get("teamPosition","UNKNOWN"), oog, breakdown)
        await channel.send(embed=embed, file=sprite, view=view, delete_after=172800)

    @app_commands.command(name="alerts_test", description="Force un poll immédiat")
    async def alerts_test(self, interaction: discord.Interaction):
        log.info("Manual poll triggered")
        await interaction.response.defer(ephemeral=True)
        await self.poll_matches()
        await interaction.followup.send("✅ Poll exécuté !", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(MatchAlertsCog(bot))
