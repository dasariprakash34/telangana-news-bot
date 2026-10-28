import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, update

from tgnews import db
from tgnews.analyze import (DEFAULT_WEIGHTS, MAX_POSTS_PER_RUN, InsightsUnavailable,
                            pull_insights, run_analysis, score)
from tgnews.instagram import InstagramError
from tgnews.models import PostState, Story

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
SECRET = "SECRET-TOKEN-123"


def values_shape(v):
    return {"data": [{"name": "x", "period": "lifetime", "values": [{"value": v}]}]}


def total_shape(v):
    return {"data": [{"name": "x", "total_value": {"value": v}}]}


class FakeClient:
    def __init__(self, data=None, refuse=(), fail_media=(), shape=values_shape):
        self.data = data or {"reach": 100, "likes": 10, "comments": 5, "shares": 3,
                             "saved": 2, "views": 500}
        self.refuse = set(refuse)
        self.fail_media = set(fail_media)
        self.shape = shape
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        media = path.split("/")[0]
        metric = params["metric"]
        if media in self.fail_media or metric in self.refuse:
            raise InstagramError("Instagram API error (HTTP 400): message=unsupported metric")
        return self.shape(self.data[metric])


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    db.init(e)
    db.save_story(e, Story(id="s1", title="t", summary="s", url="https://e.com/a",
                           source_name="x", published_at=NOW, topic_hint=None))
    return e


def published(engine, media_id, age_hours=72):
    pid = db.save_post(engine, story_id="s1", format="single", state=PostState.published)
    db.set_post_media(engine, pid, media_id, NOW - timedelta(hours=age_hours))
    return pid


def test_score_formula_matches_spec():
    m = {"reach": 100, "likes": 10, "comments": 5, "shares": 3, "saves": 2, "views": 500}
    expected = (3 * 1.0 + 2 * 1.0 + 10 * 0.1 + 5 * 0.2) / 100
    assert score(m) == pytest.approx(expected) == pytest.approx(0.07)
    assert DEFAULT_WEIGHTS == {"shares": 1.0, "saves": 1.0, "likes": 0.1, "comments": 0.2}
    assert score(m, {"shares": 2.0, "saves": 0.0, "likes": 0.0, "comments": 0.0}) == pytest.approx(0.06)


def test_zero_reach_returns_none():
    base = {"likes": 1, "comments": 1, "shares": 1, "saves": 1}
    assert score({**base, "reach": 0}) is None
    assert score({**base, "reach": None}) is None


def test_missing_core_signals_return_none_and_missing_likes_comments_count_zero():
    assert score({"reach": 100, "shares": None, "saves": 2, "likes": 1, "comments": 1}) is None
    assert score({"reach": 100, "shares": 3, "saves": None, "likes": 1, "comments": 1}) is None
    assert score({"reach": 100, "shares": 3, "saves": 2, "likes": None,
                  "comments": None}) == pytest.approx(0.05)


def test_pull_insights_one_call_per_metric_and_maps_saved():
    c = FakeClient()
    m = pull_insights("M1", c)
    assert m == {"reach": 100, "likes": 10, "comments": 5, "shares": 3, "saves": 2, "views": 500}
    assert [(p, q) for p, q in c.calls] == [
        (f"M1/insights", {"metric": n}) for n in
        ("reach", "likes", "comments", "shares", "saved", "views")]


def test_both_response_shapes_parsed():
    assert pull_insights("M1", FakeClient(shape=total_shape))["shares"] == 3
    assert pull_insights("M1", FakeClient(shape=values_shape))["shares"] == 3


def test_refused_metric_marked_unavailable_not_raised():
    m = pull_insights("M1", FakeClient(refuse={"views", "shares"}))
    assert m["views"] is None and m["shares"] is None and m["reach"] == 100


def test_all_metrics_failing_raises_sanitized():
    with pytest.raises(InsightsUnavailable):
        pull_insights("M1", FakeClient(fail_media={"M1"}))


