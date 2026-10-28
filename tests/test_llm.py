"""Gemini provider, build_llm and auth propagation. No network: httpx.MockTransport."""
import importlib
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, update

from tgnews import cli, config, db, llm, pipeline, report
from tgnews.config import ConfigError
from tgnews.llm import LLM_AUTH_ERRORS, GeminiClient, LLMAuthError, LLMError, build_llm
from tgnews.models import Story
from tgnews.rewrite import RewriteFailed, rewrite

KEY = "AIzaSyFAKE-SECRET-KEY-1234567890"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
STORY = Story(id="s1", title="Rain", summary="Heavy rain lashed Hyderabad.", url="https://e.com/a",
              source_name="The Hindu", published_at=NOW, topic_hint="weather")
GOOD = dict(
    headline_te="హైదరాబాద్‌లో భారీ వర్షం, రోడ్లు జలమయం",
    points_te=["సోమవారం నగరంలో భారీ వర్షం కురిసింది", "పలు రోడ్లు నీట మునిగాయి"],
    caption_te="హైదరాబాద్ నగరాన్ని భారీ వర్షం ముంచెత్తింది. పలు ప్రాంతాల్లో రోడ్లు జలమయమయ్యాయి. మూలం: The Hindu",
    hashtags=["#Hyderabad"], source_credit="The Hindu", topic="weather")


def ok_body(text, finish="STOP"):
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                            "finishReason": finish}]}


def client_for(handler, **kw):
    seen = []

    def wrapped(req):
        seen.append(req)
        return handler(req)

    http = httpx.Client(transport=httpx.MockTransport(wrapped))
    return GeminiClient(KEY, http=http, **kw), seen


def call(c, model="m-1"):
    return c.messages.create(model=model, max_tokens=50, system="SYS",
                             messages=[{"role": "user", "content": "hi"}])


def test_success_shape_and_request():
    c, seen = client_for(lambda r: httpx.Response(200, json=ok_body("hello")))
    r = call(c)
    assert r.content[0].text == "hello" and r.stop_reason == "end_turn"
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == "https://generativelanguage.googleapis.com/v1beta/models/m-1:generateContent"
    assert json.loads(req.content) == {
        "systemInstruction": {"parts": [{"text": "SYS"}]},
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "generationConfig": {"maxOutputTokens": 8192}}


def test_key_in_header_never_in_url():
    c, seen = client_for(lambda r: httpx.Response(200, json=ok_body("x")))
    call(c)
    assert seen[0].headers["x-goog-api-key"] == KEY
    assert KEY not in str(seen[0].url) and "key=" not in str(seen[0].url)


def test_multiple_parts_are_concatenated():
    body = {"candidates": [{"content": {"parts": [{"text": "a"}, {"text": "b"}]}}]}
    c, _ = client_for(lambda r: httpx.Response(200, json=body))
    assert call(c).content[0].text == "ab"


def test_max_tokens_finish_reason():
    c, _ = client_for(lambda r: httpx.Response(200, json=ok_body("cut", "MAX_TOKENS")))
    assert call(c).stop_reason == "max_tokens"


@pytest.mark.parametrize("body", [
    {}, {"candidates": []}, {"promptFeedback": {"blockReason": "SAFETY"}},
    {"candidates": [{"finishReason": "SAFETY"}]},
    ok_body("partial", "SAFETY"), ok_body("x", "RECITATION"), ok_body("x", "OTHER"),
    ok_body("   "), {"candidates": [{"content": {}}]}, [1, 2]])
def test_unusable_replies_are_transient_not_auth(body):
    c, _ = client_for(lambda r: httpx.Response(200, json=body))
    with pytest.raises(LLMError) as ei:
        call(c)
    assert not isinstance(ei.value, LLMAuthError)


def test_non_json_reply_is_transient():
    c, _ = client_for(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(LLMError):
        call(c)


@pytest.mark.parametrize("code", [401, 403])
def test_401_403_are_auth_errors(code):
    c, _ = client_for(lambda r: httpx.Response(code, json={"error": {"message": f"bad {KEY}"}}))
    with pytest.raises(LLMAuthError) as ei:
        call(c)
    assert KEY not in str(ei.value) and KEY not in repr(ei.value)


def test_400_invalid_key_is_auth_error():
    body = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "API key not valid",
                      "details": [{"reason": "API_KEY_INVALID"}]}}
    c, _ = client_for(lambda r: httpx.Response(400, json=body))
    with pytest.raises(LLMAuthError):
        call(c)


