"""RSS + HTTP fetchers.

Every fetcher is fail-OPEN: a network error or parse error returns an
empty list and logs a warning. The scanner aggregates whatever sources
respond.

A `RawSignal` is the minimal shape we hand to the classifier:
    {source, title, body, url, published_at}
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import feedparser
import httpx

from competitive_intel.settings import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawSignal:
    source: str
    title: str
    body: str
    url: str
    published_at: str  # ISO-8601 string (UTC if known)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "published_at": self.published_at,
        }


# ─── helpers (pure) ─────────────────────────────────────────────────


def _entry_published(entry: Any) -> str:
    """Extract a best-effort ISO-8601 publish timestamp from a feedparser
    entry. Falls back to 'now UTC' when absent.

    Pure helper — easy to unit-test.
    """
    for key in ("published_parsed", "updated_parsed"):
        val = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if val:
            try:
                # mypy can't prove the unpacked 6-tuple won't collide with the
                # tzinfo kwarg; it's valid at runtime (year..second + tzinfo).
                return datetime(*val[:6], tzinfo=UTC).isoformat()  # type: ignore[misc]
            except (TypeError, ValueError):
                pass
    return datetime.now(UTC).isoformat()


def parse_rss(source: str, raw_xml: str, max_items: int) -> list[RawSignal]:
    """Pure: bytes/string of RSS → list[RawSignal]. No I/O.

    Returns at most `max_items` entries. Skips entries without a usable
    title or link. Safe against malformed feeds (feedparser swallows
    most errors itself).
    """
    parsed = feedparser.parse(raw_xml)
    out: list[RawSignal] = []
    for entry in parsed.entries[:max_items]:
        title = (getattr(entry, "title", "") or "").strip()
        link = (getattr(entry, "link", "") or "").strip()
        if not title or not link:
            continue
        body = (
            getattr(entry, "summary", "")
            or getattr(entry, "description", "")
            or ""
        ).strip()
        out.append(
            RawSignal(
                source=source,
                title=title,
                body=body[:2000],
                url=link,
                published_at=_entry_published(entry),
            )
        )
    return out


def parse_defillama_protocols(
    payload: list[dict[str, Any]],
    tracked: tuple[str, ...] | list[str],
    delta_pct_threshold: float,
) -> list[RawSignal]:
    """Pure: DefiLlama /protocols response → list[RawSignal].

    Only protocols whose 24h TVL change magnitude exceeds the threshold
    are surfaced. The "title" encodes direction and percentage so the
    classifier has enough to label REGIME signals.
    """
    out: list[RawSignal] = []
    wanted = {p.lower() for p in tracked}
    for p in payload:
        slug = str(p.get("slug") or "").lower()
        if slug not in wanted:
            continue
        change = p.get("change_1d")
        if change is None:
            continue
        try:
            change_f = float(change)
        except (TypeError, ValueError):
            continue
        if abs(change_f) < delta_pct_threshold:
            continue
        name = p.get("name") or slug
        tvl = p.get("tvl")
        direction = "up" if change_f > 0 else "down"
        title = f"{name} TVL {direction} {change_f:+.1f}% 24h"
        body = f"protocol={slug} tvl_usd={tvl} change_1d_pct={change_f:.2f}"
        out.append(
            RawSignal(
                source="defillama",
                title=title,
                body=body,
                url=f"https://defillama.com/protocol/{slug}",
                published_at=datetime.now(UTC).isoformat(),
            )
        )
    return out


# ─── HTTP fetchers (fail-OPEN) ───────────────────────────────────────


async def _fetch_rss(
    client: httpx.AsyncClient, source: str, url: str, max_items: int,
) -> list[RawSignal]:
    try:
        r = await client.get(url, timeout=settings.http_timeout_sec)
        r.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("rss.fetch_failed source=%s url=%s err=%s", source, url, exc)
        return []
    try:
        return parse_rss(source, r.text, max_items)
    except (ValueError, AttributeError) as exc:
        log.warning("rss.parse_failed source=%s err=%s", source, exc)
        return []


async def fetch_aave_governance(client: httpx.AsyncClient) -> list[RawSignal]:
    return await _fetch_rss(
        client, "aave_governance", settings.src_aave_governance, settings.max_per_source,
    )


async def fetch_chainlink_blog(client: httpx.AsyncClient) -> list[RawSignal]:
    return await _fetch_rss(
        client, "chainlink_blog", settings.src_chainlink_blog, settings.max_per_source,
    )


async def fetch_gmx_governance(client: httpx.AsyncClient) -> list[RawSignal]:
    return await _fetch_rss(
        client, "gmx_governance", settings.src_gmx_governance, settings.max_per_source,
    )


async def fetch_hyperliquid_twitter(client: httpx.AsyncClient) -> list[RawSignal]:
    return await _fetch_rss(
        client, "hyperliquid_twitter", settings.src_hyperliquid_twitter,
        settings.max_per_source,
    )


async def fetch_dlnews(client: httpx.AsyncClient) -> list[RawSignal]:
    return await _fetch_rss(
        client, "dlnews", settings.src_dlnews, settings.max_per_source,
    )


async def fetch_theblock(client: httpx.AsyncClient) -> list[RawSignal]:
    return await _fetch_rss(
        client, "theblock", settings.src_theblock, settings.max_per_source,
    )


async def fetch_polymarket_twitter(client: httpx.AsyncClient) -> list[RawSignal]:
    return await _fetch_rss(
        client, "polymarket_twitter", settings.src_polymarket_twitter,
        settings.max_per_source,
    )


async def fetch_defillama(client: httpx.AsyncClient) -> list[RawSignal]:
    """Hit /protocols, return synthetic RawSignals for any tracked
    protocol exceeding the TVL-change threshold.
    """
    try:
        r = await client.get(
            settings.src_defillama_protocols, timeout=settings.http_timeout_sec,
        )
        r.raise_for_status()
        payload = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("defillama.fetch_failed err=%s", exc)
        return []
    if not isinstance(payload, list):
        log.warning("defillama.unexpected_shape type=%s", type(payload).__name__)
        return []
    return parse_defillama_protocols(
        payload, settings.defillama_protocols, settings.defillama_tvl_delta_pct,
    )


# Registry — scanner iterates this. Adding a fetcher = adding one line.
FETCHERS = (
    fetch_aave_governance,
    fetch_chainlink_blog,
    fetch_gmx_governance,
    fetch_hyperliquid_twitter,
    fetch_dlnews,
    fetch_theblock,
    fetch_polymarket_twitter,
    fetch_defillama,
)


def active_fetchers() -> tuple[Any, ...]:
    """The fetcher list for this run.

    Always includes the original v0.1 ``FETCHERS``. Additively appends the
    venue-coverage fetchers (Snapshot/Discourse/GDELT) when
    ``settings.venue_coverage_enabled`` is on. Importing ``venues`` here
    (not at module top) avoids a circular import (venues imports RawSignal
    from this module).
    """
    if not settings.venue_coverage_enabled:
        return FETCHERS
    from competitive_intel.venues import VENUE_FETCHERS

    return (*FETCHERS, *VENUE_FETCHERS)


async def fetch_all() -> list[RawSignal]:
    """Run every active fetcher concurrently; merge results.

    Any fetcher that raises returns [] thanks to its own fail-OPEN
    wrapper. We do NOT cancel sibling fetches on partial failure.
    """
    import asyncio

    fetchers = active_fetchers()
    out: list[RawSignal] = []
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_sec,
        follow_redirects=True,
        headers={"User-Agent": "competitive-intel/0.2 (+https://github.com/1305a001-ctrl/competitive-intel)"},
    ) as client:
        results = await asyncio.gather(
            *(fn(client) for fn in fetchers), return_exceptions=True,
        )
        for fn, res in zip(fetchers, results, strict=True):
            if isinstance(res, BaseException):
                log.warning("fetcher.crashed name=%s err=%s", fn.__name__, res)
                continue
            out.extend(res)
    return out
