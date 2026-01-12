#!/usr/bin/env python3
"""
tools/build_winrates.py
Génère winrates.json (Emerald+) sans clé Riot.
Tentatives :
  1. U.GG         patch_<maj>_<min>/tierlist_rank.json
  2. WarGraphs    patch_<maj>_<min>/champion_stats.json
  3. WarGraphs    latest/emerald_plus/champion_stats.json   ← TOUJOURS dispo
  4. Lolalytics   diamond_plus.json
"""
import json, requests, sys
from pathlib import Path

UA     = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/125 Safari/537.36")
HEAD   = {"User-Agent": UA, "Referer": "https://u.gg/"}

# ── patch courant ─────────────────────────────────────────────────────────
ver  = requests.get("https://ddragon.leagueoflegends.com/api/versions.json",
                    headers=HEAD, timeout=10).json()[0]  # "15.13.1"
maj, minr, *_ = ver.split(".")
slug = f"{maj}_{minr}"                                    # "15_13"
print("Patch courant :", maj + "." + minr)

def fetch(url: str):
    r = requests.get(url, headers=HEAD, timeout=10)
    if not r.ok:
        raise RuntimeError(r.status_code)
    return r.json()

# ── sources à la chaîne ───────────────────────────────────────────────────
SOURCES = [
    ("U.GG", lambda j: {x["name"]: round(x["winRate"]/100, 4) for x in j["champions"]},
     f"https://stats2.u.gg/lol/patch{slug}/global/emerald_plus/tierlist_rank.json"),

    ("WG patch", lambda j: {x["name"]: round(x["general"]["winRate"], 4) for x in j},
     f"https://raw.githubusercontent.com/WarGraphs/statistics-data/master/"
     f"AGGREGATED_STATS/patch_{slug}/global/emerald_plus/champion_stats.json"),

    ("WG latest", lambda j: {x["name"]: round(x["general"]["winRate"], 4) for x in j},
     "https://raw.githubusercontent.com/WarGraphs/statistics-data/master/"
     "AGGREGATED_STATS/latest/global/emerald_plus/champion_stats.json"),

    ("Lolalytics", lambda j: {x["name"]: round(x["wr"]/100, 4) for x in j["data"]},
     f"https://cdn.lolalytics.com/api/tierlist/{maj}.{minr}/diamond_plus.json"),
]

wr = None
for label, parser, url in SOURCES:
    print("📥  Tentative :", label)
    try:
        wr = parser(fetch(url))
        print(f"✅  {label} OK – {len(wr)} champions")
        break
    except Exception as e:
        print("    ↳", e)

if not wr:
    sys.exit("⛔  Aucune source n’a répondu.")

Path(__file__).with_name("winrates.json").write_text(
    json.dumps(wr, indent=2, ensure_ascii=False)
)
print("✔️  winrates.json mis à jour.")
