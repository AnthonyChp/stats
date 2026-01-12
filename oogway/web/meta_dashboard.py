# oogway/web/meta_dashboard.py
# Meta Dashboard — FastAPI + Chart.js
# Lancement :
#   python -m uvicorn oogway.web.meta_dashboard:app --host 0.0.0.0 --port 8000

from __future__ import annotations

import os
import json
import csv
import io
import asyncio
from typing import Dict, List, Tuple, Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

try:
    import redis.asyncio as redis  # redis-py >= 4.2 (async)
except Exception:  # pragma: no cover
    import redis  # type: ignore

try:
    import aiohttp
except Exception as e:
    raise RuntimeError("Veuillez installer aiohttp : pip install aiohttp") from e

APP_TITLE = "LoL Customs — Meta Dashboard"
META_KEY = "meta:champions"  # {"picks": {cid:int}, "bans": {cid:int}, "wins": {cid:int}}

# ------------------------- Config Redis --------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
rclient = redis.from_url(REDIS_URL, decode_responses=True)

# ------------------------- Data Dragon ---------------------------
DD_VERSION: str | None = None
DD_LOCK = asyncio.Lock()

async def get_dd_version() -> str:
    global DD_VERSION
    if DD_VERSION:
        return DD_VERSION
    async with DD_LOCK:
        if DD_VERSION:
            return DD_VERSION
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get("https://ddragon.leagueoflegends.com/api/versions.json") as r:
                r.raise_for_status()
                DD_VERSION = (await r.json())[0]
                return DD_VERSION

def champ_icon_url(cid: str, ver: str) -> str:
    return f"https://ddragon.leagueoflegends.com/cdn/{ver}/img/champion/{cid}.png"

# ------------------------- Meta Helpers --------------------------
async def meta_load() -> Dict[str, Dict[str, int]]:
    raw = await rclient.get(META_KEY)
    if not raw:
        return {"picks": {}, "bans": {}, "wins": {}}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        data = raw
    data.setdefault("picks", {})
    data.setdefault("bans", {})
    data.setdefault("wins", {})
    # cast
    data["picks"] = {str(k): int(v) for k, v in data["picks"].items()}
    data["bans"]  = {str(k): int(v) for k, v in data["bans"].items()}
    data["wins"]  = {str(k): int(v) for k, v in data["wins"].items()}
    return data

def _apply_query_filter(P: Dict[str,int], B: Dict[str,int], W: Dict[str,int], q: str|None):
    if not q:
        return P, B, W
    ql = q.strip().lower()
    P2 = {cid:cnt for cid,cnt in P.items() if ql in cid.lower()}
    B2 = {cid:cnt for cid,cnt in B.items() if ql in cid.lower()}
    # Wins n’est utilisé que pour WR (lié à picks)
    W2 = {cid:cnt for cid,cnt in W.items() if cid in P2}
    return P2, B2, W2

