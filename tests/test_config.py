import pytest

from tgnews.config import Settings


def test_mode_defaults_to_review():
    s = Settings.from_env({})
    assert s.mode == "review"
    assert s.explore_rate == 0.2
    assert s.min_scored_posts == 30
    assert s.score_weights == {"shares": 1.0, "saves": 1.0, "likes": 0.1, "comments": 0.2}


def test_auto_mode():
    assert Settings.from_env({"MODE": "auto"}).mode == "auto"


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        Settings.from_env({"MODE": "turbo"})
