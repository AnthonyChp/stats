# =============================================================
# oogway/cogs/profile.py
# -------------------------------------------------------------
# /profil – Fiche League of Legends
# Pages :
#   0. Résumé            3. Courbe LP 30 j (NOUVEAU DESIGN)
#   1. Dernière partie   4. Synergie mates
#   2. Heat-map perf     5. (slot libre)
# =============================================================

from __future__ import annotations
import io, json, time, datetime as dt
from collections import Counter
from typing import Dict, List

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.dates import DateFormatter

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

# palette LoL moderne pour les graphiques
BG_COLOR = '#0a1428'
GRID_COLOR = '#1e2d3d'
GOLD_COLOR = '#c89b3c'
WIN_COLOR = '#2ecc71'
LOSS_COLOR = '#e74c3c'
TEXT_COLOR = '#f0f0f0'
ACCENT_COLOR = '#785a28'

# Couleurs Discord
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

# =============================================================
# ------------- Graphiques modernes ---------------------------
# =============================================================

def create_modern_lp_curve(lp_hist: dict, matches: list, puuid: str) -> discord.File:
    """
    Crée une courbe LP moderne avec :
    - Design dark LoL
    - Gradient de fond
    - Marqueurs victoire/défaite
    - Stats intégrées
    - Trend line
    """
    if not lp_hist or len(lp_hist) < 2:
        return None
    
    # Préparer les données
    data = sorted(lp_hist.items())
    timestamps = [dt.datetime.fromtimestamp(int(k)) for k, _ in data]
    lp_values = [v for _, v in data]
    
    # Calculer la trend line
    x_numeric = np.arange(len(lp_values))
    z = np.polyfit(x_numeric, lp_values, 1)
    trend_line = np.poly1d(z)
    
    # Stats
    lp_start = lp_values[0]
    lp_end = lp_values[-1]
    lp_delta = lp_end - lp_start
    lp_max = max(lp_values)
    lp_min = min(lp_values)
    lp_range = lp_max - lp_min if lp_max != lp_min else 1
    
    # Extraire résultats des 20 dernières games (pour marqueurs)
    game_results = []
    for match in matches[:20]:
        info = match.get("info", {})
        game_time = info.get("gameEndTimestamp", info.get("gameCreation", 0)) / 1000
        
        # Trouver le participant
        part = next((p for p in info.get("participants", []) 
                    if p.get("puuid") == puuid), None)
        
        if part and info.get("queueId") == 420:  # Solo/Duo uniquement
            game_results.append({
                "time": dt.datetime.fromtimestamp(game_time),
                "win": part.get("win", False)
            })
    
    # === CRÉATION DU GRAPHIQUE ===
    fig, ax = plt.subplots(figsize=(12, 6), facecolor=BG_COLOR)
    ax.set_facecolor(BG_COLOR)
    
    # Gradient de fond subtil
    y_gradient = np.linspace(0, 1, 100).reshape(-1, 1)
    gradient = np.hstack([y_gradient] * 100)
    extent = [timestamps[0], timestamps[-1], lp_min - lp_range * 0.1, lp_max + lp_range * 0.1]
    ax.imshow(gradient, extent=extent, aspect='auto', alpha=0.03, 
              cmap='YlOrBr', origin='lower')
    
    # Zone de promo (si proche de 100 LP)
    if any(lp >= 75 for lp in lp_values):
        promo_line = 100
        ax.axhline(y=promo_line, color=GOLD_COLOR, linestyle='--', 
                   linewidth=1.5, alpha=0.3, label='Promo')
        ax.fill_between(timestamps, promo_line, lp_max + lp_range * 0.1, 
                       color=GOLD_COLOR, alpha=0.05)
    
    # Trend line (pointillés)
    ax.plot(timestamps, trend_line(x_numeric), color=ACCENT_COLOR, 
            linestyle=':', linewidth=2, alpha=0.6, label='Tendance')
    
    # Courbe LP principale avec glow effect
    for i in range(3):
        alpha = 0.1 * (3 - i)
        width = 4 + i * 2
        ax.plot(timestamps, lp_values, color=GOLD_COLOR, 
                linewidth=width, alpha=alpha, solid_capstyle='round')
    
    # Courbe principale
    ax.plot(timestamps, lp_values, color=GOLD_COLOR, 
           linewidth=3, marker='o', markersize=6, 
           markeredgecolor='white', markeredgewidth=1.5,
           label='LP', zorder=5)
    
    # Marqueurs victoires/défaites sur la courbe
    for game in game_results:
        # Trouver le point LP le plus proche dans le temps
        closest_idx = min(range(len(timestamps)), 
                         key=lambda i: abs((timestamps[i] - game["time"]).total_seconds()))
        
        if closest_idx < len(timestamps):
            marker_color = WIN_COLOR if game["win"] else LOSS_COLOR
            marker = '^' if game["win"] else 'v'
            ax.scatter(timestamps[closest_idx], lp_values[closest_idx], 
                      s=100, c=marker_color, marker=marker, 
                      edgecolors='white', linewidths=1.5, zorder=10, alpha=0.8)
    
    # Annoter les points extrêmes
    max_idx = lp_values.index(lp_max)
    min_idx = lp_values.index(lp_min)
    
    ax.annotate(f'{lp_max} LP', 
                xy=(timestamps[max_idx], lp_max),
                xytext=(0, 15), textcoords='offset points',
                ha='center', fontsize=9, color=WIN_COLOR,
                bbox=dict(boxstyle='round,pad=0.3', facecolor=BG_COLOR, 
                         edgecolor=WIN_COLOR, alpha=0.8),
                arrowprops=dict(arrowstyle='->', color=WIN_COLOR, lw=1.5))
    
    if lp_min != lp_max:
        ax.annotate(f'{lp_min} LP', 
                    xy=(timestamps[min_idx], lp_min),
                    xytext=(0, -15), textcoords='offset points',
                    ha='center', fontsize=9, color=LOSS_COLOR,
                    bbox=dict(boxstyle='round,pad=0.3', facecolor=BG_COLOR, 
                             edgecolor=LOSS_COLOR, alpha=0.8),
                    arrowprops=dict(arrowstyle='->', color=LOSS_COLOR, lw=1.5))
    
    # Styling des axes
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['bottom'].set_color(GRID_COLOR)
    ax.spines['left'].set_color(GRID_COLOR)
    ax.spines['bottom'].set_linewidth(2)
    ax.spines['left'].set_linewidth(2)
    
    # Grille subtile
    ax.grid(True, alpha=0.15, color=GRID_COLOR, linestyle='-', linewidth=1)
    ax.set_axisbelow(True)
    
    # Labels et titres
    ax.set_xlabel('Date', fontsize=11, color=TEXT_COLOR, fontweight='bold')
    ax.set_ylabel('LP', fontsize=11, color=TEXT_COLOR, fontweight='bold')
    
    # Format des dates
    ax.xaxis.set_major_formatter(DateFormatter('%d %b'))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right')
    
    # Couleur des ticks
    ax.tick_params(colors=TEXT_COLOR, which='both', labelsize=9)
    
    # Titre avec stats
    delta_symbol = '▲' if lp_delta >= 0 else '▼'
    
    title = f'Progression LP - Dernier mois'
    ax.text(0.5, 1.08, title, transform=ax.transAxes, 
            fontsize=14, fontweight='bold', color=GOLD_COLOR, 
            ha='center', va='top')
    
    # Sous-titre avec delta
    subtitle = f'{delta_symbol} {abs(lp_delta):+.0f} LP  •  Range: {lp_min}-{lp_max} LP'
    ax.text(0.5, 1.02, subtitle, transform=ax.transAxes, 
            fontsize=10, color=TEXT_COLOR, ha='center', va='top', alpha=0.8)
    
    # Légende personnalisée
    legend_elements = [
        mpatches.Patch(color=WIN_COLOR, label='Victoire'),
        mpatches.Patch(color=LOSS_COLOR, label='Défaite'),
        plt.Line2D([0], [0], color=GOLD_COLOR, linewidth=3, label='LP'),
        plt.Line2D([0], [0], color=ACCENT_COLOR, linewidth=2, 
                   linestyle=':', label='Tendance')
    ]
    
    legend = ax.legend(handles=legend_elements, loc='upper left', 
                      framealpha=0.9, facecolor=BG_COLOR, 
                      edgecolor=GRID_COLOR, fontsize=9)
    plt.setp(legend.get_texts(), color=TEXT_COLOR)
    
    # Box avec stats en bas à droite
    stats_text = (
        f'Départ: {lp_start} LP\n'
        f'Actuel: {lp_end} LP\n'
        f'Peak: {lp_max} LP'
    )
    
    props = dict(boxstyle='round,pad=0.5', facecolor=BG_COLOR, 
                 edgecolor=GRID_COLOR, alpha=0.9, linewidth=2)
    ax.text(0.98, 0.02, stats_text, transform=ax.transAxes, 
            fontsize=9, color=TEXT_COLOR, ha='right', va='bottom',
            bbox=props, family='monospace')
    
    plt.tight_layout()
    
    # Sauvegarder
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight', 
                facecolor=BG_COLOR, edgecolor='none')
    plt.close(fig)
    buf.seek(0)
    
    return discord.File(buf, filename='lp_curve.png')


