from __future__ import annotations

import hashlib
import html
import logging
import re
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol

import feedparser
import httpx
import yaml

from tgnews.models import Story

log = logging.getLogger(__name__)

TELANGANA_KEYWORDS: tuple[str, ...] = (
    "Telangana", "Hyderabad", "Warangal", "Karimnagar", "Khammam", "Nizamabad",
    "తెలంగాణ", "హైదరాబాద్", "వరంగల్", "కరీంనగర్", "ఖమ్మం", "నిజామాబాద్",
)

TOPICS = (
    "politics", "crime", "weather", "jobs", "farming",
    "sports", "education", "health", "business", "other",
)

# Checked in order; first topic with a matching word-prefix wins.
_TOPIC_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("weather", ("rain", "thunder", "flood", "cyclone", "heatwave", "weather", "monsoon", "temperature",
                 "వర్ష", "వాతావరణ", "వరద")),
    ("crime", ("police", "arrest", "murder", "robbery", "theft", "crime", "accused", "fraud", "cheating",
               "పోలీసు", "అరెస్ట్", "హత్య", "దొంగతనం")),
    ("politics", ("minister", "chief minister", "assembly", "mla", "mp ", "election", "party", "congress",
                  "brs", "bjp", "government", "govt", "cm ", "రాజకీయ", "మంత్రి", "ఎన్నిక", "ప్రభుత్వ")),
    ("farming", ("farmer", "paddy", "crop", "cotton", "agricultur", "irrigation", "harvest", "rythu",
                 "రైతు", "వరి", "పత్తి", "వ్యవసాయ")),
    ("jobs", ("job", "recruitment", "vacanc", "hiring", "employment", "notification", "ఉద్యోగ", "నియామక")),
    ("sports", ("cricket", "football", "kabaddi", "tournament", "olympic", "ipl", "athlete", "badminton",
                "క్రీడ", "క్రికెట్")),
    ("education", ("school", "college", "university", "student", "exam", "admission", "scholarship",
                   "విద్య", "పాఠశాల", "విశ్వవిద్యాలయ", "పరీక్ష")),
    ("health", ("hospital", "dengue", "doctor", "health", "disease", "vaccine", "medical", "fever",
                "ఆసుపత్రి", "ఆరోగ్య")),
    ("business", ("sensex", "market", "startup", "invest", "company", "funding", "industry", "stock", "gst",
                  "వ్యాపార", "పెట్టుబడి")),
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class NewsSource(Protocol):
    def fetch(self) -> list[Story]: ...


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text or ""))).strip()


def assign_topic_hint(title: str, summary: str) -> str:
    text = f"{title} {summary}".lower()
    for topic, words in _TOPIC_KEYWORDS:
        for w in words:
            if re.search(r"(?<!\w)" + re.escape(w), text):
                return topic
    return "other"


def _story_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def _entry_time(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc)
    return None


@dataclass
class RssSource:
    url: str
    name: str
    fetch_text: Callable[[], str] | None = None
    timeout: float = 15.0

    def _download(self) -> str:
        if self.fetch_text is not None:
            return self.fetch_text()
        resp = httpx.get(self.url, timeout=self.timeout, follow_redirects=True)
        resp.raise_for_status()
        return resp.text

    def fetch(self) -> list[Story]:
        feed = feedparser.parse(self._download())
        if feed.get("bozo") and not feed.entries:
            log.warning("feed %s (%s) returned no entries and was malformed: %r",
                        self.name, self.url, feed.get("bozo_exception"))
        stories: list[Story] = []
        for entry in feed.entries:
            url = (entry.get("link") or "").strip()
            title = _clean(entry.get("title", ""))
            if not url or not title:
                continue
            published_at = _entry_time(entry)
            if published_at is None:
                log.info("skipping undated entry from %s: %s", self.name, url)
                continue
            summary = _clean(entry.get("summary", ""))
            stories.append(
                Story(
                    id=_story_id(url),
                    title=title,
                    summary=summary,
                    url=url,
                    source_name=self.name,
                    published_at=published_at,
                    topic_hint=assign_topic_hint(title, summary),
                )
            )
        return stories


def _matches(story: Story, keywords: Iterable[str]) -> bool:
    text = f"{story.title} {story.summary}".casefold()
    return any(k.casefold() in text for k in keywords)


@dataclass
class FetchReport:
    """How many sources were tried and how many raised."""
    sources: int = 0
    failed: int = 0


def fetch_stories(
    sources: Iterable[NewsSource], keywords: Iterable[str] = TELANGANA_KEYWORDS,
    report: FetchReport | None = None,
) -> list[Story]:
    """Fetch and filter stories. Pass a FetchReport to learn how many sources failed."""
    keywords = tuple(keywords)
    out: list[Story] = []
    seen: set[str] = set()
    if report is not None:
        report.sources = report.failed = 0
    for src in sources:
        if report is not None:
            report.sources += 1
        try:
            stories = src.fetch()
        except Exception:
            log.exception("news source failed: %r", src)
            if report is not None:
                report.failed += 1
            continue
        for s in stories:
            if s.id in seen or not _matches(s, keywords):
                continue
            seen.add(s.id)
            out.append(s)
    return out


def load_sources(path: str | Path) -> list[RssSource]:
    p = Path(path)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [RssSource(url=f["url"], name=f["name"]) for f in (data.get("feeds") or [])]
