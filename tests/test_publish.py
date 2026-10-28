import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, update

from tgnews import db
from tgnews.instagram import InstagramClient, InstagramError
from tgnews.models import PostState, Story
from tgnews.publish import (AlreadyPublishing, PublishError, publish_post,
                            reconcile_publishing, refresh_token, token_days_left)

FIX = Path(__file__).parent / "fixtures"
SECRET = "SECRET-TOKEN-123"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
URL1 = "https://pub.example.com/a.jpg"
URL2 = "https://pub.example.com/b.jpg"
URL3 = "https://pub.example.com/c.jpg"


def fx(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    db.init(e)
    db.save_story(e, Story(id="s1", title="t", summary="s", url="https://e.com/a", source_name="x",
                           published_at=NOW, topic_hint=None))
    return e


def new_post(engine, state=PostState.approved):
    return db.save_post(engine, story_id="s1", format="single", state=state)


class FakeClient:
    """Duck-typed InstagramClient replaying recorded fixtures."""
    ig_user_id = "IG1"

    def __init__(self, engine=None, statuses=("ig_status_finished.json",), publish_error=None,
                 create_error=None, recent=None):
        self.engine = engine
        self.calls = []
        self.statuses = list(statuses)
        self.publish_error = publish_error
        self.create_error = create_error
        self.recent = recent
        self.state_at_publish = None
        self.post_id = None
        self._n = 0

    def post(self, path, **params):
        self.calls.append(("POST", path, params))
        if path.endswith("/media"):
            if self.create_error:
                raise self.create_error
            self._n += 1
            return {"id": f"C{self._n}"}
        if path.endswith("/media_publish"):
            if self.engine is not None and self.post_id is not None:
                self.state_at_publish = db.get_post(self.engine, self.post_id).state
            if self.publish_error:
                raise self.publish_error
            return fx("ig_publish.json")
        raise AssertionError(path)

    def get(self, path, **params):
        self.calls.append(("GET", path, params))
        if params.get("fields") == "status_code":
            name = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return fx(name)
        if path.endswith("/media"):
            return self.recent if self.recent is not None else fx("ig_recent_media.json")
        raise AssertionError(path)

    def publish_calls(self):
        return [c for c in self.calls if c[1].endswith("/media_publish")]


def run(engine, pid, urls, client, **kw):
    client.post_id = pid
    return publish_post(pid, urls, "Telangana headline\n\n#telangana", client, engine,
                        sleep=kw.pop("sleep", lambda s: None), **kw)


def test_single_image_creates_container_then_publishes(engine):
    pid = new_post(engine)
    c = FakeClient(engine)
    assert run(engine, pid, [URL1], c) == "18000000000000009"
    kinds = [(m, p) for m, p, _ in c.calls]
    assert kinds == [("POST", "IG1/media"), ("GET", "C1"), ("POST", "IG1/media_publish")]
    assert c.calls[0][2] == {"image_url": URL1, "caption": "Telangana headline\n\n#telangana"}
    assert c.calls[2][2] == {"creation_id": "C1"}
    p = db.get_post(engine, pid)
    assert p.state == PostState.published and p.media_id == "18000000000000009"
    assert p.container_id == "C1" and p.posted_at is not None
    assert p.caption == "Telangana headline\n\n#telangana"


def test_carousel_creates_children_then_parent(engine):
    pid = new_post(engine)
    c = FakeClient(engine)
    run(engine, pid, [URL1, URL2, URL3], c)
    creates = [p for m, path, p in c.calls if path == "IG1/media"]
    assert creates[0] == {"image_url": URL1, "is_carousel_item": "true"}
    assert creates[1] == {"image_url": URL2, "is_carousel_item": "true"}
    assert creates[2] == {"image_url": URL3, "is_carousel_item": "true"}
    assert creates[3] == {"media_type": "CAROUSEL", "children": "C1,C2,C3",
                          "caption": "Telangana headline\n\n#telangana"}
    assert c.publish_calls()[0][2] == {"creation_id": "C4"}
    assert db.get_post(engine, pid).container_id == "C4"
    assert db.get_post(engine, pid).state == PostState.published


def test_container_not_ready_waits_then_publishes(engine):
    pid = new_post(engine)
    c = FakeClient(engine, statuses=["ig_status_in_progress.json", "ig_status_in_progress.json",
                                     "ig_status_finished.json"])
    sleeps = []
    run(engine, pid, [URL1], c, sleep=sleeps.append, poll_interval=7.0)
    assert sleeps == [7.0, 7.0]
    assert len(c.publish_calls()) == 1


def test_state_is_publishing_before_api_call(engine):
    pid = new_post(engine)
    c = FakeClient(engine)
    run(engine, pid, [URL1], c)
    assert c.state_at_publish == PostState.publishing


def test_api_error_marks_failed_and_does_not_retry(engine):
    # error before publish -> failed, publish never called
    pid = new_post(engine)
    c = FakeClient(engine, create_error=InstagramError("boom"))
    with pytest.raises(PublishError):
        run(engine, pid, [URL1], c)
    assert db.get_post(engine, pid).state == PostState.failed
    assert c.publish_calls() == []
    # error from media_publish -> outcome unknown: stays publishing, exactly one call
    pid2 = new_post(engine)
    c2 = FakeClient(engine, publish_error=InstagramError("timeout"))
    with pytest.raises(PublishError):
        run(engine, pid2, [URL1], c2)
    assert db.get_post(engine, pid2).state == PostState.publishing
    assert len(c2.publish_calls()) == 1


def test_unexpected_exception_from_publish_also_leaves_publishing(engine):
    pid = new_post(engine)
    c = FakeClient(engine, publish_error=RuntimeError("weird"))
    with pytest.raises(PublishError):
        run(engine, pid, [URL1], c)
    assert db.get_post(engine, pid).state == PostState.publishing
    assert len(c.publish_calls()) == 1


@pytest.mark.parametrize("state", [PostState.published, PostState.publishing])
def test_already_publishing_or_published_makes_no_api_call(engine, state):
    pid = new_post(engine, state)
    c = FakeClient(engine)
    with pytest.raises(AlreadyPublishing):
        run(engine, pid, [URL1], c)
    assert c.calls == []
    assert db.get_post(engine, pid).state == state


@pytest.mark.parametrize("state", [PostState.failed, PostState.rejected, PostState.expired])
def test_non_publishable_states_refused(engine, state):
    pid = new_post(engine, state)
    c = FakeClient(engine)
    with pytest.raises(PublishError):
        run(engine, pid, [URL1], c)
    assert c.calls == []


def test_draft_is_allowed(engine):
    pid = new_post(engine, PostState.draft)
    run(engine, pid, [URL1], FakeClient(engine))
    assert db.get_post(engine, pid).state == PostState.published


@pytest.mark.parametrize("status", ["ig_status_error.json", "ig_status_expired.json"])
def test_error_or_expired_status_marks_failed(engine, status):
    pid = new_post(engine)
    c = FakeClient(engine, statuses=[status])
    with pytest.raises(PublishError):
        run(engine, pid, [URL1], c)
    assert db.get_post(engine, pid).state == PostState.failed
    assert c.publish_calls() == []


def test_timeout_marks_failed(engine):
    pid = new_post(engine)
    c = FakeClient(engine, statuses=["ig_status_in_progress.json"])
    sleeps = []
    with pytest.raises(PublishError, match="not ready"):
        run(engine, pid, [URL1], c, sleep=sleeps.append, poll_interval=10.0, timeout=30.0)
    assert db.get_post(engine, pid).state == PostState.failed
    assert c.publish_calls() == []
    assert len(sleeps) == 3


def test_container_already_published_status_does_not_publish_again(engine):
    pid = new_post(engine)
    c = FakeClient(engine, statuses=["ig_status_published.json"])
    with pytest.raises(PublishError):
        run(engine, pid, [URL1], c)
    assert c.publish_calls() == []
    assert db.get_post(engine, pid).state == PostState.publishing  # reconcile decides


@pytest.mark.parametrize("urls", [[], [URL1] * 11, ["http://pub.example.com/a.jpg"],
                                  [URL1, "ftp://x/y.jpg"]])
def test_url_validation_before_any_call(engine, urls):
    pid = new_post(engine)
    c = FakeClient(engine)
    with pytest.raises(ValueError):
        run(engine, pid, urls, c)
    assert c.calls == []
    assert db.get_post(engine, pid).state == PostState.approved


def test_existing_publishing_post_checks_instagram_before_reposting(engine):
    pid = new_post(engine, PostState.publishing)
    db.set_post_caption(engine, pid, "Telangana headline\n\n#telangana")
    c = FakeClient(engine)
    # the post must predate the matching media item (10:05)
    with engine.begin() as conn:
        conn.execute(update(db.posts).where(db.posts.c.id == pid).values(
            created_at=datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)))
    assert reconcile_publishing(engine, c, NOW) == [pid]
    p = db.get_post(engine, pid)
    assert p.state == PostState.published and p.media_id == "18000000000000101"
    assert p.posted_at == datetime(2026, 10, 28, 10, 5, tzinfo=timezone.utc)
    assert c.publish_calls() == []
    assert not any(m == "POST" for m, _, _ in c.calls)
    assert c.calls[0][2] == {"fields": "id,caption,timestamp", "limit": 25}


