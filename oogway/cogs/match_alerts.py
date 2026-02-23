from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import math
from typing import Any, Dict, List, Optional, Tuple
from functools import lru_cache
from dataclasses import dataclass

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
import json

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

PER_USER_SLEEP = 0.4

EM_GOLD, EM_KDA, EM_VISION, EM_CS = "🟡", "⚔️", "👁️", "🌾"
ROLE_EMOJI = {
    "TOP": "<:top:1384144618404315197>",
    "JUNGLE": "<:jungle:1384144488938323>",
    "MIDDLE": "<:mid:1384144551467417671>",
    "BOTTOM": "<:bot:1384144643150577807>",
    "UTILITY": "<:sup:1384144577832685668>",
    "FILL": "<:fill:1384144523944267978>",
    "UNKNOWN": "❔",
}

BADGE_INFO = {
    # ── Badges existants ──────────────────────────────────────────────────
    "🏆 Skadoosh":              "Top dégâts + meilleur KP",
    "🔪 Lightning Lotus":       "First Blood ≤ 3 min",
    "💣 Bélier de Jade":        "Première tour détruite",
    "🔥 Poing du Panda":        "Dégâts > 140 % équipe",
    "🛡️ Oogway Insight":       "Tank > 150 % équipe",
    "👁️ Œil de Grue":          "Vision ≥ 45 ou top 1",
    "💰 Banquier de Jade":      "+1 000 po sur le laner",
    "⚡ Parchemin Express":     "Mythique ≤ 9 min",
    "🧹 Maître kung-fu":        "≤ 2 morts & KDA ≥ 5",
    "🐉 Cinq Doigts du Wuxi":  "Pentakill",
    # ── Nouveaux badges ───────────────────────────────────────────────────
    "🏗️ Démolisseur":           "3 tours ou plus détruites",
    "🔄 Phénix de Jade":        "Comeback : perdait par 3k+ gold et a gagné",
    "💚 Gardien du Temple":     "12 000+ heal/shield prodigués à l'équipe",
    "🌀 Tempête de Jade":       "30+ secondes de CC infligées aux ennemis",
}

ROLE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "TOP":     dict(KDA=.20, DMG=.25, ECO=.15, OBJ=.15, VIS=.05, UTL=.05, CLT=.10),
    "MIDDLE":  dict(KDA=.20, DMG=.25, ECO=.15, OBJ=.10, VIS=.05, UTL=.05, CLT=.15),
    "JUNGLE":  dict(KDA=.15, DMG=.20, ECO=.10, OBJ=.25, VIS=.10, UTL=.05, CLT=.15),
    "BOTTOM":  dict(KDA=.20, DMG=.30, ECO=.15, OBJ=.10, VIS=.05, UTL=.05, CLT=.10),
    "UTILITY": dict(KDA=.15, DMG=.05, ECO=.05, OBJ=.15, VIS=.30, UTL=.25, CLT=.05),
    "UNKNOWN": dict(KDA=.20, DMG=.25, ECO=.15, OBJ=.15, VIS=.10, UTL=.05, CLT=.10),
}

MYTHIC_ITEMS = frozenset({
    3031, 6671, 6672, 6673, 6675, 6691, 6692, 6693, 6694, 6695,
    3078, 3084, 3124, 3137, 3156, 3190, 3504, 4005, 4401, 4628
})

# ─── Optimized Dataclasses ───────────────────────────────────────────────────
@dataclass
class DDragon:
    version: Optional[str] = None
    icon_cache: Dict[str, Image.Image] = None
    runes_data: Optional[List[Dict]] = None       # runesReforged.json cache
    summoners_data: Optional[Dict] = None         # summoner.json cache
    _cache_timestamp: float = 0.0
    CACHE_TTL: int = 86400

    def __post_init__(self):
        if self.icon_cache is None:
            self.icon_cache = {}

    def should_refresh(self) -> bool:
        return (time.time() - self._cache_timestamp) > self.CACHE_TTL

ddragon = DDragon()


# ─── Retry decorator ─────────────────────────────────────────────────────────
def with_retry(max_attempts: int = 3, base_delay: float = 0.7):
    def deco(func):
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except aiohttp.ClientResponseError as e:
                    last_exception = e
                    if e.status not in (429,) and e.status < 500:
                        raise
                    if attempt < max_attempts:
                        delay = base_delay * (2 ** (attempt - 1))
                        log.warning(f"[retry {attempt}/{max_attempts}] {func.__name__}: {e}")
                        await asyncio.sleep(delay)
                except aiohttp.ClientError as e:
                    last_exception = e
                    if attempt < max_attempts:
                        delay = base_delay * (2 ** (attempt - 1))
                        log.warning(f"[retry {attempt}/{max_attempts}] network {func.__name__}: {e}")
                        await asyncio.sleep(delay)
            log.error(f"{func.__name__} failed after {max_attempts} attempts")
            raise last_exception
        return wrapper
    return deco


# ─── Redis Helpers ────────────────────────────────────────────────────────────
async def safe_r_get(key: str) -> Any:
    try:
        value = await r_get(key)
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, (str, bytes)):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value
    except Exception as e:
        log.warning(f"Redis get error for {key}: {e}")
        return None

async def safe_r_set(key: str, value: Any, ttl: int = None):
    try:
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        await r_set(key, value, ttl=ttl)
    except Exception as e:
        log.error(f"Redis set error for {key}: {e}")


# ─── DDragon helpers ──────────────────────────────────────────────────────────
async def ensure_ddragon_version(session: aiohttp.ClientSession, force_refresh: bool = False):
    if ddragon.version is None or force_refresh or ddragon.should_refresh():
        try:
            resp = await session.get(D_DRAGON_VERSIONS_URL, timeout=aiohttp.ClientTimeout(total=5))
            resp.raise_for_status()
            versions = await resp.json()
            ddragon.version = versions[0]
            ddragon._cache_timestamp = time.time()
            log.info(f"DDragon version {ddragon.version} loaded")
        except Exception as e:
            log.error(f"Failed to fetch DDragon version: {e}")
            if ddragon.version is None:
                ddragon.version = "14.1.1"


