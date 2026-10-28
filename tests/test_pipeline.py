import json
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from tgnews import db, pipeline
from tgnews.config import Settings
from tgnews.gate import TelegramError
from tgnews.instagram import InstagramError
from tgnews.models import PostState, Story
from tgnews.pipeline import Deps, active_slots, nearest_slot, publish_approved, run_once
from tgnews.render import ShapingUnavailable
from tgnews.safety import BlocklistError

NOW = datetime(2026, 10, 28, 7, 5, tzinfo=timezone.utc)  # nearest slot 07:00
REPLY = json.dumps({
    "headline_te": "హైదరాబాద్‌లో భారీ వర్షం, రోడ్లు జలమయం",
    "points_te": ["సోమవారం నగరంలో భారీ వర్షం కురిసింది", "పలు రోడ్లు నీట మునిగాయి"],
    "caption_te": "హైదరాబాద్ నగరాన్ని భారీ వర్షం ముంచెత్తింది. పలు ప్రాంతాల్లో రోడ్లు జలమయమయ్యాయి. మూలం: The Hindu",
    "hashtags": ["#Hyderabad", "#Rain"],
    "topic": "weather",
})


TITLES = ["Hyderabad metro adds new stations", "Warangal farmers get fresh seed subsidy",
          "Karimnagar school wins state science prize", "Khammam hospital opens new trauma ward",
          "Nizamabad turmeric prices climb sharply", "Telangana IT exports touch record high"]


def story(n, title=None, summary="Heavy rain lashed Hyderabad on Monday.", age_h=1, hint="weather"):
    return Story(id=f"s{n}", title=title or TITLES[n % len(TITLES)],
                 summary=summary, url=f"https://example.com/{n}", source_name="The Hindu",
                 published_at=NOW - timedelta(hours=age_h), topic_hint=hint)


class FakeLLM:
    def __init__(self, bad_titles=(), error=None):
        self.bad_titles, self.error, self.calls = tuple(bad_titles), error, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        text = kw["messages"][0]["content"]
        body = "not json" if any(t in text for t in self.bad_titles) else REPLY
        return type("R", (), {"content": [type("B", (), {"text": body})()], "stop_reason": "end_turn"})()


class FakeIG:
    ig_user_id = "IG1"

    def __init__(self, fail_create=False):
        self.calls, self.fail_create, self.n, self.created = [], fail_create, 0, []

    def post(self, path, **p):
        self.calls.append(("POST", path))
        if path.endswith("/media_publish"):
            return {"id": "MEDIA1"}
        if self.fail_create:
            raise self.fail_create if isinstance(self.fail_create, Exception) else InstagramError("create failed")
        self.created.append(p)
        self.n += 1
        return {"id": f"C{self.n}"}

    def get(self, path, **p):
        self.calls.append(("GET", path))
        return {"status_code": "FINISHED"}


class FakeTG:
    chat_id = "1"

    def __init__(self):
        self.calls, self.n = [], 100

    def call(self, method, **payload):
        self.calls.append((method, payload))
        self.n += 1
        if method == "getUpdates":
            return []
        return {"message_id": self.n}


@pytest.fixture
def engine():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.init(e)
    return e


def make_deps(engine, tmp_path, stories, *, llm=None, ig=None, tg=None, **kw):
    uploads = []

    def render(post, fmt, out_dir, brand):
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = [out_dir / f"{fmt}-{i}.jpg" for i in range(1 if fmt == "card" else 3)]
        for p in paths:
            p.write_bytes(b"jpg")
        return paths

    def upload(paths, prefix):
        uploads.append(prefix)
        return [f"https://cdn.example.com/{prefix}/{p.name}" for p in paths]

    d = Deps(fetch=lambda: list(stories), llm=llm or FakeLLM(), render=render, upload=upload,
             instagram=ig, telegram=tg, engine=engine, rng_factory=lambda: random.Random(1),
             now=lambda: NOW, out_dir=tmp_path / "work", sleep=lambda s: None,
             check_shaping=lambda: None)
    d.uploads = uploads
    return replace(d, **kw) if kw else d


def settings(**kw):
    return Settings(mode=kw.pop("mode", "review"), **kw)