def test_reconcile_ignores_media_older_than_post(engine):
    pid = new_post(engine, PostState.publishing)
    db.set_post_caption(engine, pid, "Telangana headline\n\n#telangana")
    with engine.begin() as conn:  # post created AFTER the matching media -> a stale identical caption
        conn.execute(update(db.posts).where(db.posts.c.id == pid).values(
            created_at=datetime(2026, 10, 28, 11, 0, tzinfo=timezone.utc),
            updated_at=NOW - timedelta(minutes=5)))
    assert reconcile_publishing(engine, FakeClient(engine), NOW) == []
    assert db.get_post(engine, pid).state == PostState.publishing


def _publishing(engine, minutes_ago):
    pid = new_post(engine, PostState.publishing)
    db.set_post_caption(engine, pid, "no such caption")
    with engine.begin() as conn:
        conn.execute(update(db.posts).where(db.posts.c.id == pid).values(
            updated_at=NOW - timedelta(minutes=minutes_ago)))
    return pid


def test_reconcile_no_match_and_old_becomes_failed(engine):
    pid = _publishing(engine, 45)
    c = FakeClient(engine)
    assert reconcile_publishing(engine, c, NOW) == [pid]
    assert db.get_post(engine, pid).state == PostState.failed
    assert c.publish_calls() == []


