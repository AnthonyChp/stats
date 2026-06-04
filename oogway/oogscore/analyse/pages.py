from __future__ import annotations
import discord
from oogway.oogscore.analyse.insights import pct_to_text, COMP_LABELS, Insights
from oogway.oogscore.analyse.categories import CATEGORIES, CAT_LABELS, category_score, visible_categories
from oogway.oogscore.weights import ROLE_WEIGHTS, grade_from_score

GRADE_COLORS = {"S": 0xFFD700, "A": 0x57F287, "B": 0x5865F2, "C": 0x99AAB5, "D": 0xED4245}
GRADE_ACCENTS = {"S": "#FFD700", "A": "#57F287", "B": "#5865F2", "C": "#99AAB5", "D": "#ED4245"}

def _bar(p: float, width: int = 10) -> str:
    filled = round(p * width)
    return "█" * filled + "░" * (width - filled)

def build_page1_baseline(
    champion: str, role: str,
    baseline_dists: dict,
    component_p50: dict[str, float],
    sample_size: int,
    is_low_confidence: bool,
) -> discord.Embed:
    from oogway.oogscore.analyse.categories import visible_axes as _visible_axes
    axes = _visible_axes(role)
    vis_cats = visible_categories(role)

    embed = discord.Embed(
        title=f"📊 Baseline — {champion} {role}",
        description=f"Référence sur **{sample_size} games**" + (" ⚠️ données limitées" if is_low_confidence else ""),
        color=0x5865F2,
    )

    for cat in vis_cats:
        comps = [c for c in CATEGORIES[cat] if c in axes and c in component_p50]
        if not comps:
            continue
        lines = []
        for c in comps:
            dist_key = {
                "KDA": "kda", "KP": "kill_participation", "DMG": "team_damage_pct",
                "ECO": "gold_per_min", "OBJ": "obj_participation", "VIS": "vision_per_min",
                "UTL": "heal_shield", "LANE": "lane_cs_adv", "CC": "cc_score_per_min",
            }.get(c)
            dist = baseline_dists.get(dist_key) if dist_key else None
            if dist is None:
                continue
            lines.append(
                f"**{COMP_LABELS.get(c, c)}** (×{int(ROLE_WEIGHTS[role].get(c,0)*100)}%)\n"
                f"médiane `{dist.p50:.2f}` · top 25% `{dist.p75:.2f}` · top 10% `{dist.p90:.2f}`"
            )
        if lines:
            embed.add_field(name=CAT_LABELS[cat], value="\n".join(lines), inline=False)

    embed.set_footer(text="Page 1/3 — Vue d'ensemble baseline")
    return embed


def build_page1_joueur(
    champion: str, role: str,
    player_percentiles: dict[str, float],
    n_games: int,
    avg_score: float,
    insights: Insights,
    is_indicative: bool,
    sample_size: int,
    baseline_source: str,
) -> discord.Embed:
    grade = grade_from_score(avg_score)
    color = GRADE_COLORS.get(grade, 0x5865F2)
    vis_cats = visible_categories(role)

    indicative_note = f" ⚠️ indicatif (n={n_games})" if is_indicative else f" (n={n_games} games)"
    embed = discord.Embed(
        title=f"🎯 Analyse — {champion} {role}",
        description=(
            f"OogScore moyen : **{avg_score:.1f}/100 ({grade})**{indicative_note}\n"
            f"Réf. baseline : {baseline_source} (n={sample_size})"
        ),
        color=color,
    )

    # Insights
    strength_names = [COMP_LABELS.get(c, c) for c in insights.strengths]
    weakness_names = [COMP_LABELS.get(c, c) for c in insights.weaknesses]
    if strength_names:
        embed.add_field(name="🟢 Forces", value=", ".join(strength_names), inline=True)
    if weakness_names:
        embed.add_field(name="🔴 À travailler", value=", ".join(weakness_names), inline=True)
    if insights.advice:
        embed.add_field(name="💡 Conseil", value=insights.advice, inline=False)

    # Category bars
    for cat in vis_cats:
        cat_score = category_score(cat, role, player_percentiles)
        if cat_score is None:
            continue
        bar = _bar(cat_score)
        pct_txt = pct_to_text(cat_score)
        embed.add_field(
            name=CAT_LABELS[cat],
            value=f"`{bar}` {pct_txt}",
            inline=True,
        )

    embed.set_footer(text="Page 1/3 — Vue d'ensemble · Page 2 pour le détail")
    return embed