async def ensure_runes_data(session: aiohttp.ClientSession):
    """Load and cache runesReforged.json from DDragon."""
    if ddragon.runes_data is not None:
        return
    try:
        url = f"https://ddragon.leagueoflegends.com/cdn/{ddragon.version}/data/fr_FR/runesReforged.json"
        resp = await session.get(url, timeout=aiohttp.ClientTimeout(total=5))
        resp.raise_for_status()
        ddragon.runes_data = await resp.json()
        log.info(f"Runes data loaded ({len(ddragon.runes_data)} trees)")
    except Exception as e:
        log.error(f"Failed to fetch runes data: {e}")
        ddragon.runes_data = []


async def ensure_summoners_data(session: aiohttp.ClientSession):
    """Load and cache summoner.json from DDragon."""
    if ddragon.summoners_data is not None:
        return
    try:
        url = f"https://ddragon.leagueoflegends.com/cdn/{ddragon.version}/data/fr_FR/summoner.json"
        resp = await session.get(url, timeout=aiohttp.ClientTimeout(total=5))
        resp.raise_for_status()
        raw = await resp.json()
        ddragon.summoners_data = raw.get("data", {})
        log.info(f"Summoners data loaded ({len(ddragon.summoners_data)} spells)")
    except Exception as e:
        log.error(f"Failed to fetch summoners data: {e}")
        ddragon.summoners_data = {}


def get_rune_info(rune_id: int) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (name, icon_path) for a rune ID.
    icon_path is relative, prepend https://ddragon.leagueoflegends.com/cdn/img/
    """
    if not ddragon.runes_data:
        return None, None
    for tree in ddragon.runes_data:
        for slot in tree.get("slots", []):
            for rune in slot.get("runes", []):
                if rune["id"] == rune_id:
                    return rune["name"], rune["icon"]
        # Also check tree keystones (first slot)
        if tree.get("id") == rune_id:
            return tree["name"], tree["icon"]
    return None, None


def get_summoner_info(spell_id: int) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (name, icon_filename) for a summoner spell ID.
    Icon URL: https://ddragon.leagueoflegends.com/cdn/{version}/img/spell/{icon}
    """
    if not ddragon.summoners_data:
        return None, None
    for spell in ddragon.summoners_data.values():
        if str(spell.get("key")) == str(spell_id):
            return spell["name"], spell["image"]["full"]
    return None, None


async def fetch_icon(url: str, session: aiohttp.ClientSession) -> Image.Image:
    if url in ddragon.icon_cache:
        return ddragon.icon_cache[url]
    try:
        resp = await session.get(url, timeout=aiohttp.ClientTimeout(total=3))
        resp.raise_for_status()
        img = Image.open(io.BytesIO(await resp.read())).convert("RGBA")
        if len(ddragon.icon_cache) > 500:
            keys_to_remove = list(ddragon.icon_cache.keys())[:100]
            for key in keys_to_remove:
                ddragon.icon_cache.pop(key, None)
        ddragon.icon_cache[url] = img
        return img
    except Exception as e:
        log.warning(f"Failed to fetch icon {url}: {e}")
        return Image.new("RGBA", (SPRITE_SIZE, SPRITE_SIZE), (0, 0, 0, 0))


async def make_sprite(item_ids: List[int], session: aiohttp.ClientSession) -> Optional[discord.File]:
    if not item_ids or all(not iid for iid in item_ids):
        return None
    valid_ids = [iid for iid in item_ids if iid]
    if not valid_ids:
        return None

    fetch_tasks = [
        fetch_icon(f"https://ddragon.leagueoflegends.com/cdn/{ddragon.version}/img/item/{iid}.png", session)
        for iid in valid_ids
    ]
    icons = await asyncio.gather(*fetch_tasks, return_exceptions=True)
    valid_icons = [ic.resize((SPRITE_SIZE, SPRITE_SIZE), Image.LANCZOS)
                   for ic in icons if isinstance(ic, Image.Image)]
    if not valid_icons:
        return None

    sprite = Image.new("RGBA", (SPRITE_SIZE * len(valid_icons), SPRITE_SIZE))
    for idx, ic in enumerate(valid_icons):
        sprite.paste(ic, (idx * SPRITE_SIZE, 0), ic)

    buf = io.BytesIO()
    sprite.save(buf, "PNG", optimize=True)
    buf.seek(0)
    return discord.File(buf, filename="build.png")


# ─── LP Sparkline ─────────────────────────────────────────────────────────────
def create_sparkline_lp(lp_values: list[int]) -> str:
    if not lp_values or len(lp_values) < 2:
        return "─" * 10
    min_lp, max_lp = min(lp_values), max(lp_values)
    range_lp = max_lp - min_lp
    if range_lp == 0:
        return "─" * len(lp_values)
    chars = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█']
    sparkline = "".join(chars[int((lp - min_lp) / range_lp * 7)] for lp in lp_values)
    delta = lp_values[-1] - lp_values[0]
    arrow = '▲' if delta >= 0 else '▼'
    return f"{sparkline} {arrow}{abs(delta)}LP"


# ─── Stats & Scores ───────────────────────────────────────────────────────────
def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))

def norm(v: float, mean: float, std: float) -> float:
    if std == 0:
        return 0.5
    return clamp01(0.5 + (v - mean) / (2 * std))