def test_reconcile_no_match_and_recent_stays_publishing(engine):
    pid = _publishing(engine, 5)
    assert reconcile_publishing(engine, FakeClient(engine), NOW) == []
    assert db.get_post(engine, pid).state == PostState.publishing


def test_reconcile_api_failure_leaves_state_untouched(engine):
    pid = _publishing(engine, 90)

    class Boom(FakeClient):
        def get(self, path, **params):
            raise InstagramError("down")
    with pytest.raises(InstagramError):
        reconcile_publishing(engine, Boom(engine), NOW)
    assert db.get_post(engine, pid).state == PostState.publishing


# ---- client / token safety -------------------------------------------------

def make_client(handler, **kw):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return InstagramClient(SECRET, "IG1", "v23.0", http=http, **kw)


def test_client_builds_versioned_url_and_never_puts_token_in_url(engine):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=fx("ig_container.json"))
    c = make_client(handler)
    assert c.post("IG1/media", image_url=URL1) == {"id": "17900000000000001"}
    c.get("17900000000000001", fields="status_code")
    assert str(seen[0].url) == "https://graph.instagram.com/v23.0/IG1/media"
    assert SECRET not in str(seen[1].url)
    assert seen[0].headers["authorization"] == f"Bearer {SECRET}"


def test_token_never_in_error_message_repr_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    c = make_client(lambda req: httpx.Response(400, json=fx("ig_error.json")))
    with pytest.raises(InstagramError) as ei:
        c.post("IG1/media", image_url=URL1)
    assert SECRET not in str(ei.value) and SECRET not in repr(ei.value)
    assert "OAuthException" in str(ei.value) and "190" in str(ei.value)
    assert "fbtrace" not in str(ei.value)
    assert SECRET not in repr(c) and SECRET not in str(c)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
    assert SECRET not in caplog.text


