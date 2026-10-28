from datetime import timezone
from pathlib import Path

import pytest

from tgnews.fetch import (
    RssSource,
    assign_topic_hint,
    fetch_stories,
    load_sources,
)
from tgnews.models import Story

FIXTURES = Path(__file__).parent / "fixtures"


def _source(fixture: str, name: str = "Sample") -> RssSource:
    xml = (FIXTURES / fixture).read_text(encoding="utf-8")
    return RssSource(url=f"https://example.com/{fixture}", name=name, fetch_text=lambda: xml)


def test_rss_parses_stories():
    stories = _source("mixed_feed.xml", "Sample City").fetch()
    assert len(stories) == 3
    s = stories[0]
    assert isinstance(s, Story)
    assert s.title.startswith("Heavy rain lashes Hyderabad")
    assert s.url == "https://news.example.com/hyderabad-rain"
    assert s.source_name == "Sample City"
    assert "<" not in s.summary and "Hyderabad" in s.summary
    assert s.published_at.tzinfo is not None
    assert s.published_at.astimezone(timezone.utc).hour == 14
    assert len(s.id) == 40
    assert s.id == _source("mixed_feed.xml").fetch()[0].id  # stable


def test_non_telangana_filtered_out():
    stories = fetch_stories([_source("mixed_feed.xml")])
    titles = " ".join(s.title for s in stories)
    assert "Hyderabad" in titles
    assert "Warangal" in titles
    assert "Mumbai" not in titles
    assert len(stories) == 2


def test_telugu_keywords_match():
    stories = fetch_stories([_source("telugu_feed.xml")])
    assert [s.url for s in stories] == ["https://telugu.example.com/job-mela"]


def test_keyword_match_is_case_insensitive():
    stories = fetch_stories([_source("mixed_feed.xml")], keywords=("HYDERABAD",))
    assert len(stories) == 1


class _Boom:
    def fetch(self):
        raise RuntimeError("network down")


def test_one_failing_source_does_not_stop_others():
    stories = fetch_stories([_Boom(), _source("mixed_feed.xml")])
    assert len(stories) == 2


def test_failing_fetch_text_is_isolated():
    def bad():
        raise OSError("timeout")

    broken = RssSource(url="https://x", name="Broken", fetch_text=bad)
    assert len(fetch_stories([broken, _source("mixed_feed.xml")])) == 2


def test_duplicate_urls_across_sources_deduped():
    stories = fetch_stories([_source("mixed_feed.xml", "A"), _source("mixed_feed.xml", "B")])
    assert len(stories) == 2


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Heavy rain and thunderstorm alert", "weather"),
        ("Police arrest accused in robbery case", "crime"),
        ("Minister meets chief minister before assembly session", "politics"),
        ("Farmers protest paddy procurement", "farming"),
        ("Group 2 recruitment notification released", "jobs"),
        ("Hyderabad FC wins cricket match", "sports"),
        ("Osmania University exam results out", "education"),
        ("Dengue cases rise at hospital", "health"),
        ("Sensex rises as startup raises funding", "business"),
        ("Temple festival draws crowds", "other"),
    ],
)
def test_assign_topic_hint(text, expected):
    assert assign_topic_hint(text, "") == expected


def test_stories_carry_topic_hint():
    stories = _source("mixed_feed.xml").fetch()
    assert stories[0].topic_hint == "weather"
    assert stories[1].topic_hint == "farming"


def test_load_sources(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("feeds:\n  - name: A\n    url: https://a.example/rss\n", encoding="utf-8")
    out = load_sources(p)
    assert [(s.name, s.url) for s in out] == [("A", "https://a.example/rss")]


def test_load_sources_missing_or_empty(tmp_path):
    assert load_sources(tmp_path / "nope.yaml") == []
    empty = tmp_path / "e.yaml"
    empty.write_text("", encoding="utf-8")
    assert load_sources(empty) == []
    assert load_sources(Path(__file__).parent.parent / "config" / "sources.yaml") == []
