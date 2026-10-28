from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from tgnews import db
from tgnews.models import PostState, Story


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    db.init(e)
    return e


def make_story(url="https://example.com/a"):
    return Story(id="s1", title="t", summary="s", url=url, source_name="src",
                 published_at=datetime(2026, 10, 28, tzinfo=timezone.utc), topic_hint="politics")


def test_story_seen_after_save(engine):
    assert db.story_seen(engine, "https://example.com/a") is False
    db.save_story(engine, make_story())
    assert db.story_seen(engine, "https://example.com/a") is True
    db.save_story(engine, make_story())  # idempotent
    assert db.story_seen(engine, "https://example.com/b") is False


def test_post_state_roundtrip(engine):
    db.save_story(engine, make_story())
    pid = db.save_post(engine, story_id="s1", format="single", topic="politics",
                       time_slot="morning", headline_style="question", state=PostState.draft)
    assert db.get_post(engine, pid).state == PostState.draft
    db.update_post_state(engine, pid, PostState.published, media_id="m1",
                         posted_at=datetime(2026, 10, 27, tzinfo=timezone.utc))
    p = db.get_post(engine, pid)
    assert p.state == PostState.published and p.media_id == "m1"
    now = datetime(2026, 10, 29, tzinfo=timezone.utc)
    assert [x.id for x in db.posts_due_for_analysis(engine, now)] == [pid]
    assert db.posts_due_for_analysis(engine, now - timedelta(days=3)) == []
