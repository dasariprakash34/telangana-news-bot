from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

VALID_MODES = ("review", "auto")


def _default_weights() -> dict[str, float]:
    return {"shares": 1.0, "saves": 1.0, "likes": 0.1, "comments": 0.2}


@dataclass(frozen=True)
class Settings:
    mode: str = "review"
    daily_cap: int = 3
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
            daily_cap=int(env.get("DAILY_CAP", "3")),
            database_url=env.get("DATABASE_URL", "sqlite:///tgnews.db"),
        )
