from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class PostState(str, Enum):
    draft = "draft"
    approved = "approved"
    publishing = "publishing"
    published = "published"
    failed = "failed"
    expired = "expired"
    rejected = "rejected"


@dataclass(frozen=True)
class Story:
    id: str
    title: str
    summary: str
    url: str
    source_name: str
    published_at: datetime
    topic_hint: str | None = None


@dataclass
class Post:
    id: int
    story_id: str
    format: str
    topic: str | None
    time_slot: str | None
    headline_style: str | None
    state: PostState
    media_id: str | None = None
    posted_at: datetime | None = None
    metrics: dict[str, Any] | None = None
    container_id: str | None = None
    caption: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