def test_malformed_payload_is_none():
    class C(FakeClient):
        def get(self, path, **params):
            if params["metric"] == "likes":
                return {"data": []}
            return super().get(path, **params)
    assert pull_insights("M1", C())["likes"] is None


def test_run_analysis_records_metrics_and_score(engine):
    pid = published(engine, "M1")
    assert run_analysis(engine, FakeClient(), NOW) == 1
    metrics = db.get_post(engine, pid).metrics
    assert metrics["shares"] == 3 and metrics["saves"] == 2
    assert metrics["score"] == pytest.approx(0.07)
    assert db.posts_due_for_analysis(engine, NOW) == []


def test_posts_under_48h_are_skipped(engine):
    pid = published(engine, "M1", age_hours=47)
    c = FakeClient()
    assert run_analysis(engine, c, NOW) == 0
    assert c.calls == []
    assert db.get_post(engine, pid).metrics is None


def test_all_failing_post_not_stored_others_continue(engine):
    bad = published(engine, "BAD", age_hours=100)
    good = published(engine, "GOOD", age_hours=90)
    assert run_analysis(engine, FakeClient(fail_media={"BAD"}), NOW) == 1
    assert db.get_post(engine, bad).metrics is None
    assert db.get_post(engine, good).metrics is not None
    assert [p.id for p in db.posts_due_for_analysis(engine, NOW)] == [bad]


def test_all_none_values_not_stored(engine):
    pid = published(engine, "M1")
    c = FakeClient(data={k: None for k in ("reach", "likes", "comments", "shares", "saved", "views")})
    assert run_analysis(engine, c, NOW) == 0
    assert db.get_post(engine, pid).metrics is None


def test_per_post_error_isolation(engine):
    bad = published(engine, "BAD", age_hours=100)
    good = published(engine, "GOOD", age_hours=90)

    class C(FakeClient):
        def get(self, path, **params):
            if path.startswith("BAD"):
                raise RuntimeError("boom")
            return super().get(path, **params)
    assert run_analysis(engine, C(), NOW) == 1
    assert db.get_post(engine, good).metrics is not None
    assert db.get_post(engine, bad).metrics is None


def test_metric_with_null_score_still_stored(engine):
    pid = published(engine, "M1")
    c = FakeClient(refuse={"shares"})
    assert run_analysis(engine, c, NOW) == 1
    m = db.get_post(engine, pid).metrics
    assert m["score"] is None and m["reach"] == 100


def test_post_cap_per_run(engine):
    for i in range(MAX_POSTS_PER_RUN + 3):
        published(engine, f"M{i}")
    c = FakeClient()
    assert run_analysis(engine, c, NOW) == MAX_POSTS_PER_RUN
    assert len(c.calls) <= MAX_POSTS_PER_RUN * 6
    assert len(db.posts_due_for_analysis(engine, NOW)) == 3


def test_token_never_logged(engine, caplog):
    published(engine, "BAD")

    class C(FakeClient):
        def get(self, path, **params):
            raise InstagramError("Instagram API error (HTTP 190): message=expired [REDACTED]")
    with caplog.at_level(logging.DEBUG):
        assert run_analysis(engine, C(), NOW) == 0
    assert SECRET not in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_real_client_error_scrubs_token_from_logs(engine, caplog):
    import httpx
    from tgnews.instagram import InstagramClient
    published(engine, "M1")

    def handler(request):
        return httpx.Response(400, json={"error": {"message": f"bad token {SECRET}",
                                                   "type": "OAuthException", "code": 190}})
    client = InstagramClient(SECRET, "IG1", "v23.0",
                             http=httpx.Client(transport=httpx.MockTransport(handler)))
    with caplog.at_level(logging.DEBUG):
        assert run_analysis(engine, client, NOW) == 0
    assert SECRET not in caplog.text
    assert "REDACTED" in caplog.text