def test_other_400_is_transient():
    c, _ = client_for(lambda r: httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT"}}))
    with pytest.raises(LLMError) as ei:
        call(c)
    assert not isinstance(ei.value, LLMAuthError)


@pytest.mark.parametrize("code", [429, 500, 503])
def test_429_and_5xx_are_transient(code):
    c, _ = client_for(lambda r: httpx.Response(code, json={"error": {"message": KEY}}))
    with pytest.raises(LLMError) as ei:
        call(c)
    assert not isinstance(ei.value, LLMAuthError) and KEY not in str(ei.value)


def test_network_error_is_transient_without_key_or_context(caplog):
    def boom(req):
        raise httpx.ConnectError(f"cannot reach {req.url} with {KEY}", request=req)

    c, _ = client_for(boom)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LLMError) as ei:
            call(c)
    assert KEY not in str(ei.value) and ei.value.__cause__ is None and ei.value.__suppress_context__
    assert KEY not in caplog.text


def test_repr_hides_key_and_loggers_quiet():
    c, _ = client_for(lambda r: httpx.Response(200, json=ok_body("x")))
    assert KEY not in repr(c) and KEY not in str(vars(c).get("messages", ""))
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_empty_key_rejected_without_echo():
    with pytest.raises(ConfigError):
        GeminiClient("  ")


# --- rewrite / report / pipeline integration ---

def test_rewrite_counts_blocked_reply_as_failed_attempt_then_fails():
    c, seen = client_for(lambda r: httpx.Response(200, json=ok_body("x", "SAFETY")))
    with pytest.raises(RewriteFailed) as ei:
        rewrite(STORY, c, "statement", "m-1")
    assert len(seen) == 2 and isinstance(ei.value.__cause__, LLMError)


def test_rewrite_recovers_after_transient_503():
    replies = iter([httpx.Response(503), httpx.Response(200, json=ok_body(json.dumps(GOOD)))])
    c, seen = client_for(lambda r: next(replies))
    assert rewrite(STORY, c, "statement", "m-1").topic == "weather"
    assert len(seen) == 2


def test_rewrite_truncation_is_failed_attempt():
    c, seen = client_for(lambda r: httpx.Response(200, json=ok_body("{", "MAX_TOKENS")))
    with pytest.raises(RewriteFailed):
        rewrite(STORY, c, "statement", "m-1")
    assert len(seen) == 2


@pytest.mark.parametrize("resp", [
    httpx.Response(401), httpx.Response(403),
    httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT",
                                        "details": [{"reason": "API_KEY_INVALID"}]}})])
def test_auth_error_propagates_through_rewrite(resp):
    c, seen = client_for(lambda r: resp)
    with pytest.raises(LLMAuthError):
        rewrite(STORY, c, "statement", "m-1")
    assert len(seen) == 1


def _engine_with_scored(n=40):
    e = create_engine("sqlite:///:memory:")
    db.init(e)
    db.save_story(e, Story(id="s", title="t", summary="s", url="https://x/1", source_name="n",
                           published_at=NOW))
    for i in range(n):
        pid = db.save_post(e, story_id="s", format="card", topic="politics", time_slot="09:00",
                           headline_style="question")
        db.set_post_metrics(e, pid, {"score": 0.1 + (i % 5) / 100})
        with e.begin() as cn:
            cn.execute(update(db.posts).where(db.posts.c.id == pid)
                       .values(posted_at=NOW - timedelta(days=i % 10)))
    return e


def test_auth_error_propagates_through_weekly_report():
    c, _ = client_for(lambda r: httpx.Response(403))
    with pytest.raises(LLMAuthError):
        report.weekly_report(_engine_with_scored(), c, NOW, "m-1")


def test_weekly_report_passes_the_given_model_and_falls_back_on_transient():
    c, seen = client_for(lambda r: httpx.Response(429))
    out = report.weekly_report(_engine_with_scored(), c, NOW, "my-model")
    assert out.startswith("Weekly report: 40") and "my-model:generateContent" in str(seen[0].url)


def test_pipeline_propagates_llm_auth_error():
    assert LLMAuthError in pipeline._PROPAGATE
    assert all(t in pipeline._PROPAGATE for t in LLM_AUTH_ERRORS)