def build_page2_baseline(
    champion: str, role: str,
    baseline_dists: dict,
    sample_size: int,
) -> discord.Embed:
    from oogway.oogscore.analyse.categories import visible_axes as _visible_axes
    axes = _visible_axes(role)

    embed = discord.Embed(
        title=f"📋 Détail composantes — {champion} {role}",
        description=f"Seuils de référence (n={sample_size})",
        color=0x5865F2,
    )

    dist_map = {
        "KDA": "kda", "KP": "kill_participation", "DMG": "team_damage_pct",
        "ECO": "gold_per_min", "OBJ": "obj_participation", "VIS": "vision_per_min",
        "UTL": "heal_shield", "LANE": "lane_cs_adv", "CC": "cc_score_per_min",
    }

    lines = ["```", f"{'Composante':<12} {'Médiane':>8} {'Top 25%':>8} {'Top 10%':>8} {'Poids':>6}", "─"*48]
    for code in axes:
        dk = dist_map.get(code)
        dist = baseline_dists.get(dk) if dk else None
        if dist is None:
            continue
        weight = ROLE_WEIGHTS.get(role, {}).get(code, 0)
        lines.append(
            f"{COMP_LABELS.get(code,code):<12} {dist.p50:>8.2f} {dist.p75:>8.2f} {dist.p90:>8.2f} {int(weight*100):>5}%"
        )
    lines.append("```")
    embed.add_field(name="Seuils par composante", value="\n".join(lines), inline=False)
    embed.set_footer(text="Page 2/3 — Détail · Page 3 pour l'évolution")
    return embed


def build_page2_joueur(
    champion: str, role: str,
    player_percentiles: dict[str, float],
    baseline_dists: dict,
    n_games: int,
    sample_size: int,
) -> discord.Embed:
    from oogway.oogscore.analyse.categories import visible_axes as _visible_axes
    axes = _visible_axes(role)

    embed = discord.Embed(
        title=f"📋 Détail composantes — {champion} {role}",
        description=f"Toi vs baseline (n={sample_size} ref · {n_games} games perso)",
        color=0x5865F2,
    )

    dist_map = {
        "KDA": "kda", "KP": "kill_participation", "DMG": "team_damage_pct",
        "ECO": "gold_per_min", "OBJ": "obj_participation", "VIS": "vision_per_min",
        "UTL": "heal_shield", "LANE": "lane_cs_adv", "CC": "cc_score_per_min",
    }

    lines = ["```", f"{'Composante':<12} {'Toi':>10} {'Médiane':>10} {'Poids':>6}", "─"*44]
    for code in axes:
        p = player_percentiles.get(code)
        if p is None:
            continue
        weight = ROLE_WEIGHTS.get(role, {}).get(code, 0)
        pct_txt = pct_to_text(p)
        arrow = "↑" if p >= 0.55 else ("↓" if p <= 0.45 else "→")
        lines.append(f"{COMP_LABELS.get(code,code):<12} {arrow} {pct_txt:<15} {int(weight*100):>5}%")
    lines.append("```")
    embed.add_field(name="Percentile par composante", value="\n".join(lines), inline=False)
    embed.set_footer(text="Page 2/3 — Détail · Page 3 pour l'évolution")
    return embed


def build_page3_no_data(champion: str, role: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"📈 Évolution — {champion} {role}",
        description="Pas assez de games pour tracer une courbe d'évolution (minimum 3).\nJoue plus de ranked pour débloquer cette page !",
        color=0x5865F2,
    )
    embed.set_footer(text="Page 3/3 — Évolution")
    return embed


def build_page3_not_linked() -> discord.Embed:
    embed = discord.Embed(
        title="📈 Évolution — Non disponible",
        description="Cette page est réservée aux membres liés.\nUtilise `/link` pour associer ton compte Riot et débloquer l'analyse personnelle.",
        color=0x99AAB5,
    )
    embed.set_footer(text="Page 3/3 — Évolution")
    return embed


def build_page3_joueur(champion: str, role: str, score_history: list[float]) -> discord.Embed:
    n = len(score_history)
    avg = sum(score_history) / n if n else 0
    trend = score_history[-1] - score_history[0] if n >= 2 else 0
    trend_txt = f"{'↗' if trend >= 0 else '↘'} {trend:+.1f} pts sur {n} games"

    embed = discord.Embed(
        title=f"📈 Évolution — {champion} {role}",
        description=f"OogScore moyen : **{avg:.1f}** · {trend_txt}",
        color=0x5865F2,
    )
    embed.set_image(url="attachment://curve.png")
    embed.set_footer(text="Page 3/3 — Évolution (voir image)")
    return embed
