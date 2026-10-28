import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine

from tgnews import db, gate
from tgnews.gate import Decision, TelegramClient, TelegramError
from tgnews.models import PostState, Story

TOKEN = "123456:SECRET-TOKEN-abc"
CHAT = "555"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
CAPTION = "హైదరాబాద్ నగరాన్ని భారీ వర్షం ముంచెత్తింది. పలు ప్రాంతాల్లో రోడ్లు జలమయమయ్యాయి. మూలం: The Hindu #Hyderabad"
PREVIEWS = ["https://cdn.example.com/p/1.jpg", "https://cdn.example.com/p/2.jpg"]


class FakeTelegram:
    """Fake Bot API served through httpx.MockTransport."""

    def __init__(self):
        self.updates = []
        self.calls = []  # (method, payload)
        self._next_msg = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content) if request.content else {}
        self.calls.append((method, payload))
        if method == "getUpdates":
            off = payload.get("offset", 0)
            return httpx.Response(200, json={"ok": True, "result":
                                             [u for u in self.updates if u["update_id"] >= off]})
        if method in ("sendMessage", "sendPhoto"):
            self._next_msg += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self._next_msg}})
        if method == "sendMediaGroup":
            return httpx.Response(200, json={"ok": True, "result": [{"message_id": 1}, {"message_id": 2}]})
        return httpx.Response(200, json={"ok": True, "result": True})

    def of(self, method):
        return [p for m, p in self.calls if m == method]

    def client(self):
        return TelegramClient(TOKEN, CHAT, http=httpx.Client(transport=httpx.MockTransport(self.handler)))


def cb(update_id, post_id, action, chat=CHAT):
    return {"update_id": update_id, "callback_query": {
        "id": f"cq{update_id}", "data": f"{action}:{post_id}",
        "message": {"message_id": 101, "chat": {"id": int(chat)}}}}


def reply(update_id, text, to_message_id, chat=CHAT):
    return {"update_id": update_id, "message": {
        "message_id": 900 + update_id, "chat": {"id": int(chat)}, "text": text,
        "reply_to_message": {"message_id": to_message_id}}}


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    db.init(e)
    db.save_story(e, Story(id="s1", title="t", summary="s", url="https://example.com/a",
                           source_name="src", published_at=NOW, topic_hint="weather"))
    return e


@pytest.fixture
def fake():
    return FakeTelegram()


@pytest.fixture
def draft(engine, fake):
    pid = db.save_post(engine, story_id="s1", format="carousel", topic="weather",
                       headline_style="statement", state=PostState.draft)
    db.set_post_caption(engine, pid, CAPTION)
    gate.send_draft(engine, fake.client(), pid, PREVIEWS, CAPTION)
    return pid


def state(engine, pid):
    return db.get_post(engine, pid).state


def test_send_draft_sends_previews_caption_and_keyboard(engine, fake, draft):
    assert fake.of("sendMediaGroup")[0]["media"][0]["media"] == PREVIEWS[0]
    msg = fake.of("sendMessage")[0]
    assert CAPTION in msg["text"]
    buttons = [b for row in msg["reply_markup"]["inline_keyboard"] for b in row]
    assert {b["callback_data"] for b in buttons} == {f"a:{draft}", f"r:{draft}", f"e:{draft}"}
    with engine.connect() as conn:
        rows = conn.execute(db.drafts.select()).all()
    assert len(rows) == 1 and rows[0].post_id == draft and rows[0].caption == CAPTION


def test_approve_sets_state_approved(engine, fake, draft):
    fake.updates = [cb(1, draft, "a")]
    out = gate.poll_decisions(engine, fake.client(), NOW)
    assert out == [Decision(post_id=draft, action="approve")]
    assert state(engine, draft) == PostState.approved
    assert fake.of("answerCallbackQuery")[0]["callback_query_id"] == "cq1"


def test_reject_sets_state_rejected(engine, fake, draft):
    fake.updates = [cb(1, draft, "r")]
    out = gate.poll_decisions(engine, fake.client(), NOW)
    assert out == [Decision(post_id=draft, action="reject")]
    assert state(engine, draft) == PostState.rejected


def test_approve_and_reject_send_a_confirmation_message(engine, fake, draft):
    fake.updates = [cb(1, draft, "a")]
    gate.poll_decisions(engine, fake.client(), NOW)
    texts = [p["text"] for p in fake.of("sendMessage")]
    assert any(f"Approved post #{draft}" in t for t in texts)
    fake.calls.clear()
    fake.updates = [cb(2, draft, "r")]  # draft is no longer pending: the tap is explained, not silent
    gate.poll_decisions(engine, fake.client(), NOW)
    assert any("no longer pending" in p["text"] for p in fake.of("sendMessage"))


def test_reject_confirmation_message(engine, fake, draft):
    fake.updates = [cb(1, draft, "r")]
    gate.poll_decisions(engine, fake.client(), NOW)
    assert any(f"Rejected post #{draft}" in p["text"] for p in fake.of("sendMessage"))


