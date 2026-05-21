"""Main scanner loop.

`run_once()` executes one daily-skim pass:
  1. fetch all sources concurrently
  2. drop signals whose URL is already in the Redis seen-urls set
  3. classify each new signal via classifier.classify_signal
  4. match against the watchlist
  5. emit alert iff gating rules pass
  6. log every signal (alerted or not) to JSONL
  7. mark all processed URLs as seen

`run_loop()` calls `run_once` every `settings.skim_interval_sec`.

Idempotency: a duplicate URL on a later scan is filtered in step 2 and
will not re-alert.

Fail-OPEN: every source error is swallowed by the fetcher; classifier
LLM errors yield low-confidence stubs; Redis errors are logged but do
not halt the loop.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import redis.asyncio as redis_asyncio

from competitive_intel import alerter, classifier, lane_impact, signal_log, sources
from competitive_intel.settings import settings
from competitive_intel.watchlist import Build, load_watchlist, match_signal

log = logging.getLogger(__name__)


# ─── idempotency helpers ────────────────────────────────────────────


async def _filter_seen(
    r: Any, signals: list[sources.RawSignal],
) -> list[sources.RawSignal]:
    """Drop signals whose URL is already in the Redis seen-urls SET.

    On Redis error we fail-OPEN (return all signals) and log a warning.
    Worst case we re-alert; we'd rather have duplicates than miss.
    """
    if not signals:
        return []
    out: list[sources.RawSignal] = []
    try:
        pipe = r.pipeline()
        for s in signals:
            pipe.sismember(settings.seen_urls_key, s.url)
        results = await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        log.warning("scanner.sismember_failed err=%s", exc)
        return signals
    for s, seen in zip(signals, results, strict=True):
        if not seen:
            out.append(s)
    return out


async def _mark_seen(r: Any, signals: list[sources.RawSignal]) -> None:
    if not signals:
        return
    try:
        await r.sadd(settings.seen_urls_key, *(s.url for s in signals))
        # Approximate LRU trim: we don't strictly need this, but it keeps
        # the SET bounded over months of operation. We trim only when
        # the set exceeds the soft cap by 20%.
        size = await r.scard(settings.seen_urls_key)
        if size > settings.seen_urls_max * 1.2:
            # Random-evict ~ overflow size. SET doesn't preserve order
            # so this is best-effort.
            overflow = size - settings.seen_urls_max
            victims = await r.srandmember(settings.seen_urls_key, overflow)
            if victims:
                await r.srem(settings.seen_urls_key, *victims)
    except Exception as exc:  # noqa: BLE001
        log.warning("scanner.mark_seen_failed err=%s", exc)


# ─── one pass ──────────────────────────────────────────────────────


async def run_once(
    r: Any, watchlist: dict[str, Build],
) -> dict[str, int]:
    """Execute one daily-skim cycle. Returns counters (for logging /
    smoke tests).
    """
    fetched = await sources.fetch_all()
    fresh = await _filter_seen(r, fetched)
    log.info(
        "scanner.scanned fetched=%d fresh=%d", len(fetched), len(fresh),
    )

    alerted_count = 0
    structural_count = 0
    logged_count = 0
    for raw_signal in fresh:
        sig_dict = raw_signal.as_dict()
        # classify
        cls = await classifier.classify_signal(
            source=raw_signal.source,
            title=raw_signal.title,
            body=raw_signal.body,
        )
        # enrich the signal with the classifier's affected_categories
        # so the watchlist matcher has more to work with
        stage2 = cls.get("stage2") or {}
        enriched = {
            **sig_dict,
            "affected_categories": stage2.get("affected_categories") or [],
        }
        matched = match_signal(enriched, watchlist)
        alerted = False
        if alerter.should_alert(cls, matched):
            payload = alerter.build_alert_payload(
                sig_dict, cls, matched, build_lookup=watchlist,
            )
            entry_id = await alerter.emit_alert(r, payload)
            alerted = entry_id is not None
            if alerted:
                alerted_count += 1
                log.info(
                    "scanner.alert_emitted type=%s conf=%.2f builds=%s url=%s",
                    cls["type"], cls["confidence"], matched, raw_signal.url,
                )

        # ── lane-viability radar (independent of the LLM gate) ──
        # Deterministic structural scoring on the LLM-enriched signal. Fires
        # even when the LLM is down (confidence 0.0) — this is the fix for the
        # Atlas miss. Outbound notification is gated off by default; persist +
        # log always happen so the radar is observable.
        structural_alerted = False
        impact = lane_impact.score_lane_impact(
            enriched, critical_severity=settings.structural_alert_min_severity,
        )
        if impact.critical:
            record = alerter.build_structural_alert(sig_dict, impact, cls)
            try:
                status = await alerter.dispatch_structural_alert(r, record)
            except Exception as exc:  # noqa: BLE001
                log.error("scanner.structural_dispatch_failed err=%s", exc)
                status = {"persisted": [], "outbound_sent": False}
            structural_alerted = bool(status.get("persisted")) or bool(
                status.get("outbound_sent")
            )
            if structural_alerted:
                structural_count += 1
                log.info(
                    "scanner.structural_alert lanes=%s sev=%.2f outbound=%s url=%s",
                    impact.lanes, impact.severity,
                    status.get("outbound_sent"), raw_signal.url,
                )

        # always log, alerted or not
        row = signal_log.build_log_row(
            signal=sig_dict,
            classification=cls,
            matched_builds=matched,
            alerted=alerted,
        )
        # annotate the log row with the structural verdict (additive keys)
        row["structural_critical"] = impact.critical
        row["structural_alerted"] = structural_alerted
        row["lane_impact"] = impact.as_dict()
        try:
            signal_log.append_row(settings.signal_log_path, row)
            logged_count += 1
        except OSError as exc:
            log.error("scanner.log_append_failed err=%s", exc)

    await _mark_seen(r, fresh)
    return {
        "fetched": len(fetched),
        "fresh": len(fresh),
        "alerted": alerted_count,
        "structural_alerted": structural_count,
        "logged": logged_count,
    }


# ─── main loop ─────────────────────────────────────────────────────


async def run_loop() -> None:
    """Forever: load watchlist, open Redis, run_once, sleep, repeat.

    The watchlist is reloaded on every iteration so operators can edit
    the YAML in place without restarting the container.
    """
    while True:
        try:
            watchlist = load_watchlist(settings.watchlist_path)
        except (OSError, ValueError) as exc:
            log.error("scanner.watchlist_load_failed err=%s", exc)
            await asyncio.sleep(settings.skim_interval_sec)
            continue

        r = redis_asyncio.from_url(
            settings.redis_url, decode_responses=True,
        )
        try:
            counters = await run_once(r, watchlist)
            log.info("scanner.cycle_done %s", counters)
        except Exception as exc:  # noqa: BLE001
            log.exception("scanner.cycle_failed err=%s", exc)
        finally:
            try:
                await r.aclose()
            except Exception:  # noqa: BLE001
                pass

        await asyncio.sleep(settings.skim_interval_sec)
