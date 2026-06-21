"""Tests for PatternDetector — deterministic phrase scanner."""
from pathlib import Path
import tempfile

import pytest

from src.extractors.pattern_detector import PatternDetector, should_extract


class TestPatternDetector:
    """Test weighted pattern detection and LLM gating."""

    def test_strong_decision_signal(self):
        """Strong decision language should exceed threshold."""
        detector = PatternDetector()
        text = "We decided to go with Cloudflare Workers over Vercel because edge deployment is cheaper"
        score, matches = detector.scan(text)
        assert score >= 4, f"Expected >=4, got {score}"
        assert any(m["phrase"] == "go with" for m in matches)
        assert any(m["phrase"] == "decided to" for m in matches)

    def test_weak_signal_below_threshold(self):
        """Casual conversation should NOT trigger LLM extraction."""
        detector = PatternDetector()
        text = "Hello, how are you? The weather is nice today."
        score, matches = detector.scan(text)
        assert score == 0, f"Expected 0, got {score}"
        assert len(matches) == 0

    def test_ddia_pattern_name_high_weight(self):
        """DDIA pattern names (trade-off matrix) should get max weight."""
        detector = PatternDetector()
        text = "This is a trade-off matrix situation for our architecture"
        score, matches = detector.scan(text)
        assert score >= 4
        # 'trade-off matrix' should match at weight 4
        matrix_matches = [m for m in matches if "trade" in m["phrase"]]
        assert sum(m["weight"] for m in matrix_matches) >= 4

    def test_should_extract_convenience(self):
        """should_extract convenience function should work."""
        assert should_extract("We decided to use Postgres over MySQL because it has better JSON support")
        assert not should_extract("Hello world")

    def test_custom_pattern_addition(self):
        """Adding a custom pattern should affect scoring."""
        detector = PatternDetector()
        detector.add_pattern("custom_flag", 5)
        score, matches = detector.scan("This has a custom_flag in it")
        assert score == 5
        assert any(m["phrase"] == "custom_flag" for m in matches)

    def test_custom_pattern_persistence(self, tmp_path):
        """Custom patterns should persist in SQLite."""
        db_path = tmp_path / "test_patterns.db"
        detector = PatternDetector(db_path=db_path)
        detector.add_pattern("my_framework", 4)

        # New detector instance should load persisted patterns
        detector2 = PatternDetector(db_path=db_path)
        score, matches = detector2.scan("Use my_framework here")
        assert score >= 4
        detector2.close()

    def test_threshold_property(self):
        """Threshold should be gettable and settable."""
        detector = PatternDetector(threshold=5)
        assert detector.threshold == 5
        detector.threshold = 3
        assert detector.threshold == 3

    def test_should_invoke_llm_api(self):
        """should_invoke_llm should return tuple with full data."""
        detector = PatternDetector(threshold=4)
        result, score, matches = detector.should_invoke_llm("We decided to use Postgres over MySQL")
        assert result is True
        assert score >= 4
        assert len(matches) > 0

    def test_empty_text(self):
        """Empty text should return 0 score."""
        detector = PatternDetector()
        score, matches = detector.scan("")
        assert score == 0
        assert matches == []

    def test_patterns_property(self):
        """patterns property should return a copy of current patterns."""
        detector = PatternDetector()
        pats = detector.patterns
        assert isinstance(pats, dict)
        assert len(pats) > 10  # Many built-in patterns
        # Should be a copy, not a reference
        pats["test"] = 1
        assert "test" not in detector.patterns

    def test_context_manager(self):
        """PatternDetector should work as context manager."""
        with PatternDetector() as detector:
            score, _ = detector.scan("We decided to migrate because of performance issues")
            assert score >= 3

    def test_false_positive_scenario(self):
        """False-positive phrases should not trigger."""
        detector = PatternDetector()
        # "over" alone in "over the weekend" should not match
        text = "We worked on this over the weekend"
        # 'over ' requires trailing space
        score, matches = detector.scan(text)
        # If 'over ' with space matches, it should be low
        over_matches = [m for m in matches if m["phrase"] == "over "]
        assert len(over_matches) == 0 or sum(m["weight"] for m in over_matches) < 4