def test_notify_never_raises(engine):
    class Boom(FakeTelegram):
        def handler(self, request):
            return httpx.Response(500, json={"ok": False, "description": "down"})
    gate.notify(Boom().client(), "hello")  # must not raise


def test_edited_caption_is_saved_and_revalidated(engine, fake, draft):
    fake.updates = [cb(1, draft, "e")]
    assert gate.poll_decisions(engine, fake.client(), NOW) == []
    prompt = fake.of("sendMessage")[-1]
    assert prompt["reply_to_message_id"] == 101  # asks the user to reply to the draft message

    new = "వర్షాలతో నగరం అతలాకుతలం అయింది. మూలం: The Hindu #Rain"
    fake.updates = [reply(2, new, to_message_id=101)]
    out = gate.poll_decisions(engine, fake.client(), NOW)
    assert out == [Decision(post_id=draft, action="edit", caption=new)]
    assert db.get_post(engine, draft).caption == new
    assert state(engine, draft) == PostState.draft  # still needs Approve

    # invalid edits (English caption; missing source credit) keep the old caption
    for uid, bad in ((3, "This is an English caption. మూలం: The Hindu"),
                     (4, "వర్షాలతో నగరం అతలాకుతలం అయింది. పలు ప్రాంతాల్లో జలమయం")):
        fake.updates = [reply(uid, bad, to_message_id=101)]
        n = len(fake.of("sendMessage"))
        assert gate.poll_decisions(engine, fake.client(), NOW) == []
        assert db.get_post(engine, draft).caption == new
        assert len(fake.of("sendMessage")) == n + 1  # problems reported to user


def test_old_draft_expires(engine):
    old = db.save_post(engine, story_id="s1", format="single", state=PostState.draft)
    fresh = db.save_post(engine, story_id="s1", format="single", state=PostState.draft)
    appr = db.save_post(engine, story_id="s1", format="single", state=PostState.approved)
    from sqlalchemy import update
    with engine.begin() as conn:
        for pid, age in ((old, 7), (appr, 30), (fresh, 1)):
            conn.execute(update(db.posts).where(db.posts.c.id == pid)
                         .values(created_at=NOW - timedelta(hours=age)))
    assert gate.expire_old_drafts(engine, NOW) == 1
    assert state(engine, old) == PostState.expired
    assert state(engine, fresh) == PostState.draft
    assert state(engine, appr) == PostState.approved


def test_only_configured_chat_id_can_approve(engine, fake, draft):
    fake.updates = [cb(1, draft, "a", chat="999"), reply(2, "x", 101, chat="999")]
    assert gate.poll_decisions(engine, fake.client(), NOW) == []
    assert state(engine, draft) == PostState.draft
    assert db.get_kv(engine, gate.KV_OFFSET) == "3"  # ignored updates still advance the offset


def test_offset_persisted_and_updates_not_reprocessed(engine, fake, draft):
    fake.updates = [cb(7, draft, "a")]
    assert len(gate.poll_decisions(engine, fake.client(), NOW)) == 1
    assert db.get_kv(engine, gate.KV_OFFSET) == "8"
    fake.calls.clear()
    assert gate.poll_decisions(engine, fake.client(), NOW) == []
    assert fake.of("getUpdates")[0]["offset"] == 8


def test_double_tap_on_non_draft_ignored(engine, fake, draft):
    fake.updates = [cb(1, draft, "a"), cb(2, draft, "r")]
    out = gate.poll_decisions(engine, fake.client(), NOW)
    assert out == [Decision(post_id=draft, action="approve")]
    assert state(engine, draft) == PostState.approved


def test_edit_on_non_draft_ignored(engine, fake, draft):
    fake.updates = [cb(1, draft, "e")]
    gate.poll_decisions(engine, fake.client(), NOW)
    db.update_post_state(engine, draft, PostState.approved)
    fake.updates = [reply(2, "వర్షాలతో నగరం అతలాకుతలం. మూలం: The Hindu", 101)]
    assert gate.poll_decisions(engine, fake.client(), NOW) == []
    assert db.get_post(engine, draft).caption == CAPTION