# --- build_llm ---

def test_build_llm_defaults_to_gemini():
    client, model = build_llm({"GEMINI_API_KEY": KEY, "LLM_MODEL": "some-flash-id"})
    assert isinstance(client, GeminiClient) and model == "some-flash-id"
    assert KEY not in repr(client)


def test_build_llm_gemini_requires_model_and_key():
    with pytest.raises(ConfigError) as e1:
        build_llm({"GEMINI_API_KEY": KEY})
    assert "LLM_MODEL" in str(e1.value) and KEY not in str(e1.value)
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        build_llm({"LLM_MODEL": "m"})


def test_build_llm_unknown_provider_does_not_echo_value():
    with pytest.raises(ConfigError) as ei:
        build_llm({"LLM_PROVIDER": "supersecretprovider", "GEMINI_API_KEY": KEY})
    assert "supersecretprovider" not in str(ei.value)


def test_build_llm_anthropic_provider_and_default_model():
    anthropic = pytest.importorskip("anthropic")
    from tgnews.rewrite import DEFAULT_MODEL
    client, model = build_llm({"LLM_PROVIDER": "Anthropic", "ANTHROPIC_API_KEY": "k-123456"})
    assert isinstance(client, anthropic.Anthropic) and model == DEFAULT_MODEL
    assert build_llm({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k-123456",
                      "LLM_MODEL": "x"})[1] == "x"
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        build_llm({"LLM_PROVIDER": "anthropic"})


def test_settings_carry_llm_fields():
    s = config.Settings.from_env({"LLM_PROVIDER": "Gemini", "LLM_MODEL": " m "})
    assert (s.llm_provider, s.llm_model) == ("gemini", "m")
    d = config.Settings.from_env({})
    assert (d.llm_provider, d.llm_model) == ("gemini", "")


def test_build_deps_uses_build_llm(monkeypatch, tmp_path):
    src = tmp_path / "s.yaml"
    src.write_text("feeds: []\n")
    env = {"GEMINI_API_KEY": KEY, "LLM_MODEL": "flash-x", "SOURCES_PATH": str(src)}
    deps = cli.build_deps(config.Settings.from_env(env), env, purpose="dry-run")
    assert isinstance(deps.llm, GeminiClient) and deps.llm_model == "flash-x"


def test_build_deps_missing_model_fails_clearly():
    env = {"GEMINI_API_KEY": KEY}
    with pytest.raises(ConfigError, match="LLM_MODEL"):
        cli.build_deps(config.Settings.from_env(env), env, purpose="report")


def test_default_install_works_without_anthropic(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    saved = {k: sys.modules.pop(k) for k in [k for k in sys.modules if k.startswith("tgnews")]}
    try:
        fresh = importlib.import_module("tgnews.llm")
        assert fresh.LLM_AUTH_ERRORS == (fresh.LLMAuthError,)
        importlib.import_module("tgnews.rewrite")
        importlib.import_module("tgnews.report")
        importlib.import_module("tgnews.pipeline")
        importlib.import_module("tgnews.cli")
        client, _ = fresh.build_llm({"GEMINI_API_KEY": KEY, "LLM_MODEL": "m"})
        assert isinstance(client, fresh.GeminiClient)
        with pytest.raises(fresh.ConfigError, match="anthropic"):
            fresh.build_llm({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k-123456"})
    finally:
        for k in [k for k in sys.modules if k.startswith("tgnews")]:
            del sys.modules[k]
        sys.modules.update(saved)


# --- review fixes ---
from tgnews.llm import LLMModelError, LLMRateLimited  # noqa: E402
from tgnews.rewrite import RewriteUnavailable  # noqa: E402
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from test_pipeline import FakeTG, engine, make_deps, settings, story  # noqa: E402,F401  (fixtures/helpers)


def test_max_output_tokens_has_safety_margin_and_keeps_larger_requests():
    c, seen = client_for(lambda r: httpx.Response(200, json=ok_body("x")))
    c.messages.create(model="m", max_tokens=100, system="s", messages=[{"role": "user", "content": "h"}])
    c.messages.create(model="m", max_tokens=9000, system="s", messages=[{"role": "user", "content": "h"}])
    assert [json.loads(r.content)["generationConfig"]["maxOutputTokens"] for r in seen] == [8192, 9000]


def test_400_key_problem_detected_from_message_without_details():
    for msg in ("API key not valid. Please pass a valid API key.", "api_key missing"):
        c, _ = client_for(lambda r, m=msg: httpx.Response(400, json={"error": {"message": m}}))
        with pytest.raises(LLMAuthError):
            call(c)


def test_404_is_fatal_model_error_without_body_or_key():
    c, _ = client_for(lambda r: httpx.Response(404, json={"error": {"message": f"models/x {KEY}"}}))
    with pytest.raises(LLMModelError) as ei:
        call(c)
    assert str(ei.value) == "model not found; check LLM_MODEL" and KEY not in repr(ei.value)
    assert issubclass(LLMModelError, LLMAuthError) and LLMAuthError in LLM_AUTH_ERRORS
    with pytest.raises(LLMModelError):
        rewrite(STORY, c, "statement", "m-1")


def test_thought_parts_are_skipped():
    body = {"candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True},
                                                  {"text": "answer"}]}}]}
    c, _ = client_for(lambda r: httpx.Response(200, json=body))
    assert call(c).content[0].text == "answer"


