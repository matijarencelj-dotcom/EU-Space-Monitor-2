"""Weighted-keyword detection engine.

Agenda and minutes items are scored against a weighted keyword list loaded
from a TOML file. An item is flagged when the sum of matched keyword
weights reaches the configured threshold.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Keyword:
    """A single weighted keyword, pre-compiled into a regex."""

    term: str
    weight: float
    lang: str
    pattern: re.Pattern

    def matches(self, text: str) -> re.Match | None:
        return self.pattern.search(text)


@dataclass(frozen=True)
class KeywordSet:
    """Loaded keyword configuration."""

    threshold: float
    excerpt_chars: int
    entries: tuple[Keyword, ...]

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class Match:
    """The result of scoring one text item."""

    score: float
    # (term, language, weight) tuples, in keywords.toml order.
    matched: tuple[tuple[str, str, float], ...]
    # Position of the earliest keyword hit, for excerpt building.
    first_pos: int


def _compile(term: str, regex: bool = False) -> re.Pattern:
    """Compile a keyword into a case-insensitive pattern.

    Plain terms use word-boundary lookarounds so that short tokens such as
    "ESA" or "vega" do not match inside longer words, while multi-word
    phrases still match as substrings.
    """
    if regex:
        return re.compile(term, re.IGNORECASE)
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)


def _parse_entry(term: str, value: object, lang: str) -> Keyword:
    """Accept either `term = 2.5` or `term = { weight = 2.5, regex = true }`."""
    if isinstance(value, dict):
        weight = float(value.get("weight", 1.0))
        regex = bool(value.get("regex", False))
    else:
        weight = float(value)
        regex = False
    return Keyword(term=term, weight=weight, lang=lang, pattern=_compile(term, regex))


def load_keywords(path: str | Path) -> KeywordSet:
    """Load and compile the keyword set from a TOML file."""
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    scoring = raw.get("scoring", {})
    threshold = float(scoring.get("threshold", 2.0))
    excerpt_chars = int(scoring.get("excerpt_chars", 260))

    entries: list[Keyword] = []
    for lang, table in raw.get("keywords", {}).items():
        for term, value in table.items():
            entries.append(_parse_entry(term, value, lang))
    return KeywordSet(threshold=threshold, excerpt_chars=excerpt_chars, entries=tuple(entries))


def score_text(text: str, keywords: KeywordSet) -> Match | None:
    """Score one item text. Returns a Match when the threshold is reached."""
    if not text:
        return None
    total = 0.0
    matched: list[tuple[str, str, float]] = []
    first_pos: int | None = None
    for kw in keywords.entries:
        m = kw.matches(text)
        if m:
            matched.append((kw.term, kw.lang, kw.weight))
            total += kw.weight
            if first_pos is None or m.start() < first_pos:
                first_pos = m.start()
    if not matched or total < keywords.threshold:
        return None
    return Match(score=round(total, 2), matched=tuple(matched), first_pos=first_pos if first_pos is not None else 0)


def excerpt_around(text: str, pos: int, chars: int) -> str:
    """Return a whitespace-normalised excerpt centred on the first hit."""
    if not text:
        return ""
    start = max(0, pos - chars // 4)
    end = min(len(text), pos + chars)
    snippet = " ".join(text[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + snippet + suffix
