"""Tests for the venue-coverage fetchers — pure parsers (no real network)."""
from __future__ import annotations

from competitive_intel.venues import (
    parse_discourse_latest,
    parse_gdelt_articles,
    parse_snapshot_proposals,
)

# ─── Snapshot ───────────────────────────────────────────────────────

SNAPSHOT_PAYLOAD = {
    "data": {
        "proposals": [
            {
                "id": "0xabc",
                "title": "[ARFC] Onboard new oracle for USDC",
                "body": "Risk parameter update affecting liquidations.",
                "state": "active",
                "created": 1_770_000_000,
                "space": {"id": "aavedao.eth"},
            },
            {
                "id": "0xdef",
                "title": "GMX v2.3 fee switch",
                "body": "",
                "state": "closed",
                "created": None,  # missing timestamp → falls back to now
                "space": {"id": "gmx.eth"},
            },
            {
                # missing id → skipped
                "title": "broken",
                "space": {"id": "aavedao.eth"},
            },
        ]
    }
}


def test_parse_snapshot_extracts_proposals():
    out = parse_snapshot_proposals(SNAPSHOT_PAYLOAD, max_items=10)
    assert len(out) == 2  # third has no id
    assert out[0].source == "snapshot:aavedao.eth"
    assert "Onboard new oracle" in out[0].title
    assert out[0].title.startswith("[active]")
    assert "snapshot.org" in out[0].url
    assert "0xabc" in out[0].url
    assert "Risk parameter" in out[0].body


def test_parse_snapshot_handles_graphql_errors():
    assert parse_snapshot_proposals({"errors": [{"message": "boom"}]}, 10) == []
    assert parse_snapshot_proposals({}, 10) == []
    assert parse_snapshot_proposals({"data": {"proposals": None}}, 10) == []


def test_parse_snapshot_respects_max_items():
    out = parse_snapshot_proposals(SNAPSHOT_PAYLOAD, max_items=1)
    assert len(out) == 1


def test_parse_snapshot_timestamp_iso():
    out = parse_snapshot_proposals(SNAPSHOT_PAYLOAD, max_items=10)
    # 1_770_000_000 → 2026-02-x
    assert out[0].published_at.startswith("2026-")
    # second proposal had None created → "now" ISO with a T separator
    assert "T" in out[1].published_at


# ─── Discourse ──────────────────────────────────────────────────────

DISCOURSE_PAYLOAD = {
    "topic_list": {
        "topics": [
            {
                "id": 123,
                "title": "ARFC: deprecate stale price feed",
                "slug": "arfc-deprecate-stale-price-feed",
                "created_at": "2026-05-01T10:00:00.000Z",
                "excerpt": "We propose sunsetting the legacy feed.",
            },
            {
                # no slug → URL falls back to /t/<id>
                "id": 456,
                "title": "Routine governance update",
                "created_at": "2026-05-02T10:00:00.000Z",
            },
            {
                # no id → skipped
                "title": "broken topic",
                "slug": "broken",
            },
        ]
    }
}


def test_parse_discourse_extracts_topics():
    out = parse_discourse_latest(
        "aave_discourse", "https://governance.aave.com", DISCOURSE_PAYLOAD, 10
    )
    assert len(out) == 2  # third has no id
    assert out[0].source == "aave_discourse"
    assert "deprecate stale price feed" in out[0].title
    assert out[0].url == (
        "https://governance.aave.com/t/arfc-deprecate-stale-price-feed/123"
    )
    assert "sunsetting" in out[0].body
    # second has no slug
    assert out[1].url == "https://governance.aave.com/t/456"


def test_parse_discourse_handles_garbage():
    assert parse_discourse_latest("x", "https://e.com", {}, 10) == []
    assert parse_discourse_latest("x", "https://e.com", {"topic_list": {}}, 10) == []


# ─── GDELT ──────────────────────────────────────────────────────────

GDELT_PAYLOAD = {
    "articles": [
        {
            "title": "Chainlink acquires Atlas in oracle MEV push",
            "url": "https://news.example.com/chainlink-atlas",
            "domain": "news.example.com",
            "seendate": "20260521T120000Z",
        },
        {
            # missing url → skipped
            "title": "no url here",
            "domain": "x.com",
        },
    ]
}


def test_parse_gdelt_extracts_articles():
    out = parse_gdelt_articles(GDELT_PAYLOAD, 10)
    assert len(out) == 1  # second has no url
    assert out[0].source == "gdelt"
    assert "Chainlink acquires Atlas" in out[0].title
    assert out[0].url == "https://news.example.com/chainlink-atlas"
    assert "news.example.com" in out[0].body
    # seendate parsed to ISO
    assert out[0].published_at.startswith("2026-05-21T12:00:00")


def test_parse_gdelt_handles_bad_seendate():
    payload = {"articles": [{"title": "t", "url": "https://x.com/a", "seendate": "garbage"}]}
    out = parse_gdelt_articles(payload, 10)
    assert len(out) == 1
    # fell back to now-ISO
    assert "T" in out[0].published_at


def test_parse_gdelt_handles_empty_or_text():
    assert parse_gdelt_articles({}, 10) == []
    assert parse_gdelt_articles({"articles": None}, 10) == []
