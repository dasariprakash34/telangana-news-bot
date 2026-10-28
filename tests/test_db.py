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


def test_posts_due_for_analysis_48h_boundary(engine):
    db.save_story(engine, make_story())
    now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    ids = {}
    for hours in (24, 47, 48, 72):
        pid = db.save_post(engine, story_id="s1", format="single", state=PostState.draft)
        db.update_post_state(engine, pid, PostState.published, media_id=f"m{hours}",
                             posted_at=now - timedelta(hours=hours))
        ids[hours] = pid
    due = {p.id for p in db.posts_due_for_analysis(engine, now)}
    assert due == {ids[48], ids[72]}


def test_save_story_twice_does_not_raise(engine):
    db.save_story(engine, make_story())
    db.save_story(engine, make_story())


def test_save_story_integrity_error_is_noop(engine, monkeypatch):
    # Simulate the race: the pre-check misses, the insert hits the unique constraint.
    db.save_story(engine, make_story())
    monkeypatch.setattr(db, "select", lambda *a, **k: _EmptySelect())
    db.save_story(engine, make_story())


class _EmptySelect:
    def where(self, *a, **k):
        from sqlalchemy import literal, select as real
        return real(literal(1)).where(literal(False))
