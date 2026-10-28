"""Persistence via SQLAlchemy Core; portable across SQLite (tests) and Postgres."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (JSON, Column, DateTime, Float, ForeignKey, Integer, MetaData,
                        String, Table, Text, insert, select, update)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from .models import Post, PostState, Story

metadata = MetaData()

stories = Table(
    "stories", metadata,
    Column("id", String(64), primary_key=True),
    Column("title", Text, nullable=False),
    Column("summary", Text, nullable=False, default=""),
    Column("url", String(1024), nullable=False, unique=True),
    Column("source_name", String(128), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=False),
    Column("topic_hint", String(64)),
)

posts = Table(
    "posts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("story_id", String(64), ForeignKey("stories.id"), nullable=False),
    Column("format", String(32), nullable=False),
    Column("topic", String(64)),
    Column("time_slot", String(32)),
    Column("headline_style", String(64)),
    Column("media_id", String(128)),
    Column("state", String(16), nullable=False),
    Column("posted_at", DateTime(timezone=True)),
    Column("metrics", JSON),
)

drafts = Table(
    "drafts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("post_id", Integer, ForeignKey("posts.id"), nullable=False),
    Column("caption", Text, nullable=False, default=""),
    Column("slides", JSON),
    Column("created_at", DateTime(timezone=True)),
)

learning = Table(
    "learning", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("dimension", String(32), nullable=False),
    Column("key", String(128), nullable=False),
    Column("score", Float, nullable=False, default=0.0),
    Column("n", Integer, nullable=False, default=0),
    Column("updated_at", DateTime(timezone=True)),
)

ANALYSIS_DELAY = timedelta(hours=48)


def _utc(dt: datetime | None) -> datetime | None:
    """Normalize to aware UTC (SQLite drops tzinfo on round trip)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def init(engine: Engine) -> None:
    metadata.create_all(engine)


def save_story(engine: Engine, story: Story) -> None:
    """Insert a story; no-op if its id or url is already stored (race-safe)."""
    try:
        with engine.begin() as conn:
            exists = conn.execute(
                select(stories.c.id).where((stories.c.url == story.url) | (stories.c.id == story.id))
            ).first()
            if exists:
                return
            conn.execute(insert(stories).values(
                id=story.id, title=story.title, summary=story.summary, url=story.url,
                source_name=story.source_name, published_at=_utc(story.published_at),
                topic_hint=story.topic_hint))
    except IntegrityError:
        # Concurrent ingestion inserted the same id/url between check and insert.
        pass


def story_seen(engine: Engine, url: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(select(stories.c.id).where(stories.c.url == url)).first() is not None


def save_post(engine: Engine, *, story_id: str, format: str, topic: str | None = None,
              time_slot: str | None = None, headline_style: str | None = None,
              state: PostState = PostState.draft) -> int:
    with engine.begin() as conn:
        res = conn.execute(insert(posts).values(
            story_id=story_id, format=format, topic=topic, time_slot=time_slot,
            headline_style=headline_style, state=state.value))
        return int(res.inserted_primary_key[0])


def _row_to_post(row: Any) -> Post:
    return Post(id=row.id, story_id=row.story_id, format=row.format, topic=row.topic,
                time_slot=row.time_slot, headline_style=row.headline_style,
                state=PostState(row.state), media_id=row.media_id,
                posted_at=_utc(row.posted_at), metrics=row.metrics)


def get_post(engine: Engine, post_id: int) -> Post | None:
    with engine.connect() as conn:
        row = conn.execute(select(posts).where(posts.c.id == post_id)).first()
    return _row_to_post(row) if row else None


def update_post_state(engine: Engine, post_id: int, state: PostState, *,
                      media_id: str | None = None, posted_at: datetime | None = None) -> None:
    values: dict[str, Any] = {"state": state.value}
    if media_id is not None:
        values["media_id"] = media_id
    if posted_at is not None:
        values["posted_at"] = _utc(posted_at)
    with engine.begin() as conn:
        conn.execute(update(posts).where(posts.c.id == post_id).values(**values))


def posts_due_for_analysis(engine: Engine, now: datetime) -> list[Post]:
    """Published posts at least 48h old that have no metrics recorded yet."""
    cutoff = _utc(now) - ANALYSIS_DELAY
    with engine.connect() as conn:
        rows = conn.execute(
            select(posts).where(posts.c.state == PostState.published.value,
                                posts.c.posted_at.is_not(None),
                                posts.c.metrics.is_(None))
            .order_by(posts.c.posted_at)).all()
    return [p for p in map(_row_to_post, rows) if p.posted_at <= cutoff]
