"""Telegram review gate: send a draft for Approve / Reject / Edit-caption and poll decisions.

Security rule: the bot token must never reach an exception message, log line or repr. It
lives only in the request URL path, httpx errors are re-raised without their URL/cause, and
API error descriptions are scrubbed of the token. Only the configured chat id is honored.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.engine import Engine

from . import db
from .models import PostState
from .rewrite import RewrittenPost, validate

# httpx logs full request URLs at INFO, and the token is part of the Telegram URL path.
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

API_BASE = "https://api.telegram.org"
KV_OFFSET = "tg_update_offset"
KV_MSG_PREFIX = "tg_msg:"  # tg_msg:<telegram message_id> -> post_id (draft + edit-prompt messages)

_ACTIONS = {"a": "approve", "r": "reject", "e": "edit"}
_CREDIT_RE = re.compile(r"మూలం:\s*(.+)")


class TelegramError(Exception):
    """Telegram call or configuration failure. The message is sanitized and safe to log."""


@dataclass(frozen=True)
class Decision:
    post_id: int
    action: str  # "approve" | "reject" | "edit"
    caption: str | None = None  # set for "edit": the new, validated caption


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str, *, http: httpx.Client | None = None,
                 timeout: float = 30.0):
        if not bot_token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        if not str(chat_id).strip():
            raise TelegramError("TELEGRAM_CHAT_ID is not set")
        self._token = bot_token
        self.chat_id = str(chat_id).strip()
        self._http = http if http is not None else httpx.Client(timeout=timeout)

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, http: httpx.Client | None = None) -> "TelegramClient":
        return cls((env.get("TELEGRAM_BOT_TOKEN") or "").strip(),
                   (env.get("TELEGRAM_CHAT_ID") or "").strip(), http=http)

    def __repr__(self) -> str:
        return f"TelegramClient(chat_id={self.chat_id!r})"

    __str__ = __repr__

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "[REDACTED]")

    def call(self, method: str, **payload: Any) -> Any:
        url = f"{API_BASE}/bot{self._token}/{method}"
        try:
            resp = self._http.post(url, json=payload)
        except Exception as exc:  # incl. httpx.InvalidURL, whose message may hold the token
            # Re-raise without the exception chain: it carries the URL (and so the token).
            raise TelegramError(f"{method} request failed: {type(exc).__name__}") from None
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or not isinstance(body, dict) or not body.get("ok"):
            desc = body.get("description", "") if isinstance(body, dict) else ""
            raise TelegramError(self._scrub(f"{method} failed: HTTP {resp.status_code} {desc}").strip())
        return body.get("result")


# -- sending ----------------------------------------------------------------
def send_draft(engine: Engine, client: TelegramClient, post_id: int, previews: list[str],
               caption: str) -> None:
    """Send previews + caption with Approve / Reject / Edit buttons; record the draft row and
    map the Telegram message id to the post so edit replies can be matched."""
    if len(previews) == 1:
        client.call("sendPhoto", chat_id=client.chat_id, photo=previews[0])
    elif previews:
        client.call("sendMediaGroup", chat_id=client.chat_id,
                    media=[{"type": "photo", "media": u} for u in previews[:10]])
    keyboard = {"inline_keyboard": [[
        {"text": "Approve", "callback_data": f"a:{post_id}"},
        {"text": "Reject", "callback_data": f"r:{post_id}"},
        {"text": "Edit caption", "callback_data": f"e:{post_id}"},
    ]]}
    try:
        msg = client.call("sendMessage", chat_id=client.chat_id, reply_markup=keyboard,
                          text=f"Draft post #{post_id}\n\n{caption}")
    except TelegramError as exc:
        if previews:
            log.warning("draft %s: previews were sent but the review message failed: %s",
                        post_id, exc)
        raise
    db.set_kv(engine, f"{KV_MSG_PREFIX}{msg['message_id']}", str(post_id))
    with engine.begin() as conn:
        conn.execute(db.drafts.insert().values(
            post_id=post_id, caption=caption, slides=list(previews), created_at=db._now()))


# -- polling ----------------------------------------------------------------
def poll_decisions(engine: Engine, client: TelegramClient, now: datetime) -> list[Decision]:
    """Fetch new Telegram updates and apply them. Only updates from the configured chat are
    honored; others are ignored but still advance the persisted offset so nothing is reprocessed.
    Only posts currently in `draft` state can change. ``now`` is accepted for a uniform job
    signature; decisions are applied as they arrive."""
    raw = db.get_kv(engine, KV_OFFSET)
    offset = int(raw) if raw else 0
    updates = client.call("getUpdates", offset=offset, timeout=0,
                          allowed_updates=["callback_query", "message"]) or []
    decisions: list[Decision] = []
    for upd in sorted(updates, key=lambda u: u["update_id"]):
        try:
            if "callback_query" in upd:
                d = _handle_callback(engine, client, upd["callback_query"])
            elif "message" in upd:
                d = _handle_message(engine, client, upd["message"])
            else:
                d = None
        except Exception as exc:
            # Handlers are idempotent (compare-and-set), so a tap lost here is safe to drop:
            # the draft stays draft. Log only the type/sanitized text, keep going.
            msg = client._scrub(str(exc)) if isinstance(exc, TelegramError) else type(exc).__name__
            log.warning("skipping update %s: %s", upd.get("update_id"), msg)
            d = None
        # Persist progress per update so earlier updates are never re-applied.
        db.set_kv(engine, KV_OFFSET, str(upd["update_id"] + 1))
        if d:
            decisions.append(d)
    return decisions


def _from_our_chat(client: TelegramClient, chat: Mapping[str, Any] | None) -> bool:
    return chat is not None and str(chat.get("id")) == client.chat_id


def _safe(client: TelegramClient, method: str, **payload: Any) -> None:
    try:
        client.call(method, **payload)
    except TelegramError as exc:
        logging.getLogger(__name__).warning("telegram notify failed: %s", exc)


def _handle_callback(engine: Engine, client: TelegramClient, cq: Mapping[str, Any]) -> Decision | None:
    message = cq.get("message") or {}
    if not _from_our_chat(client, message.get("chat")):
        return None
    try:
        code, pid_s = str(cq.get("data", "")).split(":", 1)
        post_id = int(pid_s)
        action = _ACTIONS[code]
    except (ValueError, KeyError):
        return None
    post = db.get_post(engine, post_id)
    if post is None or post.state != PostState.draft:
        _safe(client, "answerCallbackQuery", callback_query_id=cq["id"],
              text="This draft is no longer pending.")
        return None
    if action == "edit":
        prompt = client.call(
            "sendMessage", chat_id=client.chat_id, reply_to_message_id=message.get("message_id"),
            text=f"Reply to this draft with the new caption for post #{post_id}.")
        db.set_kv(engine, f"{KV_MSG_PREFIX}{prompt['message_id']}", str(post_id))
        _safe(client, "answerCallbackQuery", callback_query_id=cq["id"], text="Send the new caption")
        return None
    target = PostState.approved if action == "approve" else PostState.rejected
    if not db.transition_post_state(engine, post_id, PostState.draft, target):
        _safe(client, "answerCallbackQuery", callback_query_id=cq["id"],
              text="This draft is no longer pending.")
        return None
    _safe(client, "answerCallbackQuery", callback_query_id=cq["id"],
          text="Approved" if action == "approve" else "Rejected")
    return Decision(post_id=post_id, action=action)


def _handle_message(engine: Engine, client: TelegramClient, msg: Mapping[str, Any]) -> Decision | None:
    if not _from_our_chat(client, msg.get("chat")):
        return None
    target = (msg.get("reply_to_message") or {}).get("message_id")
    text = (msg.get("text") or "").strip()
    if target is None or not text:
        return None
    mapped = db.get_kv(engine, f"{KV_MSG_PREFIX}{target}")
    if mapped is None:
        return None
    post_id = int(mapped)
    post = db.get_post(engine, post_id)
    if post is None or post.state != PostState.draft:
        return None
    problems = validate(_candidate(post.caption or "", text))
    if problems:
        _safe(client, "sendMessage", chat_id=client.chat_id, reply_to_message_id=msg.get("message_id"),
              text="Caption not saved (old caption kept):\n- " + "\n- ".join(problems))
        return None
    if not db.set_post_caption_if_state(engine, post_id, text, PostState.draft):
        _safe(client, "sendMessage", chat_id=client.chat_id,
              reply_to_message_id=msg.get("message_id"),
              text=f"Post #{post_id} is no longer pending; caption not changed.")
        return None
    _safe(client, "sendMessage", chat_id=client.chat_id, reply_to_message_id=msg.get("message_id"),
          text=f"Caption updated for post #{post_id}. It still needs Approve.")
    return Decision(post_id=post_id, action="edit", caption=text)


def _candidate(old_caption: str, new_caption: str) -> RewrittenPost:
    """Build a RewrittenPost to run rewrite.validate on an edited caption.

    Only the caption is user-editable, and only caption-related rules matter here, so the
    other fields are fixed valid placeholders: headline/points are short Telugu strings, the
    hashtags are those in the new caption, and the source credit is the one in the OLD caption
    (text after "మూలం:", up to the first hashtag). If the old caption has no credit the
    credit is empty and validation fails, so an edit can never drop or alter the credit line.
    """
    m = _CREDIT_RE.search(old_caption)
    credit = re.split(r"\s#", m.group(1).splitlines()[0])[0].strip() if m else ""
    return RewrittenPost(
        headline_te="శీర్షిక", points_te=["అంశం ఒకటి", "అంశం రెండు"], caption_te=new_caption,
        hashtags=re.findall(r"#\S+", new_caption), source_credit=credit, topic="other",
        headline_style="statement")


# -- expiry -----------------------------------------------------------------
def expire_old_drafts(engine: Engine, now: datetime, max_age_hours: int = 6) -> int:
    """Mark draft posts older than ``max_age_hours`` (by created_at) as expired. Approved posts
    are never touched. Returns the number expired."""
    cutoff = db._utc(now) - timedelta(hours=max_age_hours)
    n = 0
    for post in db.list_posts_by_state(engine, PostState.draft):
        if post.created_at is not None and post.created_at < cutoff:
            if db.transition_post_state(engine, post.id, PostState.draft, PostState.expired, now):
                n += 1
    return n
