from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Pattern

import yaml

from tgnews.models import Story

BLOCKLIST_PATH = Path(__file__).with_name("blocklist.yaml")
_MODES = ("review", "auto")


class Verdict(Enum):
    ALLOW = "allow"
    HOLD = "hold"
    BLOCK = "block"


@dataclass(frozen=True)
class _Group:
    name: str
    verdict: Verdict
    patterns: tuple[Pattern[str], ...]


def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _compile(keyword: str) -> Pattern[str]:
    kw = re.escape(_norm(keyword.strip()))
    # ASCII keywords match from a word start (so "arrest" catches "arrested"
    # but "rape" does not match "grape"). Telugu is agglutinative and written
    # without word-boundary cues, so non-ASCII keywords match as substrings.
    if keyword.isascii():
        return re.compile(r"(?<!\w)" + kw)
    return re.compile(kw)


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[_Group, ...]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    groups = []
    for name, spec in (data.get("groups") or {}).items():
        try:
            verdict = Verdict(spec["verdict"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"blocklist group {name!r}: verdict must be block or hold") from exc
        if verdict is Verdict.ALLOW:
            raise ValueError(f"blocklist group {name!r}: verdict must be block or hold")
        keywords = [k for k in (spec.get("keywords") or []) if str(k).strip()]
        groups.append(_Group(name, verdict, tuple(_compile(str(k)) for k in keywords)))
    return tuple(groups)


def check(story: Story, mode: str, blocklist_path: Path | str = BLOCKLIST_PATH) -> Verdict:
    """Classify a story. BLOCK is never posted. HOLD is flagged but postable:
    ALLOW in review mode (a human reviews every draft), HOLD in auto mode."""
    if mode not in _MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {_MODES}")
    text = _norm(f"{story.title}\n{story.summary}")
    flagged = False
    for group in _load(str(blocklist_path)):
        if any(p.search(text) for p in group.patterns):
            if group.verdict is Verdict.BLOCK:
                return Verdict.BLOCK
            flagged = True
    if flagged and mode == "auto":
        return Verdict.HOLD
    return Verdict.ALLOW
