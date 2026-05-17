"""Tests for the source-fetcher pure helpers (no real network)."""
from __future__ import annotations

from competitive_intel.sources import parse_defillama_protocols, parse_rss

SAMPLE_RSS = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Aave Gov</title>
    <item>
      <title>SVR expansion to Arbitrum/Base proposal</title>
      <link>https://governance.aave.com/t/123</link>
      <description>The ARFC has been filed.</description>
      <pubDate>Sat, 06 Mar 2026 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Risk parameter update for USDC</title>
      <link>https://governance.aave.com/t/124</link>
      <description>Routine update.</description>
    </item>
    <item>
      <!-- missing link, should be skipped -->
      <title>Bogus item</title>
      <description>no link</description>
    </item>
  </channel>
</rss>
"""


def test_parse_rss_extracts_entries():
    out = parse_rss("aave_governance", SAMPLE_RSS, max_items=10)
    assert len(out) == 2  # third item has no link
    assert out[0].source == "aave_governance"
    assert "SVR expansion" in out[0].title
    assert out[0].url == "https://governance.aave.com/t/123"
    assert "ARFC" in out[0].body


def test_parse_rss_respects_max_items():
    out = parse_rss("x", SAMPLE_RSS, max_items=1)
    assert len(out) == 1


def test_parse_rss_handles_empty_feed():
    empty = "<?xml version='1.0'?><rss><channel></channel></rss>"
    out = parse_rss("x", empty, max_items=10)
    assert out == []


def test_parse_rss_assigns_iso_timestamp():
    out = parse_rss("x", SAMPLE_RSS, max_items=10)
    # The first item has a pubDate (2026-03-06)
    assert out[0].published_at.startswith("2026-03-06")
    # The second item has no pubDate → falls back to "now" ISO
    assert "T" in out[1].published_at


def test_parse_defillama_threshold_filters_small_moves():
    payload = [
        {"slug": "aave-v3", "name": "Aave V3", "tvl": 1.2e10, "change_1d": 2.0},  # small
        {"slug": "polymarket", "name": "Polymarket", "tvl": 5e8, "change_1d": -15.5},
        {"slug": "untracked", "name": "Other", "tvl": 1e9, "change_1d": 50.0},  # not tracked
    ]
    out = parse_defillama_protocols(
        payload, tracked=("aave-v3", "polymarket"), delta_pct_threshold=10.0,
    )
    assert len(out) == 1
    assert out[0].source == "defillama"
    assert "Polymarket" in out[0].title
    assert "-15.5%" in out[0].title


def test_parse_defillama_ignores_missing_change():
    payload = [
        {"slug": "aave-v3", "name": "Aave", "tvl": 1e10},  # no change_1d
        {"slug": "aave-v3", "name": "Aave", "tvl": 1e10, "change_1d": None},
    ]
    out = parse_defillama_protocols(
        payload, tracked=("aave-v3",), delta_pct_threshold=10.0,
    )
    assert out == []


def test_parse_defillama_handles_string_change():
    payload = [
        {"slug": "aave-v3", "name": "Aave", "tvl": 1e10, "change_1d": "garbage"},
        {"slug": "aave-v3", "name": "Aave", "tvl": 1e10, "change_1d": "12.5"},
    ]
    out = parse_defillama_protocols(
        payload, tracked=("aave-v3",), delta_pct_threshold=10.0,
    )
    # "garbage" filtered, "12.5" coerces to float
    assert len(out) == 1
    assert "+12.5%" in out[0].title
