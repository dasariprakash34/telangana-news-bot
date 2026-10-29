"""Publish a rendered post to Instagram, with duplicate prevention as the top priority.

Order of operations (the duplicate guard):
  validate -> check state -> ATOMIC CLAIM (one conditional UPDATE draft/approved -> publishing,
  sets claimed_at; only the winner continues) -> store caption -> create container(s) -> store
  container id -> poll until FINISHED -> media_publish EXACTLY ONCE -> record media id + `published`.
A loser of the claim makes no API call and writes no state. After the claim, a failure before
media_publish marks the post `failed` with a conditional update (only if still publishing with
no media id, so a live post is never demoted). A failure of media_publish itself leaves the
post `publishing` (unknown outcome), is never retried, and is resolved later by
reconcile_publishing against Instagram. Never log the token.
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
    """`cause_status`/`cause_code` carry the numeric HTTP status / Graph code of an underlying
    InstagramError (never text), so callers can recognise auth failures."""

    def __init__(self, message: str = "", *, cause_status: int | None = None,
                 cause_code: int | None = None):
        super().__init__(message)
        self.cause_status = cause_status
        self.cause_code = cause_code


class AlreadyPublishing(PublishError):
    """Post is already publishing or published; no API call was made."""


@dataclass(frozen=True)
class Token:
    access_token: str = field(repr=False)
    expires_at: datetime


REEL_TIMEOUT = 600.0  # Instagram needs longer to process a video than an image


def _validate_urls(urls: list[str], kind: str = "images") -> None:
    if kind == "reel":
        if len(urls) != 1:
            raise ValueError(f"a reel needs exactly 1 video URL, got {len(urls)}")
    elif not 1 <= len(urls) <= MAX_CHILDREN:
        raise ValueError(f"urls must contain 1 to {MAX_CHILDREN} items, got {len(urls)}")
    for u in urls:
        if not isinstance(u, str) or not u.startswith("https://"):
            raise ValueError("every image URL must be an https URL")


def _create_container(client: InstagramClient, urls: list[str], caption: str,
                      kind: str = "images") -> str:
    path = f"{client.ig_user_id}/media"
    if kind == "reel":
        res = client.post(path, media_type="REELS", video_url=urls[0], caption=caption,
                          share_to_feed="true")
    elif len(urls) == 1:
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
                 timeout: float = 300.0, kind: str = "images") -> str:
    """Publish; returns the Instagram media id. Never calls media_publish more than once.
    `kind` is "images" (one image or a carousel) or "reel" (one public MP4 URL)."""
    _validate_urls(urls, kind)
    if kind == "reel":
        timeout = max(timeout, REEL_TIMEOUT)
    post = db.get_post(engine, post_id)
    if post is None:
        raise PublishError(f"post {post_id} not found")
    if post.state in (PostState.publishing, PostState.published):
        raise AlreadyPublishing(f"post {post_id} is already {post.state.value}")
    if post.state not in (PostState.draft, PostState.approved):
        raise PublishError(f"post {post_id} is {post.state.value}; only draft/approved can be published")

    if not db.claim_post(engine, post_id):
        raise AlreadyPublishing(f"post {post_id} was claimed by another publisher")

    # Claim won: we are the only publisher. Failures before media_publish mean nothing was
    # published, so mark failed (conditionally: never overwrite a live post).
    try:
        db.set_post_caption(engine, post_id, caption)
        container_id = _create_container(client, urls, caption, kind)
        db.set_post_container(engine, post_id, container_id)
        _wait_until_finished(client, container_id, sleep, poll_interval, timeout)
    except _ContainerAlreadyPublished as exc:
        # Instagram says this container is already live: do NOT publish again; stays
        # `publishing` so reconcile_publishing can attach the media id.
        raise PublishError(f"post {post_id}: {exc}; already published on Instagram, "
                           "left as publishing for reconciliation") from None
    except Exception as exc:
        db.fail_unpublished(engine, post_id)
        log.warning("post %s failed before publish: %s", post_id, _safe(exc))
        if isinstance(exc, PublishError):
            raise
        raise PublishError(f"post {post_id} failed before publishing: {_safe(exc)}",
                           **_cause(exc)) from None

    # From here the outcome of the single media_publish call is the only thing that matters.
    try:
        res = client.post(f"{client.ig_user_id}/media_publish", creation_id=container_id)
        media_id = str(res["id"])
    except Exception as exc:  # unknown outcome: no retry, no `failed`
        log.error("post %s: media_publish outcome unknown, left as publishing: %s",
                  post_id, _safe(exc))
        raise PublishError(f"post {post_id}: media_publish outcome unknown ({_safe(exc)}); "
                           "left as publishing for reconciliation, not retried",
                          **_cause(exc)) from None
    try:
        db.set_post_media(engine, post_id, media_id, datetime.now(timezone.utc))
        db.update_post_state(engine, post_id, PostState.published)
    except Exception:
        raise PublishError(f"post {post_id} published as media {media_id} but recording it failed; "
                           "left as publishing, reconcile will fix") from None
    return media_id


def _cause(exc: Exception) -> dict:
    if isinstance(exc, InstagramError):
        return {"cause_status": exc.status, "cause_code": exc.code}
    return {}


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


def _find_media(items: list, post, used: set[str]):
    """Oldest recent media with the post's caption, not already owned by another post and not
    older than claimed_at - 5 minutes. Returns (media_id, timestamp) or None."""
    want = (post.caption or "").strip()
    if not want:
        return None
    floor = _reference_time(post)
    floor = floor - timedelta(minutes=5) if floor else None
    best = None
    for item in items:
        mid = str(item.get("id") or "")
        ts = _parse_ts(item.get("timestamp", ""))
        if not mid or ts is None or mid in used:
            continue
        if str(item.get("caption") or "").strip() != want:
            continue
        if floor is not None and ts < floor:
            continue
        if best is None or ts < best[1]:
            best = (mid, ts)
    return best


def _reference_time(post) -> datetime | None:
    return post.claimed_at or post.updated_at or post.created_at


def reconcile_publishing(engine, client: InstagramClient, now: datetime, *,
                         max_age_minutes: int = 30) -> list[int]:
    """Resolve posts stuck in `publishing` by looking at Instagram. Never posts anything.

    With a stored container id its status decides: PUBLISHED -> the post is live, attach its media
    id by caption match (excluding media owned by other posts); if the media cannot be found it
    stays `publishing` (never failed when known live). ERROR/EXPIRED -> failed. FINISHED or
    IN_PROGRESS -> failed once older than max_age_minutes (from claimed_at), else unchanged.
    Without a container id: caption match, else failed when old. Returns ids that changed state."""
    now = db._utc(now)
    stuck = db.list_posts_by_state(engine, PostState.publishing)
    if not stuck:
        return []
    recent_items: list | None = None

    def recent() -> list:
        nonlocal recent_items
        if recent_items is None:
            res = client.get(f"{client.ig_user_id}/media", fields="id,caption,timestamp", limit=25)
            recent_items = res.get("data", []) if isinstance(res, dict) else []
        return recent_items

    changed: list[int] = []
    for post in stuck:
        if post.media_id:  # media id already recorded; only the state write was lost
            db.update_post_state(engine, post.id, PostState.published)
            changed.append(post.id)
            continue
        ref = _reference_time(post)
        old = ref is None or now - ref > timedelta(minutes=max_age_minutes)

        def attach() -> bool:
            match = _find_media(recent(), post, db.media_ids_in_use(engine, post.id))
            if not match:
                return False
            db.set_post_media(engine, post.id, match[0], match[1])
            db.update_post_state(engine, post.id, PostState.published)
            changed.append(post.id)
            return True

        if post.container_id:
            status = client.get(post.container_id, fields="status_code").get("status_code")
            if status == "PUBLISHED":
                if not attach():
                    log.warning("post %s is live on Instagram but its media was not found in "
                                "recent media; left as publishing", post.id)
            elif status in ("ERROR", "EXPIRED") or old:
                if db.fail_unpublished(engine, post.id):
                    changed.append(post.id)
        elif not attach() and old:
            if db.fail_unpublished(engine, post.id):
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
