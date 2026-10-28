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


# --- fix round 1 ---
from tgnews.safety import BlocklistError


@pytest.mark.parametrize(
    "text,expected",
    [
        ("అత్యాచారానికి గురైన బాలిక", Verdict.BLOCK),
        ("అత్యాచారం కేసు", Verdict.BLOCK),
        ("నిందితుడు అరెస్టు", Verdict.HOLD),
        ("యువకుడు అరెస్ట్", Verdict.HOLD),
        ("ఎన్నికల ఫలితాలు", Verdict.HOLD),
        ("ఎన్నికలు ప్రారంభం", Verdict.HOLD),
        ("వృద్ధుడు మరణించారు", Verdict.HOLD),
        ("గుడిలో అల్లర్లలో గాయాలు", Verdict.BLOCK),
        ("ఆలయంలో ఉత్సవం", Verdict.HOLD),
    ],
)
def test_inflected_telugu_terms_match(text, expected):
    assert check(_story(text), "auto") is expected


@pytest.mark.parametrize(
    "title",
    [
        "Rapeseed prices rise in Nizamabad",
        "Deadline for crop insurance extended",
        "Farmers meet in the courtyard of collectorate",
        "Cured turmeric fetches good price",
    ],
)
def test_whole_word_matching_avoids_false_positives(title):
    assert check(_story(title), "auto") is Verdict.ALLOW


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Man raped in village, police say", Verdict.BLOCK),
        ("Three rapists held", Verdict.BLOCK),
        ("Police arrested the suspect", Verdict.HOLD),  # prefix keyword arrest*
        ("Court adjourns hearing", Verdict.HOLD),
    ],
)
def test_real_reports_still_match(title, expected):
    assert check(_story(title), "auto") is expected


def test_block_beats_hold():
    s = _story("Police arrest accused in rape case outside court")
    assert check(s, "review") is Verdict.BLOCK
    assert check(s, "auto") is Verdict.BLOCK


def _bl(tmp_path, text):
    p = tmp_path / "bl.yaml"
    p.write_text(text, encoding="utf-8")
    return p


@pytest.mark.parametrize(
    "text",
    [
        "",
        "group:\n  a:\n    verdict: block\n    keywords: [x]\n",
        "groups: {}\n",
        "- a\n- b\n",
        "groups: [a]\n",
        "groups:\n  a: just text\n",
        "groups:\n  a:\n    verdict: allow\n    keywords: [x]\n",
        "groups:\n  a:\n    keywords: [x]\n",
        "groups:\n  a:\n    verdict: block\n    keywords: riot\n",
        "groups:\n  a:\n    verdict: block\n    keywords: []\n",
        "groups:\n  a:\n    verdict: block\n    keywords: ['']\n",
        "groups:\n  a:\n    verdict: block\n    keywords: ['*']\n",
        "groups:\n  a:\n    verdict: block\n    keywords: [5]\n",
        "groups: [unclosed\n",
    ],
)
def test_malformed_blocklist_fails_closed(tmp_path, text):
    with pytest.raises(BlocklistError):
        check(_story("anything"), "auto", blocklist_path=_bl(tmp_path, text))


def test_missing_blocklist_fails_closed(tmp_path):
    with pytest.raises(BlocklistError):
        check(_story("x"), "auto", blocklist_path=tmp_path / "nope.yaml")


def test_custom_blocklist_prefix_syntax(tmp_path):
    p = _bl(tmp_path, "groups:\n  a:\n    verdict: hold\n    keywords: ['flood*', storm]\n")
    assert check(_story("Flooding in city"), "auto", blocklist_path=p) is Verdict.HOLD
    assert check(_story("Storms expected"), "auto", blocklist_path=p) is Verdict.ALLOW
    assert check(_story("A storm"), "auto", blocklist_path=p) is Verdict.HOLD