def all_posts(engine):
    return [p for st in PostState for p in db.list_posts_by_state(engine, st)]


def test_review_mode_creates_draft_and_does_not_publish(engine, tmp_path):
    ig, tg = FakeIG(), FakeTG()
    deps = make_deps(engine, tmp_path, [story(1)], ig=ig, tg=tg)
    summary = run_once(settings(), deps)
    assert (summary.fetched, summary.new, summary.drafted, summary.published) == (1, 1, 1, 0)
    [post] = all_posts(engine)
    assert post.state is PostState.draft and post.caption and "మూలం: The Hindu" in post.caption
    assert post.time_slot == "07:00" and post.topic == "weather"
    assert ig.calls == []
    assert [m for m, _ in tg.calls].count("sendMessage") == 1
    assert db.story_seen(engine, "https://example.com/1")
    [row] = engine.connect().execute(db.drafts.select()).all()
    assert all(u.startswith("https://cdn.example.com/") for u in row.slides)


def test_auto_mode_publishes_allowed_story(engine, tmp_path):
    ig = FakeIG()
    deps = make_deps(engine, tmp_path, [story(1)], ig=ig)
    summary = run_once(settings(mode="auto"), deps)
    assert (summary.published, summary.held, summary.failed) == (1, 0, 0)
    [post] = all_posts(engine)
    assert post.state is PostState.published and post.media_id == "MEDIA1"
    assert [c for c in ig.calls if c[1].endswith("/media_publish")] == [("POST", "IG1/media_publish")]


def test_auto_mode_holds_flagged_story_as_draft(engine, tmp_path):
    ig, tg = FakeIG(), FakeTG()
    flagged = story(1, title="Man arrested in Hyderabad over theft")
    summary = run_once(settings(mode="auto"), make_deps(engine, tmp_path, [flagged], ig=ig, tg=tg))
    assert (summary.held, summary.published, summary.drafted) == (1, 0, 0)
    [post] = all_posts(engine)
    assert post.state is PostState.draft
    assert ig.calls == []
    assert any(m == "sendMessage" for m, _ in tg.calls)


def test_held_story_without_telegram_is_saved_draft(engine, tmp_path):
    flagged = story(1, title="Man arrested in Hyderabad over theft")
    summary = run_once(settings(mode="auto"), make_deps(engine, tmp_path, [flagged], ig=FakeIG()))
    assert summary.held == 1 and all_posts(engine)[0].state is PostState.draft


def test_daily_cap_stops_run(engine, tmp_path):
    db.save_story(engine, story(90))
    db.save_post(engine, story_id="s90", format="card", state=PostState.draft)
    db.save_post(engine, story_id="s90", format="card", state=PostState.rejected)  # not counted
    db.save_post(engine, story_id="s90", format="card", state=PostState.failed)  # not counted
    deps = make_deps(engine, tmp_path, [story(1), story(2), story(3)], tg=FakeTG())
    summary = run_once(settings(daily_cap=2), deps)
    assert summary.drafted == 1 and summary.cap_reached is True
    assert len(db.list_posts_by_state(engine, PostState.draft)) == 2


def test_cap_already_reached_does_nothing(engine, tmp_path):
    for _ in range(2):
        db.save_post(engine, story_id="x", format="card", state=PostState.published)
    llm = FakeLLM()
    summary = run_once(settings(daily_cap=2), make_deps(engine, tmp_path, [story(1)], llm=llm, tg=FakeTG()))
    assert summary.cap_reached and summary.drafted == 0 and llm.calls == []


def test_one_story_failure_does_not_abort_run(engine, tmp_path):
    stories = [story(1, title="BROKEN story one"), story(2), story(3)]
    deps = make_deps(engine, tmp_path, stories, llm=FakeLLM(bad_titles=["BROKEN"]), tg=FakeTG())
    summary = run_once(settings(), deps)
    assert summary.failed == 1 and summary.drafted == 2
    assert len(summary.errors) == 1 and "s1" in summary.errors[0]
    assert db.story_seen(engine, "https://example.com/1")  # poison story marked seen
    assert len(all_posts(engine)) == 2


