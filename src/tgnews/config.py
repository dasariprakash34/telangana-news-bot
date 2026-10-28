from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

VALID_MODES = ("review", "auto")
DEFAULT_DAILY_CAP = 5
DEFAULT_TIME_SLOTS = ("02:00", "07:00", "13:00", "15:30")  # UTC HH:MM
DEFAULT_ACTIVE_SLOT_COUNT = 3


class ConfigError(ValueError):
    """Invalid configuration. Messages name the setting only and NEVER contain env values."""


def _default_weights() -> dict[str, float]:
    return {"shares": 1.0, "saves": 1.0, "likes": 0.1, "comments": 0.2}


def _parse_cap(raw: str | None) -> int:
    if raw is None or not raw.strip():
        return DEFAULT_DAILY_CAP
    try:
        cap = int(raw.strip())
    except ValueError:
        raise ConfigError("DAILY_CAP must be a positive integer") from None
    if cap <= 0:
        raise ConfigError("DAILY_CAP must be a positive integer")
    return cap


def _parse_slots(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return DEFAULT_TIME_SLOTS
    from .learn import validate_options  # one slot grammar, owned by the learning loop
    slots = tuple(s.strip() for s in raw.split(",") if s.strip())
    try:
        return tuple(validate_options("time_slot", slots))
    except ValueError:
        raise ConfigError("TIME_SLOTS must be comma-separated UTC HH:MM") from None


def _parse_active_count(raw: str | None) -> int:
    if raw is None or not raw.strip():
        return DEFAULT_ACTIVE_SLOT_COUNT
    try:
        n = int(raw.strip())
    except ValueError:
        raise ConfigError("ACTIVE_SLOT_COUNT must be a positive integer") from None
    if n <= 0:
        raise ConfigError("ACTIVE_SLOT_COUNT must be a positive integer")
    return n


def _parse_per_run_cap(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None  # derived: ceil(daily_cap / active_slot_count)
    try:
        n = int(raw.strip())
    except ValueError:
        raise ConfigError("PER_RUN_CAP must be a positive integer") from None
    if n <= 0:
        raise ConfigError("PER_RUN_CAP must be a positive integer")
    return n


def _parse_provider(raw: str | None) -> str:
    value = (raw or "").strip().lower() or "gemini"
    if value not in ("gemini", "anthropic"):
        raise ConfigError("LLM_PROVIDER must be one of ('gemini', 'anthropic')")
    return value


def normalize_database_url(url: str) -> str:
    """Hosting dashboards hand out `postgres://` or `postgresql://`; SQLAlchemy would then pick the
    psycopg2 driver, which is not installed. Map both to psycopg 3. Other schemes are untouched."""
    url = url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@dataclass(frozen=True)
class Settings:
    mode: str = "review"
    daily_cap: int = DEFAULT_DAILY_CAP
    database_url: str = "sqlite:///tgnews.db"
    score_weights: dict[str, float] = field(default_factory=_default_weights)
    explore_rate: float = 0.2
    min_scored_posts: int = 30
    brand_name: str = "Telangana News"
    time_slots: tuple[str, ...] = DEFAULT_TIME_SLOTS
    active_slot_count: int = DEFAULT_ACTIVE_SLOT_COUNT
    per_run_cap: int | None = None
    llm_provider: str = "gemini"
    llm_model: str = ""  # empty: build_llm picks the provider default (gemini has none; required)

    @property
    def effective_per_run_cap(self) -> int:
        """Most posts/drafts one run may create: PER_RUN_CAP, else ceil(daily_cap / active slots)."""
        if self.per_run_cap is not None:
            return self.per_run_cap
        return max(1, math.ceil(self.daily_cap / max(1, self.active_slot_count)))

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        mode = env.get("MODE", "review").strip().lower() or "review"
        if mode not in VALID_MODES:
            raise ConfigError(f"MODE must be one of {VALID_MODES}")
        return cls(
            mode=mode,
            daily_cap=_parse_cap(env.get("DAILY_CAP")),
            database_url=normalize_database_url(env.get("DATABASE_URL") or "sqlite:///tgnews.db"),
            brand_name=(env.get("BRAND_NAME") or "").strip() or "Telangana News",
            time_slots=_parse_slots(env.get("TIME_SLOTS")),
            active_slot_count=_parse_active_count(env.get("ACTIVE_SLOT_COUNT")),
            per_run_cap=_parse_per_run_cap(env.get("PER_RUN_CAP")),
            llm_provider=_parse_provider(env.get("LLM_PROVIDER")),
            llm_model=(env.get("LLM_MODEL") or "").strip(),
        )
