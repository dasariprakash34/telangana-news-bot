"""Provider-agnostic LLM layer. Default: Google Gemini API free tier (plain REST, no SDK).

Both providers expose `client.messages.create(model=, max_tokens=, system=, messages=)` and
return an object with `.content[0].text` and `.stop_reason`, which is all rewrite.py/report.py use.

Security rule: the API key must never reach an exception message, log line or repr. It travels
in the `x-goog-api-key` header (never the URL), and httpx errors are re-raised without them.

NOTE: the Gemini request/response shape is written from memory of the public REST docs and was
not verified against live docs; all response parsing is deliberately tolerant.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from .config import ConfigError

# httpx logs full request URLs at INFO; keep it quiet like instagram.py.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
TIMEOUT_SECONDS = 60.0
PROVIDERS = ("gemini", "anthropic")
# Thinking-capable models spend the output budget on thinking and Telugu is token-expensive, so
# never ask for less than this; callers' own limits still apply through their validation.
MIN_OUTPUT_TOKENS = 8192
_BAD_FINISH = {"SAFETY", "RECITATION", "OTHER", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}

try:  # anthropic is an optional extra
    import anthropic as _anthropic
except ImportError:  # pragma: no cover - exercised via sys.modules patching
    _anthropic = None


class LLMError(Exception):
    """Transient LLM failure (rate limit, server/network error, blocked or empty reply).
    Messages are sanitized and safe to log."""


class LLMContentError(LLMError):
    """The call worked but produced no usable content (safety block, no candidates, empty text).
    Deterministic per story, so callers treat it as a content failure, not an outage."""


class LLMRateLimited(LLMError):
    """HTTP 429: quota or rate limit. Retrying immediately only burns more quota."""


class LLMAuthError(Exception):
    """The API key was rejected. Must propagate; never retried or swallowed."""


class LLMModelError(LLMAuthError):
    """HTTP 404: the model id is wrong. Fatal (configuration), propagates like an auth error."""


LLM_AUTH_ERRORS: tuple[type[BaseException], ...] = (LLMAuthError,)  # includes LLMModelError
if _anthropic is not None:
    LLM_AUTH_ERRORS += (_anthropic.AuthenticationError, _anthropic.PermissionDeniedError)


@dataclass
class _Block:
    text: str


@dataclass
class _Response:
    content: list[_Block]
    stop_reason: str


def _error_info(resp: httpx.Response) -> tuple[str, str, str]:
    """(status, reason, message) strings from a Google error body; tolerant of any shape."""
    try:
        err = resp.json().get("error", {})
        status = str(err.get("status") or "")
        reason = ""
        for d in err.get("details") or []:
            if isinstance(d, dict) and d.get("reason"):
                reason = str(d["reason"])
                break
        return status, reason, str(err.get("message") or "")
    except Exception:
        return "", "", ""


def _is_key_problem(code: int, resp: httpx.Response) -> bool:
    if code in (401, 403):
        return True
    if code == 400:
        status, reason, message = _error_info(resp)
        text = f"{status} {reason} {message}".upper().replace("API KEY", "API_KEY")
        return "API_KEY" in text or "UNAUTHENTICATED" in text
    return False


class _Messages:
    def __init__(self, owner: "GeminiClient"):
        self._owner = owner

    def create(self, *, model: str, max_tokens: int, system: str, messages: list[dict]) -> _Response:
        return self._owner._generate(model, max_tokens, system, messages)


class GeminiClient:
    def __init__(self, api_key: str, *, http: httpx.Client | None = None,
                 base_url: str = DEFAULT_BASE_URL):
        if not api_key or not api_key.strip():
            raise ConfigError("GEMINI_API_KEY is empty")
        self._api_key = api_key.strip()
        self._http = http or httpx.Client(timeout=TIMEOUT_SECONDS)
        self._base_url = base_url.rstrip("/")
        self.messages = _Messages(self)

    def __repr__(self) -> str:
        return "GeminiClient(api_key=***)"

    def _generate(self, model: str, max_tokens: int, system: str, messages: list[dict]) -> _Response:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": m["content"]}]} for m in messages],
            "generationConfig": {"maxOutputTokens": max(max_tokens, MIN_OUTPUT_TOKENS)},
        }
        url = f"{self._base_url}/models/{model}:generateContent"
        try:
            resp = self._http.post(url, json=body, headers={"x-goog-api-key": self._api_key},
                                   timeout=TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:  # message may embed the URL; keep only the type
            raise LLMError(f"Gemini request failed: {type(exc).__name__}") from None
        code = resp.status_code
        if _is_key_problem(code, resp):
            raise LLMAuthError(f"Gemini rejected the API key (HTTP {code})") from None
        if code == 404:
            raise LLMModelError("model not found; check LLM_MODEL") from None
        if code == 429:
            raise LLMRateLimited("Gemini rate limit or quota exceeded (HTTP 429)") from None
        if code >= 400:
            raise LLMError(f"Gemini returned HTTP {code}") from None
        try:
            data: Any = resp.json()
        except ValueError:
            raise LLMError("Gemini returned a non-JSON reply") from None
        return _parse(data)


def _parse(data: Any) -> _Response:
    cands = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(cands, list) or not cands or not isinstance(cands[0], dict):
        raise LLMContentError("Gemini reply has no candidates (possibly blocked)")
    cand = cands[0]
    finish = str(cand.get("finishReason") or "").upper()
    if finish in _BAD_FINISH:
        raise LLMContentError(f"Gemini reply not usable (finishReason {finish})")
    content = cand.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    text = "".join(p["text"] for p in parts or []
                   if isinstance(p, dict) and isinstance(p.get("text"), str)
                   and p.get("thought") is not True)
    if not text.strip():
        raise LLMContentError("Gemini reply has no text")
    return _Response([_Block(text)], "max_tokens" if finish == "MAX_TOKENS" else "end_turn")


def build_llm(env: Mapping[str, str]) -> tuple[Any, str]:
    """Return (client, model) for LLM_PROVIDER. Messages name settings only, never values."""
    provider = (env.get("LLM_PROVIDER") or "").strip().lower() or "gemini"
    if provider not in PROVIDERS:
        raise ConfigError(f"LLM_PROVIDER must be one of {PROVIDERS}")
    if provider == "gemini":
        key = (env.get("GEMINI_API_KEY") or "").strip()
        model = (env.get("LLM_MODEL") or "").strip()
        if not key:
            raise ConfigError("GEMINI_API_KEY is required when LLM_PROVIDER is gemini")
        if not model:
            raise ConfigError("LLM_MODEL is required when LLM_PROVIDER is gemini "
                              "(copy a current Flash or Flash-Lite model id from Google AI Studio)")
        return GeminiClient(key), model
    key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise ConfigError("ANTHROPIC_API_KEY is required when LLM_PROVIDER is anthropic")
    if _anthropic is None:
        raise ConfigError('LLM_PROVIDER=anthropic needs the optional extra: pip install -e ".[anthropic]"')
    from .rewrite import DEFAULT_MODEL  # lazy: rewrite imports this module
    return _anthropic.Anthropic(api_key=key), (env.get("LLM_MODEL") or "").strip() or DEFAULT_MODEL
