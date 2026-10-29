"""Telegram review gate: send a draft for Approve / Reject / Edit-caption and poll decisions.

Security rule: the bot token must never reach an exception message, log line or repr. It
lives only in the request URL path, httpx errors are re-raised without their URL/cause, and
API error descriptions are scrubbed of the token. Only the configured chat id is honored.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
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
KV_CONTENT_PREFIX = "post_content:"  # post_content:<post_id> -> JSON of the poster text
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


    def upload(self, method: str, field: str, path, **payload: Any) -> Any:
        """Send a local file (multipart), for when Telegram cannot fetch a public URL. Limit 50 MB."""
        url = f"{API_BASE}/bot{self._token}/{method}"
        try:
            with open(path, "rb") as fh:
                resp = self._http.post(url, data={k: str(v) for k, v in payload.items()},
                                       files={field: (Path(path).name, fh)}, timeout=180)
        except Exception as exc:
            raise TelegramError(f"{method} upload failed: {type(exc).__name__}") from None
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or not isinstance(body, dict) or not body.get("ok"):
            desc = body.get("description", "") if isinstance(body, dict) else ""
            raise TelegramError(self._scrub(f"{method} upload failed: HTTP {resp.status_code} {desc}").strip())
        return body.get("result")


# -- sending ----------------------------------------------------------------
Redraw = Callable[[int, RewrittenPost, str], list[str]]  # (post_id, post, format) -> public URLs


def _send_draft_message(client: TelegramClient, post_id: int, caption: str) -> Any:
    keyboard = {"inline_keyboard": [[
        {"text": "Approve", "callback_data": f"a:{post_id}"},
        {"text": "Reject", "callback_data": f"r:{post_id}"},
        {"text": "Edit caption", "callback_data": f"e:{post_id}"},
    ]]}
    return client.call(
        "sendMessage", chat_id=client.chat_id, reply_markup=keyboard,
        text=f"Draft post #{post_id}\n\n{caption}\n\n"
             "To edit the caption: reply with the new caption.\nTo fix the poster text: reply with\nheadline: new headline\n- point 1\n- point 2\nThen tap Approve.")


def send_draft(engine: Engine, client: TelegramClient, post_id: int, previews: list[str],
               caption: str, local_video: Path | None = None) -> None:
    """Send previews + caption with Approve / Reject / Edit buttons; record the draft row and
    map the Telegram message id to the post so edit replies can be matched."""
    preview_ids: list[int] = []
    if len(previews) == 1 and previews[0].lower().endswith(".mp4"):  # a reel draft
        try:
            sent = client.call("sendVideo", chat_id=client.chat_id, video=previews[0],
                               supports_streaming=True)
        except TelegramError as exc:
            if local_video is None:
                raise
            log.warning("draft %s: Telegram could not fetch the video URL (%s); uploading the file", post_id, exc)
            sent = client.upload("sendVideo", "video", local_video, chat_id=client.chat_id,
                                 supports_streaming="true")
        preview_ids = [sent["message_id"]] if isinstance(sent, Mapping) and "message_id" in sent else []
    elif len(previews) == 1:
        sent = client.call("sendPhoto", chat_id=client.chat_id, photo=previews[0])
        preview_ids = [sent["message_id"]] if isinstance(sent, Mapping) and "message_id" in sent else []
    elif previews:
        sent = client.call("sendMediaGroup", chat_id=client.chat_id,
                           media=[{"type": "photo", "media": u} for u in previews[:10]])
        preview_ids = [m["message_id"] for m in (sent or []) if isinstance(m, Mapping) and "message_id" in m]
    try:
        msg = _send_draft_message(client, post_id, caption)
    except TelegramError as exc:
        if previews:
            log.warning("draft %s: previews were sent but the review message failed: %s",
                        post_id, exc)
        raise
    db.set_kv(engine, f"{KV_MSG_PREFIX}{msg['message_id']}", str(post_id))
    for mid in preview_ids:  # a reply to the image itself must also find its draft
        db.set_kv(engine, f"{KV_MSG_PREFIX}{mid}", str(post_id))
    with engine.begin() as conn:
        conn.execute(db.drafts.insert().values(
            post_id=post_id, caption=caption, slides=list(previews), created_at=db._now()))


def save_content(engine: Engine, post_id: int, post: RewrittenPost, fmt: str) -> None:
    """Remember the poster text so it can be edited and redrawn from Telegram."""
    db.set_kv(engine, f"{KV_CONTENT_PREFIX}{post_id}", json.dumps({
        "headline_te": post.headline_te, "points_te": post.points_te, "caption_te": post.caption_te,
        "hashtags": post.hashtags, "source_credit": post.source_credit, "topic": post.topic,
        "headline_style": post.headline_style, "fmt": fmt}, ensure_ascii=False))


# -- polling ----------------------------------------------------------------
def poll_decisions(engine: Engine, client: TelegramClient, now: datetime,
                   require_credit: bool = True, redraw: Redraw | None = None) -> list[Decision]:
    """Fetch new Telegram updates and apply them. Only updates from the configured chat are
    honored; others are ignored but still advance the persisted offset so nothing is reprocessed.
    Only posts currently in `draft` state can change. ``now`` is accepted for a uniform job
    signature; decisions are applied as they arrive."""
    raw = db.get_kv(engine, KV_OFFSET)
    offset = int(raw) if raw else 0
    updates = client.call("getUpdates", offset=offset, timeout=0,
                          allowed_updates=["callback_query", "message", "edited_message"]) or []
    decisions: list[Decision] = []
    for upd in sorted(updates, key=lambda u: u["update_id"]):
        try:
            if "callback_query" in upd:
                d = _handle_callback(engine, client, upd["callback_query"])
            elif "message" in upd or "edited_message" in upd:
                d = _handle_message(engine, client, upd.get("message") or upd["edited_message"],
                                    require_credit, redraw)
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


def notify(client: TelegramClient, text: str, reply_to: int | None = None) -> None:
    """Send a plain confirmation message to the review chat. Never raises: a failed notice must
    not stop a run. (Button pop-ups cannot be used for this: polling happens minutes after the
    tap, when Telegram has already expired the callback query.)"""
    payload: dict[str, Any] = {"chat_id": client.chat_id, "text": text}
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
    _safe(client, "sendMessage", **payload)


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
        notify(client, f"Post #{post_id} is no longer pending (already handled or expired), "
                       "so your tap did nothing.", message.get("message_id"))
        return None
    if action == "edit":
        prompt = client.call(
            "sendMessage", chat_id=client.chat_id, reply_to_message_id=message.get("message_id"),
            text=f"Reply to this message with the new caption for post #{post_id}. "
                 "(You can also reply directly to the draft message.)")
        db.set_kv(engine, f"{KV_MSG_PREFIX}{prompt['message_id']}", str(post_id))
        _safe(client, "answerCallbackQuery", callback_query_id=cq["id"], text="Send the new caption")
        return None
    target = PostState.approved if action == "approve" else PostState.rejected
    if not db.transition_post_state(engine, post_id, PostState.draft, target):
        _safe(client, "answerCallbackQuery", callback_query_id=cq["id"],
              text="This draft is no longer pending.")
        notify(client, f"Post #{post_id} is no longer pending, so your tap did nothing.",
               message.get("message_id"))
        return None
    _safe(client, "answerCallbackQuery", callback_query_id=cq["id"],
          text="Approved" if action == "approve" else "Rejected")
    notify(client, (f"Approved post #{post_id}. It will be published at the next run (within about 30 minutes)."
                    if action == "approve" else f"Rejected post #{post_id}. It will not be published."),
           message.get("message_id"))
    return Decision(post_id=post_id, action=action)


_POSTER_RE = re.compile(r"^\s*(?:headline|title|poster|శీర్షిక)\s*[:：]\s*(.*)$", re.I | re.S)
_BULLET_RE = re.compile(r"^\s*(?:[-•*]|\d+[.)])\s*(.+)$")


def parse_poster_edit(text: str) -> tuple[str, list[str] | None] | None:
    """``headline: X`` (first line) plus optional bullet lines for the points. None when the
    text is not a poster edit."""
    m = _POSTER_RE.match(text)
    if not m:
        return None
    lines = [ln for ln in m.group(1).splitlines()]
    headline = (lines[0] if lines else "").strip()
    points = []
    for ln in lines[1:]:
        b = _BULLET_RE.match(ln)
        if b:
            points.append(b.group(1).strip())
    return headline, (points or None)


def _poster_edit(engine: Engine, client: TelegramClient, msg: Mapping[str, Any], post_id: int,
                 headline: str, points: list[str] | None, require_credit: bool,
                 redraw: Redraw | None) -> Decision | None:
    mid = msg.get("message_id")
    raw = db.get_kv(engine, f"{KV_CONTENT_PREFIX}{post_id}")
    if redraw is None or raw is None:
        notify(client, f"Poster text for post #{post_id} cannot be edited here (its original text was "
                       "not saved). Reject it and wait for the next draft, or edit the caption instead.", mid)
        return None
    c = json.loads(raw)
    post = RewrittenPost(
        headline_te=headline or c["headline_te"], points_te=points or list(c["points_te"]),
        caption_te=c["caption_te"], hashtags=list(c["hashtags"]), source_credit=c["source_credit"],
        topic=c["topic"], headline_style=c["headline_style"])
    live = db.get_post(engine, post_id)
    if live is not None and live.caption:
        post.caption_te = live.caption  # validate against the caption as it stands now
    problems = validate(post, require_credit)
    if problems:
        notify(client, "Poster not changed (old poster kept):\n- " + "\n- ".join(problems), mid)
        return None
    try:
        urls = redraw(post_id, post, c["fmt"])
    except Exception as exc:
        log.warning("poster redraw failed for %s: %s", post_id, type(exc).__name__)
        notify(client, f"Could not redraw the poster for post #{post_id}; the old poster is kept.", mid)
        return None
    c.update(headline_te=post.headline_te, points_te=post.points_te)
    db.set_kv(engine, f"{KV_CONTENT_PREFIX}{post_id}", json.dumps(c, ensure_ascii=False))
    notify(client, f"Poster updated for post #{post_id}. Check the new preview and tap Approve on the "
                   "draft below to publish it.", mid)
    send_draft(engine, client, post_id, urls, (live.caption if live else None) or c["caption_te"])
    return Decision(post_id=post_id, action="poster")


def _handle_message(engine: Engine, client: TelegramClient, msg: Mapping[str, Any],
                    require_credit: bool = True, redraw: Redraw | None = None) -> Decision | None:
    if not _from_our_chat(client, msg.get("chat")):
        return None
    target = (msg.get("reply_to_message") or {}).get("message_id")
    text = (msg.get("text") or "").strip()
    if target is None or not text:
        return None
    mapped = db.get_kv(engine, f"{KV_MSG_PREFIX}{target}")
    if mapped is None:
        notify(client, "I could not match your reply to a draft, so no caption was changed. "
                       "Reply directly to the draft message (the one with the Approve button).",
               msg.get("message_id"))
        return None
    post_id = int(mapped)
    post = db.get_post(engine, post_id)
    if post is None or post.state != PostState.draft:
        notify(client, f"Post #{post_id} is no longer pending (already approved, rejected or expired), "
                       "so your caption was not applied.", msg.get("message_id"))
        return None
    poster = parse_poster_edit(text)
    if poster is not None:
        return _poster_edit(engine, client, msg, post_id, poster[0], poster[1], require_credit, redraw)
    problems = validate(_candidate(post.caption or "", text), require_credit)
    if problems:
        _safe(client, "sendMessage", chat_id=client.chat_id, reply_to_message_id=msg.get("message_id"),
              text="Caption not saved (old caption kept):\n- " + "\n- ".join(problems))
        return None
    if not db.set_post_caption_if_state(engine, post_id, text, PostState.draft):
        _safe(client, "sendMessage", chat_id=client.chat_id,
              reply_to_message_id=msg.get("message_id"),
              text=f"Post #{post_id} is no longer pending; caption not changed.")
        return None
    notify(client, f"Caption updated for post #{post_id}. Tap Approve on the draft below to publish it.",
           msg.get("message_id"))
    try:  # fresh draft message carrying the NEW caption and the buttons
        fresh = _send_draft_message(client, post_id, text)
        db.set_kv(engine, f"{KV_MSG_PREFIX}{fresh['message_id']}", str(post_id))
    except TelegramError as exc:
        log.warning("could not resend draft %s after edit: %s", post_id, exc)
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
def expire_old_drafts(engine: Engine, now: datetime, max_age_hours: int = 5) -> int:
    """Mark draft posts older than ``max_age_hours`` (by created_at) as expired. Approved posts
    are never touched. Returns the number expired."""
    cutoff = db._utc(now) - timedelta(hours=max_age_hours)
    n = 0
    for post in db.list_posts_by_state(engine, PostState.draft):
        if post.created_at is not None and post.created_at < cutoff:
            if db.transition_post_state(engine, post.id, PostState.draft, PostState.expired, now):
                n += 1
    return n
