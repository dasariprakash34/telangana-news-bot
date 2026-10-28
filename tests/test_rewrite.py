import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tgnews.models import Story
from tgnews.rewrite import RewriteFailed, RewrittenPost, rewrite, validate

STORY = Story(
    id="s1",
    title="Hyderabad rains lash city",
    summary="Heavy rain lashed Hyderabad on Monday, flooding several roads.",
    url="https://example.com/a",
    source_name="The Hindu",
    published_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
    topic_hint="weather",
)

GOOD = dict(
    headline_te="హైదరాబాద్‌లో భారీ వర్షం, రోడ్లు జలమయం",
    points_te=["సోమవారం నగరంలో భారీ వర్షం కురిసింది", "పలు రోడ్లు నీట మునిగాయి"],
    caption_te="హైదరాబాద్ నగరాన్ని భారీ వర్షం ముంచెత్తింది. పలు ప్రాంతాల్లో రోడ్లు జలమయమయ్యాయి. మూలం: The Hindu",
    hashtags=["#Hyderabad", "#Rain"],
    source_credit="The Hindu",
    topic="weather",
    headline_style="statement",
)


def good(**kw):
    return RewrittenPost(**{**GOOD, **kw})


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.replies.pop(0)

        class R:
            content = [type("B", (), {"text": text})()]

        return R()


def test_valid_telugu_post_passes():
    assert validate(good()) == []


def test_english_only_headline_fails():
    problems = validate(good(headline_te="Heavy rain lashes Hyderabad"))
    assert any("headline" in p for p in problems)


def test_missing_source_credit_fails():
    assert validate(good(source_credit="")) != []
    problems = validate(good(source_credit="Eenadu"))
    assert any("source_credit" in p for p in problems)


def test_overlong_headline_fails():
    problems = validate(good(headline_te="వర్షం " * 20))
    assert any("90" in p for p in problems)


def test_rewrite_retries_once_then_raises_RewriteFailed():
    bad = json.dumps({**GOOD, "headline_te": "English only"})
    client = FakeClient([bad, bad, bad])
    with pytest.raises(RewriteFailed):
        rewrite(STORY, client, "statement")
    assert len(client.calls) == 2
    assert "headline" in client.calls[1]["messages"][0]["content"]


def test_story_with_empty_summary_is_skipped():
    client = FakeClient([])
    with pytest.raises(RewriteFailed):
        rewrite(replace(STORY, summary="   "), client, "statement")
    assert client.calls == []


def test_rewrite_returns_post_and_recovers_on_retry():
    client = FakeClient(["not json", "```json\n" + json.dumps(GOOD) + "\n```"])
    post = rewrite(STORY, client, "number")
    assert post.headline_style == "number"
    assert post.topic == "weather"
    assert len(client.calls) == 2


def test_prompt_delimits_untrusted_text():
    client = FakeClient([json.dumps(GOOD)])
    rewrite(STORY, client, "question")
    call = client.calls[0]
    assert "<story_title>" in call["messages"][0]["content"]
    assert "ignore" in call["system"].lower()


def test_unknown_style_raises_value_error():
    with pytest.raises(ValueError):
        rewrite(STORY, FakeClient([]), "clickbait")