def compute_tables(
    data: Dict[str, Dict[str,int]],
    *,
    top: int = 10,
    min_picks_for_wr: int = 10,
    q: str | None = None,
    sort: str = "presence",   # presence|picks|bans|wr
    order: str = "desc",      # asc|desc
) -> Dict[str, Any]:

    P: Dict[str, int] = data["picks"]
    B: Dict[str, int] = data["bans"]
    W: Dict[str, int] = data["wins"]

    P, B, W = _apply_query_filter(P, B, W, q)

    presence: List[Tuple[str, int]] = [(cid, P.get(cid, 0) + B.get(cid, 0)) for cid in set(P) | set(B)]
    presence.sort(key=lambda x: x[1], reverse=True)

    top_picks: List[Tuple[str, int]] = sorted(P.items(), key=lambda x: x[1], reverse=True)
    top_bans:  List[Tuple[str, int]] = sorted(B.items(), key=lambda x: x[1], reverse=True)

    wr_entries: List[Tuple[str, float, int]] = []
    for cid, pcount in P.items():
        if pcount >= min_picks_for_wr:
            wr = (W.get(cid, 0) / max(1, pcount)) * 100.0
            wr_entries.append((cid, wr, pcount))
    wr_entries.sort(key=lambda x: x[1], reverse=True)

    # Tri global (utilisé pour le "tableau unifié" + pagination côté API si voulu)
    if sort == "presence":
        unified = [(cid, P.get(cid,0)+B.get(cid,0), P.get(cid,0), B.get(cid,0), (W.get(cid,0)/max(1,P.get(cid,0)))*100.0 if P.get(cid,0) else 0.0)
                   for cid in set(P)|set(B)]
        key_fn = 1
    elif sort == "picks":
        unified = [(cid, P.get(cid,0)+B.get(cid,0), P.get(cid,0), B.get(cid,0), (W.get(cid,0)/max(1,P.get(cid,0)))*100.0 if P.get(cid,0) else 0.0)
                   for cid in set(P)]
        key_fn = 2
    elif sort == "bans":
        unified = [(cid, P.get(cid,0)+B.get(cid,0), P.get(cid,0), B.get(cid,0), (W.get(cid,0)/max(1,P.get(cid,0)))*100.0 if P.get(cid,0) else 0.0)
                   for cid in set(B)]
        key_fn = 3
    else:  # wr
        unified = [(cid, P.get(cid,0)+B.get(cid,0), P.get(cid,0), B.get(cid,0), (W.get(cid,0)/max(1,P.get(cid,0)))*100.0 if P.get(cid,0) else 0.0)
                   for cid in set(P)]
        key_fn = 4

    unified.sort(key=lambda row: row[key_fn], reverse=(order!="asc"))

    return {
        "presence": presence[:top],
        "picks": top_picks[:top],
        "bans": top_bans[:top],
        "winrates": wr_entries[:top],
        "unified": unified,  # [(cid, presence, picks, bans, wr)]
        "totals": {
            "games_estimate": sum(P.values()) // 10 if P else 0,
            "unique_champs": len(set(P) | set(B)),
            "filtered_unique": len({u[0] for u in unified}),
        }
    }

