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
from datetime import UTC, datetime
from typing import Any

from competitive_intel.lane_impact import LANES, LaneImpact
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


# ─── structural-event lane-impact alerts (slice a) ──────────────────
# These fire *independently of the LLM gate*: a deterministic lane-impact
# verdict (lane_impact.score_lane_impact) decides. The original failure was
# that the LLM gate (confidence>=0.7) never tripped on the Atlas signal because
# the LLM is fail-OPEN (confidence 0.0 on error). This layer cannot be silenced
# by an LLM outage.


def format_structural_alert(
    signal: dict[str, Any], impact: LaneImpact,
) -> str:
    """Pure: Telegram-ready Markdown for a structural lane-impact alert."""
    lane_labels = ", ".join(
        LANES[lane]["label"] if lane in LANES else lane for lane in impact.lanes
    )
    lines = [
        "🛰️ *Lane-Viability Radar*",
        f"Event: {'/'.join(impact.event_kinds) or 'structural'}"
        f" (severity {impact.severity:.2f})",
        f"Lanes at risk: {lane_labels or '(none)'}",
        f"Affected builds: {', '.join(impact.builds) or '(none)'}",
        f"Source: {signal.get('source', 'unknown')}",
        f"Title: {str(signal.get('title', ''))[:200]}",
        f"URL: {signal.get('url', '')}",
    ]
    if impact.rationale:
        lines.append(f"Why: {impact.rationale}")
    lines.append("Structural — assess lane viability (not a trade signal).")
    return "\n".join(lines)


def build_structural_alert(
    signal: dict[str, Any],
    impact: LaneImpact,
    classification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure: shape the structured structural-alert record.

    Carries both human text and the structured impact so a downstream
    consumer (dashboard, hypothesis-ledger in slice b) can ingest it without
    re-parsing. ``classification`` (the LLM output, if any) is attached for
    cross-reference but is NOT required for the alert to fire.
    """
    return {
        "ts": datetime.now(UTC).isoformat(),
        "kind": "lane_structural_alert",
        "text": format_structural_alert(signal, impact),
        "url": signal.get("url"),
        "source": signal.get("source"),
        "title": signal.get("title"),
        "published_at": signal.get("published_at"),
        "impact": impact.as_dict(),
        # LLM view, for cross-reference only (may be low-confidence / absent).
        "llm_type": (classification or {}).get("type"),
        "llm_confidence": (classification or {}).get("confidence"),
    }


def structural_stream_key(lane: str) -> str:
    """Per-lane stream key, e.g. ``news:structural_alert:aave_defi``."""
    return f"{settings.structural_alert_stream_prefix}:{lane}"


async def persist_structural_alert(
    redis_client: Any, record: dict[str, Any],
) -> list[str]:
    """Persist a structural alert to Redis (always — this is the radar log).

    Writes:
      - one XADD per affected lane stream  (``news:structural_alert:<lane>``)
      - the latest-state string            (``news:structural_alert:latest``)

    Fail-OPEN: Redis errors are logged, never raised. Returns the list of
    stream entry IDs written (empty on total failure).
    """
    lanes = record.get("impact", {}).get("lanes") or []
    data = json.dumps(record, ensure_ascii=False)
    entry_ids: list[str] = []
    try:
        for lane in lanes:
            eid = await redis_client.xadd(
                structural_stream_key(str(lane)),
                {"data": data},
                maxlen=settings.structural_alert_stream_maxlen,
                approximate=True,
            )
            if eid is not None:
                entry_ids.append(eid)
        await redis_client.set(settings.structural_alert_latest_key, data)
    except Exception as exc:  # noqa: BLE001
        log.error("alerter.structural_persist_failed err=%s", exc)
    return entry_ids


async def dispatch_structural_alert(
    redis_client: Any, record: dict[str, Any],
) -> dict[str, Any]:
    """Persist a structural alert, then conditionally notify outbound.

    Outbound (pa-agent → Telegram) is GATED behind
    ``settings.structural_alert_outbound_enabled`` (default OFF). When the
    gate is off we still persist + log so the radar is fully observable in
    Redis and the signal log — merging this PR cannot spam the operator.

    Returns a small status dict (for logging / tests):
      ``{persisted: [ids], outbound_sent: bool, outbound_id: str|None}``
    """
    persisted = await persist_structural_alert(redis_client, record)
    outbound_sent = False
    outbound_id: str | None = None
    if settings.structural_alert_outbound_enabled:
        payload = {
            "text": record.get("text", ""),
            "kind": "lane_structural_alert",
            "meta": {
                "url": record.get("url"),
                "source": record.get("source"),
                "impact": record.get("impact"),
            },
        }
        outbound_id = await emit_alert(redis_client, payload)
        outbound_sent = outbound_id is not None
    else:
        log.info(
            "structural_alert.dry_run lanes=%s sev=%.2f url=%s (outbound gated off)",
            record.get("impact", {}).get("lanes"),
            record.get("impact", {}).get("severity", 0.0),
            record.get("url"),
        )
    return {
        "persisted": persisted,
        "outbound_sent": outbound_sent,
        "outbound_id": outbound_id,
    }
