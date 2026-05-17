"""Append-only JSONL signal log.

Every signal the scanner sees gets one row. Format keys (stable):

    {
      "ts":          ISO-8601 of when we logged it,
      "url":         signal url,
      "source":      fetcher name,
      "title":       headline,
      "type":        DISPLACE | UNLOCK | SUBSTITUTE | REGIME,
      "confidence":  0..1,
      "rationale":   short clause,
      "matched_builds": ["T1.02", ...],
      "alerted":     bool,
      "stage1":      {...generic classifier output...},
      "affected_categories": [...]
    }

The log is the source of truth for the weekly framework review (§1). It
is append-only — never rewrite, never truncate. Operators should rotate
files externally if size becomes an issue.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _ensure_parent(path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def build_log_row(
    *,
    signal: dict[str, Any],
    classification: dict[str, Any],
    matched_builds: list[str],
    alerted: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pure: assemble one log row. Easy to unit-test.

    `signal` is a RawSignal.as_dict() shape.
    `classification` is the dict returned by classifier.classify_signal.
    """
    ts = (now or datetime.now(UTC)).isoformat()
    stage2 = classification.get("stage2") or {}
    return {
        "ts": ts,
        "url": signal.get("url", ""),
        "source": signal.get("source", ""),
        "title": signal.get("title", ""),
        "published_at": signal.get("published_at", ""),
        "type": stage2.get("type") or classification.get("type") or "REGIME",
        "confidence": float(
            stage2.get("confidence") or classification.get("confidence") or 0.0
        ),
        "rationale": stage2.get("rationale", ""),
        "affected_categories": list(stage2.get("affected_categories") or []),
        "matched_builds": list(matched_builds),
        "alerted": bool(alerted),
        "stage1": classification.get("stage1") or {},
    }


def append_row(path: str | Path, row: dict[str, Any]) -> None:
    """Append one JSON object as a single line. Uses O_APPEND for atomic
    writes (each line is at most a few KB so the kernel won't tear).
    """
    p = _ensure_parent(path)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True)
    # O_APPEND guarantees that concurrent writers don't overlap each
    # other's lines (POSIX: writes ≤ PIPE_BUF are atomic).
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def read_all(path: str | Path) -> list[dict[str, Any]]:
    """Read every row from disk. For tests + weekly-review tooling.

    Skips malformed lines (logging a warning) rather than failing.
    """
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                log.warning("signal_log.bad_line line=%d err=%s", i, exc)
    return out
