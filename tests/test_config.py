import pytest

from tgnews import config
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


def test_brand_and_slot_defaults():
    s = Settings.from_env({})
    assert s.brand_name == "Telangana News"
    assert s.time_slots == config.DEFAULT_TIME_SLOTS and len(s.time_slots) == 36
    assert s.time_slots[0] == "00:30" and s.time_slots[-1] == "18:00"
    assert s.active_slot_count == 36


def test_brand_and_slot_overrides():
    s = Settings.from_env({"BRAND_NAME": "TG Now", "TIME_SLOTS": "01:00, 09:30", "ACTIVE_SLOT_COUNT": "2"})
    assert (s.brand_name, s.time_slots, s.active_slot_count) == ("TG Now", ("01:00", "09:30"), 2)


@pytest.mark.parametrize("env", [{"TIME_SLOTS": "25:00"}, {"TIME_SLOTS": "7:00"}, {"TIME_SLOTS": ""},
                                 {"ACTIVE_SLOT_COUNT": "0"}, {"ACTIVE_SLOT_COUNT": "x"}])
def test_bad_slot_settings_raise(env):
    if env == {"TIME_SLOTS": ""}:  # blank means default, not an error
        assert Settings.from_env(env).time_slots == config.DEFAULT_TIME_SLOTS
        return
    with pytest.raises(ValueError):
        Settings.from_env(env)


@pytest.mark.parametrize("raw,expected", [
    ("postgres://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
    ("postgresql://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
    ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
    ("sqlite:///tgnews.db", "sqlite:///tgnews.db"),
    ("sqlite://", "sqlite://"),
])
def test_database_url_normalised(raw, expected):
    assert Settings.from_env({"DATABASE_URL": raw}).database_url == expected


def test_database_url_default_and_blank():
    assert Settings.from_env({}).database_url == "sqlite:///tgnews.db"
    assert Settings.from_env({"DATABASE_URL": ""}).database_url == "sqlite:///tgnews.db"


def test_require_source_credit_parsing():
    assert Settings.from_env({}).require_source_credit is True
    assert Settings.from_env({"REQUIRE_SOURCE_CREDIT": "false"}).require_source_credit is False
    assert Settings.from_env({"REQUIRE_SOURCE_CREDIT": " TRUE "}).require_source_credit is True
    with pytest.raises(ValueError, match="REQUIRE_SOURCE_CREDIT"):
        Settings.from_env({"REQUIRE_SOURCE_CREDIT": "maybe"})
