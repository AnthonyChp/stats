"""Tests for oogway/oogscore/analyse/ package."""
from __future__ import annotations
import pytest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# categories.py
# ---------------------------------------------------------------------------
from oogway.oogscore.analyse.categories import (
    visible_categories, category_score, visible_axes, CATEGORIES,
)
from oogway.oogscore.weights import ROLE_WEIGHTS


class TestVisibleCategories:
    def test_top_has_all_three_categories(self):
        cats = visible_categories("TOP")
        assert "COMBAT" in cats
        assert "ECONOMIE" in cats
        assert "VISION_MAP" in cats

    def test_support_combat_visible(self):
        # SUPPORT has non-zero KDA, KP, DMG, CC weights
        cats = visible_categories("SUPPORT")
        assert "COMBAT" in cats

    def test_adc_no_zero_weight_lane_excluded(self):
        # ADC has LANE=0.12 so ECONOMIE is visible
        cats = visible_categories("ADC")
        assert "ECONOMIE" in cats

    def test_jungle_no_lane_weight(self):
        # JUNGLE has LANE=0.00 — but ECO=0.08 so ECONOMIE still visible
        assert ROLE_WEIGHTS["JUNGLE"]["LANE"] == 0.0
        cats = visible_categories("JUNGLE")
        # ECO is non-zero so ECONOMIE is still visible
        assert "ECONOMIE" in cats

    def test_unknown_role_returns_empty(self):
        cats = visible_categories("UNKNOWN_ROLE")
        assert cats == []


class TestCategoryScore:
    def test_all_components_at_median(self):
        pct = {c: 0.5 for c in ["KDA", "KP", "DMG", "CC", "ECO", "LANE", "OBJ", "VIS", "UTL"]}
        score = category_score("COMBAT", "TOP", pct)
        assert score == pytest.approx(0.5, abs=1e-6)

    def test_returns_none_when_no_active_components(self):
        # Empty percentiles dict → no components present
        score = category_score("COMBAT", "TOP", {})
        assert score is None

    def test_weighted_average(self):
        # For TOP, COMBAT = KDA(.15) KP(.10) DMG(.22) CC(.10)
        pct = {"KDA": 1.0, "KP": 0.0, "DMG": 0.0, "CC": 0.0}
        score = category_score("COMBAT", "TOP", pct)
        w = ROLE_WEIGHTS["TOP"]
        total_w = w["KDA"] + w["KP"] + w["DMG"] + w["CC"]
        expected = w["KDA"] / total_w
        assert score == pytest.approx(expected, abs=1e-6)

    def test_support_zero_weight_lane_ignored(self):
        # SUPPORT has ECO=0.0 → ECONOMIE category has only LANE which is also 0
        pct = {"ECO": 0.9, "LANE": 0.9}
        score = category_score("ECONOMIE", "SUPPORT", pct)
        assert score is None


class TestVisibleAxes:
    def test_adc_no_cc(self):
        axes = visible_axes("ADC")
        assert "CC" not in axes  # ADC CC=0.0

    def test_jungle_no_lane(self):
        axes = visible_axes("JUNGLE")
        assert "LANE" not in axes  # JUNGLE LANE=0.0

    def test_support_no_eco(self):
        axes = visible_axes("SUPPORT")
        assert "ECO" not in axes  # SUPPORT ECO=0.0

    def test_axis_order_respected(self):
        AXIS_ORDER = ["KDA", "KP", "DMG", "CC", "ECO", "LANE", "OBJ", "VIS", "UTL"]
        axes = visible_axes("TOP")
        # Axes should appear in AXIS_ORDER order
        indices = [AXIS_ORDER.index(a) for a in axes]
        assert indices == sorted(indices)


# ---------------------------------------------------------------------------
# insights.py
# ---------------------------------------------------------------------------
from oogway.oogscore.analyse.insights import (
    generate_insights, pct_to_text, Insights,
)


class TestPctToText:
    def test_top_10(self):
        assert "🟢" in pct_to_text(0.95)

    def test_top_25(self):
        txt = pct_to_text(0.80)
        assert "top" in txt
        assert "🟢" not in txt

    def test_average(self):
        assert pct_to_text(0.50) == "dans la moyenne"

    def test_bottom_25(self):
        txt = pct_to_text(0.27)  # >= 0.25, no red emoji
        assert "bottom" in txt
        assert "🔴" not in txt

    def test_bottom_10(self):
        assert "🔴" in pct_to_text(0.05)