def test_token_never_in_error_or_repr(fake):
    client = fake.client()
    assert TOKEN not in repr(client) and TOKEN not in str(client)

    def boom(request):
        return httpx.Response(401, json={"ok": False, "description": f"bad token {TOKEN}"})

    bad = TelegramClient(TOKEN, CHAT, http=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(TelegramError) as ei:
        bad.call("getMe")
    assert TOKEN not in str(ei.value) and TOKEN not in repr(ei.value)

    def netfail(request):
        raise httpx.ConnectError("cannot connect", request=request)

    down = TelegramClient(TOKEN, CHAT, http=httpx.Client(transport=httpx.MockTransport(netfail)))
    with pytest.raises(TelegramError) as ei:
        down.call("getMe")
    assert TOKEN not in str(ei.value) and ei.value.__cause__ is None


def test_from_env_requires_vars():
    with pytest.raises(TelegramError):
        TelegramClient.from_env({"TELEGRAM_BOT_TOKEN": TOKEN})
    c = TelegramClient.from_env({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT})
    assert c.chat_id == CHAT and TOKEN not in repr(c)


# -- atomic transitions / failure isolation -----------------------------------
def _flip_after_read(monkeypatch, engine, pid, to):
    """Make db.get_post/list_posts_by_state return the stale (draft) view, then flip the row."""
    real_get, real_list = db.get_post, db.list_posts_by_state

    def flip():
        db.update_post_state(engine, pid, to)

    def get_post(e, i):
        p = real_get(e, i)
        flip()
        return p

    def list_by_state(e, s):
        out = real_list(e, s)
        flip()
        return out

    monkeypatch.setattr(db, "get_post", get_post)
    monkeypatch.setattr(db, "list_posts_by_state", list_by_state)


@pytest.mark.parametrize("action", ["a", "r"])
@pytest.mark.parametrize("winner", [PostState.expired, PostState.publishing])
def test_stale_read_does_not_overwrite_state(engine, fake, draft, monkeypatch, action, winner):
    fake.updates = [cb(1, draft, action)]
    _flip_after_read(monkeypatch, engine, draft, winner)
    assert gate.poll_decisions(engine, fake.client(), NOW) == []
    assert state(engine, draft) == winner
    assert "no longer pending" in fake.of("answerCallbackQuery")[0]["text"]


def test_stale_read_does_not_overwrite_caption(engine, fake, draft, monkeypatch):
    fake.updates = [cb(1, draft, "e")]
    gate.poll_decisions(engine, fake.client(), NOW)
    fake.updates = [reply(2, "వర్షాలతో నగరం అతలాకుతలం అయింది. మూలం: The Hindu", 101)]
    _flip_after_read(monkeypatch, engine, draft, PostState.approved)
    assert gate.poll_decisions(engine, fake.client(), NOW) == []
    assert db.get_post(engine, draft).caption == CAPTION
    assert "no longer pending" in fake.of("sendMessage")[-1]["text"]


def test_expire_stale_read_does_not_overwrite_approved(engine, monkeypatch):
    from sqlalchemy import update
    pid = db.save_post(engine, story_id="s1", format="single", state=PostState.draft)
    with engine.begin() as conn:
        conn.execute(update(db.posts).where(db.posts.c.id == pid)
                     .values(created_at=NOW - timedelta(hours=9)))
    _flip_after_read(monkeypatch, engine, pid, PostState.approved)
    assert gate.expire_old_drafts(engine, NOW) == 0
    assert state(engine, pid) == PostState.approved


def test_approved_post_never_expired(engine):
    from sqlalchemy import update
    pid = db.save_post(engine, story_id="s1", format="single", state=PostState.approved)
    with engine.begin() as conn:
        conn.execute(update(db.posts).where(db.posts.c.id == pid)
                     .values(created_at=NOW - timedelta(hours=99)))
    assert gate.expire_old_drafts(engine, NOW) == 0
    assert state(engine, pid) == PostState.approved
    assert db.transition_post_state(engine, pid, PostState.draft, PostState.expired) is False


def test_handler_failure_keeps_earlier_decisions(engine, fake, draft, monkeypatch, caplog):
    p2 = db.save_post(engine, story_id="s1", format="single", state=PostState.draft)
    fake.updates = [cb(1, draft, "a"), cb(2, p2, "r"), cb(3, draft, "r")]
    real = db.transition_post_state

    def flaky(e, pid, frm, to, now=None):
        if pid == p2:
            raise RuntimeError(f"boom {TOKEN}")
        return real(e, pid, frm, to, now)

    monkeypatch.setattr(db, "transition_post_state", flaky)
    out = gate.poll_decisions(engine, fake.client(), NOW)
    assert out == [Decision(post_id=draft, action="approve")]
    assert state(engine, p2) == PostState.draft
    assert db.get_kv(engine, gate.KV_OFFSET) == "4"
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("token", ["bad token\nwith newline", "http://[::1"])
def test_malformed_token_error_is_sanitized(token):
    class Boom:
        def post(self, url, **kw):
            raise httpx.InvalidURL(f"Invalid URL {url}")

    c = TelegramClient(token, CHAT, http=Boom())
    with pytest.raises(TelegramError) as ei:
        c.call("getMe")
    assert token not in str(ei.value) and ei.value.__cause__ is None


def test_send_message_failure_after_photos_is_logged(engine, caplog):
    def h(request):
        if request.url.path.endswith("sendMessage"):
            return httpx.Response(500, json={"ok": False, "description": "oops"})
        return httpx.Response(200, json={"ok": True, "result": []})

    c = TelegramClient(TOKEN, CHAT, http=httpx.Client(transport=httpx.MockTransport(h)))
    with pytest.raises(TelegramError):
        gate.send_draft(engine, c, 1, PREVIEWS, CAPTION)
    assert "previews were sent" in caplog.text and TOKEN not in caplog.text