def create_performance_heatmap(matches: list, puuid: str) -> discord.File:
    """
    Crée une heatmap des performances par rôle et heure de la journée
    """
    # Préparer les données
    role_map = {'TOP': 0, 'JUNGLE': 1, 'MIDDLE': 2, 'BOTTOM': 3, 'UTILITY': 4}
    
    # Matrice performances (5 rôles x 24 heures)
    perf_matrix = np.zeros((5, 24))
    count_matrix = np.zeros((5, 24))
    
    for match in matches:
        info = match.get('info', {})
        part = next((p for p in info.get('participants', []) 
                    if p.get('puuid') == puuid), None)
        
        if not part:
            continue
            
        # Extraire infos
        role = part.get('teamPosition', 'UTILITY')
        if role not in role_map:
            continue
            
        game_time = info.get('gameEndTimestamp', info.get('gameCreation', 0)) / 1000
        hour = dt.datetime.fromtimestamp(game_time).hour
        
        # Score de performance simple
        kills = part.get('kills', 0)
        deaths = part.get('deaths', 1)
        assists = part.get('assists', 0)
        kda = (kills + assists) / deaths
        win_bonus = 2 if part.get('win') else 0
        score = min(10, kda + win_bonus)
        
        role_idx = role_map[role]
        perf_matrix[role_idx][hour] += score
        count_matrix[role_idx][hour] += 1
    
    # Moyennes
    with np.errstate(divide='ignore', invalid='ignore'):
        avg_perf = np.where(count_matrix > 0, perf_matrix / count_matrix, 0)
    
    # Créer la heatmap
    fig, ax = plt.subplots(figsize=(14, 5), facecolor=BG_COLOR)
    ax.set_facecolor(BG_COLOR)
    
    # Heatmap
    im = ax.imshow(avg_perf, cmap='YlOrRd', aspect='auto', 
                   interpolation='bilinear', vmin=0, vmax=10)
    
    # Axes
    role_labels = ['Top', 'Jungle', 'Mid', 'ADC', 'Support']
    ax.set_yticks(range(5))
    ax.set_yticklabels(role_labels, fontsize=10, color=TEXT_COLOR)
    
    ax.set_xticks(range(0, 24, 2))
    ax.set_xticklabels([f'{h:02d}h' for h in range(0, 24, 2)], 
                       fontsize=9, color=TEXT_COLOR)
    
    ax.set_xlabel('Heure de la journée', fontsize=11, 
                  color=TEXT_COLOR, fontweight='bold')
    ax.set_ylabel('Rôle', fontsize=11, color=TEXT_COLOR, fontweight='bold')
    
    # Titre
    ax.text(0.5, 1.05, 'Performances par rôle et heure', 
            transform=ax.transAxes, fontsize=14, fontweight='bold', 
            color=GOLD_COLOR, ha='center')
    
    # Colorbar
    cbar = plt.colorbar(im, ax=ax, pad=0.02)
    cbar.set_label('Performance', rotation=270, labelpad=20, 
                   color=TEXT_COLOR, fontsize=10)
    cbar.ax.tick_params(colors=TEXT_COLOR, labelsize=9)
    
    # Annotate values (seulement si > 0)
    for i in range(5):
        for j in range(24):
            if count_matrix[i][j] > 0:
                text_color = "white" if avg_perf[i, j] > 5 else "black"
                ax.text(j, i, f'{avg_perf[i, j]:.1f}',
                       ha="center", va="center", 
                       color=text_color,
                       fontsize=7, fontweight='bold')
    
    plt.tight_layout()
    
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=120, bbox_inches='tight', 
                facecolor=BG_COLOR)
    plt.close(fig)
    buf.seek(0)
    
    return discord.File(buf, filename='performance_heatmap.png')

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
        lp_hist  = await r_get(f"lp_hist:{puid}:420") or {}  # SoloQ uniquement
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

        # n'affiche que si la série ≥ 2
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
        # Page 2 : Heatmap performances
        # ---------------------------------------------------
        e2 = discord.Embed(title="**🔥 Performances**", colour=discord.Colour.red())

        if len(matches) >= 5:
            heatmap_file = create_performance_heatmap(matches, puid)
            if heatmap_file:
                e2.set_image(url="attachment://performance_heatmap.png")
                page2file[2] = [heatmap_file]
                e2.description = "Analyse de tes performances par rôle et tranche horaire"
        else:
            e2.description = "Pas assez de parties pour l'analyse (minimum 5)"
            e2.add_field(
                name="À venir",
                value="Continue de jouer pour débloquer cette analyse !",
                inline=False
            )

        embeds.append(e2)

        # ---------------------------------------------------
        # Page 3 : Courbe LP MODERNE
        # ---------------------------------------------------
        lp_hist_int = {int(k): v for k, v in lp_hist.items()}
        e3 = discord.Embed(title="**📈 Progression LP**", colour=COLOR_GOLD)

        if len(lp_hist_int) >= 2:
            curve_file = create_modern_lp_curve(lp_hist_int, matches, puid)
            if curve_file:
                e3.set_image(url="attachment://lp_curve.png")
                page2file[3] = [curve_file]
                
                # Stats textuelles
                lp_values = list(lp_hist_int.values())
                delta = lp_values[-1] - lp_values[0]
                peak = max(lp_values)
                
                arrow = "📈" if delta >= 0 else "📉"
                e3.description = (
                    f"{arrow} **{abs(delta)} LP** sur 30 jours\n"
                    f"🏔️ Peak: **{peak} LP**\n"
                    f"📊 {len(lp_hist_int)} points de données"
                )
        else:
            e3.description = "Pas assez de données pour générer la courbe 💤"
            e3.add_field(
                name="Comment ça marche ?",
                value="La courbe LP se construit automatiquement au fur et à mesure de tes parties ranked !",
                inline=False
            )

        embeds.append(e3)

        # ---------------------------------------------------
        # Page 4 : Synergie mates (statique)
        # ---------------------------------------------------
        e4=discord.Embed(title="**Synergie – Mates fréquents**",
                         colour=discord.Colour.dark_teal())
        if mates:
            e4.description="\n".join(f"• **{n}** – {c} games" for n,c in mates)
        else:
            e4.description="Aucun mate récurrent dans les 20 dernières parties."
        embeds.append(e4)

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
