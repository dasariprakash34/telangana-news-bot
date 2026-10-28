from datetime import datetime, timezone

from tgnews.dedupe import dedupe
from tgnews.models import Story


def _story(id_: str, title: str, url: str) -> Story:
    return Story(
        id=id_,
        title=title,
        summary="",
        url=url,
        source_name="Sample",
        published_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
    )


def test_same_url_dropped():
    s = _story("1", "Metro fare revised", "https://x.com/a")
    assert dedupe([s], seen_urls={"https://x.com/a"}, recent_titles=[]) == []


def test_near_identical_title_dropped():
    recent = ["Heavy rain lashes Hyderabad, roads flooded"]
    near = _story("1", "Heavy rain lashes Hyderabad; roads flooded", "https://x.com/b")
    assert dedupe([near], seen_urls=set(), recent_titles=recent) == []
    # Duplicate within the same batch: only the first is kept, order preserved.
    a = _story("2", "Metro fare revised in Hyderabad", "https://x.com/c")
    b = _story("3", "Metro fare revised in Hyderabad.", "https://x.com/d")
    assert dedupe([a, b], seen_urls=set(), recent_titles=[]) == [a]


def test_distinct_story_kept():
    a = _story("1", "Heavy rain lashes Hyderabad", "https://x.com/e")
    b = _story("2", "Telangana budget session begins", "https://x.com/f")
    out = dedupe([a, b], seen_urls=set(), recent_titles=["Cricket final tonight"])
    assert out == [a, b]