def test_429_skips_second_attempt_and_raises_unavailable():
    c, seen = client_for(lambda r: httpx.Response(429))
    with pytest.raises(RewriteUnavailable) as ei:
        rewrite(STORY, c, "statement", "m-1")
    assert len(seen) == 1 and isinstance(ei.value.__cause__, LLMRateLimited)


def test_rewrite_failure_kinds():
    c, _ = client_for(lambda r: httpx.Response(503))
    with pytest.raises(RewriteUnavailable):
        rewrite(STORY, c, "statement", "m-1")
    # mixed: transport error first, invalid content last -> content failure
    replies = iter([httpx.Response(503), httpx.Response(200, json=ok_body("not json"))])
    c, _ = client_for(lambda r: next(replies))
    with pytest.raises(RewriteFailed) as ei:
        rewrite(STORY, c, "statement", "m-1")
    assert not isinstance(ei.value, RewriteUnavailable)
    # mixed the other way: content first, transport last -> unavailable
    replies = iter([httpx.Response(200, json=ok_body("not json")), httpx.Response(503)])
    c, _ = client_for(lambda r: next(replies))
    with pytest.raises(RewriteUnavailable):
        rewrite(STORY, c, "statement", "m-1")


def test_llm_outage_releases_claims_and_aborts_run(engine, tmp_path):
    c, seen = client_for(lambda r: httpx.Response(503))
    stories = [story(i) for i in range(1, 6)]
    with pytest.raises(pipeline.PipelineError, match="infrastructure"):
        pipeline.run_once(settings(), make_deps(engine, tmp_path, stories, llm=c, tg=FakeTG()))
    assert len(seen) <= 4
    assert all(db.save_story(engine, s) for s in stories)  # none was left marked seen


def test_quota_429_burst_releases_claims_and_aborts(engine, tmp_path):
    c, seen = client_for(lambda r: httpx.Response(429))
    stories = [story(i) for i in range(1, 6)]
    with pytest.raises(pipeline.PipelineError):
        pipeline.run_once(settings(), make_deps(engine, tmp_path, stories, llm=c, tg=FakeTG()))
    assert len(seen) <= 4
    assert all(db.save_story(engine, s) for s in stories)


def test_invalid_content_failures_still_mark_stories_seen(engine, tmp_path):
    c, _ = client_for(lambda r: httpx.Response(200, json=ok_body("not json")))
    stories = [story(1), story(2)]
    summary = pipeline.run_once(settings(), make_deps(engine, tmp_path, stories, llm=c, tg=FakeTG()))
    assert summary.failed == 2
    assert not any(db.save_story(engine, s) for s in stories)  # already seen


def test_wrong_model_404_aborts_run(engine, tmp_path):
    c, seen = client_for(lambda r: httpx.Response(404))
    with pytest.raises(LLMModelError):
        pipeline.run_once(settings(), make_deps(engine, tmp_path, [story(1), story(2)], llm=c, tg=FakeTG()))
    assert len(seen) == 1


def test_settings_reject_unknown_provider_without_echo():
    with pytest.raises(ConfigError) as ei:
        config.Settings.from_env({"LLM_PROVIDER": "weirdprovider"})
    assert "weirdprovider" not in str(ei.value)
