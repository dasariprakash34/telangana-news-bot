"""Bandit learning loop. Tunes only topic, format, time slot and headline style.

Deliberately imports nothing from safety/config: it cannot alter safety rules, source-credit
rules or the mode setting.
"""
from __future__ import annotations

import random
import re
from collections import defaultdict

from sqlalchemy.engine import Engine

from . import db
from .rewrite import STYLES, TOPICS

ATTRIBUTES = ("topic", "format", "time_slot", "headline_style")
FORMATS = ("card", "carousel")
SCORED_THRESHOLD = 30
MIN_N_PER_OPTION = 3
EXPLOIT_PROBABILITY = 0.8
_SLOT_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")

_FIXED = {"topic": TOPICS, "format": FORMATS, "headline_style": STYLES}


def validate_options(attribute: str, options) -> list[str]:
    if attribute not in ATTRIBUTES:
        raise ValueError(f"unknown attribute {attribute!r}; allowed: {ATTRIBUTES}")
    opts = list(options)
    if not opts:
        raise ValueError("options must not be empty")
    for o in opts:
        ok = (isinstance(o, str) and _SLOT_RE.fullmatch(o)) if attribute == "time_slot" \
            else o in _FIXED[attribute]
        if not ok:
            raise ValueError(f"option {o!r} is not allowed for {attribute}")
    return opts


def update_learning(engine: Engine) -> None:
    """Recompute mean score and n per (dimension, key) from scored posts; replace atomically."""
    sums: dict[tuple[str, str], list[float]] = defaultdict(list)
    for p in db.scored_posts(engine):
        for dim in ATTRIBUTES:
            if p[dim] is not None:
                sums[(dim, p[dim])].append(p["score"])
    db.replace_learning(engine, [(d, k, sum(v) / len(v), len(v))
                                 for (d, k), v in sorted(sums.items())])


def choose(engine: Engine, attribute: str, options, rng: random.Random) -> str:
    opts = validate_options(attribute, options)
    if len(db.scored_posts(engine)) < SCORED_THRESHOLD:
        return rng.choice(opts)
    stats = db.get_learning(engine, attribute)
    best, best_score = None, None
    for o in opts:  # option order breaks ties
        score, n = stats.get(o, (0.0, 0))
        if n >= MIN_N_PER_OPTION and (best_score is None or score > best_score):
            best, best_score = o, score
    if best is not None and rng.random() < EXPLOIT_PROBABILITY:
        return best
    return rng.choice(opts)
