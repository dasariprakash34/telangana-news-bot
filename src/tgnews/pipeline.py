"""Orchestration: fetch -> dedupe -> safety -> rewrite -> format -> render -> upload -> gate -> publish.

Every collaborator is injected through `Deps` so tests run with no network and no real LLM.
Fail-closed errors (BlocklistError, ShapingUnavailable, Anthropic auth/permission errors) and
KeyboardInterrupt/SystemExit propagate; any other per-story error is recorded (sanitized) and the
loop continues. A story is saved (marked seen) as soon as it is chosen and cleared by safety, so a
story that keeps failing cannot loop; BLOCKed stories are never saved at all.
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from . import db, gate, learn, publish, safety
from .config import Settings
from .dedupe import dedupe
from .fetch import FetchReport
from .llm import LLM_AUTH_ERRORS
from .instagram import InstagramClient, InstagramError
from .models import PostState, Story
from .reel import ReelUnavailable
from .render import ShapingUnavailable, assert_shaping_available, render_card, render_carousel
from .rewrite import DEFAULT_MODEL, STYLES, TOPICS, RewriteFailed, RewriteUnavailable, RewrittenPost, rewrite
from .safety import BlocklistError, Verdict
from .storage import StorageError

log = logging.getLogger(__name__)

MAX_STORY_AGE = timedelta(hours=6)  # only fresh, ongoing news
CAP_WINDOW = timedelta(hours=24)
# An approved post older than this is never published (stale news; also lets the images repo prune
# safely: IMAGES_KEEP_DAYS is at least 3 days).
MAX_PUBLISH_AGE = timedelta(hours=48)
NOT_COUNTED = (PostState.rejected, PostState.expired, PostState.failed)
FORMATS = learn.FORMATS
MAX_CAPTION_CHARS = 2200  # Instagram caption limit
MAX_INFRA_FAILURES = 2  # consecutive infrastructure failures before the run stops
_PROPAGATE = (BlocklistError, ShapingUnavailable) + LLM_AUTH_ERRORS
_SAFE_MESSAGE = (InstagramError, publish.PublishError, StorageError, gate.TelegramError, RewriteFailed,
                 ReelUnavailable)


class PipelineError(Exception):
    """Misconfiguration found at run time (e.g. auto mode without an Instagram client)."""


def _render(post: RewrittenPost, fmt: str, out_dir: Path, brand: str) -> list[Path]:
    return (render_card if fmt == "card" else render_carousel)(post, out_dir, brand)


@dataclass
class Deps:
    fetch: Callable[[], list[Story]]
    llm: Any
    engine: Engine | None = None
    render: Callable[[RewrittenPost, str, Path, str], list[Path]] = _render
    upload: Callable[[list[Path], str], list[str]] | None = None
    # Reels (only used when REELS_ENABLED): build the MP4, and put it at a public URL
    render_reel: Callable[[RewrittenPost, Path, str], list[Path]] | None = None
    upload_video: Callable[[Path, str], str] | None = None
    instagram: InstagramClient | None = None
    telegram: gate.TelegramClient | None = None
    rng_factory: Callable[[], random.Random] = random.Random
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    fetch_report: FetchReport | None = None  # filled by the fetcher when it can report failures
    out_dir: Path = Path("tgnews-out")
    sleep: Callable[[float], None] = time.sleep
    check_shaping: Callable[[], None] = assert_shaping_available
    llm_model: str = DEFAULT_MODEL  # build_deps sets it from build_llm


@dataclass
class RunSummary:
    fetched: int = 0
    new: int = 0
    skipped_blocked: int = 0
    skipped_stale: int = 0
    skipped_duplicate: int = 0
    per_run_cap_reached: bool = False
    fetch_alert: str | None = None  # set when an active slot fetched nothing (CLI alerts on it)
    drafted: int = 0
    held: int = 0
    published: int = 0
    failed: int = 0
    cap_reached: bool = False
    slot_active: bool = True
    errors: list[str] = field(default_factory=list)
    infra_errors: list[str] = field(default_factory=list)  # subset of errors from infrastructure


@dataclass
class PublishSummary:
    expired: int = 0
    expired_stale: int = 0
    decisions: int = 0
    reconciled: int = 0
    published: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def sanitize_error(exc: BaseException, limit: int = 200) -> str:
    """Our own error types carry sanitized messages; for anything else only the type is safe."""
    text = str(exc) if isinstance(exc, _SAFE_MESSAGE) else type(exc).__name__
    return " ".join(text.split())[:limit]


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# -- time slots -------------------------------------------------------------
def _minutes(slot: str) -> int:
    h, m = slot.split(":")
    return int(h) * 60 + int(m)


def nearest_slot(slots, now: datetime) -> str:
    if not slots:
        raise ValueError("time_slots must not be empty")
    now = _aware(now)
    cur = now.hour * 60 + now.minute

    def dist(slot: str) -> int:
        d = abs(_minutes(slot) - cur)
        return min(d, 1440 - d)

    return min(slots, key=lambda s: (dist(s), s))


def active_slots(settings: Settings, now: datetime, engine: Engine) -> tuple[str, ...]:
    """The day's active slots: picked by the bandit without replacement, seeded by the UTC date
    and stored in kv so later firings (even after the learning table changes) agree."""
    if not settings.time_slots:
        raise ValueError("time_slots must not be empty")
    day = _aware(now).strftime("%Y%m%d")
    count = min(settings.active_slot_count, len(settings.time_slots))
    stored = db.get_kv(engine, f"active_slots:{day}")
    if stored:
        saved = tuple(stored.split(","))
        if len(saved) == count and set(saved) <= set(settings.time_slots):
            return saved
    rng, remaining, chosen = random.Random(int(day)), list(settings.time_slots), []
    for _ in range(count):
        pick = learn.choose(engine, "time_slot", remaining, rng)
        chosen.append(pick)
        remaining.remove(pick)
    db.set_kv(engine, f"active_slots:{day}", ",".join(chosen))
    return tuple(chosen)


# -- helpers ----------------------------------------------------------------
def compose_caption(post: RewrittenPost) -> str:
    """Caption body plus hashtags within Instagram's 2200-character limit: trailing hashtags are
    dropped until it fits; the body is never truncated (too long on its own is a story error)."""
    body = post.caption_te
    if len(body) > MAX_CAPTION_CHARS:
        raise RewriteFailed(f"caption is {len(body)} characters; Instagram allows {MAX_CAPTION_CHARS}")
    extra = [h for h in post.hashtags if h not in body]
    while extra and len(body) + 2 + len(" ".join(extra)) > MAX_CAPTION_CHARS:
        extra.pop()
    return body + ("\n\n" + " ".join(extra) if extra else "")


AUTH_CODES = (190, 102)
_INFRA = (RewriteUnavailable, StorageError, gate.TelegramError, publish.PublishError, InstagramError)


def is_auth_failure(exc: BaseException) -> bool:
    status = getattr(exc, "cause_status", None) or getattr(exc, "status", None)
    code = getattr(exc, "cause_code", None) or getattr(exc, "code", None)
    return isinstance(exc, (InstagramError, publish.PublishError)) and (
        status == 401 or code in AUTH_CODES)


def _abort_auth(exc: BaseException) -> PipelineError:
    return PipelineError(f"Instagram authentication failed ({sanitize_error(exc)}); run aborted. "
                         "Refresh the token")


def _pick_story(candidates: list[Story], topic: str) -> Story:
    for i, s in enumerate(candidates):
        if s.topic_hint == topic:
            return candidates.pop(i)
    return candidates.pop(0)


def _over_cap(settings: Settings, engine: Engine, now: datetime) -> bool:
    return db.count_posts_since(engine, now - CAP_WINDOW, NOT_COUNTED) >= settings.daily_cap


def _check_token(engine: Engine, now: datetime) -> None:
    raw = db.get_kv(engine, publish.KV_TOKEN_EXPIRES)
    expires = publish._parse_ts(raw) if raw else None
    if expires is not None and expires <= now:
        raise PipelineError("Instagram token has expired; run `tgnews refresh-token`. Posting paused")


@dataclass
class _Ctx:
    settings: Settings
    deps: Deps
    engine: Engine
    now: datetime
    slot: str
    rng: random.Random
    summary: RunSummary
    out_dir: Path
    dry_out: Path | None = None  # set in dry-run: write files instead of upload/gate/publish
    seq: int = 0
    created: int = 0  # posts/drafts created in this run


# -- the run ----------------------------------------------------------------
def run_once(settings: Settings, deps: Deps, *, ignore_slots: bool = False) -> RunSummary:
    if deps.engine is None:
        raise PipelineError("run_once needs deps.engine")
    deps.check_shaping()
    if settings.mode == "auto" and deps.instagram is None:
        raise PipelineError("auto mode needs an Instagram client")
    if settings.mode == "review" and deps.telegram is None:
        raise PipelineError("review mode needs a Telegram client to send drafts")
    return _run(settings, deps, deps.engine, ignore_slots, None)


def dry_run(settings: Settings, deps: Deps, out_dir) -> RunSummary:
    """Fetch, rewrite and render into `out_dir` using a throwaway in-memory DB. No Instagram,
    Telegram or storage call is made and the real database is not touched."""
    deps.check_shaping()
    mem = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.init(mem)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    return _run(settings, deps, mem, True, out)


def _run(settings: Settings, deps: Deps, engine: Engine, ignore_slots: bool,
         dry_out: Path | None) -> RunSummary:
    now = _aware(deps.now())
    summary = RunSummary()
    if dry_out is None and settings.mode == "auto":
        _check_token(engine, now)
    slot = nearest_slot(settings.time_slots, now)
    if not ignore_slots and slot not in active_slots(settings, now, engine):
        summary.slot_active = False
        return summary
    if _over_cap(settings, engine, now):
        summary.cap_reached = True
        return summary

    stories = deps.fetch()
    summary.fetched = len(stories)
    if not stories:
        rep = deps.fetch_report
        if rep is not None and rep.sources == 0:
            summary.fetch_alert = "no sources configured"
        elif rep is not None and rep.failed >= rep.sources:
            summary.fetch_alert = "all sources failed"
        else:
            summary.fetch_alert = "no stories fetched"
    fresh = []
    for s in stories:
        if now - _aware(s.published_at) > MAX_STORY_AGE:
            summary.skipped_stale += 1
        else:
            fresh.append(s)
    seen = [s.url for s in fresh if db.story_seen(engine, s.url)]
    candidates = dedupe(fresh, seen, db.recent_titles(engine, now - timedelta(days=7)))
    candidates.sort(key=lambda s: _aware(s.published_at), reverse=True)  # newest first
    summary.new = len(candidates)

    ctx = _Ctx(settings, deps, engine, now, slot, deps.rng_factory(), summary,
               deps.out_dir, dry_out)
    infra_failures = 0
    while candidates:
        if _over_cap(settings, engine, now):
            summary.cap_reached = True
            break
        if ctx.created >= settings.effective_per_run_cap:
            summary.per_run_cap_reached = True
            break
        topic = learn.choose(engine, "topic", TOPICS, ctx.rng)
        story = _pick_story(candidates, topic)
        created_before = ctx.created
        try:
            if _process(ctx, story):
                infra_failures = 0
        except _PROPAGATE:
            raise
        except Exception as exc:
            summary.failed += 1
            summary.errors.append(f"{story.id}: {sanitize_error(exc)}")
            log.warning("story %s failed: %s", story.id, sanitize_error(exc))
            if isinstance(exc, _INFRA):
                infra_failures += 1
                summary.infra_errors.append(f"{story.id}: {sanitize_error(exc)}")
                if ctx.created == created_before:  # nothing was posted/drafted: allow a retry
                    db.release_story(engine, story.id)
            if is_auth_failure(exc):
                raise _abort_auth(exc) from None
            if infra_failures >= MAX_INFRA_FAILURES:
                raise PipelineError(f"stopping run after {infra_failures} consecutive infrastructure "
                                    f"failures (last: {sanitize_error(exc)})") from None
    return summary


def _process(ctx: _Ctx, story: Story) -> bool:
    """True when a post/draft was created and handled; False for a skip."""
    s, deps, engine = ctx.summary, ctx.deps, ctx.engine
    verdict = safety.check(story, ctx.settings.mode)
    if verdict is Verdict.BLOCK:
        s.skipped_blocked += 1
        return False
    # Atomic claim, before any rewrite/upload/post work: an overlapping run that got here first
    # owns the story. Seen from here on, even if a later stage fails.
    if not db.save_story(engine, story):
        s.skipped_duplicate += 1
        return False
    style = learn.choose(engine, "headline_style", STYLES, ctx.rng)
    fmt = learn.choose(engine, "format", FORMATS, ctx.rng)
    if _reel_wanted(ctx):
        fmt = "reel"
    rewritten = rewrite(story, deps.llm, style, deps.llm_model, ctx.settings.require_source_credit)
    caption = compose_caption(rewritten)
    ctx.seq += 1
    if ctx.dry_out is not None:
        return _dry_output(ctx, story, verdict, fmt, style, rewritten, caption)
    fmt, urls = _make_media(ctx, rewritten, story, fmt)
    # Reels are new: they always go to Telegram for approval, even in auto mode.
    auto_publish = ctx.settings.mode == "auto" and verdict is Verdict.ALLOW and fmt != "reel"
    post_id = db.save_post(engine, story_id=story.id, format=fmt, topic=rewritten.topic,
                           time_slot=ctx.slot, headline_style=style, state=PostState.draft)
    ctx.created += 1
    db.set_post_caption(engine, post_id, caption)
    gate.save_content(engine, post_id, rewritten, fmt)
    if auto_publish:
        if not db.transition_post_state(engine, post_id, PostState.draft, PostState.approved):
            raise publish.PublishError(f"post {post_id} could not be approved")
        publish.publish_post(post_id, urls, caption, deps.instagram, engine, sleep=deps.sleep)
        s.published += 1
        return True
    if deps.telegram is not None:
        try:
            gate.send_draft(engine, deps.telegram, post_id, urls, caption)
        except Exception:
            db.update_post_state(engine, post_id, PostState.failed)  # no way to review it
            raise
    if verdict is Verdict.HOLD:
        s.held += 1
        if deps.telegram is None:
            msg = f"{story.id}: held draft saved but no Telegram client; it cannot be reviewed"
            log.warning(msg)
            s.errors.append(msg)
    else:
        s.drafted += 1
    return True


def _reel_wanted(ctx: _Ctx) -> bool:
    """True when this story should become a reel. Costs no random draw (and so changes nothing
    about the existing behaviour) unless REELS_ENABLED is on and the reel tools are wired in."""
    s, d = ctx.settings, ctx.deps
    if not s.reels_enabled or s.reel_daily_cap <= 0 or d.render_reel is None:
        return False
    if ctx.dry_out is None and d.upload_video is None:
        return False
    if ctx.rng.random() >= s.reel_share:
        return False
    since = ctx.now - CAP_WINDOW
    return db.count_posts_since(ctx.engine, since, NOT_COUNTED, fmt="reel") < s.reel_daily_cap


def _make_media(ctx: _Ctx, rewritten: RewrittenPost, story: Story, fmt: str) -> tuple[str, list[str]]:
    """Render and publish-ready URLs for one post; returns the format actually produced. A reel that
    cannot be made falls back to a carousel, so the story is still posted."""
    deps, folder = ctx.deps, ctx.out_dir / story.id
    prefix = f"{ctx.now:%Y-%m-%d}/{story.id}"
    if fmt == "reel":
        try:
            video = deps.render_reel(rewritten, folder, ctx.settings.brand_name)
            return "reel", [deps.upload_video(video[0], prefix)]
        except ReelUnavailable as exc:
            log.warning("story %s: reel unavailable (%s); posting images instead", story.id, exc)
            fmt = "carousel"
    paths = deps.render(rewritten, fmt, folder, ctx.settings.brand_name)
    return fmt, deps.upload(paths, prefix)


def _dry_output(ctx: _Ctx, story: Story, verdict: Verdict, fmt: str, style: str,
                rewritten: RewrittenPost, caption: str) -> bool:
    folder = ctx.dry_out / f"{ctx.seq:02d}-{story.id}"
    if fmt == "reel":
        try:
            ctx.deps.render_reel(rewritten, folder, ctx.settings.brand_name)
        except ReelUnavailable as exc:
            log.warning("story %s: reel unavailable (%s); rendering images instead", story.id, exc)
            fmt = "carousel"
            ctx.deps.render(rewritten, fmt, folder, ctx.settings.brand_name)
    else:
        ctx.deps.render(rewritten, fmt, folder, ctx.settings.brand_name)
    (folder / "caption.txt").write_text(caption, encoding="utf-8")
    (folder / "decision.json").write_text(json.dumps({
        "story_id": story.id, "title": story.title, "url": story.url, "mode": ctx.settings.mode,
        "verdict": verdict.value, "topic": rewritten.topic, "format": fmt,
        "headline_style": style, "time_slot": ctx.slot}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    db.save_post(ctx.engine, story_id=story.id, format=fmt, topic=rewritten.topic,
                 time_slot=ctx.slot, headline_style=style, state=PostState.draft)
    ctx.created += 1
    if verdict is Verdict.HOLD:
        ctx.summary.held += 1
    else:
        ctx.summary.drafted += 1
    return True


def _redrawer(settings: Settings, deps: Deps, now: datetime):
    """Callback that re-renders and uploads a post's slides after a poster edit (None when the
    pipeline cannot upload)."""
    if deps.upload is None:
        return None

    def redraw(post_id: int, post: RewrittenPost, fmt: str) -> list[str]:
        if fmt == "reel":
            raise ReelUnavailable("poster text cannot be edited on a reel; reject it or edit the caption")
        stamp = int(time.time())
        folder = deps.out_dir / f"edit-{post_id}-{stamp}"
        paths = deps.render(post, fmt, folder, settings.brand_name)
        return deps.upload(paths, f"{now:%Y-%m-%d}/edit-{post_id}-{stamp}")

    return redraw


# -- publish approved -------------------------------------------------------
def publish_approved(settings: Settings, deps: Deps) -> PublishSummary:
    """Housekeeping, then publish every approved post from its stored slides and caption."""
    engine, out = deps.engine, PublishSummary()
    if engine is None:
        raise PipelineError("publish_approved needs deps.engine")
    now = _aware(deps.now())
    if deps.instagram is not None:
        _check_token(engine, now)  # first: an expired token pauses everything below
    # Poll BEFORE expiring: publish-approved does not run 17:30-01:00 UTC, so an evening tap is
    # first seen hours later. A tap on a still-`draft` post is honoured whatever its age; expiry
    # then only removes drafts nobody acted on.
    if deps.telegram is not None:
        try:
            out.decisions = len(gate.poll_decisions(
                engine, deps.telegram, now, require_credit=settings.require_source_credit,
                redraw=_redrawer(settings, deps, now)))
        except gate.TelegramError as exc:
            out.errors.append(f"poll: {sanitize_error(exc)}")
    out.expired = gate.expire_old_drafts(engine, now)
    approved = db.list_posts_by_state(engine, PostState.approved)
    if deps.instagram is None:
        if approved:
            raise PipelineError("approved posts exist but no Instagram client is configured")
        return out
    out.reconciled = len(publish.reconcile_publishing(engine, deps.instagram, now))
    for post in approved:
        if post.created_at is not None and now - _aware(post.created_at) > MAX_PUBLISH_AGE:
            if db.transition_post_state(engine, post.id, PostState.approved, PostState.expired, now):
                out.expired_stale += 1
            continue
        try:
            draft = db.latest_draft(engine, post.id)
            if not draft or not draft["slides"] or not post.caption:
                db.transition_post_state(engine, post.id, PostState.approved, PostState.failed)
                raise publish.PublishError(f"post {post.id} has no stored slides or caption")
            publish.publish_post(post.id, draft["slides"], post.caption, deps.instagram, engine,
                                 sleep=deps.sleep, kind="reel" if post.format == "reel" else "images")
            out.published += 1
            if deps.telegram is not None:
                gate.notify(deps.telegram, f"Published post #{post.id} on Instagram.")
        except _PROPAGATE:
            raise
        except Exception as exc:
            out.failed += 1
            out.errors.append(f"post {post.id}: {sanitize_error(exc)}")
            if deps.telegram is not None:
                gate.notify(deps.telegram, f"Publishing post #{post.id} failed: {sanitize_error(exc)}")
            if is_auth_failure(exc):
                raise _abort_auth(exc) from None
    return out
