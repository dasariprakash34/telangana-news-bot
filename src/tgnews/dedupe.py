from __future__ import annotations

from difflib import SequenceMatcher
from typing import Iterable

from tgnews.models import Story


def _similar(a: str, b: str, threshold: float) -> bool:
    return SequenceMatcher(None, a, b).ratio() >= threshold


def dedupe(
    stories: Iterable[Story],
    seen_urls: Iterable[str],
    recent_titles: Iterable[str],
    threshold: float = 0.85,
) -> list[Story]:
    """Drop stories already seen by URL or near-duplicate by title.

    Order is preserved. A story is dropped when its URL is in ``seen_urls``,
    or its casefolded title has SequenceMatcher ratio >= ``threshold`` against
    any recent title or any story already kept from this batch.
    """
    seen = set(seen_urls)
    known_titles = [t.casefold() for t in recent_titles]
    kept: list[Story] = []
    for story in stories:
        if story.url in seen:
            continue
        title = story.title.casefold()
        if any(_similar(title, known, threshold) for known in known_titles):
            continue
        kept.append(story)
        known_titles.append(title)
    return kept
