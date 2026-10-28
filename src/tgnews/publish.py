"""Publish a rendered post to Instagram, with duplicate prevention as the top priority.

Order of operations (the duplicate guard):
  validate -> load/check state -> store caption -> create container(s) -> store container id
  -> poll until FINISHED -> atomically claim state `publishing` -> media_publish EXACTLY ONCE
  -> record media id + `published`.
Any failure before the claim marks the post `failed` (nothing was published). Any failure of
media_publish itself leaves the post `publishing` (outcome unknown), is never retried, and is
resolved later by reconcile_publishing against Instagram's recent media. Never log the token.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import db
from .instagram import InstagramClient, InstagramError
from .models import PostState

log = logging.getLogger(__name__)

KV_TOKEN = "ig_access_token"
KV_TOKEN_EXPIRES = "ig_token_expires_at"
MAX_CHILDREN = 10


class PublishError(Exception):
    pass


class AlreadyPublishing(PublishError):
    """Post is already publishing or published; no API call was made."""


@dataclass(frozen=True)
class Token:
    access_token: str = field(repr=False)
    expires_at: datetime


def _validate_urls(urls: list[str]) -> None:
    if not 1 <= len(urls) <= MAX_CHILDREN:
        raise ValueError(f"urls must contain 1 to {MAX_CHILDREN} items, got {len(urls)}")
    for u in urls:
        if not isinstance(u, str) or not u.startswith("https://"):
            raise ValueError("every image URL must be an https URL")


def _create_container(client: InstagramClient, urls: list[str], caption: str) -> str:
    path = f"{client.ig_user_id}/media"
    if len(urls) == 1:
        res = client.post(path, image_url=urls[0], caption=caption)
    else:
        children = [client.post(path, image_url=u, is_carousel_item="true")["id"] for u in urls]
        res = client.post(path, media_type="CAROUSEL", children=",".join(children), caption=caption)
    return str(res["id"])


def _wait_until_finished(client: InstagramClient, container_id: str, sleep: Callable[[float], None],
                         poll_interval: float, timeout: float) -> None:
    waited = 0.0
    while True:
        status = client.get(container_id, fields="status_code").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise PublishError(f"container {container_id} status {status}")
        if status == "PUBLISHED":
            raise _ContainerAlreadyPublished(container_id)
        if status != "IN_PROGRESS":
            raise PublishError(f"container {container_id} unexpected status {status!r}")
        if waited >= timeout:
            raise PublishError(f"container {container_id} not ready after {timeout:.0f}s")
        sleep(poll_interval)
        waited += poll_interval


class _ContainerAlreadyPublished(PublishError):
    pass


def publish_post(post_id: int, urls: list[str], caption: str, client: InstagramClient, engine, *,
                 sleep: Callable[[float], None] = time.sleep, poll_interval: float = 10.0,
                 timeout: float = 300.0) -> str:
    """Publish; returns the Instagram media id. Never calls media_publish more than once."""
    _validate_urls(urls)
    post = db.get_post(engine, post_id)
    if post is None:
        raise PublishError(f"post {post_id} not found")
    if post.state in (PostState.publishing, PostState.published):
        raise AlreadyPublishing(f"post {post_id} is already {post.state.value}")
    if post.state not in (PostState.draft, PostState.approved):
        raise PublishError(f"post {post_id} is {post.state.value}; only draft/approved can be published")

    # Everything up to and including the claim: a failure here means nothing was published.
    try:
        db.set_post_caption(engine, post_id, caption)
        container_id = _create_container(client, urls, caption)
        db.set_post_container(engine, post_id, container_id)
        _wait_until_finished(client, container_id, sleep, poll_interval, timeout)
        if not db.mark_publishing(engine, post_id):
            raise AlreadyPublishing(f"post {post_id} was claimed by another publisher")
    except AlreadyPublishing:
        raise
    except _ContainerAlreadyPublished as exc:
        # Instagram says this container is already live: do NOT publish again, and let
        # reconcile_publishing attach the media id.
        db.update_post_state(engine, post_id, PostState.publishing)
        raise PublishError(f"post {post_id}: {exc}; already published on Instagram, "
                           "left as publishing for reconciliation") from None
    except Exception as exc:
        db.update_post_state(engine, post_id, PostState.failed)
        log.warning("post %s failed before publish: %s", post_id, _safe(exc))
        if isinstance(exc, PublishError):
            raise
        raise PublishError(f"post {post_id} failed before publishing: {_safe(exc)}") from None

    # From here the outcome of the single media_publish call is the only thing that matters.
    try:
        res = client.post(f"{client.ig_user_id}/media_publish", creation_id=container_id)
        media_id = str(res["id"])
    except Exception as exc:  # unknown outcome: no retry, no `failed`
        log.error("post %s: media_publish outcome unknown, left as publishing: %s",
                  post_id, _safe(exc))
        raise PublishError(f"post {post_id}: media_publish outcome unknown ({_safe(exc)}); "
                           "left as publishing for reconciliation, not retried") from None
    try:
        db.set_post_media(engine, post_id, media_id, datetime.now(timezone.utc))
        db.update_post_state(engine, post_id, PostState.published)
    except Exception:
        raise PublishError(f"post {post_id} published as media {media_id} but recording it failed; "
                           "left as publishing, reconcile will fix") from None
    return media_id


def _safe(exc: Exception) -> str:
    """InstagramError messages are sanitized; for anything else only the type is safe to show."""
    return str(exc) if isinstance(exc, (InstagramError, PublishError)) else type(exc).__name__


def _parse_ts(value: str) -> datetime | None:
    try:
        v = value.strip()
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        elif len(v) > 5 and v[-5] in "+-" and v[-3] != ":":
            v = v[:-2] + ":" + v[-2:]
        dt = datetime.fromisoformat(v)
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def reconcile_publishing(engine, client: InstagramClient, now: datetime, *,
                         max_age_minutes: int = 30) -> list[int]:
    """Resolve posts stuck in `publishing` by looking at Instagram. Never posts anything.

    Match -> published (with the media id). No match and stuck longer than max_age_minutes ->
    failed, for a human to requeue. Returns ids whose state changed."""
    stuck = db.list_posts_by_state(engine, PostState.publishing)
    if not stuck:
        return []
    recent = client.get(f"{client.ig_user_id}/media", fields="id,caption,timestamp", limit=25)
    items = recent.get("data", []) if isinstance(recent, dict) else []
    changed: list[int] = []
    for post in stuck:
        match = None
        want = (post.caption or "").strip()
        if want:
            for item in items:
                ts = _parse_ts(item.get("timestamp", ""))
                if (str(item.get("caption") or "").strip() == want and item.get("id") and ts
                        and (post.created_at is None or ts >= post.created_at)):
                    match = (str(item["id"]), ts)
                    break
        if match:
            db.set_post_media(engine, post.id, match[0], match[1])
            db.update_post_state(engine, post.id, PostState.published)
            changed.append(post.id)
        elif post.updated_at is not None and now - post.updated_at > timedelta(minutes=max_age_minutes):
            db.update_post_state(engine, post.id, PostState.failed)
            changed.append(post.id)
    return changed


def refresh_token(client: InstagramClient, engine, now: datetime) -> Token:
    """Refresh the long-lived token (unversioned endpoint) and persist it. Never logs it."""
    res = client.get_unversioned("refresh_access_token", grant_type="ig_refresh_token")
    token, expires_in = res.get("access_token"), res.get("expires_in")
    if not isinstance(token, str) or not token or not isinstance(expires_in, int) or expires_in <= 0:
        raise InstagramError("token refresh returned an unexpected response")
    expires_at = now + timedelta(seconds=expires_in)
    db.set_kv(engine, KV_TOKEN, token)
    db.set_kv(engine, KV_TOKEN_EXPIRES, expires_at.astimezone(timezone.utc).isoformat())
    client.set_token(token)
    return Token(token, expires_at)


def token_days_left(token: Token, now: datetime) -> int:
    """Whole days until expiry (negative once expired)."""
    return int((token.expires_at - now).total_seconds() // 86400)
