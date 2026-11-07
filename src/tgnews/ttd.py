"""Tirumala Tirupati Devasthanams (TTD) updates straight from the official booking portal.

The portal's "Latest Updates" box (ticket release dates and times, special darshan and seva quotas, e-Dip
results, closures) is served by the site's own public JSON API, the same one the page reads. Each entry has a
stable id, so a new or changed announcement becomes one story. Entries that are over (sold out, a date in the
past) are skipped, so only things a devotee can still act on are made into reels. The official TTD news feed
(news.tirumala.org/feed) is read as an ordinary RSS source (config/sources.yaml).
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, timedelta, timezone

import httpx

from .models import Story

log = logging.getLogger(__name__)

PORTAL_API = "https://ttdevasthanams.ap.gov.in/cms/api/universal-latest-updates?populate=*"
PORTAL_URL = "https://ttdevasthanams.ap.gov.in/home/dashboard"
SOURCE_NAME = "TTD official website"
UA = "Mozilla/5.0 (compatible; tgnews/0.1)"
IST = timezone(timedelta(hours=5, minutes=30))
GRACE = timedelta(days=1)  # an announcement stays useful for a day after its booking date

_LINK = re.compile(r"\[[^\]]*\]\([^)]*\)")  # "[Click Here](/arjitha-seva/...)": the link is no use in a reel
_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b")
_BOOKING = re.compile(r"(?:from|w\.e\.f\.?,?|on|before)\s+(\d{1,2})\.(\d{1,2})\.(20\d{2})", re.I)
_OVER = re.compile(r"\b(?:has|have) been completed\b|\bcompleted\b|\bsold out\b", re.I)


def clean(text: str) -> str:
    text = _LINK.sub(" ", text or "")
    text = re.sub(r"\bPlease\s*(?:click here)?[.:]?\s*$", " ", " ".join(text.split()), flags=re.I)
    return " ".join(text.split())


def _day(d: str, m: str, y: str) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def reference_date(text: str) -> date | None:
    """The date the announcement is about: when booking opens ("from 06.11.2026"), else the latest date."""
    m = _BOOKING.search(text)
    if m and _day(*m.groups()):
        return _day(*m.groups())
    days = [d for d in (_day(*g) for g in _DATE.findall(text)) if d]
    return max(days) if days else None


def is_actionable(text: str, today: date) -> bool:
    """Something a devotee can still act on: not sold out, and its date has not passed."""
    if not text or _OVER.search(text):
        return False
    ref = reference_date(text)
    return ref is not None and ref >= today - GRACE


def parse_updates(payload: dict) -> list[tuple[int, str]]:
    """(entry id, cleaned text) for every entry of every record in the portal's reply."""
    out: list[tuple[int, str]] = []
    for rec in payload.get("data") or []:
        for u in (rec.get("attributes") or {}).get("update") or []:
            text = clean(str(u.get("data") or ""))
            if text and isinstance(u.get("id"), int):
                out.append((u["id"], text))
    return out


def to_story(entry_id: int, text: str, now: datetime) -> Story:
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    title = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0][:110].rstrip(" .,:;")
    return Story(id=f"ttd-{entry_id}-{digest}", title=title, summary=text,
                 url=f"{PORTAL_URL}#update-{entry_id}-{digest}", source_name=SOURCE_NAME,
                 published_at=now, topic_hint="temples")


def portal_stories(now: datetime | None = None, http: httpx.Client | None = None) -> list[Story]:
    """Today's actionable TTD announcements as stories; [] (and a log line) when the portal cannot be read."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(IST).date()
    try:
        resp = (http or httpx).get(PORTAL_API, headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        updates = parse_updates(resp.json())
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        log.warning("TTD portal unavailable (%s)", type(exc).__name__)
        return []
    return [to_story(i, t, now) for i, t in updates if is_actionable(t, today)]
