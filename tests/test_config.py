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


def test_daily_cap_defaults_to_5():
    assert Settings.from_env({}).daily_cap == 5
    assert Settings().daily_cap == 5


def test_daily_cap_override():
    assert Settings.from_env({"DAILY_CAP": "2"}).daily_cap == 2


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "2.5"])
def test_daily_cap_invalid_raises(bad):
    with pytest.raises(ValueError, match="DAILY_CAP"):
        Settings.from_env({"DAILY_CAP": bad})