def _csv_from_unified(unified: List[Tuple[str,int,int,int,float]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Champion", "Presence", "Picks", "Bans", "Winrate(%)"])
    for cid, presence, picks, bans, wr in unified:
        writer.writerow([cid, presence, picks, bans, f"{wr:.1f}"])
    return buf.getvalue().encode("utf-8")

# --------------------------- FastAPI -----------------------------
app = FastAPI(title=APP_TITLE)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["GET"],
    allow_headers=["*"],
)

@app.get("/healthz")
async def healthz():
    try:
        pong = await rclient.ping()
        return {"ok": True, "redis": pong}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/meta")
async def api_meta(
    top: int = 10,
    min_picks: int = 10,
    q: str | None = Query(None, description="Filtre texte sur champion (contains)"),
    sort: str = Query("presence", regex="^(presence|picks|bans|wr)$"),
    order: str = Query("desc", regex="^(asc|desc)$"),
):
    data = await meta_load()
    ver = await get_dd_version()
    tables = compute_tables(
        data,
        top=max(1, min(top, 50)),
        min_picks_for_wr=max(1, min_picks),
        q=q,
        sort=sort,
        order=order,
    )

    def add_icons_list(lst):
        return [{"cid": cid, "count": cnt, "icon": champ_icon_url(cid, ver)} for cid, cnt in lst]

    def add_icons_wr(lst):
        return [{"cid": cid, "wr": wr, "picks": picks, "icon": champ_icon_url(cid, ver)} for cid, wr, picks in lst]

    unified = [
        {
            "cid": cid,
            "presence": presence,
            "picks": picks,
            "bans": bans,
            "wr": wr,
            "icon": champ_icon_url(cid, ver),
        }
        for (cid, presence, picks, bans, wr) in tables["unified"]
    ]

    payload = {
        "presence": add_icons_list(tables["presence"]),
        "picks": add_icons_list(tables["picks"]),
        "bans": add_icons_list(tables["bans"]),
        "winrates": add_icons_wr(tables["winrates"]),
        "unified": unified,
        "totals": tables["totals"],
        "dd_version": ver,
        "params": {"top": top, "min_picks": min_picks, "q": q or "", "sort": sort, "order": order},
    }
    return JSONResponse(payload)

@app.get("/api/meta/export")
async def api_meta_export(
    fmt: str = Query("csv", regex="^(csv|json)$"),
    min_picks: int = 1,
    q: str | None = None,
    sort: str = Query("presence", regex="^(presence|picks|bans|wr)$"),
    order: str = Query("desc", regex="^(asc|desc)$"),
):
    data = await meta_load()
    tables = compute_tables(
        data,
        top=10**6,
        min_picks_for_wr=max(1, min_picks),
        q=q,
        sort=sort,
        order=order,
    )
    unified = tables["unified"]

    if fmt == "json":
        ver = await get_dd_version()
        payload = [
            {
                "champion": cid,
                "presence": presence,
                "picks": picks,
                "bans": bans,
                "wr": wr,
                "icon": champ_icon_url(cid, ver),
            }
            for (cid, presence, picks, bans, wr) in unified
        ]
        return JSONResponse(payload)

    # CSV
    content = _csv_from_unified(unified)
    return StreamingResponse(
        io.BytesIO(content),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=meta.csv"},
    )

@app.get("/", response_class=HTMLResponse)
async def index():
    html = f"""
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <title>{APP_TITLE}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {{
      --bg:#0b1220; --card:#121a2b; --muted:#9fb0c2; --txt:#e6eef8; --accent:#21c994; --border:#1b2943;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      background:var(--bg); color:var(--txt);
      font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,sans-serif;
      margin:0; padding:20px 16px 60px;
    }}
    h1 {{ margin:0 0 6px 0; font-size:22px; }}
    .sub {{ color:var(--muted); font-size:13px; margin-bottom:16px; }}
    .toolbar {{
      display:flex; flex-wrap:wrap; gap:10px; align-items:center;
      background:rgba(255,255,255,.02); border:1px solid var(--border); border-radius:12px; padding:12px;
      position:sticky; top:0; z-index:10; backdrop-filter: blur(6px);
    }}
    .toolbar input[type="text"], .toolbar select, .toolbar input[type="number"] {{
      background:#0f1729; color:var(--txt); border:1px solid var(--border);
      border-radius:10px; padding:8px 10px; outline: none;
    }}
    .toolbar label {{ font-size:12px; color:var(--muted); }}
    .btn {{
      background:var(--accent); color:#002b1e; border:none; border-radius:10px; padding:8px 12px; font-weight:600; cursor:pointer;
    }}
    .btn.secondary {{ background:#0f1729; color:var(--txt); border:1px solid var(--border); }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(320px,1fr)); gap:16px; margin-top:16px; }}
    .card {{ background:var(--card); border-radius:14px; padding:16px; box-shadow:0 6px 18px rgba(0,0,0,.25); border:1px solid var(--border); }}
    .row {{ display:flex; gap:10px; align-items:center; margin:6px 0; }}
    .cid {{ width:28px; height:28px; border-radius:6px; object-fit:cover; }}
    .tag {{ font-size:12px; padding:2px 8px; background:#0e1526; border-radius:999px; color:var(--muted); border:1px solid var(--border);}}
    .kpi {{ display:flex; gap:12px; margin:10px 0 12px; flex-wrap:wrap; }}
    .kpi .box {{ background:#0f1729; border:1px solid var(--border); border-radius:12px; padding:10px 14px; color:var(--muted); }}
    canvas {{ width:100% !important; height:260px !important; }}

    table {{ width:100%; border-collapse: collapse; margin-top:10px; }}
    th, td {{ padding:8px; border-bottom:1px solid var(--border); font-size:14px; }}
    th {{ text-align:left; color:var(--muted); cursor:pointer; }}
    tr:hover td {{ background:#0e1526; }}
    .champ {{ display:flex; align-items:center; gap:10px; }}
    .pagination {{ display:flex; gap:8px; align-items:center; justify-content:flex-end; margin-top:10px; }}
    .pagination button {{ padding:6px 10px; border-radius:8px; border:1px solid var(--border); background:#0f1729; color:var(--txt);}}
  </style>
</head>
<body>
  <h1>{APP_TITLE}</h1>
  <div class="sub">Dashboard méta basé sur les parties reportées (bouton ✅). Actualisez ou laissez l’auto-refresh actif.</div>

  <div class="toolbar">
    <div>
      <label>🔎 Recherche</label><br/>
      <input id="q" type="text" placeholder="ex: aatrox, yone, lux..." />
    </div>
    <div>
      <label>Top N</label><br/>
      <input id="top" type="number" min="1" max="50" value="10" style="width:90px" />
    </div>
    <div>
      <label>Min picks (WR)</label><br/>
      <input id="minp" type="number" min="1" max="9999" value="10" style="width:90px" />
    </div>
    <div>
      <label>Tri</label><br/>
      <select id="sort">
        <option value="presence">Presence</option>
        <option value="picks">Picks</option>
        <option value="bans">Bans</option>
        <option value="wr">Winrate</option>
      </select>
    </div>
    <div>
      <label>Ordre</label><br/>
      <select id="order">
        <option value="desc">Desc</option>
        <option value="asc">Asc</option>
      </select>
    </div>
    <div>
      <label>&nbsp;</label><br/>
      <button id="btnRefresh" class="btn">Recharger</button>
      <button id="btnExportJSON" class="btn secondary">Export JSON</button>
      <button id="btnExportCSV" class="btn secondary">Export CSV</button>
    </div>
    <div style="margin-left:auto">
      <label><input id="auto" type="checkbox" /> Auto-refresh (30s)</label>
    </div>
  </div>

  <div class="kpi">
    <div class="box" id="kpi-games">Total games estimé: —</div>
    <div class="box" id="kpi-unique">Champions vus (filtres): —</div>
    <div class="box">Data Dragon: <span id="kpi-dd">—</span></div>
  </div>

  <div class="grid">
    <div class="card">
      <h3>👀 Présence (picks + bans)</h3>
      <canvas id="chartPresence"></canvas>
      <div id="listPresence"></div>
    </div>
    <div class="card">
      <h3>✅ Top Picks</h3>
      <canvas id="chartPicks"></canvas>
      <div id="listPicks"></div>
    </div>
    <div class="card">
      <h3>🚫 Top Bans</h3>
      <canvas id="chartBans"></canvas>
      <div id="listBans"></div>
    </div>
    <div class="card">
      <h3>🏆 Winrates (min picks)</h3>
      <canvas id="chartWR"></canvas>
      <div id="listWR"></div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <h3>📋 Tableau unifié</h3>
    <div class="sub">Tri en cliquant sur les en-têtes · Pagination locale</div>
    <table id="table">
      <thead>
        <tr>
          <th data-k="cid">Champion</th>
          <th data-k="presence">Presence</th>
          <th data-k="picks">Picks</th>
          <th data-k="bans">Bans</th>
          <th data-k="wr">WR (%)</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
    <div class="pagination">
      <span id="pageInfo"></span>
      <button id="prev">Préc.</button>
      <button id="next">Suiv.</button>
      <select id="perPage">
        <option>10</option><option selected>20</option><option>50</option><option>100</option>
      </select>
    </div>
  </div>

<script>
let CHARTS = {{}};
let DATA = null;
let timer = null;
let sortLocal = {{ key: 'presence', dir: 'desc' }};
let page = 1;

const els = {{
  q: document.getElementById('q'),
  top: document.getElementById('top'),
  minp: document.getElementById('minp'),
  sort: document.getElementById('sort'),
  order: document.getElementById('order'),
  btnRefresh: document.getElementById('btnRefresh'),
  btnExportJSON: document.getElementById('btnExportJSON'),
  btnExportCSV: document.getElementById('btnExportCSV'),
  auto: document.getElementById('auto'),
  kGames: document.getElementById('kpi-games'),
  kUnique: document.getElementById('kpi-unique'),
  kDD: document.getElementById('kpi-dd'),
  table: document.getElementById('table'),
  tbody: document.querySelector('#table tbody'),
  pageInfo: document.getElementById('pageInfo'),
  prev: document.getElementById('prev'),
  next: document.getElementById('next'),
  perPage: document.getElementById('perPage')
}};

function params() {{
  const p = new URLSearchParams();
  p.set('top', els.top.value || 10);
  p.set('min_picks', els.minp.value || 10);
  p.set('q', els.q.value.trim());
  p.set('sort', els.sort.value);
  p.set('order', els.order.value);
  return p.toString();
}}

async function load() {{
  const res = await fetch(`/api/meta?${{params()}}`);
  DATA = await res.json();
  render();
}}

function mkChart(cid, labels, values, title, fmt=(v)=>v) {{
  const ctx = document.getElementById(cid).getContext('2d');
  if (CHARTS[cid]) CHARTS[cid].destroy();
  CHARTS[cid] = new Chart(ctx, {{
    type: 'bar',
    data: {{ labels, datasets: [{{ label:title, data:values }}] }},
    options: {{
      responsive: true,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{ callbacks: {{ label: (ctx)=>`${{ctx.label}}: ${{fmt(ctx.parsed.y)}}` }} }}
      }},
      scales: {{ y: {{ beginAtZero:true }} }}
    }}
  }});
}}

function renderList(mountId, items, rightKey='count', rightFmt=(v)=>v, extraRight=null) {{
  const m = document.getElementById(mountId);
  m.innerHTML = items.map(x => `
    <div class="row">
      <img class="cid" src="${{x.icon}}" alt="${{x.cid}}" width="28" height="28">
      <div style="flex:1">${{x.cid}}</div>
      <div class="tag">${{extraRight ? extraRight(x) : rightFmt(x[rightKey])}}</div>
    </div>
  `).join('');
}}

function render() {{
  if (!DATA) return;
  els.kGames.textContent = `Total games estimé: ${{DATA.totals.games_estimate}}`;
  els.kUnique.textContent = `Champions vus (filtres): ${{DATA.totals.filtered_unique}}`;
  els.kDD.textContent = DATA.dd_version;

  const pres = {{
    labels: DATA.presence.map(x=>x.cid),
    values: DATA.presence.map(x=>x.count)
  }};
  mkChart('chartPresence', pres.labels, pres.values, 'Présence');

  const picks = {{
    labels: DATA.picks.map(x=>x.cid),
    values: DATA.picks.map(x=>x.count)
  }};
  mkChart('chartPicks', picks.labels, picks.values, 'Picks');

  const bans = {{
    labels: DATA.bans.map(x=>x.cid),
    values: DATA.bans.map(x=>x.count)
  }};
  mkChart('chartBans', bans.labels, bans.values, 'Bans');

  const wr = {{
    labels: DATA.winrates.map(x=>x.cid),
    values: DATA.winrates.map(x=>x.wr),
    picks:  DATA.winrates.map(x=>x.picks)
  }};
  mkChart('chartWR', wr.labels, wr.values, 'WR (%)', (v)=>Number(v).toFixed(1)+'%');

  renderList('listPresence', DATA.presence);
  renderList('listPicks', DATA.picks);
  renderList('listBans', DATA.bans);
  renderList('listWR', DATA.winrates, 'wr', (v)=>Number(v).toFixed(1)+'%', (x)=> (x.wr.toFixed(1) + '% · ' + x.picks + ' picks'));

  renderTable();
}}

function renderTable() {{
  let rows = DATA.unified.slice();
  // tri local
  rows.sort((a,b) => {{
    const k = sortLocal.key;
    let va = a[k], vb = b[k];
    if (typeof va === 'string') {{ va = va.toLowerCase(); vb = vb.toLowerCase(); }}
    return (va < vb ? -1 : va > vb ? 1 : 0) * (sortLocal.dir === 'asc' ? 1 : -1);
  }});
  const per = parseInt(els.perPage.value||20,10);
  const total = rows.length;
  const pages = Math.max(1, Math.ceil(total/per));
  if (page > pages) page = pages;
  const slice = rows.slice((page-1)*per, page*per);

  els.tbody.innerHTML = slice.map(x => `
    <tr>
      <td class="champ"><img class="cid" src="${{x.icon}}" alt="${{x.cid}}" width="28" height="28">${{x.cid}}</td>
      <td>${{x.presence}}</td>
      <td>${{x.picks}}</td>
      <td>${{x.bans}}</td>
      <td>${{x.wr.toFixed(1)}}</td>
    </tr>
  `).join('');

  els.pageInfo.textContent = `Page ${{page}} / ${{pages}} (${{total}} lignes)`;

  els.prev.disabled = page<=1;
  els.next.disabled = page>=pages;
}}

function debounce(fn, ms) {{
  let t=null;
  return (...args) => {{
    clearTimeout(t);
    t=setTimeout(()=>fn(...args), ms);
  }};
}}

function applyHandlers() {{
  els.btnRefresh.onclick = () => load();
  els.q.oninput = debounce(()=>{{ page=1; load(); }}, 250);
  els.top.onchange = () => {{ page=1; load(); }};
  els.minp.onchange = () => {{ page=1; load(); }};
  els.sort.onchange = () => {{ page=1; load(); }};
  els.order.onchange = () => {{ page=1; load(); }};

  els.auto.onchange = () => {{
    if (els.auto.checked) {{
      timer = setInterval(load, 30000);
    }} else {{
      if (timer) clearInterval(timer);
      timer = null;
    }}
  }};

  els.prev.onclick = () => {{ page=Math.max(1,page-1); renderTable(); }};
  els.next.onclick = () => {{ page=page+1; renderTable(); }};
  els.perPage.onchange = () => {{ page=1; renderTable(); }};

  // tri local depuis l'en-tête du tableau
  document.querySelectorAll('#table th').forEach(th => {{
    th.onclick = () => {{
      const key = th.getAttribute('data-k');
      if (!key) return;
      sortLocal.dir = (sortLocal.key === key && sortLocal.dir === 'desc') ? 'asc' : 'desc';
      sortLocal.key = key;
      renderTable();
    }};
  }});

  els.btnExportJSON.onclick = () => {{
    const p = new URLSearchParams();
    p.set('fmt','json');
    p.set('min_picks', els.minp.value||1);
    p.set('q', els.q.value.trim());
    p.set('sort', els.sort.value);
    p.set('order', els.order.value);
    window.open('/api/meta/export?'+p.toString(), '_blank');
  }};
  els.btnExportCSV.onclick = () => {{
    const p = new URLSearchParams();
    p.set('fmt','csv');
    p.set('min_picks', els.minp.value||1);
    p.set('q', els.q.value.trim());
    p.set('sort', els.sort.value);
    p.set('order', els.order.value);
    window.open('/api/meta/export?'+p.toString(), '_blank');
  }};
}}

applyHandlers();
load();
</script>
</body>
</html>
"""
    return HTMLResponse(html)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("oogway.web.meta_dashboard:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
