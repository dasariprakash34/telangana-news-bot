from datetime import datetime, timezone

import pytest

from tgnews.models import Story
from tgnews.safety import Verdict, check


def _story(title: str, summary: str = "") -> Story:
    return Story(
        id="1",
        title=title,
        summary=summary,
        url="https://x.com/s",
        source_name="Sample",
        published_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
    )


def test_blocked_topic_blocks():
    s = _story("Mob attack in town", "Police say the riot was a communal clash between groups")
    assert check(s, "review") is Verdict.BLOCK
    assert check(s, "auto") is Verdict.BLOCK


def test_auto_mode_holds_borderline_story_for_review():
    s = _story("Police arrest two men over chain snatching")
    assert check(s, "auto") is Verdict.HOLD


def test_review_mode_allows_borderline_story():
    s = _story("Police arrest two men over chain snatching")
    assert check(s, "review") is Verdict.ALLOW


def test_clean_story_allowed_and_bad_mode_rejected():
    s = _story("New park opens in Hyderabad")
    assert check(s, "auto") is Verdict.ALLOW
    with pytest.raises(ValueError):
        check(s, "yolo")
