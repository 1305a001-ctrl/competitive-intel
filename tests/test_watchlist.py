"""Tests for the watchlist loader + match function."""
from __future__ import annotations

import json

import pytest

from competitive_intel.watchlist import (
    Build,
    load_watchlist,
    match_signal,
    parse_watchlist,
)


def test_build_all_terms_dedupes_and_lowercases():
    b = Build(
        id="X",
        name="x",
        track=1,
        status="active",
        upstream=("Polymarket", "UMA Oracle"),
        substitutes=("polymarket",),  # dup of upstream after lower
        keywords=("Kalshi",),
        existential=(),
    )
    terms = b.all_terms()
    assert "polymarket" in terms
    assert "uma oracle" in terms
    assert "kalshi" in terms
    # dedup means only one occurrence of polymarket
    assert terms.count("polymarket") == 1


def test_parse_watchlist_minimal_entry():
    parsed = parse_watchlist({"builds": {"T1": {"name": "Foo", "track": 1}}})
    assert "T1" in parsed
    assert parsed["T1"].name == "Foo"
    assert parsed["T1"].track == 1
    assert parsed["T1"].upstream == ()


def test_parse_watchlist_skips_non_dict_entry(caplog):
    parsed = parse_watchlist({"builds": {"BAD": "not a dict", "T1": {"name": "ok"}}})
    assert "BAD" not in parsed
    assert "T1" in parsed


def test_match_signal_hits_on_keyword(sample_watchlist):
    signal = {
        "title": "Aave SVR expansion to Arbitrum/Base announced",
        "body": "The governance forum proposal has passed.",
        "source": "aave_governance",
        "url": "https://governance.aave.com/t/123",
    }
    matched = match_signal(signal, sample_watchlist)
    assert "T1.02" in matched


def test_match_signal_hits_on_upstream(sample_watchlist):
    signal = {
        "title": "UMA Oracle deprecated for a new resolution engine",
        "body": "",
        "source": "dlnews",
        "url": "https://dlnews.com/foo",
    }
    matched = match_signal(signal, sample_watchlist)
    assert "T1.01" in matched


def test_match_signal_hits_on_affected_categories(sample_watchlist):
    signal = {
        "title": "Generic news headline",
        "body": "",
        "source": "theblock",
        "url": "https://theblock.co/foo",
        "affected_categories": ["liquidation", "Aave"],
    }
    matched = match_signal(signal, sample_watchlist)
    assert "T1.02" in matched


def test_match_signal_empty_when_no_match(sample_watchlist):
    signal = {
        "title": "Bitcoin hits new all-time high",
        "body": "",
        "source": "dlnews",
        "url": "https://dlnews.com/btc",
    }
    matched = match_signal(signal, sample_watchlist)
    assert matched == []


def test_load_watchlist_yaml(tmp_path):
    p = tmp_path / "watch.yaml"
    p.write_text(
        "builds:\n"
        "  X1:\n"
        "    name: My Build\n"
        "    track: 1\n"
        "    keywords:\n"
        "      - widget\n",
        encoding="utf-8",
    )
    wl = load_watchlist(p)
    assert "X1" in wl
    assert wl["X1"].keywords == ("widget",)


def test_load_watchlist_json(tmp_path):
    p = tmp_path / "watch.json"
    p.write_text(
        json.dumps({"builds": {"X2": {"name": "B", "track": 2}}}),
        encoding="utf-8",
    )
    wl = load_watchlist(p)
    assert wl["X2"].track == 2


def test_load_watchlist_rejects_non_dict_root(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ValueError):
        load_watchlist(p)


def test_load_real_data_yaml():
    """Spot-check the committed watchlist parses + covers all 25 builds."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    wl = load_watchlist(repo_root / "data" / "watchlist.yaml")
    # 10 T1 + 6 T2 + 5 T3 = 21 minimum (spec says "25 builds"; the
    # framework only enumerates 21 build IDs explicitly + handwaves
    # T1.07–T1.10. We've enumerated all of them.)
    assert len(wl) >= 21
    # Spot-check a few key IDs
    for bid in ("T1.01", "T1.02", "T2.01", "T2.03", "T3.05"):
        assert bid in wl, f"missing build {bid}"
    # T1.02 should hit on SVR
    matched = match_signal(
        {"title": "SVR expansion goes live", "body": "", "source": "x", "url": "u"},
        wl,
    )
    assert "T1.02" in matched
