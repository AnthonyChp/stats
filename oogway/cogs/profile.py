# =============================================================
# oogway/cogs/profile.py
# -------------------------------------------------------------
# /profil – Fiche League of Legends
# Pages :
#   0. Résumé            3. Courbe LP 30 j
#   1. Dernière partie   4. Synergie mates
#   2. (Heat-map, etc.)  ← slots libres pour la suite
# =============================================================

from __future__ import annotations
import io, json, time, datetime as dt
from collections import Counter
from typing import Dict, List

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from PIL import Image
import requests
import urllib.parse as _uq
import redis.exceptions as _redis_exc

from oogway.config import settings
from oogway.database import SessionLocal, User
from oogway.riot.client import RiotClient

# =============================================================
# ------------------------ Redis ------------------------------
# =============================================================
try:
    import redis.asyncio as aioredis
    REDIS = aioredis.from_url(
        getattr(settings, "REDIS_URL", "redis://localhost:6379/0"),
        encoding="utf-8", decode_responses=True
    )
except ModuleNotFoundError:                                 # fallback dev
    class _Mem(dict):
        async def get(s, k): return super().get(k)
        async def set(s, k, v, ex=None): super().__setitem__(k, v)
        async def delete(s, k): super().__delitem__(k)
    REDIS = _Mem()

async def r_get(key):
    try:
        raw = await REDIS.get(key)
    except _redis_exc.ResponseError:
        await REDIS.delete(key); return None
    return json.loads(raw or "null")

async def r_set(key, value, ttl=3600):
    data = json.dumps(value)
    try:
        await REDIS.set(key, data, ex=ttl)
    except _redis_exc.ResponseError:
        await REDIS.delete(key); await REDIS.set(key, data, ex=ttl)

# =============================================================
# --------------------- Riot / constantes ---------------------
# =============================================================
RIOT   = RiotClient(settings.RIOT_API_KEY)
REGION = getattr(settings, "DEFAULT_REGION",
                 getattr(settings, "RIOT_REGION", "EUW1"))

TIER_COLOR = {"IRON":0x484d50,"BRONZE":0xcd7f32,"SILVER":0x9ea9b3,
              "GOLD":0xe7b71d,"PLATINUM":0x27e2a4,"EMERALD":0x2cd97d,
              "DIAMOND":0x5ab4ff,"MASTER":0x9e4aff,"GRANDMASTER":0xff4747,
              "CHALLENGER":0x009df6}
EMOJI_TIER = {"IRON":"♙","BRONZE":"♘","SILVER":"♗","GOLD":"♖","PLATINUM":"♕",
              "EMERALD":"♔","DIAMOND":"💎","MASTER":"🔮","GRANDMASTER":"🟥",
              "CHALLENGER":"🏆"}
ROLE_EMOJI = {"TOP":"🛡️ Top","JUNGLE":"🌲 Jungle","MIDDLE":"🎯 Mid",
              "BOTTOM":"🏹 ADC","UTILITY":"✨ Support","NONE":"❔"}

# palette LoL
COLOR_BG   = discord.Colour.from_rgb(26, 35, 46)   # #1a232e
COLOR_GOLD = discord.Colour.from_rgb(200, 155, 60) # #c89b3c

DEV_GUILD_ID = getattr(settings, "DEBUG_GUILD_ID", None)

# =============================================================
# ---------------------- Helpers divers -----------------------
# =============================================================
def fig_to_file(fig, name) -> discord.File:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor="#0d1117")
    plt.close(fig); buf.seek(0)
    return discord.File(buf, filename=name)

# ---------- DD Dragon version & sprites ----------------------
_DDRAGON_VER = None; SPRITE_SIZE = 32
def _ensure_dd_version() -> str:
    global _DDRAGON_VER
    if _DDRAGON_VER is None:
        _DDRAGON_VER = requests.get(
            "https://ddragon.leagueoflegends.com/api/versions.json", timeout=5
        ).json()[0]
    return _DDRAGON_VER