def compute_team_stats(participants: List[Dict], team_id: int) -> Dict[str, Any]:
    team = [p for p in participants if p["teamId"] == team_id]
    for p in team:
        p["kda_p"] = (p["kills"] + p["assists"]) / max(1, p["deaths"])
        p["cs_p"] = p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0)
        p["obj_p"] = p.get("dragonKills", 0) + p.get("baronKills", 0) + p.get("towerKills", 0)
        p["util_p"] = p.get("totalHealOnTeammates", 0) + p.get("totalDamageShieldedOnTeammates", 0)
    stats = {}
    for key in ["kda_p", "totalDamageDealtToChampions", "totalDamageTaken",
                "goldEarned", "cs_p", "obj_p", "visionScore", "util_p"]:
        values = [p.get(key, 0) for p in team]
        mean = sum(values) / len(team) if team else 0
        variance = sum((v - mean) ** 2 for v in values) / len(team) if team else 0
        std = math.sqrt(variance)
        stats[key] = {"mean": mean, "std": std or 1.0}
    return stats

def compute_oogscore(part: Dict, participants: List[Dict]) -> Tuple[int, Dict]:
    lane = part.get("teamPosition", "UNKNOWN")
    w = ROLE_WEIGHTS.get(lane, ROLE_WEIGHTS["UNKNOWN"])
    team_stats = compute_team_stats(participants, part["teamId"])
    if "kda_p" not in part:
        part["kda_p"] = (part["kills"] + part["assists"]) / max(1, part["deaths"])
    if "cs_p" not in part:
        part["cs_p"] = part.get("totalMinionsKilled", 0) + part.get("neutralMinionsKilled", 0)
    if "obj_p" not in part:
        part["obj_p"] = part.get("dragonKills", 0) + part.get("baronKills", 0) + part.get("towerKills", 0)
    if "util_p" not in part:
        part["util_p"] = part.get("totalHealOnTeammates", 0) + part.get("totalDamageShieldedOnTeammates", 0)

    kda_n = norm(part["kda_p"], team_stats["kda_p"]["mean"], team_stats["kda_p"]["std"])
    dmg_n = (0.6 * norm(part.get("totalDamageDealtToChampions", 0),
                        team_stats["totalDamageDealtToChampions"]["mean"],
                        team_stats["totalDamageDealtToChampions"]["std"]) +
             0.4 * norm(part.get("totalDamageTaken", 0),
                        team_stats["totalDamageTaken"]["mean"],
                        team_stats["totalDamageTaken"]["std"]))
    eco_n = (0.5 * norm(part.get("goldEarned", 0),
                        team_stats["goldEarned"]["mean"],
                        team_stats["goldEarned"]["std"]) +
             0.5 * norm(part["cs_p"],
                        team_stats["cs_p"]["mean"],
                        team_stats["cs_p"]["std"]))
    obj_n = norm(part["obj_p"], team_stats["obj_p"]["mean"], team_stats["obj_p"]["std"])
    vis_n = norm(part.get("visionScore", 0),
                 team_stats["visionScore"]["mean"],
                 team_stats["visionScore"]["std"])
    utl_n = norm(part["util_p"], team_stats["util_p"]["mean"], team_stats["util_p"]["std"])
    clt_n = clamp01(part.get("pentaKills", 0))

    scores = {"KDA": kda_n, "DMG": dmg_n, "ECO": eco_n, "OBJ": obj_n,
              "VIS": vis_n, "UTL": utl_n, "CLT": clt_n}
    total = 0.0
    breakdown = {}
    for k, v in scores.items():
        pts = v * w[k] * 100
        total += pts
        breakdown[k] = (v, w[k])
    return round(min(100.0, total)), breakdown


# ─── Timeline parsing (étendu) ────────────────────────────────────────────────
def parse_timeline(raw: Dict[str, Any], puuid: Optional[str] = None) -> Dict[str, Any]:
    """
    Parse timeline for key events.
    Now also tracks gold deficit per team over time for comeback detection.
    Returns:
        fb        : minute of first blood (int|None)
        ft        : minute of first tower  (int|None)
        mythic    : minute of first mythic (int|None)
        gold_diffs: list of (minute, gold_diff) where gold_diff = our_team - enemy_team
                    (positive = we were ahead, negative = we were behind)
        puuid_part_id: participant_id of the tracked player (for team assignment)
    """
    result: Dict[str, Any] = {
        "fb": None, "ft": None, "mythic": None,
        "gold_diffs": [],
        "puuid_part_id": None,
    }

    info = raw.get("info", {})
    frames = info.get("frames", [])
    participants_meta = info.get("participants", [])

    # Map puuid → participantId (1-indexed in timeline)
    puuid_to_part_id: Dict[str, int] = {}
    for pm in participants_meta:
        puuid_to_part_id[pm.get("puuid", "")] = pm.get("participantId", 0)

    if puuid and puuid in puuid_to_part_id:
        result["puuid_part_id"] = puuid_to_part_id[puuid]

    if not frames:
        return result

    for fr in frames:
        minute = fr.get("timestamp", 0) // 60000

        # Gold diffs: sum gold for each team
        part_frames = fr.get("participantFrames", {})
        if part_frames and result["puuid_part_id"] is not None:
            our_id = result["puuid_part_id"]
            # Team IDs: participants 1-5 = team 100, 6-10 = team 200
            our_team_ids = list(range(1, 6)) if our_id <= 5 else list(range(6, 11))
            enemy_team_ids = list(range(6, 11)) if our_id <= 5 else list(range(1, 6))

            our_gold = sum(
                part_frames.get(str(pid), {}).get("totalGold", 0)
                for pid in our_team_ids
            )
            enemy_gold = sum(
                part_frames.get(str(pid), {}).get("totalGold", 0)
                for pid in enemy_team_ids
            )
            result["gold_diffs"].append((minute, our_gold - enemy_gold))

        # Events
        for ev in fr.get("events", []):
            t = ev.get("timestamp", 0) // 60000
            et = ev.get("type")

            if et == "CHAMPION_KILL" and result["fb"] is None:
                result["fb"] = t
            elif et == "BUILDING_KILL" and ev.get("buildingType") == "TOWER" and result["ft"] is None:
                result["ft"] = t
            elif et == "ITEM_PURCHASED" and ev.get("itemId") in MYTHIC_ITEMS and result["mythic"] is None:
                result["mythic"] = t

    return result


