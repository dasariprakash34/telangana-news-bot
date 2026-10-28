import random
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from tgnews import cli, db, pipeline
from tgnews.analyze import InsightsAborted
from tgnews.models import PostState
from tgnews.pipeline import Deps

NOW = datetime(2026, 10, 28, 7, 5, tzinfo=timezone.utc)
SECRET = "sk-ant-SUPERSECRET-123"


@pytest.fixture
def engine():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.init(e)
    return e


class TG:
    chat_id = "1"

    def __init__(self):
        self.calls = []

    def call(self, method, **p):
        self.calls.append((method, p))
        return {"message_id": 1}


def builder(engine, **kw):
    def build(settings, env, *, purpose="run"):
        base = dict(fetch=lambda: [], llm=object(), engine=engine, now=lambda: NOW,
                    rng_factory=lambda: random.Random(1), check_shaping=lambda: None)
        base.update(kw)
        return Deps(**base)
    return build


def run(argv, build, env=None, monkeypatch=None):
    return cli.main(argv, env if env is not None else {"MODE": "review"}, deps_builder=build)


def test_run_command_returns_zero_and_passes_ignore_slots(engine, monkeypatch, capsys):
    seen = {}

    def fake_run_once(settings, deps, *, ignore_slots=False):
        seen["ignore"] = ignore_slots
        return pipeline.RunSummary(fetched=3)

    monkeypatch.setattr(cli.pipeline, "run_once", fake_run_once)
    assert run(["run", "--ignore-slots"], builder(engine)) == 0
    assert seen["ignore"] is True
    assert "fetched=3" in capsys.readouterr().out


def test_fatal_error_returns_nonzero_prints_no_secret_and_alerts(engine, monkeypatch, capsys):
    tg = TG()

    def boom(settings, deps, *, ignore_slots=False):
        raise RuntimeError(f"failed with key {SECRET}")

    monkeypatch.setattr(cli.pipeline, "run_once", boom)
    env = {"MODE": "review", "ANTHROPIC_API_KEY": SECRET}
    code = cli.main(["run"], env, deps_builder=builder(engine, telegram=tg), alert_client=lambda e: tg)
    out = capsys.readouterr()
    assert code == 1
    assert SECRET not in out.out + out.err and "tgnews" in out.err
    assert tg.calls and SECRET not in str(tg.calls)


def test_bad_config_is_fatal_without_traceback(engine, capsys):
    assert cli.main(["run"], {"MODE": "turbo"}, deps_builder=builder(engine)) == 1
    assert "MODE" in capsys.readouterr().err


def test_analyze_updates_learning_and_normalizes_naive_now(engine, monkeypatch, capsys):
    got = {}

    def fake_analysis(eng, client, now):
        got["now"] = now
        return 2

    monkeypatch.setattr(cli.analyze, "run_analysis", fake_analysis)
    monkeypatch.setattr(cli.learn, "update_learning", lambda eng: got.setdefault("learned", True))
    naive = builder(engine, instagram=object(), now=lambda: datetime(2026, 10, 28, 7, 5))
    assert run(["analyze"], naive) == 0
    assert got["now"].tzinfo is not None and got["learned"]
    assert "recorded=2" in capsys.readouterr().out


def test_analyze_aborted_exits_nonzero_with_alert(engine, monkeypatch, capsys):
    tg = TG()

    def abort(eng, client, now):
        raise InsightsAborted("token expired", status=401)

    monkeypatch.setattr(cli.analyze, "run_analysis", abort)
    code = cli.main(["analyze"], {"MODE": "review"}, deps_builder=builder(engine, instagram=object(), telegram=tg))
    assert code == 1
    assert "ERROR" in capsys.readouterr().err
    assert tg.calls


def test_report_prints_and_sends_via_telegram(engine, monkeypatch, capsys):
    tg = TG()
    monkeypatch.setattr(cli.report, "weekly_report", lambda eng, client, now: "REPORT TEXT")
    assert run(["report"], builder(engine, telegram=tg)) == 0
    assert "REPORT TEXT" in capsys.readouterr().out
    assert tg.calls[0][0] == "sendMessage" and tg.calls[0][1]["text"] == "REPORT TEXT"


def test_refresh_token_never_prints_token(engine, monkeypatch, capsys):
    from tgnews.publish import Token

    monkeypatch.setattr(cli.publish, "refresh_token",
                        lambda client, eng, now: Token("IGTOKEN-XYZ", NOW.replace(year=2026, month=12)))
    assert run(["refresh-token"], builder(engine, instagram=object())) == 0
    out = capsys.readouterr()
    assert "IGTOKEN-XYZ" not in out.out + out.err and "days" in out.out


def test_publish_approved_and_dry_run_dispatch(engine, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli.pipeline, "publish_approved",
                        lambda s, d: pipeline.PublishSummary(published=4))
    assert run(["publish-approved"], builder(engine)) == 0
    assert "published=4" in capsys.readouterr().out
    seen = {}
    monkeypatch.setattr(cli.pipeline, "dry_run",
                        lambda s, d, out: seen.setdefault("out", out) and pipeline.RunSummary(drafted=1))
    assert run(["dry-run", "--out", str(tmp_path / "o")], builder(engine)) == 0
    assert str(seen["out"]) == str(tmp_path / "o")


def test_build_deps_requires_ig_api_version_and_hides_secrets(engine):
    from tgnews.config import Settings
    env = {"MODE": "auto", "ANTHROPIC_API_KEY": SECRET, "IG_ACCESS_TOKEN": "tok", "IG_USER_ID": "1",
           "DATABASE_URL": "sqlite://"}
    with pytest.raises(Exception) as ei:
        cli.build_deps(Settings.from_env(env), env, purpose="publish")
    assert "IG_API_VERSION" in str(ei.value) and SECRET not in str(ei.value)
