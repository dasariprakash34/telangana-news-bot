"""Upload rendered JPEGs to an S3-compatible bucket and return public HTTPS URLs."""
from __future__ import annotations

import os
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

CACHE_CONTROL = "public, max-age=31536000, immutable"
_JPEG_SUFFIXES = {".jpg", ".jpeg"}


class StorageError(RuntimeError):
    """Storage configuration or upload failure. Messages never contain secrets."""


@dataclass(frozen=True)
class StorageConfig:
    endpoint_url: str
    bucket: str
    public_base: str
    access_key_id: str = ""
    secret_access_key: str = ""
    region: str = "auto"

    def __repr__(self) -> str:
        return (f"StorageConfig(endpoint_url={self.endpoint_url!r}, bucket={self.bucket!r}, "
                f"public_base={self.public_base!r}, region={self.region!r})")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> StorageConfig:
        def need(name: str) -> str:
            value = env.get(name, "")
            if not value.strip():
                raise StorageError(f"missing required environment variable {name}")
            return value.strip()

        public_base = need("STORAGE_PUBLIC_BASE")
        if not public_base.startswith("https://"):
            raise StorageError("STORAGE_PUBLIC_BASE must start with https:// (Instagram requires HTTPS)")
        return cls(
            endpoint_url=need("STORAGE_ENDPOINT"),
            bucket=need("STORAGE_BUCKET"),
            public_base=public_base,
            access_key_id=need("STORAGE_ACCESS_KEY_ID"),
            secret_access_key=need("STORAGE_SECRET_ACCESS_KEY"),
            region=(env.get("STORAGE_REGION") or "auto").strip() or "auto",
        )


def _build_client(config: StorageConfig):
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        region_name=config.region,
    )


def _clean_prefix(prefix: str) -> str:
    cleaned = prefix.strip("/")
    if not cleaned:
        raise StorageError("prefix must not be empty")
    if ".." in cleaned.split("/") or ".." in cleaned:
        raise StorageError("prefix must not contain '..'")
    return cleaned


def upload(paths: list[Path], prefix: str, *, client=None, config: StorageConfig | None = None) -> list[str]:
    """Upload JPEG files under ``prefix``; return public URLs in the order of ``paths``."""
    key_prefix = _clean_prefix(prefix)
    if config is None:
        config = StorageConfig.from_env(os.environ)
    items: list[tuple[Path, str]] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            raise StorageError(f"not an existing file: {path}")
        if path.suffix.lower() not in _JPEG_SUFFIXES:
            raise StorageError(f"only JPEG files are supported, got {path.name}")
        items.append((path, f"{key_prefix}/{path.name}"))
    if client is None:
        try:
            client = _build_client(config)
        except Exception as exc:
            raise StorageError("could not create storage client") from exc

    base = config.public_base.rstrip("/")
    urls: list[str] = []
    for path, key in items:
        try:
            client.put_object(
                Bucket=config.bucket,
                Key=key,
                Body=path.read_bytes(),
                ContentType="image/jpeg",
                CacheControl=CACHE_CONTROL,
            )
        except Exception as exc:
            raise StorageError(f"upload failed for {key}: {type(exc).__name__}") from exc
        urls.append(f"{base}/{urllib.parse.quote(key)}")
    return urls


def storage_provider(env: Mapping[str, str]) -> str:
    """The validated STORAGE_PROVIDER ("github" by default or "s3"); the one place that checks it."""
    provider = (env.get("STORAGE_PROVIDER") or "").strip().lower() or "github"
    if provider not in ("github", "s3"):
        raise StorageError("STORAGE_PROVIDER must be 'github' or 's3'")
    return provider


def build_uploader(env: Mapping[str, str]):
    """Return ``upload(paths, prefix) -> urls`` for the provider in STORAGE_PROVIDER (default github)."""
    provider = storage_provider(env)
    if provider == "github":
        from .storage_github import GithubImageStore

        return GithubImageStore.from_env(env).upload
    cfg = StorageConfig.from_env(env)
    return lambda paths, prefix: upload(paths, prefix, config=cfg)


def build_video_uploader(env: Mapping[str, str]):
    """Return ``upload_video(path, prefix) -> url`` for Reels. Only the github provider is supported."""
    if storage_provider(env) != "github":
        raise StorageError("reels need STORAGE_PROVIDER=github (video upload is not implemented for s3)")
    from .storage_github import GithubImageStore

    return GithubImageStore.from_env(env).upload_video
