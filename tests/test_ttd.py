from datetime import date, datetime, timezone

import httpx
import pytest

from tgnews import ttd
from tgnews.fetch import RssSource, fetch_stories
from tgnews.models import Story
from tgnews.rewrite import RewrittenPost, numbers_not_in_source

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 7)
KUMKUM = ("Laksha Kumkumarchana tickets for 05.12.2026 at Sri Padmavathi Ammavari Temple, Tiruchanoor will be "
          "available for booking from 06.11.2026 at 03:00 PM. Please [Click Here](/arjitha-seva/slot-booking?x=1)\n ")
PAYLOAD = {"data": [
    {"id": 75, "attributes": {"update": [
        {"id": 139, "data": KUMKUM},
        {"id": 140, "data": "Special Entry Darshan (Rs.300) tickets for January-2027 have been completed."},
        {"id": 141, "data": "The SRIVANI Current Booking will remain closed on Saturday(03.10.2026)."},
        {"id": 142, "data": "TTD - Local Temples Seva Quota for December-2026 will be available for booking "
                            "w.e.f 10.11.2026 at 10:00 AM. Please [Click Here](/x)"},
    ]}},
    {"id": 76, "attributes": {"update": []}},
]}


def test_links_are_removed_from_the_text():
    assert "Click" not in ttd.clean(KUMKUM) and ttd.clean(KUMKUM).endswith("03:00 PM.")


def test_the_booking_date_is_the_one_that_counts():
    assert ttd.reference_date(ttd.clean(KUMKUM)) == date(2026, 11, 6)  # not the 05.12 seva date
    assert ttd.reference_date("Darshan 03.10.2026 and 05.10.2026") == date(2026, 10, 5)
    assert ttd.reference_date("no date here") is None


def test_only_things_a_devotee_can_still_act_on_are_kept():
    texts = {i: t for i, t in ttd.parse_updates(PAYLOAD)}
    assert set(texts) == {139, 140, 141, 142}
    kept = {i for i, t in texts.items() if ttd.is_actionable(t, TODAY)}
    assert kept == {139, 142}  # 140 sold out, 141 date past


def test_an_announcement_becomes_a_temples_story_with_a_stable_id():
    s = ttd.to_story(142, ttd.clean(PAYLOAD["data"][0]["attributes"]["update"][3]["data"]), NOW)
    assert s.topic_hint == "temples" and s.source_name == ttd.SOURCE_NAME and s.published_at == NOW
    assert s.title.startswith("TTD - Local Temples Seva Quota") and "w.e.f 10.11.2026" in s.summary
    assert s.url == ttd.to_story(142, s.summary, NOW).url  # the same text is the same story


def test_portal_stories_reads_the_api_and_survives_an_outage():
    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=PAYLOAD)))
    assert len(ttd.portal_stories(NOW, ok)) == 2
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    assert ttd.portal_stories(NOW, down) == []
    junk = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="not json")))
    assert ttd.portal_stories(NOW, junk) == []


FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>SV MUSEUM REMAINS TEMPORARILY SUSPENDED ON NOVEMBER 8 AND 9</title><link>https://news.tirumala.org/a</link>
<pubDate>Sat, 07 Nov 2026 04:37:00 +0000</pubDate><description>The museum will be closed on both days.</description></item>
<item><title>Total pilgrims who had darshan on 06.11.2026: 65,467</title><link>https://news.tirumala.org/b</link>
<pubDate>Sat, 07 Nov 2026 01:48:00 +0000</pubDate><description>Total pilgrims 65,467.</description></item>
</channel></rss>"""


def test_the_official_feed_needs_no_keyword_and_skips_the_daily_head_count():
    src = RssSource(url="x", name="TTD official news", fetch_text=lambda: FEED, topic="temples", unfiltered=True,
                    skip_titles=("Total pilgrims who had darshan",))
    [s] = fetch_stories([src])  # no "Telangana" anywhere in it
    assert s.title.startswith("SV MUSEUM") and s.topic_hint == "temples"


def story(summary):
    return Story(id="x", title="t", summary=summary, url="https://x", source_name="s", published_at=NOW, topic_hint="temples")


def post(headline, points=(), caption=""):
    return RewrittenPost(headline_te=headline, points_te=list(points), caption_te=caption, hashtags=[],
                         source_credit="", topic="temples", headline_style="statement")


def test_a_date_or_time_that_is_not_in_the_source_is_caught():
    src = story("tickets for 05.12.2026 available from 06.11.2026 at 03:00 PM, Rs. 300")
    assert numbers_not_in_source(post("టికెట్లు 6 నవంబర్ సాయంత్రం 3 గంటలకు", ["ధర 300 రూపాయలు", "డిసెంబర్ 5"]), src) == []
    bad = numbers_not_in_source(post("టికెట్లు 7 నవంబర్", ["ధర 500 రూపాయలు"]), src)
    assert bad and "7" in bad[0] and "500" in bad[0]