def make_sprite_sync(item_ids: list[int]) -> discord.File | None:
    icons: list[Image.Image] = []
    ver = _ensure_dd_version()
    for iid in item_ids:
        if not iid: continue
        url = f"https://ddragon.leagueoflegends.com/cdn/{ver}/img/item/{iid}.png"
        try:
            data = requests.get(url, timeout=5).content
            img = Image.open(io.BytesIO(data)).convert("RGBA")
            icons.append(img.resize((SPRITE_SIZE, SPRITE_SIZE)))
        except Exception:
            pass
    if not icons:
        return None
    sprite = Image.new("RGBA", (SPRITE_SIZE*len(icons), SPRITE_SIZE))
    for i, ic in enumerate(icons):
        sprite.paste(ic, (i*SPRITE_SIZE, 0), ic)
    buf = io.BytesIO(); sprite.save(buf, "PNG"); buf.seek(0)
    return discord.File(buf, filename="build.png")

# ---------- QuickChart pour la courbe LP --------------------
def quickchart_lp(lp_hist: dict[int, int]) -> str:
    data = sorted(lp_hist.items())
    labels = [dt.datetime.fromtimestamp(k).strftime('%d %b') for k, _ in data]
    values = [v for _, v in data]
    cfg = {
        "type": "line",
        "data": {"labels": labels,
                 "datasets": [{
                     "data": values,
                     "borderColor": "#c89b3c",
                     "borderWidth": 3,
                     "fill": False,
                     "tension": .35,
                     "pointRadius": 0}]},
        "options": {
            "backgroundColor": "#0d1117",
            "layout": {"padding": 14},
            "plugins": {"legend": {"display": False}},
            "scales": {
                "x": {"ticks": {"color": "#f0f0f0", "maxRotation": 0},
                      "grid": {"display": False}},
                "y": {"ticks": {"color": "#f0f0f0"},
                      "grid": {"color": "rgba(255,255,255,0.08)"}}
            }
        }
    }
    return f"https://quickchart.io/chart?c={_uq.quote(json.dumps(cfg), safe='')}"

# =============================================================
# ----------------- API wrapper helper calls ------------------
# =============================================================
async def fetch_ranked(puid):
    key=f"ranked:{puid}"; d=await r_get(key)
    if d is None:
        d=RIOT.get_league_entries_by_puuid(REGION, puid); await r_set(key,d)
    return {q["queueType"]:q for q in d}

async def fetch_match(mid):
    key=f"match:{mid}"; m=await r_get(key)
    if m is None:
        m=RIOT.get_match_by_id(REGION, mid); await r_set(key,m)
    return m

async def fetch_mastery(puid):
    key=f"mastery:{puid}"; top=await r_get(key)
    if top is None:
        url=(f"https://{REGION.lower()}.api.riotgames.com"
             f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puid}/top")
        top=RIOT._request(url)[:5]; await r_set(key,top,86400)
    cmap=await r_get("champ_map")
    if cmap is None:
        ver=_ensure_dd_version()
        data=requests.get(
            f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/champion.json"
        ).json()
        cmap={int(v["key"]):v["id"] for v in data["data"].values()}
        await r_set("champ_map",cmap,604800)
    for d in top: d["championName"]=cmap.get(d["championId"], str(d["championId"]))
    return top

