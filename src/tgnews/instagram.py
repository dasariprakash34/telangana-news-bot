"""Minimal Instagram Graph API client ("Instagram API with Instagram Login").

Security rule: the access token must never reach an exception message, log line or repr.
The token travels in an Authorization header (never in the URL, except on the one
documented refresh endpoint), httpx errors are re-raised without their URL, and API
errors expose only the error's message/type/code, scrubbed of the token.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping

import httpx

# httpx logs full request URLs (incl. query strings) at INFO; the refresh call carries the
# token in its query string, so keep that logger quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

DEFAULT_BASE_URL = "https://graph.instagram.com"
KV_TOKEN = "ig_access_token"
_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class InstagramError(Exception):
    """Instagram call failed. The message is sanitized and safe to log.

    `status` (HTTP status) and `code` (Graph `error.code`) are optional, numeric only.
    """

    def __init__(self, message: str = "", *, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class InstagramClient:
    def __init__(self, access_token: str, ig_user_id: str, api_version: str, *,
                 http: httpx.Client | None = None, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = 30.0):
        if not access_token:
            raise InstagramError("IG_ACCESS_TOKEN is not set")
        if not ig_user_id:
            raise InstagramError("IG_USER_ID is not set")
        if not api_version or not api_version.strip() or not _VERSION_RE.match(api_version.strip()):
            raise InstagramError("IG_API_VERSION is not set or invalid (e.g. v23.0)")
        self._token = access_token
        self.ig_user_id = ig_user_id
        self.api_version = api_version.strip()
        self.base_url = base_url.rstrip("/")
        self._http = http if http is not None else httpx.Client(timeout=timeout)

    @classmethod
    def from_env(cls, env: Mapping[str, str], engine: Any = None, *,
                 http: httpx.Client | None = None) -> "InstagramClient":
        from . import db  # local import: keeps this module importable without a database
        token = (env.get("IG_ACCESS_TOKEN") or "").strip()
        if engine is not None:
            stored = db.get_kv(engine, KV_TOKEN)
            if stored:
                token = stored
        return cls(token, (env.get("IG_USER_ID") or "").strip(),
                   (env.get("IG_API_VERSION") or "").strip(), http=http)

    def __repr__(self) -> str:
        return f"InstagramClient(ig_user_id={self.ig_user_id!r}, api_version={self.api_version!r})"

    __str__ = __repr__

    def set_token(self, token: str) -> None:
        self._token = token

    # -- requests -----------------------------------------------------------
    def get(self, path: str, **params: Any) -> dict:
        return self._request("GET", self._url(path), params=params)

    def post(self, path: str, **params: Any) -> dict:
        return self._request("POST", self._url(path), data=params)

    def get_unversioned(self, path: str, **params: Any) -> dict:
        """Unversioned endpoint (token refresh). Documented to take the token as a query
        parameter, so the httpx loggers are silenced above."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        return self._request("GET", url, params={**params, "access_token": self._token},
                             bearer=False)

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{self.api_version}/{path.lstrip('/')}"

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "[REDACTED]")

    def _request(self, method: str, url: str, *, params: dict | None = None,
                 data: dict | None = None, bearer: bool = True) -> dict:
        headers = {"Authorization": f"Bearer {self._token}"} if bearer else {}
        try:
            resp = self._http.request(method, url, params=params, data=data, headers=headers,
                                      follow_redirects=False)
        except httpx.HTTPError as exc:
            # Deliberately no URL, query string or chained exception (both may hold the token).
            raise InstagramError(f"Instagram request failed ({type(exc).__name__})") from None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code >= 400 or (isinstance(body, dict) and "error" in body):
            raise InstagramError(self._describe_error(resp.status_code, body),
                                 status=resp.status_code, code=self._error_code(body)) from None
        if not isinstance(body, dict):
            raise InstagramError(f"Instagram returned an unexpected response (HTTP {resp.status_code})",
                                 status=resp.status_code)
        return body

    @staticmethod
    def _error_code(body: Any) -> int | None:
        err = body.get("error") if isinstance(body, dict) else None
        code = err.get("code") if isinstance(err, dict) else None
        return code if isinstance(code, int) and not isinstance(code, bool) else None

    def _describe_error(self, status: int, body: Any) -> str:
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            parts = [f"{k}={self._scrub(str(err[k]))}" for k in ("message", "type", "code")
                     if err.get(k) is not None]
            return f"Instagram API error (HTTP {status}): " + "; ".join(parts)
        return f"Instagram API error (HTTP {status})"
