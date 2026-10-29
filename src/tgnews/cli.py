"""`tgnews` command line. Secrets come only from the environment and are never printed."""
from __future__ import annotations

import argparse
import dataclasses
import functools
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from sqlalchemy import create_engine

from . import analyze, db, gate, learn, pipeline, publish, reel, report, viral
from .config import ConfigError, Settings
from .fetch import FetchReport, fetch_stories, load_sources
from .gate import TelegramClient
from .instagram import InstagramClient
from .llm import build_llm
from .pipeline import Deps, PipelineError
from .render import ShapingUnavailable
from .rewrite import DEFAULT_MODEL
from .safety import BlocklistError
from . import storage_github
from .storage import build_uploader, build_video_uploader, storage_provider

DEFAULT_SOURCES = "config/sources.yaml"
_SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "DATABASE_URL")
# Only our own error types have messages known to be free of env values; everything else is
# shown as its type name (and then scrubbed).
_SHOWN = pipeline._SAFE_MESSAGE + (PipelineError, BlocklistError, ShapingUnavailable, ConfigError)
_NEEDS_LLM = {"run", "dry-run", "report", "daily-viral"}
_NEEDS_IG = {"publish", "analyze", "refresh-token"}


def build_deps(settings: Settings, env: Mapping[str, str], *, purpose: str = "run") -> Deps:
    """Construct real collaborators from the environment for one command (`purpose`)."""
    def need(name: str) -> str:
        value = (env.get(name) or "").strip()
        if not value:
            raise PipelineError(f"missing required environment variable {name}")
        return value

    llm, llm_model = None, DEFAULT_MODEL
    if purpose in _NEEDS_LLM:
        llm, llm_model = build_llm(env)
    engine = None
    if purpose != "dry-run":
        try:
            engine = create_engine(settings.database_url, pool_pre_ping=True)  # long reels outlive idle connections
            db.init(engine)
        except Exception:  # URL parsing / driver errors can echo parts of the password
            raise PipelineError("DATABASE_URL is invalid or the database is unreachable") from None
    ig = None
    if purpose in _NEEDS_IG or (purpose == "run" and settings.mode == "auto"):
        ig = InstagramClient.from_env(env, engine)
    tg = None
    if purpose != "dry-run" and (env.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_CHAT_ID")):
        tg = TelegramClient.from_env(env)
    up = None
    if purpose == "run":
        up = build_uploader(env)
    make_reel, up_video = None, None
    if (settings.reels_enabled and purpose in ("run", "dry-run")) or purpose == "daily-viral":
        make_reel = functools.partial(reel.render_reel, voice=settings.reel_voice,
                                      rate=settings.reel_rate, style=settings.reel_style,
                                      require_credit=settings.require_source_credit)
        if purpose in ("run", "daily-viral"):
            up_video = build_video_uploader(env)
    sources = load_sources(env.get("SOURCES_PATH") or DEFAULT_SOURCES)
    report = FetchReport()
    return Deps(fetch=lambda: fetch_stories(sources, report=report), fetch_report=report,
                llm=llm, llm_model=llm_model, engine=engine, upload=up, instagram=ig, telegram=tg,
                render_reel=make_reel, upload_video=up_video,
                out_dir=Path(env.get("OUT_DIR") or tempfile.mkdtemp(prefix="tgnews-")))


def _scrub(text: str, env: Mapping[str, str]) -> str:
    for name, value in env.items():
        if value and len(value) >= 4 and any(h in name.upper() for h in _SECRET_HINTS):
            text = text.replace(value, "[REDACTED]")
    return text


def _describe(exc: BaseException, env: Mapping[str, str]) -> str:
    text = str(exc) if isinstance(exc, _SHOWN) else type(exc).__name__
    return _scrub(" ".join(text.split())[:300], env) or type(exc).__name__


def _alert(telegram, text: str) -> None:
    if telegram is None:
        return
    try:
        telegram.call("sendMessage", chat_id=telegram.chat_id, text=text[:4000])
    except Exception:  # best effort only
        pass


def _fmt(summary) -> str:
    return " ".join(f"{k}={v}" for k, v in dataclasses.asdict(summary).items() if k != "errors")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tgnews")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="one pipeline run").add_argument(
        "--ignore-slots", action="store_true", help="run even if this time slot is not active")
    sub.add_parser("dry-run", help="render to a directory, no network side effects").add_argument(
        "--out", required=True, metavar="DIR")
    sub.add_parser("prune-images", help="delete old images from the GitHub images repo")
    sub.add_parser("check-images", help="GET one public image URL and report status and content type"
                   ).add_argument("url")
    listen = sub.add_parser("listen", help="long-poll Telegram and act on taps within seconds")
    listen.add_argument("--minutes", type=float, default=340.0, help="stop after this many minutes")
    listen.add_argument("--post", action="store_true",
                        help="also run the posting pipeline at every :00 and :30 (UTC), so drafts do not "
                             "depend on GitHub's unreliable cron")
    for name in ("publish-approved", "analyze", "report", "refresh-token"):
        sub.add_parser(name)
    sub.add_parser("daily-viral", help="make today's trending-topic reel and send it to Telegram for approval")
    return p


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _publish_pass(settings: Settings, deps: Deps) -> None:
    summary = pipeline.publish_approved(settings, deps)
    print(_fmt(summary), flush=True)
    for e in summary.errors:
        print(f"error: {e}", file=sys.stderr, flush=True)