def test_storage_and_telegram_failures_are_isolated(engine, tmp_path):
    calls = {"n": 0}

    def flaky_upload(paths, prefix):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("secret-in-message")
        return [f"https://cdn.example.com/{prefix}/{p.name}" for p in paths]

    deps = make_deps(engine, tmp_path, [story(1), story(2)], tg=FakeTG(), upload=flaky_upload)
    summary = run_once(settings(), deps)
    assert summary.failed == 1 and summary.drafted == 1
    assert "secret-in-message" not in " ".join(summary.errors)  # unknown exceptions: type name only


def test_telegram_failure_marks_post_failed(engine, tmp_path):
    class BadTG(FakeTG):
        def call(self, method, **p):
            raise TelegramError("sendMessage failed")

    summary = run_once(settings(), make_deps(engine, tmp_path, [story(1)], tg=BadTG()))
    assert summary.failed == 1 and summary.drafted == 0
    assert all_posts(engine)[0].state is PostState.failed


def test_auto_publish_failure_leaves_post_failed_and_run_continues(engine, tmp_path):
    ig = FakeIG(fail_create=True)
    summary = run_once(settings(mode="auto"), make_deps(engine, tmp_path, [story(1), story(2)], ig=ig))
    assert summary.published == 0 and summary.failed == 2
    assert {p.state for p in all_posts(engine)} == {PostState.failed}
    assert not any(c[1].endswith("/media_publish") for c in ig.calls)


def test_block_story_never_saved_or_posted(engine, tmp_path):
    blocked = story(1, title="Communal violence erupts in Hyderabad")
    llm = FakeLLM()
    summary = run_once(settings(), make_deps(engine, tmp_path, [blocked], llm=llm, tg=FakeTG()))
    assert summary.skipped_blocked == 1 and summary.drafted == 0
    assert all_posts(engine) == [] and not db.story_seen(engine, blocked.url) and llm.calls == []


def test_blocklist_error_propagates(engine, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise BlocklistError("bad blocklist")

    monkeypatch.setattr(pipeline.safety, "check", boom)
    with pytest.raises(BlocklistError):
        run_once(settings(), make_deps(engine, tmp_path, [story(1)], tg=FakeTG()))


def test_shaping_unavailable_and_auth_errors_propagate(engine, tmp_path):
    def no_shaping():
        raise ShapingUnavailable("no raqm")

    with pytest.raises(ShapingUnavailable):
        run_once(settings(), make_deps(engine, tmp_path, [story(1)], tg=FakeTG(), check_shaping=no_shaping))
    resp = httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com"))
    err = anthropic.AuthenticationError("bad key", response=resp, body=None)
    with pytest.raises(anthropic.AuthenticationError):
        run_once(settings(), make_deps(engine, tmp_path, [story(1)], tg=FakeTG(), llm=FakeLLM(error=err)))


def test_stale_story_skipped(engine, tmp_path):
    summary = run_once(settings(), make_deps(engine, tmp_path, [story(1, age_h=49), story(2)], tg=FakeTG()))
    assert summary.skipped_stale == 1 and summary.drafted == 1 and summary.fetched == 2


def test_seen_and_similar_stories_are_deduped(engine, tmp_path):
    first = story(1)
    db.save_story(engine, first)
    near = replace(story(2), title=first.title + "!")
    summary = run_once(settings(), make_deps(engine, tmp_path, [first, near, story(3)], tg=FakeTG()))
    assert summary.new == 1 and summary.drafted == 1


def test_topic_bandit_prefers_story_with_matching_hint(engine, tmp_path, monkeypatch):
    deps = make_deps(engine, tmp_path, [story(1, hint="crime"), story(2, hint="sports")], tg=FakeTG())
    real_choose = pipeline.learn.choose
    monkeypatch.setattr(pipeline.learn, "choose", lambda eng, attr, options, rng:
                        "sports" if attr == "topic" else real_choose(eng, attr, options, rng))
    run_once(settings(daily_cap=1), deps)
    assert db.story_seen(engine, "https://example.com/2")
    assert not db.story_seen(engine, "https://example.com/1")


def test_inactive_slot_does_nothing_and_ignore_slots_overrides(engine, tmp_path):
    s = settings(time_slots=("02:00", "07:00"), active_slot_count=1)
    active = active_slots(s, NOW, engine)
    inactive = [x for x in s.time_slots if x not in active][0]
    h, m = map(int, inactive.split(":"))
    clock = lambda: NOW.replace(hour=h, minute=m)  # noqa: E731
    deps = make_deps(engine, tmp_path, [story(1)], tg=FakeTG(), now=clock)
    summary = run_once(s, deps)
    assert summary.slot_active is False and summary.fetched == 0 and all_posts(engine) == []
    summary = run_once(s, deps, ignore_slots=True)
    assert summary.slot_active is True and summary.drafted == 1
    assert all_posts(engine)[0].time_slot == inactive


def test_active_slots_same_date_same_set(engine):
    s = settings(active_slot_count=2)
    a = active_slots(s, NOW, engine)
    other = create_engine("sqlite://")
    db.init(other)
    assert a == active_slots(s, NOW + timedelta(hours=5), other) and len(a) == 2
    assert set(a) <= set(s.time_slots)


def test_nearest_slot_wraps_midnight():
    assert nearest_slot(("02:00", "23:30"), datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc)) == "23:30"
    assert nearest_slot(("02:00", "07:00"), NOW) == "07:00"


