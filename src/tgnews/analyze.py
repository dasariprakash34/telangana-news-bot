"""Pull Instagram insights for published posts and score them.

Each metric is requested in its own call (Meta changes/refuses metrics per media type), so a
refused metric becomes None instead of failing the post. Credentials are never logged: only
sanitized InstagramError messages and exception type names reach the log.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Mapping

from . import db
from .instagram import InstagramError

log = logging.getLogger(__name__)

# (output key, Graph API metric name)
METRICS = (("reach", "reach"), ("likes", "likes"), ("comments", "comments"),
           ("shares", "shares"), ("saves", "saved"), ("views", "views"))
DEFAULT_WEIGHTS: dict[str, float] = {"shares": 1.0, "saves": 1.0, "likes": 0.1, "comments": 0.2}
MAX_POSTS_PER_RUN = 25  # x 6 metric calls = 150 calls, under the ~200/hour rate limit


class InsightsUnavailable(Exception):
    """Every metric call for a media failed or returned nothing; retry later."""


def _extract(body: Any) -> float | None:
    try:
        item = body["data"][0]
        if isinstance(item.get("values"), list) and item["values"]:
            value = item["values"][0]["value"]
        else:
            value = item["total_value"]["value"]
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def pull_insights(media_id: str, client: Any) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    last_error = ""
    failures = 0
    for key, api_name in METRICS:
        try:
            out[key] = _extract(client.get(f"{media_id}/insights", metric=api_name))
        except InstagramError as exc:  # message is sanitized by the client
            out[key] = None
            failures += 1
            last_error = str(exc)
    if failures == len(METRICS):
        raise InsightsUnavailable(f"all insight calls failed: {last_error}")
    return out


def score(metrics: Mapping[str, float | None],
          weights: Mapping[str, float] = DEFAULT_WEIGHTS) -> float | None:
    """(shares*ws + saves*wsv + likes*wl + comments*wc) / reach.

    None when reach is missing/zero or when a core signal (shares, saves) is unavailable.
    Unavailable likes/comments contribute 0 (they are low-weight signals).
    """
    reach = metrics.get("reach")
    shares, saves = metrics.get("shares"), metrics.get("saves")
    if not reach or shares is None or saves is None:
        return None
    total = (shares * weights["shares"] + saves * weights["saves"]
             + (metrics.get("likes") or 0) * weights["likes"]
             + (metrics.get("comments") or 0) * weights["comments"])
    return total / reach


def run_analysis(engine: Any, client: Any, now: datetime) -> int:
    """Record insights for published posts >= 48h old. Returns the number recorded."""
    recorded = 0
    for post in db.posts_due_for_analysis(engine, now)[:MAX_POSTS_PER_RUN]:
        if not post.media_id:
            continue
        try:
            metrics = pull_insights(post.media_id, client)
            if all(v is None for v in metrics.values()):
                log.warning("insights not available yet for post %s; will retry", post.id)
                continue
            db.set_post_metrics(engine, post.id, {**metrics, "score": score(metrics)})
            recorded += 1
        except InsightsUnavailable as exc:
            log.warning("insights unavailable for post %s, will retry: %s", post.id, exc)
        except Exception as exc:  # isolate: one bad post must not stop the rest
            log.warning("insights failed for post %s (%s)", post.id, type(exc).__name__)
    return recorded
