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


class BlocklistError(ValueError):
    """The blocklist file is missing, unreadable or malformed. The filter fails
    closed: it raises rather than silently allowing everything."""


def _compile(keyword: str) -> Pattern[str]:
    kw = keyword.strip()
    prefix = kw.endswith("*")
    if prefix:
        kw = kw[:-1].rstrip()
    esc = re.escape(_norm(kw))
    # Telugu is agglutinative and has no word-boundary cues, so non-ASCII
    # keywords are plain substrings (list stems, e.g. without a final vowel sign).
    if not kw.isascii():
        return re.compile(esc)
    # ASCII keywords match whole words unless they end in "*" (prefix match).
    return re.compile(r"(?<!\w)" + esc + ("" if prefix else r"(?!\w)"))


def _parse(path: Path) -> tuple[_Group, ...]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BlocklistError(f"cannot read blocklist {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise BlocklistError(f"blocklist {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise BlocklistError(f"blocklist {path}: root must be a mapping with a 'groups' key")
    groups_spec = data.get("groups")
    if not isinstance(groups_spec, dict) or not groups_spec:
        raise BlocklistError(f"blocklist {path}: 'groups' must be a non-empty mapping")
    groups = []
    for name, spec in groups_spec.items():
        where = f"blocklist {path}, group {name!r}"
        if not isinstance(spec, dict):
            raise BlocklistError(f"{where}: must be a mapping")
        verdict_raw = spec.get("verdict")
        if verdict_raw not in ("block", "hold"):
            raise BlocklistError(f"{where}: verdict must be 'block' or 'hold', got {verdict_raw!r}")
        keywords = spec.get("keywords")
        if not isinstance(keywords, list) or not keywords:
            raise BlocklistError(f"{where}: keywords must be a non-empty list")
        patterns = []
        for k in keywords:
            if not isinstance(k, str) or not k.strip().rstrip("*").strip():
                raise BlocklistError(f"{where}: keywords must be non-empty strings, got {k!r}")
            patterns.append(_compile(k))
        groups.append(_Group(str(name), Verdict(verdict_raw), tuple(patterns)))
    return tuple(groups)


@lru_cache(maxsize=8)
def _load_cached(path: str, mtime_ns: int) -> tuple[_Group, ...]:
    return _parse(Path(path))


def _load(path: str) -> tuple[_Group, ...]:
    try:
        mtime = Path(path).stat().st_mtime_ns
    except OSError as exc:
        raise BlocklistError(f"cannot read blocklist {path}: {exc}") from exc
    return _load_cached(path, mtime)


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