def _posting_run(settings: Settings, builder: Callable[[], Deps] | None) -> None:
    """One posting run, in a background thread so taps keep being handled while it works.
    A failure here is printed and never takes the listener down."""
    try:
        run_deps = (builder or (lambda: build_deps(settings, os.environ, purpose="run")))()
        summary = pipeline.run_once(settings, run_deps)
        print("post: " + _fmt(summary), flush=True)
        for e in summary.errors:
            print(f"post error: {e}", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"listen: posting run failed: {_describe(exc, os.environ)}", file=sys.stderr, flush=True)


def _next_half_hour(now: datetime) -> datetime:
    base = now.replace(second=0, microsecond=0)
    return base.replace(minute=30) if now.minute < 30 else (base.replace(minute=0) + timedelta(hours=1))


def _listen(args, settings: Settings, deps: Deps, *, clock: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep, wall: Callable[[], datetime] = _now,
            run_deps_builder: Callable[[], Deps] | None = None) -> int:
    """Hold a Telegram long poll open so a tap is handled within seconds instead of at the next
    scheduled run. The database is touched only when Telegram reports a new update, so an idle
    listener does not keep a serverless database awake. A getUpdates 409 means another poller
    (a posting run) took the connection; it already handled the updates, so just poll again."""
    if deps.telegram is None:
        raise pipeline.PipelineError("listen needs Telegram")
    end = clock() + args.minutes * 60
    _publish_pass(settings, deps)

    def offset() -> int:
        raw = db.get_kv(deps.engine, gate.KV_OFFSET)
        return int(raw) if raw else 0

    current = offset()
    failures = 0
    post = bool(getattr(args, "post", False))
    worker: threading.Thread | None = None
    next_post = wall() if post else None  # post straight away on start, then every :00 and :30
    while clock() < end:
        if next_post is not None and wall() >= next_post:
            next_post = _next_half_hour(wall())
            if worker is None or not worker.is_alive():  # never two posting runs at once
                worker = threading.Thread(target=_posting_run, daemon=True,
                                          args=(settings, run_deps_builder))
                worker.start()
        try:
            updates = deps.telegram.call("getUpdates", offset=current, timeout=20,
                                         allowed_updates=["callback_query", "message", "edited_message"]) or []
            failures = 0
        except gate.TelegramError as exc:
            failures += 1
            print(f"listen: poll failed: {exc}", file=sys.stderr, flush=True)
            sleep(min(5 * failures, 60))
            continue
        if not updates:
            continue
        try:
            _publish_pass(settings, deps)
        except pipeline.PipelineError:
            raise
        except Exception as exc:
            print(f"listen: pass failed: {_describe(exc, os.environ)}", file=sys.stderr, flush=True)
        new = offset()
        if new == current:  # nothing advanced: avoid a hot loop on a poison update
            sleep(30)
        current = new
    return 0