def detect_comeback(gold_diffs: List[Tuple[int, int]], won: bool, deficit_threshold: int = 3000) -> bool:
    """
    Returns True if team was behind by deficit_threshold+ gold at some point
    but ultimately won the game.
    """
    if not won or not gold_diffs:
        return False
    # Check if there was a point where we were significantly behind
    min_diff = min(diff for _, diff in gold_diffs)
    return min_diff <= -deficit_threshold


# ─── Badges ───────────────────────────────────────────────────────────────────
def compute_badges(
    part: Dict[str, Any],
    info: Dict[str, Any],
    opponent: Optional[Dict[str, Any]],
    timeline: Dict[str, Any],
) -> List[str]:
    team = [p for p in info["participants"] if p["teamId"] == part["teamId"]]
    avg_dmg  = sum(p["totalDamageDealtToChampions"] for p in team) / len(team)
    avg_tank = sum(p["totalDamageTaken"] for p in team) / len(team)

    badges: List[str] = []

    # ── Badges existants ──────────────────────────────────────────────────
    if part["totalDamageDealtToChampions"] == max(p["totalDamageDealtToChampions"] for p in team):
        badges.append("🏆 Skadoosh")

    if timeline.get("fb") is not None and timeline["fb"] <= 3 and part["kills"] > 0:
        badges.append("🔪 Lightning Lotus")

    if timeline.get("ft") is not None and part.get("towerKills", 0) > 0:
        badges.append("💣 Bélier de Jade")

    if part["totalDamageDealtToChampions"] > 1.4 * avg_dmg:
        badges.append("🔥 Poing du Panda")

    if part["totalDamageTaken"] > 1.5 * avg_tank:
        badges.append("🛡️ Oogway Insight")

    vis = part.get("visionScore", 0)
    max_vis = max(p.get("visionScore", 0) for p in info["participants"])
    if vis >= 45 or vis == max_vis:
        badges.append("👁️ Œil de Grue")

    if opponent and part["goldEarned"] - opponent["goldEarned"] > 1000:
        badges.append("💰 Banquier de Jade")

    if timeline.get("mythic") is not None and timeline["mythic"] <= 9:
        badges.append("⚡ Parchemin Express")

    if part.get("pentaKills", 0) > 0:
        badges.append("🐉 Cinq Doigts du Wuxi")

    if part["deaths"] <= 2:
        kda = (part["kills"] + part["assists"]) / max(1, part["deaths"])
        if kda >= 5:
            badges.append("🧹 Maître kung-fu")

    # ── Nouveaux badges ───────────────────────────────────────────────────

    # 🏗️ Démolisseur — 3 tours ou plus détruites
    if part.get("turretKills", 0) >= 3 or part.get("towerKills", 0) >= 3:
        badges.append("🏗️ Démolisseur")

    # 🔄 Phénix de Jade — comeback (était à -3k gold ou plus et a gagné)
    gold_diffs = timeline.get("gold_diffs", [])
    if detect_comeback(gold_diffs, part["win"], deficit_threshold=3000):
        badges.append("🔄 Phénix de Jade")

    # 💚 Gardien du Temple — heal + shield prodigués ≥ 12 000
    heal_shield = (
        part.get("totalHealOnTeammates", 0)
        + part.get("totalDamageShieldedOnTeammates", 0)
    )
    if heal_shield >= 12000:
        badges.append("💚 Gardien du Temple")

    # 🌀 Tempête de Jade — ≥ 30 secondes de CC infligées
    # totalTimeCCDealt est en millisecondes dans certaines versions, en centisecondes dans d'autres
    # On gère les deux : si > 3000 c'est probablement en centisecondes (÷100), sinon en secondes
    cc_raw = part.get("totalTimeCCDealt", 0)
    cc_seconds = cc_raw / 1000 if cc_raw > 3000 else cc_raw
    if cc_seconds >= 30:
        badges.append("🌀 Tempête de Jade")

    return badges