def test_transport_error_is_sanitized(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(req):
        raise httpx.ConnectError(f"cannot reach {req.url} token={SECRET}", request=req)
    c = make_client(handler)
    with pytest.raises(InstagramError) as ei:
        c.get("IG1/media", fields="id")
    assert SECRET not in str(ei.value) and "graph.instagram.com" not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
    assert SECRET not in caplog.text


def test_non_json_error_response_is_sanitized():
    c = make_client(lambda req: httpx.Response(502, text=f"<html>{SECRET}</html>"))
    with pytest.raises(InstagramError) as ei:
        c.get("IG1/media")
    assert SECRET not in str(ei.value) and "502" in str(ei.value)


def test_missing_api_version_names_variable():
    with pytest.raises(InstagramError, match="IG_API_VERSION"):
        InstagramClient.from_env({"IG_ACCESS_TOKEN": SECRET, "IG_USER_ID": "1"})
    with pytest.raises(InstagramError, match="IG_API_VERSION"):
        InstagramClient.from_env({"IG_ACCESS_TOKEN": SECRET, "IG_USER_ID": "1", "IG_API_VERSION": " "})


def test_from_env_db_token_wins(engine):
    env = {"IG_ACCESS_TOKEN": "BOOT", "IG_USER_ID": "1", "IG_API_VERSION": "v23.0"}
    seen = []
    http = httpx.Client(transport=httpx.MockTransport(
        lambda r: (seen.append(r), httpx.Response(200, json={}))[1]))
    db.set_kv(engine, "ig_access_token", "FROM-DB")
    c = InstagramClient.from_env(env, engine, http=http)
    c.get("x")
    assert seen[0].headers["authorization"] == "Bearer FROM-DB"
    c2 = InstagramClient.from_env(env, None, http=http)
    c2.get("x")
    assert seen[1].headers["authorization"] == "Bearer BOOT"


def test_refresh_token_stores_token_and_expiry(engine, caplog):
    caplog.set_level(logging.DEBUG)
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=fx("ig_refresh.json"))
    c = make_client(handler)
    tok = refresh_token(c, engine, NOW)
    assert seen[0].url.path == "/refresh_access_token"  # unversioned
    assert dict(seen[0].url.params)["grant_type"] == "ig_refresh_token"
    assert tok.access_token == "NEW-REFRESHED-TOKEN-456"
    assert tok.expires_at == NOW + timedelta(seconds=5183944)
    assert db.get_kv(engine, "ig_access_token") == "NEW-REFRESHED-TOKEN-456"
    assert datetime.fromisoformat(db.get_kv(engine, "ig_token_expires_at")) == tok.expires_at
    assert token_days_left(tok, NOW) == 59
    assert token_days_left(tok, NOW + timedelta(days=59, hours=1)) == 0
    assert "NEW-REFRESHED-TOKEN-456" not in repr(tok)
    assert "NEW-REFRESHED-TOKEN-456" not in caplog.text and SECRET not in caplog.text
    # client now uses the new token
    c.get("x")
    assert seen[-1].headers["authorization"] == "Bearer NEW-REFRESHED-TOKEN-456"


def test_refresh_token_rejects_malformed_response(engine):
    c = make_client(lambda r: httpx.Response(200, json={"token_type": "bearer"}))
    with pytest.raises(InstagramError):
        refresh_token(c, engine, NOW)
    assert db.get_kv(engine, "ig_access_token") is None