class TestGenerateInsights:
    def test_strength_detected(self):
        pct = {c: 0.5 for c in ROLE_WEIGHTS["MID"]}
        pct["DMG"] = 0.85  # strong, weight 0.25 >= 0.10
        ins = generate_insights(pct, "MID")
        assert "DMG" in ins.strengths

    def test_weakness_detected(self):
        pct = {c: 0.5 for c in ROLE_WEIGHTS["ADC"]}
        pct["DMG"] = 0.20  # weak, weight 0.30 >= 0.10
        ins = generate_insights(pct, "ADC")
        assert "DMG" in ins.weaknesses

    def test_low_weight_component_ignored(self):
        pct = {c: 0.5 for c in ROLE_WEIGHTS["TOP"]}
        pct["VIS"] = 0.10  # weak but VIS weight=0.03 < 0.10
        ins = generate_insights(pct, "TOP")
        assert "VIS" not in ins.weaknesses

    def test_no_weakness_no_focus(self):
        pct = {c: 0.60 for c in ROLE_WEIGHTS["MID"]}
        ins = generate_insights(pct, "MID")
        assert ins.focus is None
        assert ins.advice is None

    def test_focus_is_highest_weight_weakness(self):
        pct = {c: 0.5 for c in ROLE_WEIGHTS["JUNGLE"]}
        pct["KP"] = 0.20   # weak, w=0.15
        pct["OBJ"] = 0.20  # weak, w=0.25 — should be focus
        ins = generate_insights(pct, "JUNGLE")
        assert ins.focus == "OBJ"


# ---------------------------------------------------------------------------
# aggregate.py — get_player_component_percentiles with mock distributions
# ---------------------------------------------------------------------------
from oogway.oogscore.analyse.aggregate import (
    get_player_component_percentiles, PlayerAggregate, COMP_TO_STAT,
)
from oogway.oogscore.models import StatDistribution


def _make_dist(mean=1.0, std=0.5):
    return StatDistribution(
        mean=mean, std=std, p10=0.2, p25=0.5, p50=1.0, p75=1.5, p90=2.0, sample_size=100
    )


def _make_agg(avg_stats: dict) -> PlayerAggregate:
    return PlayerAggregate(
        champion="Lux", role="MID", puuid="test",
        n_games=10, avg_stats=avg_stats, avg_score=50.0, score_history=[50.0] * 5,
    )


class TestGetPlayerComponentPercentiles:
    def test_returns_float_between_0_and_1(self):
        avg_stats = {k: 1.0 for k in ["kda", "kill_participation", "team_damage_pct",
                                       "dmg_per_min", "gold_per_min", "cs_per_min",
                                       "vision_per_min", "heal_shield", "cc_score_per_min",
                                       "obj_participation", "lane_cs_adv"]}
        agg = _make_agg(avg_stats)
        dists = {k: _make_dist() for k in avg_stats}
        result = get_player_component_percentiles(agg, dists)
        for code, p in result.items():
            assert 0.0 <= p <= 1.0, f"{code}={p} out of range"

    def test_missing_dist_skips_component(self):
        agg = _make_agg({"kda": 1.0})
        result = get_player_component_percentiles(agg, {"kda": _make_dist()})
        # Only KDA should be computed; others missing
        assert "KDA" in result
        assert "KP" not in result

    def test_dmg_composite(self):
        avg_stats = {"team_damage_pct": 1.0, "dmg_per_min": 1.0}
        agg = _make_agg(avg_stats)
        dists = {
            "team_damage_pct": _make_dist(mean=1.0, std=0.5),
            "dmg_per_min": _make_dist(mean=1.0, std=0.5),
        }
        result = get_player_component_percentiles(agg, dists)
        assert "DMG" in result
        assert 0.0 <= result["DMG"] <= 1.0

    def test_eco_composite(self):
        avg_stats = {"gold_per_min": 1.0, "cs_per_min": 1.0}
        agg = _make_agg(avg_stats)
        dists = {
            "gold_per_min": _make_dist(mean=1.0, std=0.5),
            "cs_per_min": _make_dist(mean=1.0, std=0.5),
        }
        result = get_player_component_percentiles(agg, dists)
        assert "ECO" in result

    def test_above_mean_gives_pct_above_50(self):
        avg_stats = {"kda": 2.0}
        agg = _make_agg(avg_stats)
        dists = {"kda": _make_dist(mean=1.0, std=0.5)}
        result = get_player_component_percentiles(agg, dists)
        assert result["KDA"] > 0.5

    def test_below_mean_gives_pct_below_50(self):
        avg_stats = {"kda": 0.0}
        agg = _make_agg(avg_stats)
        dists = {"kda": _make_dist(mean=1.0, std=0.5)}
        result = get_player_component_percentiles(agg, dists)
        assert result["KDA"] < 0.5