def test_dry_run_writes_files_and_makes_no_instagram_calls(engine, tmp_path):
    ig, tg = FakeIG(), FakeTG()
    deps = make_deps(engine, tmp_path, [story(1), story(2)], ig=ig, tg=tg)
    out = tmp_path / "dry"
    summary = pipeline.dry_run(settings(mode="auto"), deps, out)
    assert summary.drafted == 2
    captions = sorted(out.glob("*/caption.txt"))
    assert len(captions) == 2 and "మూలం: The Hindu" in captions[0].read_text(encoding="utf-8")
    decision = json.loads(next(out.glob("*/decision.json")).read_text(encoding="utf-8"))
    assert decision["verdict"] == "allow" and decision["story_id"] in ("s1", "s2")
    assert list(out.glob("*/*.jpg"))
    assert ig.calls == [] and tg.calls == [] and deps.uploads == []
    assert all_posts(engine) == [] and not db.story_seen(engine, "https://example.com/1")


def test_dry_run_ignores_slots(engine, tmp_path):
    s = settings(time_slots=("02:00", "07:00"), active_slot_count=1)
    active = active_slots(s, NOW, engine)
    inactive = [x for x in s.time_slots if x not in active][0]
    h, m = map(int, inactive.split(":"))
    deps = make_deps(engine, tmp_path, [story(1)], now=lambda: NOW.replace(hour=h, minute=m))
    assert pipeline.dry_run(s, deps, tmp_path / "o").drafted == 1


def test_publish_approved_publishes_only_approved_using_drafts_urls(engine, tmp_path):
    db.save_story(engine, story(1))
    ids = {}
    for st in (PostState.approved, PostState.draft, PostState.rejected):
        pid = db.save_post(engine, story_id="s1", format="card", state=st)
        db.set_post_caption(engine, pid, f"caption {st.value}")
        with engine.begin() as c:
            c.execute(db.drafts.insert().values(post_id=pid, caption="old", slides=[f"https://cdn.example.com/{st.value}.jpg"]))
        ids[st] = pid
    ig = FakeIG()
    result = publish_approved(settings(), make_deps(engine, tmp_path, [], ig=ig, tg=FakeTG()))
    assert result.published == 1 and result.failed == 0
    assert db.get_post(engine, ids[PostState.approved]).state is PostState.published
    assert db.get_post(engine, ids[PostState.draft]).state is PostState.draft
    [created] = ig.created
    assert created["caption"] == "caption approved"  # posts.caption, not the drafts "old"
    assert created["image_url"] == "https://cdn.example.com/approved.jpg"  # drafts URL
    assert len([c for c in ig.calls if c[1].endswith("/media_publish")]) == 1