def _dispatch(args, settings: Settings, deps: Deps) -> int:
    cmd, now = args.command, pipeline._aware(deps.now())
    if cmd == "run":
        summary = pipeline.run_once(settings, deps, ignore_slots=args.ignore_slots)
        print(_fmt(summary))
        for e in summary.errors:
            print(f"error: {e}", file=sys.stderr)
        if summary.fetch_alert:  # exit stays 0: nothing is broken that a retry would fix
            print(f"warning: {summary.fetch_alert}", file=sys.stderr)
            _alert(deps.telegram, f"tgnews: {summary.fetch_alert}")
        if summary.infra_errors:  # exit stays 0 (an abort raises and exits 1 elsewhere)
            _alert(deps.telegram, f"tgnews: {len(summary.infra_errors)} infrastructure failure(s) "
                                  f"in this run; last: {summary.infra_errors[-1]}")
    elif cmd == "dry-run":
        summary = pipeline.dry_run(settings, deps, args.out)
        print(_fmt(summary))
        for e in summary.errors:
            print(f"error: {e}", file=sys.stderr)
    elif cmd == "publish-approved":
        summary = pipeline.publish_approved(settings, deps)
        print(_fmt(summary))
        for e in summary.errors:
            print(f"error: {e}", file=sys.stderr)
    elif cmd == "daily-viral":
        summary = viral.run_daily(settings, deps)
        print(f"status={summary.status} kind={summary.kind or '-'} post_id={summary.post_id} "
              f"detail={summary.detail or '-'}")
        for e in summary.errors:
            print(f"error: {e}", file=sys.stderr)
    elif cmd == "listen":
        return _listen(args, settings, deps)
    elif cmd == "analyze":
        try:
            recorded = analyze.run_analysis(deps.engine, deps.instagram, now)
        except analyze.InsightsAborted as exc:
            learn.update_learning(deps.engine)  # keep what was recorded before the abort
            msg = f"ERROR: insights analysis aborted: {pipeline.sanitize_error(exc)}"
            print(msg, file=sys.stderr)
            _alert(deps.telegram, f"tgnews {msg}")
            return 1
        learn.update_learning(deps.engine)
        print(f"recorded={recorded}")
    elif cmd == "report":
        text = report.weekly_report(deps.engine, deps.llm, now, deps.llm_model)
        print(text)
        _alert(deps.telegram, text)
    elif cmd == "refresh-token":
        token = publish.refresh_token(deps.instagram, deps.engine, now)
        print(f"token refreshed; {publish.token_days_left(token, now)} days left")
    return 0


def _images_command(args, env: Mapping[str, str]) -> int:
    """Image-hosting housekeeping: needs no database, LLM or Instagram."""
    if args.command == "check-images":
        result = storage_github.check_public_url(args.url)
        print(f"status={result.status} content-type={result.content_type or '(none)'}")
        if not result.ok:
            print("error: not a publicly fetchable JPEG (expected HTTP 200 and image/jpeg)", file=sys.stderr)
        return 0 if result.ok else 1
    if storage_provider(env) != "github":
        print("prune-images: STORAGE_PROVIDER is not github; nothing to do")
        return 0
    store = storage_github.GithubImageStore.from_env(env)
    result = store.prune(storage_github.keep_days(env), _now())
    print(f"deleted={result.deleted} skipped={result.skipped}")
    return 0


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None, *,
         deps_builder: Callable[..., Deps] | None = None,
         alert_client: Callable[[Mapping[str, str]], object] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse: --help or bad usage
        return int(exc.code or 0)
    if args.command in ("prune-images", "check-images"):
        try:
            return _images_command(args, env)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            msg = f"tgnews: error: {_describe(exc, env)}"
            print(msg, file=sys.stderr)
            try:
                _alert((alert_client or _env_telegram)(env), f"{msg} (command: {args.command})")
            except Exception:
                pass
            return 1
    purpose = {"run": "run", "dry-run": "dry-run", "publish-approved": "publish",
               "listen": "publish", "analyze": "analyze", "report": "report", "refresh-token": "refresh-token",
               "daily-viral": "daily-viral"}[args.command]
    deps = None
    try:
        settings = Settings.from_env(env)
        deps = (deps_builder or build_deps)(settings, env, purpose=purpose)
        args.ignore_slots = bool(getattr(args, "ignore_slots", False)) or (env.get("IGNORE_SLOTS") or "").strip().lower() == "true"
        return _dispatch(args, settings, deps)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        msg = f"tgnews: error: {_describe(exc, env)}"
        print(msg, file=sys.stderr)
        telegram = deps.telegram if deps is not None else None
        if telegram is None:
            try:
                telegram = (alert_client or _env_telegram)(env)
            except Exception:
                telegram = None
        _alert(telegram, f"{msg} (command: {args.command})")
        return 1


def _env_telegram(env: Mapping[str, str]):
    return TelegramClient.from_env(env) if env.get("TELEGRAM_BOT_TOKEN") else None


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
