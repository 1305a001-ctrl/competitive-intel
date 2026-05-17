"""Alert formatting + delivery.

Alerts ride pa-agent's `pa:notifications` Redis stream — pa-agent is
already wired to drain it and forward to Telegram. We never call the
Telegram bot API directly; that keeps creds in one place.

Pure helpers
------------
- `should_alert(classification, matched_builds, *, min_confidence, alert_types)`
- `format_alert(signal, classification, matched_builds, build_lookup)`

Async I/O
---------
- `emit_alert(redis_client, payload)` — XADD onto `pa:notifications`.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from competitive_intel.settings import settings
from competitive_intel.watchlist import Build

log = logging.getLogger(__name__)


# ─── gating (pure) ─────────────────────────────────────────────────


def should_alert(
    classification: dict[str, Any],
    matched_builds: list[str],
    *,
    min_confidence: float | None = None,
    alert_types: tuple[str, ...] | None = None,
) -> bool:
    """Pure: do all three gating rules pass?

    1. classification.type in alert_types (default DISPLACE/SUBSTITUTE)
    2. classification.confidence >= min_confidence (default 0.7)
    3. matched_builds is non-empty
    """
    if not matched_builds:
        return False
    min_conf = (
        min_confidence if min_confidence is not None else settings.alert_min_confidence
    )
    types = alert_types or settings.alert_types
    sig_type = str(classification.get("type", "")).upper()
    if sig_type not in types:
        return False
    try:
        conf = float(classification.get("confidence", 0))
    except (TypeError, ValueError):
        return False
    return conf >= min_conf


# ─── formatting (pure) ─────────────────────────────────────────────


def format_alert(
    signal: dict[str, Any],
    classification: dict[str, Any],
    matched_builds: list[str],
    build_lookup: dict[str, Build] | None = None,
) -> str:
    """Pure: produce the Telegram-ready Markdown text. No I/O.

    We use Telegram MarkdownV1 escaping (single `*` bold) — pa-agent's
    notification consumer renders this with parse_mode=Markdown.
    """
    sig_type = str(classification.get("type", "REGIME")).upper()
    try:
        conf = float(classification.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    rationale = (
        (classification.get("stage2") or {}).get("rationale")
        or classification.get("rationale")
        or ""
    )

    if build_lookup:
        affected = ", ".join(
            f"{bid} {build_lookup[bid].name}" if bid in build_lookup else bid
            for bid in matched_builds
        )
    else:
        affected = ", ".join(matched_builds)

    lines = [
        "🚨 *Competitive Intel Alert*",
        f"Type: {sig_type} ({int(round(conf * 100))}% confidence)",
        f"Source: {signal.get('source', 'unknown')}",
        f"Affected builds: {affected or '(none)'}",
        f"Title: {signal.get('title', '')[:200]}",
        f"URL: {signal.get('url', '')}",
    ]
    if rationale:
        lines.append(f"Rationale: {rationale}")
    lines.append("Decision needed within 7 days per framework §2.3")
    return "\n".join(lines)


def build_alert_payload(
    signal: dict[str, Any],
    classification: dict[str, Any],
    matched_builds: list[str],
    build_lookup: dict[str, Build] | None = None,
) -> dict[str, Any]:
    """Pure: shape the XADD payload. pa-agent expects {text, kind, meta}.

    `meta` carries the structured signal so any downstream consumer
    (dashboard, weekly review) can ingest it without re-parsing text.
    """
    return {
        "text": format_alert(signal, classification, matched_builds, build_lookup),
        "kind": "competitive_intel",
        "meta": {
            "url": signal.get("url"),
            "source": signal.get("source"),
            "type": classification.get("type"),
            "confidence": classification.get("confidence"),
            "matched_builds": matched_builds,
        },
    }


# ─── async I/O ─────────────────────────────────────────────────────


async def emit_alert(redis_client: Any, payload: dict[str, Any]) -> str | None:
    """XADD payload onto `pa:notifications`. Fail-OPEN — return None on
    error. Caller decides whether to retry on next scan.
    """
    try:
        return await redis_client.xadd(
            settings.pa_notifications_stream,
            {"data": json.dumps(payload, ensure_ascii=False)},
            maxlen=10_000,
            approximate=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("alerter.xadd_failed err=%s", exc)
        return None
