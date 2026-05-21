"""Venue-coverage fetchers — the stack's OWN trading venues.

The v0.1 ``sources.py`` watched ~generic crypto projects + Chainlink/Hyperliquid
blogs, but had **no structural-governance coverage of the venues the stack
actually trades** (Aave/GMX governance decisions, Polymarket/Hyperliquid
listings, oracle-infra changes). That is why the Aave-Chainlink SVR/Atlas
governance signal was structurally invisible.

This module adds that coverage using **free, no-SaaS** endpoints only:

  - Snapshot GraphQL (``hub.snapshot.org/graphql``) — DeFi governance proposals
    for Aave (``aavedao.eth``) + GMX (``gmx.eth``). Catches a lane-altering
    proposal the moment it is posted on-chain.
  - Discourse ``latest.json`` — Aave + GMX governance forums. Discourse exposes
    a stable JSON feed at ``/latest.json`` on every instance (more robust than
    the per-category ``.rss`` the v0.1 fetcher used).
  - GDELT DOC 2.0 (``api.gdeltproject.org/api/v2/doc/doc``) — free global news
    index. A targeted query surfaces macro / regulatory / M&A news that names
    one of our venues (the channel the Chainlink-acquires-Atlas headline would
    have come through).

Same contract as ``sources.py``: every fetcher is **fail-OPEN** (network/parse
error → ``[]`` + a warning), pure parsers are split out for unit testing, and
each returns ``RawSignal``.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from competitive_intel.settings import settings
from competitive_intel.sources import RawSignal

log = logging.getLogger(__name__)


# ─── Snapshot (GraphQL) ─────────────────────────────────────────────

# One query, parameterised by space list + limit. ``body`` gives the
# lane-impact scorer plenty of text to match risk-param / oracle language.
SNAPSHOT_QUERY = """\
query Proposals($spaces: [String]!, $first: Int!) {
  proposals(
    first: $first
    where: { space_in: $spaces }
    orderBy: "created"
    orderDirection: desc
  ) {
    id
    title
    body
    state
    created
    space { id }
  }
}"""


def _snapshot_url(space: str, proposal_id: str) -> str:
    return f"https://snapshot.org/#/{space}/proposal/{proposal_id}"


def parse_snapshot_proposals(
    payload: dict[str, Any], max_items: int,
) -> list[RawSignal]:
    """Pure: Snapshot GraphQL response → list[RawSignal].

    Tolerant of partial/garbage payloads (GraphQL errors, missing keys).
    """
    out: list[RawSignal] = []
    data = payload.get("data") if isinstance(payload, dict) else None
    proposals = (data or {}).get("proposals") if isinstance(data, dict) else None
    if not isinstance(proposals, list):
        return out
    for p in proposals[:max_items]:
        if not isinstance(p, dict):
            continue
        title = str(p.get("title") or "").strip()
        pid = str(p.get("id") or "").strip()
        space = str((p.get("space") or {}).get("id") or "").strip()
        if not title or not pid or not space:
            continue
        body = str(p.get("body") or "").strip()
        created = p.get("created")
        try:
            published = (
                datetime.fromtimestamp(int(created), tz=UTC).isoformat()
                if created is not None
                else datetime.now(UTC).isoformat()
            )
        except (TypeError, ValueError, OSError):
            published = datetime.now(UTC).isoformat()
        state = str(p.get("state") or "").strip()
        out.append(
            RawSignal(
                source=f"snapshot:{space}",
                title=f"[{state}] {title}" if state else title,
                body=body[:2000],
                url=_snapshot_url(space, pid),
                published_at=published,
            )
        )
    return out


async def fetch_snapshot(client: httpx.AsyncClient) -> list[RawSignal]:
    """Fetch recent Snapshot proposals for the configured governance spaces."""
    if not settings.snapshot_spaces:
        return []
    variables = {
        "spaces": list(settings.snapshot_spaces),
        "first": settings.max_per_source,
    }
    try:
        r = await client.post(
            settings.src_snapshot_graphql,
            json={"query": SNAPSHOT_QUERY, "variables": variables},
            timeout=settings.http_timeout_sec,
        )
        r.raise_for_status()
        payload = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("snapshot.fetch_failed err=%s", exc)
        return []
    if not isinstance(payload, dict):
        log.warning("snapshot.unexpected_shape type=%s", type(payload).__name__)
        return []
    return parse_snapshot_proposals(payload, settings.max_per_source)


# ─── Discourse (latest.json) ────────────────────────────────────────


def parse_discourse_latest(
    source: str, base_url: str, payload: dict[str, Any], max_items: int,
) -> list[RawSignal]:
    """Pure: Discourse ``/latest.json`` response → list[RawSignal].

    Discourse returns ``{topic_list: {topics: [...]}}``. Each topic has an
    ``id``, ``title``, ``slug`` and timestamps. We build the canonical topic
    URL from ``base_url`` + slug/id.
    """
    out: list[RawSignal] = []
    topic_list = payload.get("topic_list") if isinstance(payload, dict) else None
    topics = (topic_list or {}).get("topics") if isinstance(topic_list, dict) else None
    if not isinstance(topics, list):
        return out
    base = base_url.rstrip("/")
    for t in topics[:max_items]:
        if not isinstance(t, dict):
            continue
        title = str(t.get("title") or t.get("fancy_title") or "").strip()
        tid = t.get("id")
        if not title or tid is None:
            continue
        slug = str(t.get("slug") or "").strip()
        url = f"{base}/t/{slug}/{tid}" if slug else f"{base}/t/{tid}"
        published = (
            str(t.get("created_at") or t.get("last_posted_at") or "").strip()
            or datetime.now(UTC).isoformat()
        )
        excerpt = str(t.get("excerpt") or "").strip()
        out.append(
            RawSignal(
                source=source,
                title=title,
                body=excerpt[:2000],
                url=url,
                published_at=published,
            )
        )
    return out


async def _fetch_discourse(
    client: httpx.AsyncClient, source: str, latest_url: str, base_url: str,
) -> list[RawSignal]:
    try:
        r = await client.get(latest_url, timeout=settings.http_timeout_sec)
        r.raise_for_status()
        payload = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("discourse.fetch_failed source=%s err=%s", source, exc)
        return []
    if not isinstance(payload, dict):
        log.warning("discourse.unexpected_shape source=%s", source)
        return []
    return parse_discourse_latest(source, base_url, payload, settings.max_per_source)


async def fetch_aave_discourse(client: httpx.AsyncClient) -> list[RawSignal]:
    return await _fetch_discourse(
        client,
        "aave_discourse",
        settings.src_aave_discourse_latest,
        settings.src_aave_discourse_base,
    )


async def fetch_gmx_discourse(client: httpx.AsyncClient) -> list[RawSignal]:
    return await _fetch_discourse(
        client,
        "gmx_discourse",
        settings.src_gmx_discourse_latest,
        settings.src_gmx_discourse_base,
    )


# ─── GDELT DOC 2.0 ──────────────────────────────────────────────────


def parse_gdelt_articles(
    payload: dict[str, Any], max_items: int,
) -> list[RawSignal]:
    """Pure: GDELT DOC 2.0 ``artlist`` JSON → list[RawSignal].

    GDELT returns ``{articles: [{title, url, domain, seendate, ...}]}``.
    ``seendate`` looks like ``20260521T120000Z``.
    """
    out: list[RawSignal] = []
    articles = payload.get("articles") if isinstance(payload, dict) else None
    if not isinstance(articles, list):
        return out
    for a in articles[:max_items]:
        if not isinstance(a, dict):
            continue
        title = str(a.get("title") or "").strip()
        url = str(a.get("url") or "").strip()
        if not title or not url:
            continue
        seen = str(a.get("seendate") or "").strip()
        published = _parse_gdelt_date(seen)
        domain = str(a.get("domain") or "").strip()
        out.append(
            RawSignal(
                source="gdelt",
                title=title,
                body=f"domain={domain}" if domain else "",
                url=url,
                published_at=published,
            )
        )
    return out


def _parse_gdelt_date(seen: str) -> str:
    """``20260521T120000Z`` → ISO-8601. Falls back to now-UTC."""
    if seen:
        try:
            dt = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            return dt.isoformat()
        except (ValueError, TypeError):
            pass
    return datetime.now(UTC).isoformat()


async def fetch_gdelt(client: httpx.AsyncClient) -> list[RawSignal]:
    """Query GDELT DOC 2.0 for recent news naming one of our venues."""
    params = {
        "query": settings.gdelt_query,
        "mode": "artlist",
        "format": "json",
        "sort": "datedesc",
        "maxrecords": str(min(settings.max_per_source, 75)),
        "timespan": settings.gdelt_timespan,
    }
    try:
        r = await client.get(
            settings.src_gdelt_doc, params=params, timeout=settings.http_timeout_sec,
        )
        r.raise_for_status()
        # GDELT sometimes returns text/plain on empty/error — guard json().
        payload = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("gdelt.fetch_failed err=%s", exc)
        return []
    if not isinstance(payload, dict):
        log.warning("gdelt.unexpected_shape type=%s", type(payload).__name__)
        return []
    return parse_gdelt_articles(payload, settings.max_per_source)


# Registry — scanner extends its fetcher list with these when venue coverage
# is enabled. Adding a venue fetcher = adding one line here.
VENUE_FETCHERS = (
    fetch_snapshot,
    fetch_aave_discourse,
    fetch_gmx_discourse,
    fetch_gdelt,
)
