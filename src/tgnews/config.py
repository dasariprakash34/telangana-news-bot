from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

VALID_MODES = ("review", "auto")
DEFAULT_DAILY_CAP = 5
DEFAULT_TIME_SLOTS = ("02:00", "07:00", "13:00", "15:30")  # UTC HH:MM
DEFAULT_ACTIVE_SLOT_COUNT = 3


def _default_weights() -> dict[str, float]:
    return {"shares": 1.0, "saves": 1.0, "likes": 0.1, "comments": 0.2}


def _parse_cap(raw: str | None) -> int:
    if raw is None or not raw.strip():
        return DEFAULT_DAILY_CAP
    try:
        cap = int(raw.strip())
    except ValueError:
        raise ValueError(f"DAILY_CAP must be a positive integer, got {raw!r}") from None
    if cap <= 0:
        raise ValueError(f"DAILY_CAP must be a positive integer, got {raw!r}")
    return cap


def _parse_slots(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return DEFAULT_TIME_SLOTS
    from .learn import validate_options  # one slot grammar, owned by the learning loop
    slots = tuple(s.strip() for s in raw.split(",") if s.strip())
    try:
        return tuple(validate_options("time_slot", slots))
    except ValueError:
        raise ValueError(f"TIME_SLOTS must be comma-separated UTC HH:MM, got {raw!r}") from None


def _parse_active_count(raw: str | None) -> int:
    if raw is None or not raw.strip():
        return DEFAULT_ACTIVE_SLOT_COUNT
    try:
        n = int(raw.strip())
    except ValueError:
        raise ValueError(f"ACTIVE_SLOT_COUNT must be a positive integer, got {raw!r}") from None
    if n <= 0:
        raise ValueError(f"ACTIVE_SLOT_COUNT must be a positive integer, got {raw!r}")
    return n


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

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        mode = env.get("MODE", "review").strip().lower() or "review"
        if mode not in VALID_MODES:
            raise ValueError(f"MODE must be one of {VALID_MODES}, got {mode!r}")
        return cls(
            mode=mode,
            daily_cap=_parse_cap(env.get("DAILY_CAP")),
            database_url=env.get("DATABASE_URL", "sqlite:///tgnews.db"),
            brand_name=(env.get("BRAND_NAME") or "").strip() or "Telangana News",
            time_slots=_parse_slots(env.get("TIME_SLOTS")),
            active_slot_count=_parse_active_count(env.get("ACTIVE_SLOT_COUNT")),
        )
