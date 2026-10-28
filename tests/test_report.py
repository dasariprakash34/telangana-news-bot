from datetime import datetime, timedelta, timezone

import anthropic
import httpx
import pytest
from sqlalchemy import create_engine, update

from tgnews import db, learn, report
from tgnews.models import Story

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return type("R", (), {"content": [type("B", (), {"text": self.reply})()]})()


def make(n):
    e = create_engine("sqlite:///:memory:")
    db.init(e)
    db.save_story(e, Story(id="s", title="t", summary="s", url="https://x/1", source_name="n",
                           published_at=NOW))
    for i in range(n):
        pid = db.save_post(e, story_id="s", format="card" if i % 2 else "carousel",
                           topic="politics", time_slot="09:00", headline_style="question")
        db.set_post_metrics(e, pid, {"score": 0.1 + (i % 5) / 100})
        with e.begin() as c:
            c.execute(update(db.posts).where(db.posts.c.id == pid)
                      .values(posted_at=NOW - timedelta(days=i % 10)))
    pid = db.save_post(e, story_id="s", format="card")
    db.set_post_metrics(e, pid, {"score": None})
    return e


def test_report_says_insufficient_data_under_30_posts():
    client = FakeClient("x")
    out = report.weekly_report(make(12), client, NOW)
    assert "12 scored posts" in out and "fewer than the 30" in out
    assert "no winner is declared" in out.lower() and "nothing is concluded" in out
    assert client.calls == []


def test_report_over_30_uses_model_text_with_facts_only():
    e = make(40)
    client = FakeClient("Mean score was 0.12 across 40 scored posts.")
    out = report.weekly_report(e, client, NOW)
    assert out == "Mean score was 0.12 across 40 scored posts."
    call = client.calls[0]
    assert "data" in call["system"].lower() and "speculate" in call["system"]
    content = call["messages"][0]["content"]
    assert "<facts>" in content and '"scored_posts": 40' in content
    assert "politics" in content and "SECRET" not in content


def test_report_falls_back_when_model_invents_a_number():
    out = report.weekly_report(make(40), FakeClient("Engagement rose 9876 percent."), NOW)
    assert "9876" not in out and "Weekly report: 40 scored posts" in out


@pytest.mark.parametrize("reply", ["", "   "])
def test_report_falls_back_on_empty_output(reply):
    assert report.weekly_report(make(40), FakeClient(reply), NOW).startswith("Weekly report: 40")


def test_report_falls_back_on_client_failure():
    out = report.weekly_report(make(40), FakeClient(exc=RuntimeError("boom")), NOW)
    assert out.startswith("Weekly report: 40") and "mean score" in out


def test_report_auth_errors_propagate():
    resp = httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com"))
    exc = anthropic.AuthenticationError("bad", response=resp, body=None)
    with pytest.raises(anthropic.AuthenticationError):
        report.weekly_report(make(40), FakeClient(exc=exc), NOW)


FACTS = '{"a": 0.1234, "b": 34, "slot": "09:00", "n": 40}'


@pytest.mark.parametrize("text,ok", [
    ("Mean 0.12 over 40 posts", True),
    ("Mean 0.1234", True),
    ("Best slot 9:00", True),
    ("Best slot 09:00", True),
    ("Score 0.34", False),
    ("Share was 34%", True),
    ("Share was 12%", False),
    ("Mean 0.13", False),
])
def test_numbers_ok_compares_values(text, ok):
    assert report._numbers_ok(text, FACTS) is ok


def test_system_prompt_requires_verbatim_values_and_slot_names():
    assert "verbatim" in report.SYSTEM_PROMPT and "slot names" in report.SYSTEM_PROMPT
