"""`tgnews` command line. Secrets come only from the environment and are never printed."""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable, Mapping, Sequence

from sqlalchemy import create_engine

from . import analyze, db, learn, pipeline, publish, report
from .config import ConfigError, Settings
from .fetch import FetchReport, fetch_stories, load_sources
from .gate import TelegramClient
from .instagram import InstagramClient
from .llm import build_llm
from .pipeline import Deps, PipelineError
from .render import ShapingUnavailable
from .rewrite import DEFAULT_MODEL
from .safety import BlocklistError
from .storage import StorageConfig, upload

DEFAULT_SOURCES = "config/sources.yaml"
_SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "DATABASE_URL")
# Only our own error types have messages known to be free of env values; everything else is
# shown as its type name (and then scrubbed).
_SHOWN = pipeline._SAFE_MESSAGE + (PipelineError, BlocklistError, ShapingUnavailable, ConfigError)
_NEEDS_LLM = {"run", "dry-run", "report"}
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
            engine = create_engine(settings.database_url)
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
        cfg = StorageConfig.from_env(env)
        up = lambda paths, prefix: upload(paths, prefix, config=cfg)  # noqa: E731
    sources = load_sources(env.get("SOURCES_PATH") or DEFAULT_SOURCES)
    report = FetchReport()
    return Deps(fetch=lambda: fetch_stories(sources, report=report), fetch_report=report,
                llm=llm, llm_model=llm_model, engine=engine, upload=up, instagram=ig, telegram=tg,
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
    for name in ("publish-approved", "analyze", "report", "refresh-token"):
        sub.add_parser(name)
    return p


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


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None, *,
         deps_builder: Callable[..., Deps] | None = None,
         alert_client: Callable[[Mapping[str, str]], object] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse: --help or bad usage
        return int(exc.code or 0)
    purpose = {"run": "run", "dry-run": "dry-run", "publish-approved": "publish",
               "analyze": "analyze", "report": "report", "refresh-token": "refresh-token"}[args.command]
    deps = None
    try:
        settings = Settings.from_env(env)
        deps = (deps_builder or build_deps)(settings, env, purpose=purpose)
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