# =============================================================
# ----------------------- Cog Profile -------------------------
# =============================================================
class ProfileCog(commands.Cog):
    def __init__(self, bot): self.bot=bot

    # -------- Slash /profil ---------------------------------
    @app_commands.command(name="profil", description="Fiche LoL")
    @app_commands.guilds(DEV_GUILD_ID) if DEV_GUILD_ID else (lambda f: f)
    async def profil(self, inter: Interaction, pseudo: str | None = None):
        await inter.response.defer(thinking=True)
        puid, name = await self._resolve(inter, pseudo)
        if not puid: return

        ranked   = await fetch_ranked(puid)
        solo,flex= ranked.get("RANKED_SOLO_5x5"), ranked.get("RANKED_FLEX_SR")
        mids     = RIOT.get_match_ids(REGION, puid, 20)
        matches  = [await fetch_match(mid) for mid in mids]

        roles = Counter(); w=l=0
        vision_sum=wards_p=wards_k=0
        for m in matches:
            p = next(pl for pl in m["info"]["participants"] if pl["puuid"]==puid)
            roles[p["teamPosition"] or "NONE"] += 1
            w += p["win"]; l += (not p["win"])
            vision_sum += p.get("visionScore",0)
            wards_p    += p.get("wardsPlaced",0)
            wards_k    += p.get("wardsKilled",0)

        mastery  = await fetch_mastery(puid)
        lp_hist  = await r_get(f"lp_hist:{puid}") or {}
        mates    = self._mates(matches, puid)

        embeds, page2file = self._embeds(
            name, solo, flex, roles, w, l,
            matches, mastery, lp_hist, mates,
            vision_sum, wards_p, wards_k, puid
        )

        view=Pager(embeds, page2file)
        msg=await inter.followup.send(
            embed=embeds[0], view=view, files=page2file.get(0,[])
        )
        view.message=msg

    # ---------------------- Utils --------------------------
    async def _resolve(self, inter, pseudo):
        if pseudo:
            try: ign, tag = pseudo.split("#")
            except ValueError:
                await inter.followup.send("Format : Pseudo#TAG", ephemeral=True)
                return None,None
            acc=RIOT.get_account_by_name_tag(REGION, ign, tag)
            return acc["puuid"], acc["gameName"]
        with SessionLocal() as sess:
            u=sess.get(User, str(inter.user.id))
        if not u:
            await inter.followup.send("🔗 Utilise `/link`.", ephemeral=True)
            return None,None
        return u.puuid, u.summoner_name

    def _mates(self, matches, puid):
        c=Counter()
        for m in matches:
            for p in m["info"]["participants"]:
                if p["puuid"]!=puid: c[p["summonerName"]]+=1
        return c.most_common(3)

    # ------------------ Embeds builder ---------------------
    def _embeds(self, name, solo, flex, roles, w, l,
                matches, mastery, lp_hist, mates,
                vision_sum, wards_p, wards_k, puid):
        embeds: List[discord.Embed]=[]
        page2file: Dict[int,List[discord.File]]={}

        # ---------------------------------------------------
        # Page 0 : Résumé
        # ---------------------------------------------------
        e0=discord.Embed(
            title=f"**{EMOJI_TIER.get(solo['tier'] if solo else '', '❔')} {name}**",
            colour=COLOR_BG
        )
        e0.description=f"**{w}-{l}** sur 20 parties ({w*100/(w+l or 1):.1f}% WR)"

        # --- streak Solo/Duo uniquement ---------------------------------
        streak = 0  # >0 = série de wins, <0 = série de loses
        first = True  # sert à initialiser le signe

        for g in matches:
            if g["info"].get("queueId") != 420:  # on saute tout sauf la SoloQ
                continue

            win = next(p for p in g["info"]["participants"]
                       if p["puuid"] == puid)["win"]

            if first:  # initialise la série
                streak = 1 if win else -1
                first = False
            else:
                # Si le résultat suit la même tendance, on allonge la série
                if (win and streak > 0) or (not win and streak < 0):
                    streak += 1 if win else -1
                else:  # tendance cassée → stop
                    break

            if abs(streak) == 10:  # on ne va pas au-delà de 10
                break

        # n’affiche que si la série ≥ 2
        if abs(streak) >= 2:
            arrow = "🔥" if streak > 0 else "💤"
            e0.description += f"\n{arrow} **{abs(streak)}** de suite (SoloQ)"

        fmt=lambda r:"Unranked" if not r else f"{r['tier'].title()} {r['rank']} • {r['leaguePoints']} LP"
        e0.add_field(name="SoloQ", value=fmt(solo), inline=True)
        e0.add_field(name="Flex",  value=fmt(flex), inline=True)

        tot=sum(roles.values()) or 1
        e0.add_field(
            name="Rôles",
            value="```\n"+ "\n".join(
                f"{ROLE_EMOJI[r]:8} {roles[r]*100/tot:3.0f}%"
                for r in ROLE_EMOJI if r!='NONE'
            )+"```",
            inline=False
        )

        if matches:
            avg_vs = vision_sum/len(matches)
            e0.add_field(
                name="Vision",
                value=f"👁️ {avg_vs:.1f} VS / game\n🔧 {wards_p} placés • {wards_k} détruits",
                inline=False
            )

        top="\n".join(
            f"**{m['championName']}** – {m['championLevel']}★ ({m['championPoints']:,})"
            for m in mastery
        )
        e0.add_field(name="Top 5 Maîtrise", value=top or "—", inline=False)
        embeds.append(e0)

        # ---------------------------------------------------
        # Page 1 : Dernière partie
        # ---------------------------------------------------
        if matches:
            last=matches[0]
            p=next(pl for pl in last["info"]["participants"] if pl["puuid"]==puid)
            team=next(t for t in last["info"]["teams"] if t["teamId"]==p["teamId"])
            tower_kills  = team["objectives"]["tower"]["kills"]
            dragon_kills = team["objectives"]["dragon"]["kills"]

            e1=discord.Embed(
                title="**Dernière partie**",
                colour=0x2ECC71 if p["win"] else 0xE74C3C,
                description=(
                    f"**{dt.timedelta(seconds=last['info']['gameDuration'])}** — "
                    f"{ROLE_EMOJI.get(p.get('teamPosition') or 'NONE','❔')}"
                )
            )
            cs = p.get("totalMinionsKilled",0)+p.get("neutralMinionsKilled",0)
            kda=f"**{p['kills']}/{p['deaths']}/{p['assists']}**"
            dps=p['totalDamageDealtToChampions']
            e1.add_field(name="Stats",
                         value=f"⚔️ {kda}\n💰 {p['goldEarned']} po\n🌾 {cs} cs\n🩸 {dps} dégâts",
                         inline=True)
            e1.add_field(name="Vision & objectifs",
                         value=f"👁️ {p.get('visionScore',0)}\n🏰 {tower_kills} tours\n🐉 {dragon_kills} drakes",
                         inline=True)
            sprite=make_sprite_sync([p.get(f"item{i}",0) for i in range(7)])
            if sprite:
                e1.set_image(url="attachment://build.png")
                page2file[1]=[sprite]
            champ_url=(f"https://ddragon.leagueoflegends.com/cdn/{_ensure_dd_version()}/"
                       f"img/champion/{p['championName']}.png")
            e1.set_thumbnail(url=champ_url)
            embeds.append(e1)

        # ---------------------------------------------------
        # Page 2 : Courbe LP
        # ---------------------------------------------------
        lp_hist_int={int(k):v for k,v in lp_hist.items()}
        e2=discord.Embed(title="**Courbe LP – 30 jours**", colour=COLOR_GOLD)
        if len(lp_hist_int)>=2:
            lp_url=quickchart_lp(lp_hist_int)
            e2.set_image(url=lp_url)
            delta=list(lp_hist_int.values())[-1]-list(lp_hist_int.values())[0]
            arrow="▲" if delta>=0 else "▼"
            e2.set_footer(text=f"{arrow} {abs(delta)} LP sur 30 jours")
        else:
            e2.description="Aucune donnée enregistrée pour le moment 💤"
        embeds.append(e2)

        # ---------------------------------------------------
        # Page 3 : Synergie mates (statique)
        # ---------------------------------------------------
        e3=discord.Embed(title="**Synergie – Mates fréquents**",
                         colour=discord.Colour.dark_teal())
        if mates:
            e3.description="\n".join(f"• **{n}** – {c} games" for n,c in mates)
        else:
            e3.description="Aucun mate récurrent dans les 20 dernières parties."
        embeds.append(e3)

        return embeds, page2file

# =============================================================
# ----------------------- Pager -------------------------------
# =============================================================
class Pager(discord.ui.View):
    def __init__(self, embeds, page2file):
        super().__init__(timeout=120)
        self.embeds=embeds; self.page2file=page2file
        self.page=0; self.message=None

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: Interaction, _):
        self.page=(self.page-1)%len(self.embeds); await self._refresh(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: Interaction, _):
        self.page=(self.page+1)%len(self.embeds); await self._refresh(interaction)

    async def _refresh(self, interaction: Interaction):
        await interaction.response.edit_message(
            embed=self.embeds[self.page],
            view=self,
            attachments=self.page2file.get(self.page, [])
        )

    async def on_timeout(self):
        for child in self.children: child.disabled=True
        if self.message: await self.message.edit(view=self)

# =============================================================
# ---------------------- setup -------------------------------
# =============================================================
async def setup(bot):
    await bot.add_cog(ProfileCog(bot))
