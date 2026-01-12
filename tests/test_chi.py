"""Unit tests for the chi.py prediction engine."""

import pytest
from unittest.mock import patch
from oogway.services import chi


class TestChiPrediction:
    """Test champion interaction prediction logic."""

    def test_predict_equal_teams(self):
        """Test prediction with equal strength teams."""
        with patch('oogway.champ_meta.meta') as mock_meta:
            # Mock equal winrates and no counters
            mock_meta.return_value = {"winrate": 50, "counters": []}

            picks_a = ["Champion1", "Champion2"]
            picks_b = ["Champion3", "Champion4"]

            pct_a, pct_b = chi.predict(picks_a, picks_b)

            # Should be 50-50 for equal teams
            assert pct_a == 50.0
            assert pct_b == 50.0
            assert pct_a + pct_b == 100.0

    def test_predict_stronger_team(self):
        """Test prediction with one stronger team."""
        def mock_meta_fn(champ):
            if champ in ["Strong1", "Strong2"]:
                return {"winrate": 55, "counters": []}
            return {"winrate": 45, "counters": []}

        with patch('oogway.champ_meta.meta', side_effect=mock_meta_fn):
            pct_a, pct_b = chi.predict(["Strong1", "Strong2"], ["Weak1", "Weak2"])

            # Team A should have higher win prediction
            assert pct_a > pct_b
            assert pct_a + pct_b == 100.0

    def test_predict_with_counters(self):
        """Test prediction including champion counters."""
        def mock_meta_fn(champ):
            if champ == "Counter1":
                return {"winrate": 50, "counters": ["Enemy1"]}
            return {"winrate": 50, "counters": []}

        with patch('oogway.champ_meta.meta', side_effect=mock_meta_fn):
            pct_a, pct_b = chi.predict(["Counter1"], ["Enemy1"])

            # Counter should increase win rate
            assert pct_a > 50.0
            assert pct_b < 50.0

    def test_predict_empty_teams(self):
        """Test prediction with empty team (edge case)."""
        with patch('oogway.champ_meta.meta') as mock_meta:
            mock_meta.return_value = {"winrate": 50, "counters": []}

            pct_a, pct_b = chi.predict([], ["Champion1"])

            # Empty team should still return valid percentages
            assert pct_a + pct_b == 100.0

    def test_bar_visualization(self):
        """Test the bar chart generation."""
        # Full blue (100%)
        result = chi.bar(100, blocks=20)
        assert result == "🟦" * 20

        # Full red (0%)
        result = chi.bar(0, blocks=20)
        assert result == "🟥" * 20

        # 50-50 split
        result = chi.bar(50, blocks=20)
        assert result == "🟦" * 10 + "🟥" * 10

        # Check length is always correct
        result = chi.bar(75, blocks=20)
        assert len(result) == 20


class TestChiScoring:
    """Test the internal scoring mechanism."""

    def test_score_no_picks(self):
        """Test scoring with no champions picked."""
        with patch('oogway.champ_meta.meta') as mock_meta:
            mock_meta.return_value = {"winrate": 50, "counters": []}

            from oogway.services.chi import _score
            result = _score([], ["Enemy1"])

            # Empty team should return baseline 50
            assert result == 50.0

    def test_score_with_high_winrate(self):
        """Test scoring with high winrate champions."""
        def mock_meta_fn(champ):
            return {"winrate": 60, "counters": []}

        with patch('oogway.champ_meta.meta', side_effect=mock_meta_fn):
            from oogway.services.chi import _score
            result = _score(["HighWR1", "HighWR2"], [])

            # Should return average winrate
            assert result == 60.0
