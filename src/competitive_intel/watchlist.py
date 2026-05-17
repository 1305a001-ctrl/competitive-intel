"""Watchlist loader + pure match function.

A watchlist file (YAML or JSON) maps build IDs (e.g. "T1.02") to a
record listing upstream dependencies, substitute threats, fuzzy
keywords, and any existential triggers. See data/watchlist.yaml.

Match semantics
---------------
Given a `signal` ({title, body, source, affected_categories}) and a
loaded watchlist, return the set of build IDs whose entry overlaps the
signal text.

Matching is intentionally simple — substring (case-insensitive) against
the signal's title+body+affected_categories. False positives are fine;
the alert gate filters them via type+confidence.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Build:
    id: str
    name: str
    track: int
    status: str
    upstream: tuple[str, ...] = field(default_factory=tuple)
    substitutes: tuple[str, ...] = field(default_factory=tuple)
    keywords: tuple[str, ...] = field(default_factory=tuple)
    existential: tuple[str, ...] = field(default_factory=tuple)

    def all_terms(self) -> tuple[str, ...]:
        """All matchable terms for this build, lowercased.

        Returns a deduped tuple so repeated terms (e.g. "polymarket"
        appearing both in upstream and keywords) only get one hit.
        """
        seen: set[str] = set()
        out: list[str] = []
        for term in (
            *self.upstream, *self.substitutes, *self.keywords, *self.existential,
        ):
            t = (term or "").strip().lower()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return tuple(out)


# ─── loaders ────────────────────────────────────────────────────────


def parse_watchlist(payload: dict[str, Any]) -> dict[str, Build]:
    """Pure: dict (from yaml.safe_load or json.loads) → {id: Build}.

    Tolerant of missing optional fields. Unknown keys are ignored.
    A build with no name or track is skipped with a warning.
    """
    builds: dict[str, Build] = {}
    raw = payload.get("builds") or {}
    if not isinstance(raw, dict):
        log.warning("watchlist.builds_not_dict type=%s", type(raw).__name__)
        return builds
    for bid, entry in raw.items():
        if not isinstance(entry, dict):
            log.warning("watchlist.entry_not_dict id=%s", bid)
            continue
        try:
            track_val = int(entry.get("track", 0))
        except (TypeError, ValueError):
            track_val = 0
        builds[bid] = Build(
            id=str(bid),
            name=str(entry.get("name", bid)),
            track=track_val,
            status=str(entry.get("status", "active")),
            upstream=tuple(str(x) for x in (entry.get("upstream") or [])),
            substitutes=tuple(str(x) for x in (entry.get("substitutes") or [])),
            keywords=tuple(str(x) for x in (entry.get("keywords") or [])),
            existential=tuple(str(x) for x in (entry.get("existential") or [])),
        )
    return builds


def load_watchlist(path: str | Path) -> dict[str, Build]:
    """Load a YAML or JSON watchlist file from disk.

    File extension picks the parser. A missing file raises FileNotFound
    (we want startup to fail loudly if the watchlist is misconfigured;
    the scanner cannot work without it).
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"watchlist root must be a mapping; got {type(data).__name__}")
    return parse_watchlist(data)


# ─── match (pure) ───────────────────────────────────────────────────


def _haystack(signal: dict[str, Any]) -> str:
    """Pure: build the lowercase text blob we match against.

    Includes title, body, source name, and any affected_categories from
    the stage-2 classifier output.
    """
    parts: list[str] = []
    for k in ("title", "body", "source", "url"):
        v = signal.get(k)
        if v:
            parts.append(str(v))
    cats = signal.get("affected_categories")
    if isinstance(cats, list):
        parts.extend(str(c) for c in cats)
    elif isinstance(cats, str):
        parts.append(cats)
    return "\n".join(parts).lower()


def match_signal(
    signal: dict[str, Any], watchlist: dict[str, Build],
) -> list[str]:
    """Pure: return build IDs whose terms appear in the signal text.

    Sort order: insertion order of the watchlist (stable).
    """
    hay = _haystack(signal)
    matched: list[str] = []
    for bid, build in watchlist.items():
        for term in build.all_terms():
            if term and term in hay:
                matched.append(bid)
                break
    return matched
