import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, update

from tgnews import db
from tgnews.analyze import (DEFAULT_WEIGHTS, MAX_POSTS_PER_RUN, InsightsAborted, InsightsUnavailable, _extract,
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
    pid = published(engine, "M1", age_hours=24 * 8)
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
        with pytest.raises(InsightsAborted):
            run_analysis(engine, client, NOW)
    assert SECRET not in caplog.text
    assert "REDACTED" in caplog.text


def test_stuck_posts_do_not_starve_newer(engine):
    for i in range(MAX_POSTS_PER_RUN + 5):
        published(engine, f"OLD{i}", age_hours=24 * 10 + i)
    fresh = published(engine, "FRESH", age_hours=60)
    bad = {f"OLD{i}" for i in range(MAX_POSTS_PER_RUN + 5)}
    assert run_analysis(engine, FakeClient(fail_media=bad), NOW) == 1
    assert db.get_post(engine, fresh).metrics is not None


def test_posts_without_media_id_dont_use_cap(engine):
    for _ in range(MAX_POSTS_PER_RUN + 2):
        pid = db.save_post(engine, story_id="s1", format="single", state=PostState.published)
        db.set_post_media(engine, pid, "", NOW - timedelta(hours=100))
    good = published(engine, "GOOD", age_hours=60)
    assert run_analysis(engine, FakeClient(), NOW) == 1
    assert db.get_post(engine, good).metrics is not None


def test_posts_older_than_max_age_skipped(engine):
    pid = published(engine, "OLD", age_hours=24 * 15)
    c = FakeClient()
    assert run_analysis(engine, c, NOW) == 0 and c.calls == []
    assert db.get_post(engine, pid).metrics is None


def test_partial_young_post_retried_old_post_stored(engine):
    young = published(engine, "Y", age_hours=24 * 3)
    old = published(engine, "O", age_hours=24 * 8)
    assert run_analysis(engine, FakeClient(refuse={"saved"}), NOW) == 1
    assert db.get_post(engine, young).metrics is None
    m = db.get_post(engine, old).metrics
    assert m["score"] is None and m["saves"] is None


class AuthDies(FakeClient):
    def __init__(self, after, **kw):
        super().__init__(**kw)
        self.after = after

    def get(self, path, **params):
        if len(self.calls) >= self.after:
            self.calls.append((path, params))
            raise InstagramError("Instagram API error (HTTP 400): code=190", status=400, code=190)
        return super().get(path, **params)


def test_token_dying_midway_stores_nothing_and_aborts(engine):
    first = published(engine, "A", age_hours=100)
    second = published(engine, "B", age_hours=90)
    with pytest.raises(InsightsAborted):
        run_analysis(engine, AuthDies(after=2), NOW)
    assert db.get_post(engine, first).metrics is None
    assert db.get_post(engine, second).metrics is None


def test_persistent_auth_error_aborts_after_first_call(engine):
    for i in range(5):
        published(engine, f"M{i}")
    c = AuthDies(after=0)
    with pytest.raises(InsightsAborted):
        run_analysis(engine, c, NOW)
    assert len(c.calls) == 1


def test_stored_records_kept_when_later_post_aborts(engine):
    newer = published(engine, "NEW", age_hours=60)
    older = published(engine, "OLD", age_hours=100)
    c = AuthDies(after=6)
    with pytest.raises(InsightsAborted):
        run_analysis(engine, c, NOW)
    assert db.get_post(engine, newer).metrics is not None
    assert db.get_post(engine, older).metrics is None


@pytest.mark.parametrize("status,code", [(401, None), (400, 190), (400, 102), (429, None),
                                         (400, 4), (400, 17), (400, 32), (400, 613)])
def test_abort_classification(status, code):
    class C(FakeClient):
        def get(self, path, **params):
            raise InstagramError("x", status=status, code=code)
    with pytest.raises(InsightsAborted):
        pull_insights("M1", C())


def test_other_4xx_is_just_unavailable():
    class C(FakeClient):
        def get(self, path, **params):
            if params["metric"] == "views":
                raise InstagramError("x", status=400, code=100)
            return super().get(path, **params)
    assert pull_insights("M1", C())["views"] is None


def test_client_populates_status_and_code():
    import httpx
    from tgnews.instagram import InstagramClient
    h = lambda r: httpx.Response(400, json={"error": {"message": "m", "code": 190}})
    c = InstagramClient(SECRET, "IG1", "v23.0", http=httpx.Client(transport=httpx.MockTransport(h)))
    with pytest.raises(InstagramError) as ei:
        c.get("M1/insights", metric="reach")
    assert ei.value.status == 400 and ei.value.code == 190 and SECRET not in repr(ei.value.args)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, "5"])
def test_extract_rejects_invalid_numbers(bad):
    assert _extract(values_shape(bad)) is None
    assert _extract(total_shape(bad)) is None


def test_set_post_metrics_idempotent(engine):
    pid = published(engine, "M1")
    assert db.set_post_metrics(engine, pid, {"a": 1}) is True
    assert db.set_post_metrics(engine, pid, {"a": 2}) is False
    assert db.get_post(engine, pid).metrics == {"a": 1}