def test_publish_approved_isolates_failures(engine, tmp_path):
    db.save_story(engine, story(1))
    for _ in range(2):
        pid = db.save_post(engine, story_id="s1", format="card", state=PostState.approved)
        db.set_post_caption(engine, pid, "c")
        with engine.begin() as c:
            c.execute(db.drafts.insert().values(post_id=pid, caption="c", slides=["https://cdn.example.com/a.jpg"]))
    result = publish_approved(settings(), make_deps(engine, tmp_path, [], ig=FakeIG(fail_create=True)))
    assert result.failed == 2 and result.published == 0 and len(result.errors) == 2


def test_overlapping_run_claim_prevents_double_post(engine, tmp_path, monkeypatch):
    s1 = story(1)
    db.save_story(engine, s1)  # another run already claimed it
    monkeypatch.setattr(pipeline.db, "story_seen", lambda e, u: False)
    monkeypatch.setattr(pipeline.db, "recent_titles", lambda e, since: [])
    ig, tg = FakeIG(), FakeTG()
    deps = make_deps(engine, tmp_path, [s1], ig=ig, tg=tg, llm=FakeLLM())
    for mode in ("review", "auto"):
        summary = run_once(settings(mode=mode), deps)
        assert summary.skipped_duplicate == 1 and summary.drafted == summary.published == 0
    assert all_posts(engine) == [] and deps.uploads == [] and ig.calls == [] and tg.calls == []
    assert deps.llm.calls == []


def test_save_story_reports_whether_it_inserted(engine):
    assert db.save_story(engine, story(1)) is True
    assert db.save_story(engine, story(1)) is False


def auth_err():
    return InstagramError("bad token", status=401, code=190)


def test_auth_failure_aborts_run_and_leaves_rest_unseen(engine, tmp_path):
    ig = FakeIG(fail_create=auth_err())
    deps = make_deps(engine, tmp_path, [story(1), story(2), story(3)], ig=ig)
    with pytest.raises(pipeline.PipelineError) as ei:
        run_once(settings(mode="auto"), deps)
    assert "authentication" in str(ei.value)
    seen = [db.story_seen(engine, f"https://example.com/{n}") for n in (1, 2, 3)]
    assert sum(seen) == 1  # only the story whose publish failed
    assert [p.state for p in all_posts(engine)] == [PostState.failed]


def test_publish_approved_auth_failure_aborts(engine, tmp_path):
    db.save_story(engine, story(1))
    for _ in range(2):
        pid = db.save_post(engine, story_id="s1", format="card", state=PostState.approved)
        db.set_post_caption(engine, pid, "c")
        with engine.begin() as c:
            c.execute(db.drafts.insert().values(post_id=pid, caption="c", slides=["https://cdn.example.com/a.jpg"]))
    ig = FakeIG(fail_create=auth_err())
    with pytest.raises(pipeline.PipelineError):
        publish_approved(settings(), make_deps(engine, tmp_path, [], ig=ig))
    assert len(db.list_posts_by_state(engine, PostState.approved)) == 1  # second untouched


def test_publish_approved_token_guard_runs_before_reconcile(engine, tmp_path, monkeypatch):
    db.set_kv(engine, pipeline.publish.KV_TOKEN_EXPIRES, "2020-01-01T00:00:00+00:00")
    order = []
    monkeypatch.setattr(pipeline.publish, "reconcile_publishing", lambda *a, **k: order.append("reconcile") or [])
    with pytest.raises(pipeline.PipelineError):
        publish_approved(settings(), make_deps(engine, tmp_path, [], ig=FakeIG()))
    assert order == []


def test_held_without_telegram_warns_in_errors(engine, tmp_path):
    flagged = story(1, title="Man arrested in Hyderabad over theft")
    summary = run_once(settings(mode="auto"), make_deps(engine, tmp_path, [flagged], ig=FakeIG()))
    assert summary.held == 1 and any("cannot be reviewed" in e for e in summary.errors)


def test_empty_time_slots_raise_clear_error(engine):
    with pytest.raises(ValueError, match="time_slots"):
        nearest_slot((), NOW)
    with pytest.raises(ValueError, match="time_slots"):
        active_slots(settings(time_slots=()), NOW, engine)
