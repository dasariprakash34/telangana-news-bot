"""Persistence via SQLAlchemy Core; portable across SQLite (tests) and Postgres."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (JSON, Column, DateTime, Float, ForeignKey, Integer, MetaData,
                        String, Table, Text, inspect, insert, select, text, update)
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
    Column("container_id", String(128)),
    Column("caption", Text),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),  # last state change
    Column("claimed_at", DateTime(timezone=True)),  # when a publisher claimed the post
)

kv = Table(
    "kv", metadata,
    Column("key", String(128), primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True)),
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


# Columns added after the first release; init() adds them to pre-existing tables.
_POSTS_ADDED_COLUMNS = {
    "container_id": "VARCHAR(128)", "caption": "TEXT",
    "created_at": "TIMESTAMP WITH TIME ZONE", "updated_at": "TIMESTAMP WITH TIME ZONE",
    "claimed_at": "TIMESTAMP WITH TIME ZONE",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def init(engine: Engine) -> None:
    metadata.create_all(engine)
    existing = {c["name"] for c in inspect(engine).get_columns("posts")}
    with engine.begin() as conn:
        for name, ddl in _POSTS_ADDED_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE posts ADD COLUMN {name} {ddl}"))


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
            headline_style=headline_style, state=state.value,
            created_at=_now(), updated_at=_now()))
        return int(res.inserted_primary_key[0])


def _row_to_post(row: Any) -> Post:
    return Post(id=row.id, story_id=row.story_id, format=row.format, topic=row.topic,
                time_slot=row.time_slot, headline_style=row.headline_style,
                state=PostState(row.state), media_id=row.media_id,
                posted_at=_utc(row.posted_at), metrics=row.metrics,
                container_id=row.container_id, caption=row.caption,
                created_at=_utc(row.created_at), updated_at=_utc(row.updated_at),
                claimed_at=_utc(row.claimed_at))


def get_post(engine: Engine, post_id: int) -> Post | None:
    with engine.connect() as conn:
        row = conn.execute(select(posts).where(posts.c.id == post_id)).first()
    return _row_to_post(row) if row else None


def update_post_state(engine: Engine, post_id: int, state: PostState, *,
                      media_id: str | None = None, posted_at: datetime | None = None) -> None:
    values: dict[str, Any] = {"state": state.value, "updated_at": _now()}
    if media_id is not None:
        values["media_id"] = media_id
    if posted_at is not None:
        values["posted_at"] = _utc(posted_at)
    with engine.begin() as conn:
        conn.execute(update(posts).where(posts.c.id == post_id).values(**values))


def claim_post(engine: Engine, post_id: int, now: datetime | None = None) -> bool:
    """Atomically claim a draft/approved post for publishing: one conditional UPDATE sets state
    `publishing` and claimed_at. True only for the single caller that won; a loser (or a post in
    any other state) gets False and nothing is written."""
    claimed = _utc(now) if now is not None else _now()
    with engine.begin() as conn:
        res = conn.execute(update(posts).where(
            posts.c.id == post_id,
            posts.c.state.in_([PostState.draft.value, PostState.approved.value]),
        ).values(state=PostState.publishing.value, claimed_at=claimed, updated_at=_now(),
                  container_id=None, posted_at=None))  # drop a previous attempt's leftovers
        return res.rowcount == 1


def transition_post_state(engine: Engine, post_id: int, frm: PostState, to: PostState,
                          now: datetime | None = None) -> bool:
    """Compare-and-set: move a post from `frm` to `to` in ONE conditional UPDATE. True only if
    this call changed the row; a post in any other state (stale read) is left untouched."""
    stamp = _utc(now) if now is not None else _now()
    with engine.begin() as conn:
        res = conn.execute(update(posts).where(
            posts.c.id == post_id, posts.c.state == frm.value,
        ).values(state=to.value, updated_at=stamp))
        return res.rowcount == 1


def fail_unpublished(engine: Engine, post_id: int) -> bool:
    """Mark failed ONLY if still publishing with no media id (never touches a live post)."""
    with engine.begin() as conn:
        res = conn.execute(update(posts).where(
            posts.c.id == post_id, posts.c.state == PostState.publishing.value,
            posts.c.media_id.is_(None),
        ).values(state=PostState.failed.value, updated_at=_now()))
        return res.rowcount == 1


def media_ids_in_use(engine: Engine, exclude_post_id: int) -> set[str]:
    """Media ids already recorded on other posts."""
    with engine.connect() as conn:
        rows = conn.execute(select(posts.c.media_id).where(
            posts.c.id != exclude_post_id, posts.c.media_id.is_not(None))).all()
    return {r[0] for r in rows}


def set_post_caption(engine: Engine, post_id: int, caption: str) -> None:
    with engine.begin() as conn:
        conn.execute(update(posts).where(posts.c.id == post_id).values(caption=caption))


def set_post_caption_if_state(engine: Engine, post_id: int, caption: str, state: PostState) -> bool:
    """Write the caption only while the post is still in `state` (single conditional UPDATE)."""
    with engine.begin() as conn:
        res = conn.execute(update(posts).where(
            posts.c.id == post_id, posts.c.state == state.value).values(caption=caption))
        return res.rowcount == 1


def set_post_container(engine: Engine, post_id: int, container_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(update(posts).where(posts.c.id == post_id).values(container_id=container_id))


def set_post_media(engine: Engine, post_id: int, media_id: str, posted_at: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(update(posts).where(posts.c.id == post_id).values(
            media_id=media_id, posted_at=_utc(posted_at)))


def list_posts_by_state(engine: Engine, state: PostState) -> list[Post]:
    with engine.connect() as conn:
        rows = conn.execute(select(posts).where(posts.c.state == state.value)
                            .order_by(posts.c.id)).all()
    return [_row_to_post(r) for r in rows]


def get_kv(engine: Engine, key: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(select(kv.c.value).where(kv.c.key == key)).first()
    return row[0] if row else None


def set_kv(engine: Engine, key: str, value: str) -> None:
    with engine.begin() as conn:
        res = conn.execute(update(kv).where(kv.c.key == key).values(value=value, updated_at=_now()))
        if res.rowcount == 0:
            conn.execute(insert(kv).values(key=key, value=value, updated_at=_now()))


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


def set_post_metrics(engine: Engine, post_id: int, metrics: dict) -> bool:
    """Record metrics once; a no-op (False) if the post already has metrics."""
    with engine.begin() as conn:
        res = conn.execute(update(posts).where(posts.c.id == post_id, posts.c.metrics.is_(None))
                           .values(metrics=metrics))
        return res.rowcount == 1
