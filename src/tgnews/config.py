from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

VALID_MODES = ("review", "auto")
DEFAULT_DAILY_CAP = 5


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


@dataclass(frozen=True)
class Settings:
    mode: str = "review"
    daily_cap: int = DEFAULT_DAILY_CAP
    database_url: str = "sqlite:///tgnews.db"
    score_weights: dict[str, float] = field(default_factory=_default_weights)
    explore_rate: float = 0.2
    min_scored_posts: int = 30

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        mode = env.get("MODE", "review").strip().lower() or "review"
        if mode not in VALID_MODES:
            raise ValueError(f"MODE must be one of {VALID_MODES}, got {mode!r}")
        return cls(
            mode=mode,
            daily_cap=_parse_cap(env.get("DAILY_CAP")),
            database_url=env.get("DATABASE_URL", "sqlite:///tgnews.db"),
        )
