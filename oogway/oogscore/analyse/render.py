from __future__ import annotations
import logging
from io import BytesIO
from typing import Optional

log = logging.getLogger(__name__)

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    log.warning("matplotlib not available — radar/curve rendering disabled")

AXIS_ORDER = ["KDA", "KP", "DMG", "CC", "ECO", "LANE", "OBJ", "VIS", "UTL"]

COMP_LABELS_SHORT = {
    "KDA": "KDA", "KP": "KP", "DMG": "DMG",
    "CC": "CC", "ECO": "ECO", "LANE": "Lane",
    "OBJ": "OBJ", "VIS": "VIS", "UTL": "UTL",
}

def render_radar(
    player_pct: Optional[dict[str, float]],
    visible_axes: list[str],
    accent_hex: str = "#5865F2",
) -> Optional[BytesIO]:
    if not MATPLOTLIB_AVAILABLE:
        return None
    labels = [a for a in AXIS_ORDER if a in visible_axes]
    n = len(labels)
    if n < 3:
        return None
    try:
        angles = np.linspace(0, 2 * np.pi, n, endpoint=False).tolist()
        angles_closed = angles + angles[:1]

        fig, ax = plt.subplots(figsize=(5, 5), subplot_kw=dict(polar=True))
        fig.patch.set_facecolor("#2B2D31")
        ax.set_facecolor("#2B2D31")
        ax.set_ylim(0, 100)

        # Grid styling
        ax.set_xticks(angles)
        ax.set_xticklabels([COMP_LABELS_SHORT.get(l, l) for l in labels], fontsize=9, color="white")
        ax.set_yticklabels([])
        ax.grid(color="#444444", linewidth=0.5)
        ax.spines["polar"].set_color("#444444")

        # Baseline median ring (50% everywhere)
        base = [50.0] * n + [50.0]
        ax.plot(angles_closed, base, lw=1.5, linestyle="--", color="#888888", label="Médiane")
        ax.fill(angles_closed, base, alpha=0.08, color="#888888")

        # Player overlay
        if player_pct is not None:
            me = [player_pct.get(a, 0.5) * 100 for a in labels] + [player_pct.get(labels[0], 0.5) * 100]
            ax.plot(angles_closed, me, lw=2.5, color=accent_hex, label="Toi")
            ax.fill(angles_closed, me, alpha=0.25, color=accent_hex)

        ax.legend(loc="upper right", bbox_to_anchor=(1.3, 1.1), fontsize=8, labelcolor="white",
                  facecolor="#2B2D31", edgecolor="#444444")

        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as e:
        log.error("render_radar failed: %s", e, exc_info=True)
        try:
            plt.close("all")
        except Exception:
            pass
        return None


def render_curve(score_history: list[float], champion: str, role: str) -> Optional[BytesIO]:
    if not MATPLOTLIB_AVAILABLE or len(score_history) < 3:
        return None
    try:
        fig, ax = plt.subplots(figsize=(6, 3))
        fig.patch.set_facecolor("#2B2D31")
        ax.set_facecolor("#2B2D31")

        x = list(range(1, len(score_history) + 1))
        ax.plot(x, score_history, color="#5865F2", lw=2, marker="o", markersize=4)
        ax.axhline(y=50, color="#888888", linestyle="--", lw=1, alpha=0.5)
        ax.fill_between(x, score_history, 50,
                        where=[s >= 50 for s in score_history], alpha=0.15, color="#57F287")
        ax.fill_between(x, score_history, 50,
                        where=[s < 50 for s in score_history], alpha=0.15, color="#ED4245")

        ax.set_ylim(0, 100)
        ax.set_xlabel("Games", color="white", fontsize=9)
        ax.set_ylabel("OogScore", color="white", fontsize=9)
        ax.set_title(f"Évolution — {champion} {role}", color="white", fontsize=10)
        ax.tick_params(colors="white")
        ax.spines["bottom"].set_color("#444444")
        ax.spines["left"].set_color("#444444")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.grid(color="#444444", linewidth=0.5, alpha=0.5)

        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as e:
        log.error("render_curve failed: %s", e, exc_info=True)
        try:
            plt.close("all")
        except Exception:
            pass
        return None