def find_opponent(part: Dict[str, Any], parts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    lane = part.get("teamPosition", "")
    if not lane:
        return None
    return next((p for p in parts if p["teamId"] != part["teamId"] and p.get("teamPosition") == lane), None)


# ─── Runes & Spells helpers ───────────────────────────────────────────────────
def parse_runes(part: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Returns (keystone_id, primary_tree_id, secondary_tree_id)
    from part["perks"]["styles"].
    """
    perks = part.get("perks", {})
    styles = perks.get("styles", [])
    if not styles:
        return None, None, None

    primary_style = next((s for s in styles if s.get("description") == "primaryStyle"), None)
    sub_style     = next((s for s in styles if s.get("description") == "subStyle"), None)

    keystone_id      = None
    primary_tree_id  = primary_style["style"] if primary_style else None
    secondary_tree_id = sub_style["style"] if sub_style else None

    if primary_style:
        selections = primary_style.get("selections", [])
        if selections:
            keystone_id = selections[0].get("perk")

    return keystone_id, primary_tree_id, secondary_tree_id


def format_runes_line(part: Dict[str, Any]) -> str:
    """Returns a formatted string with rune names, e.g. 'Conquerant  |  Sorcellerie'"""
    keystone_id, primary_tree_id, secondary_tree_id = parse_runes(part)

    keystone_name, _ = get_rune_info(keystone_id) if keystone_id else (None, None)
    _, primary_icon  = get_rune_info(primary_tree_id) if primary_tree_id else (None, None)
    sec_name, _      = get_rune_info(secondary_tree_id) if secondary_tree_id else (None, None)

    # Fallback : utiliser l'arbre primaire si keystone pas trouvé
    if not keystone_name:
        keystone_name, _ = get_rune_info(primary_tree_id) if primary_tree_id else (None, None)

    parts = []
    if keystone_name:
        parts.append(f"**{keystone_name}**")
    if sec_name:
        parts.append(sec_name)

    return "  |  ".join(parts) if parts else "—"


def format_summoners_line(part: Dict[str, Any]) -> str:
    """Returns a formatted string with summoner spell names, e.g. 'Flash  ·  Ignite'"""
    s1_id = part.get("summoner1Id", 0)
    s2_id = part.get("summoner2Id", 0)

    s1_name, _ = get_summoner_info(s1_id) if s1_id else (None, None)
    s2_name, _ = get_summoner_info(s2_id) if s2_id else (None, None)

    # Emojis connus pour les sorts courants
    SPELL_EMOJI: Dict[str, str] = {
        "Flash":     "⚡",
        "Ignite":    "🔥",
        "Exhaust":   "🌀",
        "Heal":      "💚",
        "Barrier":   "🛡️",
        "Ghost":     "👻",
        "Cleanse":   "✨",
        "Teleport":  "🌀",
        "Smite":     "⚔️",
        "Clarity":   "💧",
        "Snowball":  "❄️",
    }

    def fmt(name: Optional[str]) -> str:
        if not name:
            return "?"
        emoji = SPELL_EMOJI.get(name, "🔮")
        return f"{emoji} {name}"

    s1_fmt = fmt(s1_name)
    s2_fmt = fmt(s2_name)
    return f"{s1_fmt}  ·  {s2_fmt}"


# ─── LP Delta ─────────────────────────────────────────────────────────────────
def lp_delta_between(prev: Tuple[str, str, int], cur: Tuple[str, str, int]) -> int:
    prev_t, prev_d, prev_lp = prev
    cur_t, cur_d, cur_lp = cur
    if not prev_t or prev_t == "Unranked":
        return 0
    if prev_t == cur_t:
        prev_div_num = DIV_NUM.get(prev_d, 4)
        cur_div_num  = DIV_NUM.get(cur_d, 4)
        if cur_div_num == prev_div_num:
            return cur_lp - prev_lp
        elif cur_div_num < prev_div_num:
            return (100 - prev_lp) + cur_lp
        else:
            return -(prev_lp + (100 - cur_lp))
    prev_tier_idx = TIER_INDEX.get(prev_t, 0)
    cur_tier_idx  = TIER_INDEX.get(cur_t, 0)
    if cur_tier_idx > prev_tier_idx:
        return (100 - prev_lp) + cur_lp
    else:
        return -(prev_lp + (100 - cur_lp))


def detect_rank_change(prev: Tuple[str, str, int], cur: Tuple[str, str, int]) -> Optional[str]:
    prev_t, prev_d, _ = prev
    cur_t, cur_d, _   = cur
    if not prev_t or prev_t == "Unranked":
        return None
    prev_tier_idx = TIER_INDEX.get(prev_t, 0)
    cur_tier_idx  = TIER_INDEX.get(cur_t, 0)
    if cur_tier_idx > prev_tier_idx:
        return f"promotion_tier:{cur_t} {cur_d}"
    elif cur_tier_idx < prev_tier_idx:
        return f"demotion_tier:{cur_t} {cur_d}"
    if prev_t == cur_t:
        prev_div_num = DIV_NUM.get(prev_d, 4)
        cur_div_num  = DIV_NUM.get(cur_d, 4)
        if cur_div_num < prev_div_num:
            return f"promotion_div:{cur_t} {cur_d}"
        elif cur_div_num > prev_div_num:
            return f"demotion_div:{cur_t} {cur_d}"
    return None


# ─── UI View ──────────────────────────────────────────────────────────────────
class HelpView(discord.ui.View):
    def __init__(self, badges: List[str], lane: str, oog: int, breakdown: Dict[str, Tuple[float, float]]):
        super().__init__(timeout=None)
        self.badges    = badges
        self.oog       = oog
        self.breakdown = breakdown

    @staticmethod
    def format_breakdown(bd: Dict[str, Tuple[float, float]]) -> str:
        labels = {"KDA": "KDA", "DMG": "Dégâts", "ECO": "Éco", "OBJ": "Obj",
                  "VIS": "Vis", "UTL": "Util", "CLT": "Clt"}
        lines = []
        total = 0.0
        for k, (v, w) in bd.items():
            pts = v * w * 100
            total += pts
            lines.append(f"• {labels.get(k, k):5}: {v:.2f} × {int(w*100)}% = {pts:.1f}")
        lines.append("─" * 20)
        lines.append(f"Total: **{total:.1f} pts**")
        return "\n".join(lines)

    @discord.ui.button(label="ℹ️ Badges ?", style=discord.ButtonStyle.secondary)
    async def show_badges(self, i: discord.Interaction, b: discord.ui.Button):
        if not self.badges:
            txt = "Aucun badge pour cette partie"
        else:
            txt = "\n".join(f"{x} — {BADGE_INFO.get(x, 'Badge inconnu')}" for x in self.badges)
        await i.response.send_message(txt, ephemeral=True, delete_after=15)

    @discord.ui.button(label="ℹ️ OogScore ?", style=discord.ButtonStyle.primary)
    async def show_oog(self, i: discord.Interaction, b: discord.ui.Button):
        header  = f"**OogScore {self.oog}/100**\n"
        content = self.format_breakdown(self.breakdown)
        await i.response.send_message(header + content, ephemeral=True, delete_after=15)


# ─── Cog ──────────────────────────────────────────────────────────────────────
class MatchAlertsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()
        self.db   = SessionLocal()
        self.riot = RiotClient(settings.RIOT_API_KEY)

        connector = aiohttp.TCPConnector(limit=50, limit_per_host=10)
        timeout   = aiohttp.ClientTimeout(total=30, connect=10)
        self.http = aiohttp.ClientSession(connector=connector, timeout=timeout)

        self.lp_cache: Dict[str, Dict[int, Tuple[str, str, int]]] = {}
        self.sem = asyncio.Semaphore(3)
        self._user_cache: Dict[int, discord.User] = {}

    async def cog_unload(self):
        await self.http.close()
        self.db.close()

    # ─── Redis state ──────────────────────────────────────────────────────
    async def _get_last_state(self, puuid: str, queue_id: int) -> Optional[Tuple[str, str, int]]:
        key = f"lp_last_state:{puuid}:{queue_id}"
        raw = await safe_r_get(key)
        if isinstance(raw, dict) and all(k in raw for k in ("tier", "div", "lp")):
            try:
                return str(raw["tier"]), str(raw["div"]), int(raw["lp"])
            except (ValueError, TypeError):
                pass
        return None

    async def _set_last_state(self, puuid: str, queue_id: int, state: Tuple[str, str, int]):
        key = f"lp_last_state:{puuid}:{queue_id}"
        await safe_r_set(key, {"tier": state[0], "div": state[1], "lp": int(state[2])}, ttl=90*24*3600)

    async def _get_last_seen_match(self, puuid: str) -> Optional[str]:
        value = await safe_r_get(f"last_seen_match:{puuid}")
        return str(value) if value else None

    async def _set_last_seen_match(self, puuid: str, mid: str):
        await safe_r_set(f"last_seen_match:{puuid}", mid, ttl=90*24*3600)

    async def _update_streak(self, puuid: str, queue_id: int, win: bool) -> Tuple[int, bool]:
        key = f"streak:{puuid}:{queue_id}"
        raw = await safe_r_get(key)
        streak_list = raw if isinstance(raw, list) else []
        streak_list.append("W" if win else "L")
        streak_list = streak_list[-10:]
        await safe_r_set(key, streak_list, ttl=90*24*3600)
        if not streak_list:
            return 0, True
        current_result = streak_list[-1]
        streak_count = 1
        for i in range(len(streak_list) - 2, -1, -1):
            if streak_list[i] == current_result:
                streak_count += 1
            else:
                break
        return streak_count, current_result == "W"

    async def _check_personal_records(self, puuid: str, champion: str, kda: float, cs: int, vision: int) -> List[str]:
        key = f"records:{puuid}:{champion}"
        raw = await safe_r_get(key)
        try:
            records = ({"kda": float(raw.get("kda", 0)), "cs": int(raw.get("cs", 0)), "vision": int(raw.get("vision", 0))}
                       if isinstance(raw, dict) else {"kda": 0.0, "cs": 0, "vision": 0})
        except (ValueError, TypeError):
            records = {"kda": 0.0, "cs": 0, "vision": 0}
        achievements, updated = [], False
        if kda > records["kda"]:
            achievements.append(f"🎖️ Nouveau record KDA sur {champion}: {kda:.2f}")
            records["kda"] = float(kda); updated = True
        if cs > records["cs"]:
            achievements.append(f"🌾 Record de CS sur {champion}: {cs}")
            records["cs"] = int(cs); updated = True
        if vision > records["vision"]:
            achievements.append(f"👁️ Record de vision sur {champion}: {vision}")
            records["vision"] = int(vision); updated = True
        if updated:
            await safe_r_set(key, records, ttl=365*24*3600)
        return achievements

    async def _get_total_games(self, puuid: str, queue_id: int) -> int:
        try:
            return self.db.query(Match).filter_by(puuid=puuid, queue_id=queue_id).count()
        except Exception:
            return 0

    async def _get_cached_user(self, discord_id: int) -> Optional[discord.User]:
        if discord_id in self._user_cache:
            return self._user_cache[discord_id]
        try:
            user = await self.bot.fetch_user(discord_id)
            self._user_cache[discord_id] = user
            if len(self._user_cache) > 100:
                for key in list(self._user_cache.keys())[:20]:
                    self._user_cache.pop(key, None)
            return user
        except Exception:
            return None

    @commands.Cog.listener()
    async def on_ready(self):
        log.info("Bot ready, loading DDragon data…")
        await ensure_ddragon_version(self.http)
        await ensure_runes_data(self.http)
        await ensure_summoners_data(self.http)
        if not self.poll_matches.is_running():
            # Délai avant le premier poll : laisse le bot finir son init
            # et évite la rafale de requêtes Riot au démarrage
            log.info("DDragon OK — démarrage poll dans 15s")
            await asyncio.sleep(15)
            self.poll_matches.start()

    @tasks.loop(minutes=5)
    async def poll_matches(self):
        users = self.db.query(User).all()
        log.info(f"Polling {len(users)} users")
        for u in users:
            try:
                await self.handle_user(u)
            except Exception as e:
                log.error(f"Error for user {u.discord_id}: {e}", exc_info=True)
                self.db.rollback()
            await asyncio.sleep(PER_USER_SLEEP)

    @with_retry(max_attempts=3, base_delay=1.0)
    async def _get_match_ids(self, user: User, n: int):
        return await self.riot.get_match_ids(user.region, user.puuid, n)

    @with_retry(max_attempts=3, base_delay=1.0)
    async def _get_match(self, user: User, mid: str):
        return await self.riot.get_match_by_id(user.region, mid)

    @with_retry(max_attempts=3, base_delay=1.0)
    async def _get_timeline(self, user: User, mid: str):
        return await self.riot.get_match_timeline_by_id(user.region, mid)

    async def handle_user(self, user: User):
        last_seen = await self._get_last_seen_match(user.puuid)
        try:
            latest_ids = await self._get_match_ids(user, 1) or []
        except Exception as e:
            log.warning(f"IDs(1) fail for {user.discord_id}: {e}"); return
        if not latest_ids:
            return
        newest = latest_ids[0]
        if newest == last_seen:
            return
        try:
            ids = await self._get_match_ids(user, 5) or []
        except Exception:
            ids = latest_ids
        if last_seen and last_seen in ids:
            to_process = ids[:ids.index(last_seen)]
        else:
            to_process = ids
        for mid in reversed(to_process):
            exists = self.db.query(Match).filter_by(match_id=mid, puuid=user.puuid).first()
            if exists:
                continue
            try:
                await self.process_match(user, mid)
                await self._set_last_seen_match(user.puuid, mid)
            except Exception as e:
                log.error(f"Failed to process match {mid} for {user.discord_id}: {e}", exc_info=True)

    async def process_match(self, user: User, mid: str):
        async with self.sem:
            match_data = await self._get_match(user, mid)
        info = match_data.get("info")
        if not info:
            return
        part = next((p for p in info["participants"] if p["puuid"] == user.puuid), None)
        if not part:
            return
        if info["queueId"] not in RANKED_QUEUES:
            return

        queue_id = info["queueId"]
        tier, div, lp_now, wr = await self._get_rank(user, queue_id)

        prev_state = self.lp_cache.get(user.puuid, {}).get(queue_id)
        if prev_state is None:
            prev_state = await self._get_last_state(user.puuid, queue_id)
        if prev_state is None:
            prev_state = (tier, div, lp_now)

        cur_state  = (tier, div, lp_now)
        lp_delta   = lp_delta_between(prev_state, cur_state)
        self.lp_cache.setdefault(user.puuid, {})[queue_id] = cur_state
        await self._set_last_state(user.puuid, queue_id, cur_state)

        streak_count, is_win_streak = await self._update_streak(user.puuid, queue_id, part["win"])
        rank_change = detect_rank_change(prev_state, cur_state)

        now      = int(time.time())
        hist_key = f"lp_hist:{user.puuid}:{queue_id}"
        raw      = await safe_r_get(hist_key)
        try:
            hist = {int(k): int(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        except (ValueError, TypeError):
            hist = {}
        hist[now] = int(lp_now)
        thirty_days = 30 * 24 * 3600
        hist = {t: v for t, v in hist.items() if (now - t) <= thirty_days}
        await safe_r_set(hist_key, {str(t): v for t, v in hist.items()}, ttl=thirty_days)

        opponent  = find_opponent(part, info["participants"])
        gold_diff = part["goldEarned"] - (opponent["goldEarned"] if opponent else 0)
        exp_diff  = part.get("champExperience", 0) - (opponent.get("champExperience", 0) if opponent else 0)

        # Timeline — pass puuid for comeback detection
        async with self.sem:
            timeline_data = await self._get_timeline(user, mid)
        timeline = parse_timeline(timeline_data, puuid=user.puuid)
        badges   = compute_badges(part, info, opponent, timeline)

        # Duo
        duo_names: List[str] = []
        if queue_id == 420:
            same_team_puuids = {p["puuid"] for p in info["participants"]
                                if p["teamId"] == part["teamId"] and p["puuid"] != user.puuid}
            if same_team_puuids:
                for mate in self.db.query(User).filter(User.puuid.in_(same_team_puuids)).all():
                    du = await self._get_cached_user(mate.discord_id)
                    duo_names.append(du.display_name if du else mate.puuid[:6])

        kda_value    = (part["kills"] + part["assists"]) / max(1, part["deaths"])
        cs_value     = part.get("totalMinionsKilled", 0) + part.get("neutralMinionsKilled", 0)
        vision_value = part.get("visionScore", 0)
        personal_records = await self._check_personal_records(user.puuid, part["championName"], kda_value, cs_value, vision_value)

        lp_values = [lp for _, lp in sorted(hist.items())[-10:]]

        # Persist
        match = Match(
            match_id=mid, puuid=user.puuid, queue_id=info["queueId"],
            win=part["win"],
            timestamp=dt.datetime.fromtimestamp(info["gameStartTimestamp"] / 1000),
        )
        self.db.add(match)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            log.info(f"Match {mid} already in DB")
            return

        await self._send_embed(
            user, info, part, tier, div, lp_now, lp_delta, wr,
            gold_diff, exp_diff, badges,
            opponent["championName"] if opponent else "?",
            duo_names, streak_count, is_win_streak, rank_change,
            personal_records, lp_values
        )

    async def _get_rank(self, user: User, queue_id: int) -> Tuple[str, str, int, int]:
        entries = await self.riot.get_league_entries_by_puuid(user.region, user.puuid)
        qtype   = QUEUE_TYPE.get(queue_id)
        ent     = next((e for e in entries if e["queueType"] == qtype), None)
        if ent is None:
            return "Unranked", "", 0, 0
        wins, losses = ent.get("wins", 0), ent.get("losses", 0)
        wr = int(wins / max(1, wins + losses) * 100)
        return ent["tier"].title(), ent["rank"], int(ent["leaguePoints"]), wr

    async def _send_embed(
        self, user: User, info: Any, part: Any, tier: str, div: str, lp: int,
        lp_delta: int, wr: int, gold_diff: int, exp_diff: int, badges: List[str],
        opp_champ: str, duo_names: List[str], streak_count: int, is_win_streak: bool,
        rank_change: Optional[str], personal_records: List[str], lp_values: List[int]
    ):
        channel = self.bot.get_channel(settings.ALERT_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(settings.ALERT_CHANNEL_ID)

        # Special notifications
        special_notification = ""
        if rank_change:
            if rank_change.startswith("promotion"):
                _, rank_str = rank_change.split(":", 1)
                special_notification = f"🎉 **PROMOTION !** Bienvenue en {rank_str} !\n"
            elif rank_change.startswith("demotion"):
                _, rank_str = rank_change.split(":", 1)
                special_notification = f"📉 Demotion en {rank_str}\n"

        total_games = await self._get_total_games(user.puuid, info["queueId"])
        if total_games == 100:
            special_notification += "🎯 **100 parties jouées !**\n"
        if wr == 50 and total_games >= 10:
            special_notification += "⚖️ **50% de winrate atteint !**\n"

        # Description
        description_parts = [
            f"**{RANKED_QUEUES[info['queueId']]}** · "
            f"{dt.timedelta(seconds=info['gameDuration'])} · "
            f"{ROLE_EMOJI.get(part.get('teamPosition', 'UNKNOWN'))}"
        ]
        if streak_count >= 3:
            abs_delta = abs(lp_delta)
            if is_win_streak:
                description_parts.append(f"\n🔥 **{streak_count} victoires d'affilée !** +{abs_delta} LP")
            else:
                description_parts.append(f"\n❄️ **Série noire : {streak_count} défaites** -{abs_delta} LP")

        # ── Sorts d'invocateur ────────────────────────────────────────────
        summoners_line = format_summoners_line(part)

        # ── Runes ─────────────────────────────────────────────────────────
        runes_line = format_runes_line(part)

        embed = discord.Embed(
            color=0x2ECC71 if part["win"] else 0xE74C3C,
            description=special_notification + "".join(description_parts),
            timestamp=dt.datetime.fromtimestamp(info["gameEndTimestamp"] // 1000)
        )

        du = await self._get_cached_user(user.discord_id)
        name = du.display_name if du else user.puuid[:6]
        champ_icon = f"https://ddragon.leagueoflegends.com/cdn/{ddragon.version}/img/champion/{part['championName']}.png"
        prof_icon  = f"https://ddragon.leagueoflegends.com/cdn/{ddragon.version}/img/profileicon/{part.get('profileIcon', 0)}.png"

        outcome = "Victoire" if part["win"] else "Défaite"
        if duo_names:
            outcome += f" (Duo avec {', '.join(duo_names)})"

        embed.set_author(name=f"{name} — {outcome}", icon_url=champ_icon)
        embed.set_thumbnail(url=prof_icon)

        # ── Rang ──────────────────────────────────────────────────────────
        pct    = lp % 100
        filled = int(pct / (100 / LP_BAR_LEN))
        bar    = "█" * filled + "░" * (LP_BAR_LEN - filled)
        rank_value = f"{tier} {div}\n{lp} LP ({lp_delta:+})\n{bar}\n{wr}% WR"
        if pct >= 75 and tier not in ["Master", "Grandmaster", "Challenger"]:
            lp_needed    = 100 - pct
            wins_needed  = max(1, int(lp_needed / 20))
            rank_value  += f"\n🎯 Promo dans {lp_needed} LP ({wins_needed} victoire(s))"
        if pct == 0 and not part["win"] and tier not in ["Master", "Grandmaster", "Challenger"]:
            rank_value += "\n⚠️ Attention demotion ! (0 LP)"
        embed.add_field(name="Rank", value=rank_value, inline=True)

        # ── Stats ─────────────────────────────────────────────────────────
        stats_value = (
            f"{EM_KDA} **{part['kills']}/{part['deaths']}/{part['assists']}**\n"
            f"{EM_GOLD} ΔGold **{gold_diff:+}** · ΔXP **{exp_diff:+}**"
        )
        embed.add_field(name="Stats", value=stats_value, inline=True)
        embed.add_field(name="Vision", value=f"{EM_VISION} {part.get('visionScore', 0)}", inline=True)

        cs = part.get("totalMinionsKilled", 0) + part.get("neutralMinionsKilled", 0)
        cs_per_min = cs / (info["gameDuration"] / 60)
        embed.add_field(name="CS", value=f"{EM_CS} {cs} ({cs_per_min:.1f}/min)", inline=True)

        # ── Sorts d'invocateur + Runes (même champ) ───────────────────────
        embed.add_field(
            name="🧙 Sorts & Runes",
            value=f"{summoners_line}\n{runes_line}",
            inline=True,
        )

        # ── OogScore ──────────────────────────────────────────────────────
        oog, breakdown = compute_oogscore(part, info["participants"])
        all_oogscores  = [(p["puuid"], compute_oogscore(p, info["participants"])[0])
                          for p in info["participants"]]
        all_oogscores.sort(key=lambda x: x[1], reverse=True)
        player_rank = next((i + 1 for i, (puuid, _) in enumerate(all_oogscores)
                            if puuid == user.puuid), 0)

        if oog < 40:   emo, label = "🟥", "Grue bancale"
        elif oog < 70: emo, label = "🟨", "Apprenti"
        elif oog < 90: emo, label = "🟩", "Maître du Jade"
        else:          emo, label = "🟦", "Skadoosh"

        oog_value = f"{emo} **{oog}/100** — {label}"
        if player_rank == 1:
            oog_value += "\n🏆 **MVP de la game !**"
        elif player_rank <= 3:
            oog_value += f"\n🥉 Top 3 de la game (#{player_rank})"

        embed.add_field(name="OogScore", value=oog_value, inline=True)

        # ── Badges ────────────────────────────────────────────────────────
        embed.add_field(name="Badges", value=" · ".join(badges) or "—", inline=False)

        # ── Records personnels ────────────────────────────────────────────
        if personal_records:
            embed.add_field(name="Records Personnels", value="\n".join(personal_records), inline=False)

        # ── Build sprite ──────────────────────────────────────────────────
        files_to_send = []
        sprite = await make_sprite([part.get(f"item{i}", 0) for i in range(7)], self.http)
        if sprite:
            files_to_send.append(sprite)
            embed.set_image(url="attachment://build.png")

        # ── Footer sparkline ──────────────────────────────────────────────
        if lp_values and len(lp_values) >= 2:
            embed.set_footer(text=f"Partie #{total_games} | {create_sparkline_lp(lp_values)}")
        else:
            embed.set_footer(text=f"Partie #{total_games}")

        view = HelpView(badges, part.get("teamPosition", "UNKNOWN"), oog, breakdown)
        await channel.send(embed=embed, files=files_to_send, view=view, delete_after=172800)

    @app_commands.command(name="alerts_test", description="Force un poll immédiat")
    async def alerts_test(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.poll_matches()
        await interaction.followup.send("✅ Poll exécuté !", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MatchAlertsCog(bot))
