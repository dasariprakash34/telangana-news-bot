"""Free image hosting: commit JPEGs into a separate PUBLIC GitHub repo via the Contents API.

Instagram needs a public https image URL. The images repo must be public; the default URL is
https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}. Set IMAGES_PUBLIC_BASE to use a CDN style
base instead (e.g. https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}). Whether Instagram's fetcher accepts
these URLs is UNVERIFIED; test with `tgnews check-images URL` and a throwaway account.

Errors never contain the token or GitHub's response bodies.
"""
from __future__ import annotations

import base64
import logging
import re
import secrets
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .storage import StorageError

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

API = "https://api.github.com"
API_VERSION = "2022-11-28"
POSTS_DIR = "posts"
MAX_BYTES = 5 * 1024 * 1024
DEFAULT_KEEP_DAYS = 14
DEFAULT_PRUNE_LIMIT = 60
_JPEG_SUFFIXES = {".jpg", ".jpeg"}
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")
_DIR_RE = re.compile(r"\d{8}")


def _default_http() -> httpx.Client:
    return httpx.Client(timeout=30.0)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_prefix(prefix: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", prefix).strip("-")[:80].strip("-")
    if not cleaned:
        raise StorageError("prefix must not be empty")
    return cleaned


def _status_error(status: int, what: str) -> StorageError:
    if status == 401:
        return StorageError(f"GitHub rejected the token (HTTP 401): token invalid or expired while {what}")
    if status == 403:
        return StorageError(f"GitHub refused the request (HTTP 403): token has insufficient permission "
                            f"(needs Contents read/write on the images repo) or is rate limited, while {what}")
    if status == 429:
        return StorageError(f"GitHub rate limit reached (HTTP 429) while {what}")
    if status == 404:
        return StorageError(f"GitHub repository or branch not found (HTTP 404) while {what}; check "
                            "IMAGES_GH_REPO and IMAGES_GH_BRANCH and that the token can access that repo")
    if status >= 500:
        return StorageError(f"GitHub server error (HTTP {status}) while {what}")
    return StorageError(f"unexpected GitHub response (HTTP {status}) while {what}")


@dataclass(frozen=True)
class CheckResult:
    status: int
    content_type: str
    ok: bool


class GithubImageStore:
    def __init__(self, token: str, repo: str, branch: str = "main", public_base: str | None = None, *,
                 http: httpx.Client | None = None, sleep: Callable[[float], None] | None = None,
                 clock: Callable[[], datetime] = _utcnow, unique: Callable[[], str] | None = None,
                 pause: float = 1.0) -> None:
        if not (token or "").strip():
            raise StorageError("missing GitHub token")
        if not _REPO_RE.fullmatch(repo or "") or ".." in repo:
            raise StorageError("images repo must look like 'owner/name'")
        if not _BRANCH_RE.fullmatch(branch or "") or ".." in branch:
            raise StorageError("images branch name is invalid")
        if public_base is not None:
            public_base = public_base.strip()
            if not public_base.startswith("https://"):
                raise StorageError("IMAGES_PUBLIC_BASE must start with https:// (Instagram requires HTTPS)")
        self._token = token.strip()
        self.repo = repo
        self.branch = branch
        self.public_base = (public_base or
                            f"https://raw.githubusercontent.com/{repo}/{branch}").rstrip("/")
        self._http = http
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))
        self._clock = clock
        self._unique = unique or (lambda: secrets.token_hex(3))
        self._pause = pause

    def __repr__(self) -> str:
        return f"GithubImageStore(repo={self.repo!r}, branch={self.branch!r}, public_base={self.public_base!r})"

    __str__ = __repr__

    @classmethod
    def from_env(cls, env: Mapping[str, str], **kw) -> GithubImageStore:
        def need(name: str) -> str:
            value = (env.get(name) or "").strip()
            if not value:
                raise StorageError(f"missing required environment variable {name}")
            return value

        return cls(need("IMAGES_GH_TOKEN"), need("IMAGES_GH_REPO"),
                   (env.get("IMAGES_GH_BRANCH") or "").strip() or "main",
                   (env.get("IMAGES_PUBLIC_BASE") or "").strip() or None,
                   http=kw.pop("http", None) or _default_http(), **kw)

    # -- HTTP -----------------------------------------------------------------------------
    def _request(self, method: str, path: str, what: str, *, json=None, params=None) -> httpx.Response:
        url = f"{API}/repos/{self.repo}/contents/{urllib.parse.quote(path)}"
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": API_VERSION}
        http = self._http or _default_http()
        try:
            return http.request(method, url, headers=headers, json=json, params=params)
        except httpx.HTTPError as exc:
            raise StorageError(f"network error while {what} ({type(exc).__name__})") from None

    # -- upload ---------------------------------------------------------------------------
    def upload(self, paths: list[Path], prefix: str) -> list[str]:
        """Commit JPEG files as posts/YYYYMMDD/<prefix>-<random>-<n>.jpg; return public URLs in order."""
        safe = _safe_prefix(prefix)
        items: list[Path] = []
        for raw in paths:
            path = Path(raw)
            if not path.is_file():
                raise StorageError(f"not an existing file: {path}")
            if path.suffix.lower() not in _JPEG_SUFFIXES:
                raise StorageError(f"only JPEG files are supported, got {path.name}")
            if path.stat().st_size > MAX_BYTES:
                raise StorageError(f"file too large for upload (max 5 MB): {path.name}")
            items.append(path)
        stem = f"{POSTS_DIR}/{self._clock():%Y%m%d}/{safe}-{self._unique()}"
        urls: list[str] = []
        for n, path in enumerate(items, start=1):
            if n > 1:
                self._sleep(self._pause)
            target = f"{stem}-{n}.jpg"
            body = {"message": f"add {target}", "branch": self.branch,
                    "content": base64.b64encode(path.read_bytes()).decode("ascii")}
            resp = self._request("PUT", target, f"uploading {target}", json=body)
            if resp.status_code in (409, 422):
                raise StorageError(f"upload refused for {target} (HTTP {resp.status_code}): the file already "
                                   "exists or the branch changed; not overwriting")
            if resp.status_code != 201:
                if resp.status_code == 200:
                    raise StorageError(f"unexpected GitHub response (HTTP 200) for {target}: "
                                       "an existing file was updated, which must not happen")
                raise _status_error(resp.status_code, f"uploading {target}")
            urls.append(f"{self.public_base}/{urllib.parse.quote(target)}")
        return urls

    # -- housekeeping ---------------------------------------------------------------------
    def prune(self, older_than_days: int, now: datetime, *, max_files: int = DEFAULT_PRUNE_LIMIT) -> int:
        """Delete files in posts/YYYYMMDD directories older than ``older_than_days``. Returns the count
        deleted (at most ``max_files``; the rest is handled by later runs). Touches nothing outside posts/."""
        if older_than_days < 1:
            raise StorageError("IMAGES_KEEP_DAYS must be at least 1")
        cutoff = (now - timedelta(days=older_than_days)).date()
        listing = self._request("GET", POSTS_DIR, "listing old images", params={"ref": self.branch})
        if listing.status_code == 404:
            return 0
        if listing.status_code != 200:
            raise _status_error(listing.status_code, "listing old images")
        old: list[str] = []
        for entry in listing.json():
            name = entry.get("name", "") if isinstance(entry, dict) else ""
            if not (isinstance(entry, dict) and entry.get("type") == "dir" and _DIR_RE.fullmatch(name)):
                continue
            try:
                day = datetime.strptime(name, "%Y%m%d").date()
            except ValueError:
                continue
            if day < cutoff:
                old.append(name)
        deleted = 0
        for name in sorted(old):
            sub = self._request("GET", f"{POSTS_DIR}/{name}", "listing old images", params={"ref": self.branch})
            if sub.status_code == 404:
                continue
            if sub.status_code != 200:
                raise _status_error(sub.status_code, "listing old images")
            for f in sub.json():
                if not (isinstance(f, dict) and f.get("type") == "file" and f.get("sha")
                        and str(f.get("path", "")).startswith(f"{POSTS_DIR}/{name}/")):
                    continue
                if deleted >= max_files:
                    return deleted
                if deleted:
                    self._sleep(self._pause)
                resp = self._request("DELETE", f["path"], f"deleting {f['path']}",
                                     json={"message": f"prune {f['path']}", "sha": f["sha"],
                                           "branch": self.branch})
                if resp.status_code == 200:
                    deleted += 1
                elif resp.status_code not in (404, 409, 422):
                    raise _status_error(resp.status_code, f"deleting {f['path']}")
        return deleted


def keep_days(env: Mapping[str, str]) -> int:
    raw = (env.get("IMAGES_KEEP_DAYS") or "").strip()
    if not raw:
        return DEFAULT_KEEP_DAYS
    if not raw.isdigit() or int(raw) < 1:
        raise StorageError("IMAGES_KEEP_DAYS must be a positive whole number")
    return int(raw)


def check_public_url(url: str, *, http: httpx.Client | None = None) -> CheckResult:
    """GET one public URL and report status and content type (uploads nothing)."""
    if not url.startswith("https://"):
        raise StorageError("URL must start with https://")
    try:
        resp = (http or _default_http()).get(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise StorageError(f"could not fetch the URL ({type(exc).__name__})") from None
    ctype = resp.headers.get("content-type", "")
    return CheckResult(resp.status_code, ctype, resp.status_code == 200 and ctype.lower().startswith("image/jpeg"))
