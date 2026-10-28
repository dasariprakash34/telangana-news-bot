import random
from collections import Counter

import pytest
from sqlalchemy import create_engine

from tgnews import db, learn
from tgnews.models import Story
from tgnews.rewrite import STYLES, TOPICS
from datetime import datetime, timezone


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    db.init(e)
    db.save_story(e, Story(id="s", title="t", summary="s", url="https://x/1", source_name="n",
                           published_at=datetime(2026, 10, 1, tzinfo=timezone.utc)))
    return e


def add(engine, score, fmt="card", topic="politics", slot="09:00", style="question", metrics=True):
    pid = db.save_post(engine, story_id="s", format=fmt, topic=topic, time_slot=slot,
                       headline_style=style)
    if metrics:
        db.set_post_metrics(engine, pid, {"score": score})
    return pid


def fill(engine, n, **kw):
    for _ in range(n):
        add(engine, 0.1, **kw)


def test_under_30_scored_posts_explores_evenly(engine):
    fill(engine, 28, fmt="card")
    add(engine, 0.99, fmt="carousel")  # 29 scored posts, carousel looks best
    learn.update_learning(engine)
    rng = random.Random(1)
    c = Counter(learn.choose(engine, "format", learn.FORMATS, rng) for _ in range(1000))
    assert 420 <= c["card"] <= 580 and 420 <= c["carousel"] <= 580


def seeded_over_30(engine):
    fill(engine, 20, fmt="card")
    for _ in range(15):
        add(engine, 0.5, fmt="carousel")
    learn.update_learning(engine)


def test_over_30_picks_best_about_80_percent(engine):
    seeded_over_30(engine)
    rng = random.Random(2)
    c = Counter(learn.choose(engine, "format", learn.FORMATS, rng) for _ in range(2000))
    # 0.8 exploit + 0.2 * 0.5 explore = 0.9 for the winner
    assert 0.85 * 2000 <= c["carousel"] <= 0.95 * 2000


def test_options_with_fewer_than_3_posts_never_win(engine):
    fill(engine, 32, topic="politics")
    add(engine, 0.99, topic="crime")
    add(engine, 0.99, topic="crime")
    learn.update_learning(engine)
    rng = random.Random(3)
    picks = Counter(learn.choose(engine, "topic", TOPICS, rng) for _ in range(1000))
    # crime only appears via the 20% uniform exploration (~2% overall), never as exploit winner
    assert picks["politics"] > 750
    assert picks["crime"] < 80


def test_attribute_options_are_fixed(engine):
    rng = random.Random(0)
    with pytest.raises(ValueError):
        learn.choose(engine, "format", ("card", "reel"), rng)
    with pytest.raises(ValueError):
        learn.choose(engine, "mode", ("live",), rng)
    with pytest.raises(ValueError):
        learn.choose(engine, "topic", ("gossip",), rng)
    with pytest.raises(ValueError):
        learn.choose(engine, "time_slot", ("9am",), rng)
    with pytest.raises(ValueError):
        learn.choose(engine, "headline_style", (), rng)
    seeded_over_30(engine)
    for _ in range(200):
        assert learn.choose(engine, "format", ("card", "carousel"), rng) in ("card", "carousel")
        assert learn.choose(engine, "headline_style", STYLES, rng) in STYLES
        assert learn.choose(engine, "time_slot", ("06:30", "13:00"), rng) in ("06:30", "13:00")


def test_learn_module_cannot_touch_safety_or_mode():
    src = open(learn.__file__).read()
    imports = [l for l in src.splitlines() if l.startswith(("import ", "from "))]
    assert not any("safety" in l or "config" in l for l in imports)


def test_update_learning_computes_means_and_is_idempotent(engine):
    add(engine, 0.2, fmt="card")
    add(engine, 0.4, fmt="card")
    add(engine, 0.9, fmt="carousel")
    add(engine, 0.0, fmt="carousel", metrics=False)  # no metrics: ignored
    pid = db.save_post(engine, story_id="s", format="card")
    db.set_post_metrics(engine, pid, {"score": None})  # unscored: ignored
    learn.update_learning(engine)
    first = db.get_learning(engine, "format")
    assert first["card"][1] == 2 and first["card"][0] == pytest.approx(0.3)
    assert first["carousel"] == (pytest.approx(0.9), 1)
    learn.update_learning(engine)
    assert db.get_learning(engine, "format") == first
    assert db.get_learning(engine, "topic")["politics"][1] == 3
