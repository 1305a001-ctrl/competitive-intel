"""Tests for the deterministic lane-impact scorer (the lane-viability radar).

The headline requirement (and the reason this module exists): an
"Chainlink acquires Atlas / Aave SVR" headline MUST classify as lane-critical
for the Aave lane — *without* any LLM involvement.
"""
from __future__ import annotations

import pytest

from competitive_intel.lane_impact import (
    LANES,
    detect_event_kinds,
    detect_lanes,
    score_lane_impact,
)

# ─── THE Atlas regression (the motivating failure) ──────────────────


@pytest.mark.parametrize(
    "title,body",
    [
        (
            "Chainlink acquires Atlas to bring Smart Value Recapture (SVR) to Aave",
            "The acquisition lets Chainlink Data Streams capture oracle MEV via SVR "
            "for Aave liquidations.",
        ),
        # Terse variant — no body, lane named.
        ("Chainlink to acquire Atlas; Aave SVR goes live", ""),
        # SVR mechanism without the word "Aave" but with Chainlink (indirect hop).
        (
            "Chainlink rolls out Smart Value Recapture across lending oracles",
            "SVR captures oracle extractable value (OEV) on liquidations.",
        ),
    ],
)
def test_atlas_headline_is_lane_critical_for_aave(title, body):
    impact = score_lane_impact({"title": title, "body": body, "source": "theblock"})
    assert impact.critical is True
    assert "aave_defi" in impact.lanes
    # The Liquidation Bot build (T1.02) must be implicated.
    assert "T1.02" in impact.builds
    # ACQUISITION or ORACLE mechanism must be detected.
    assert {"ACQUISITION", "ORACLE"} & set(impact.event_kinds)
    # High severity — at/above the alert floor by a wide margin.
    assert impact.severity >= 0.6


def test_atlas_alert_fires_even_with_failed_llm():
    """The whole point: lane-impact is independent of the LLM.

    A signal whose LLM classification is the fail-OPEN default
    (REGIME / confidence 0.0) still triggers a lane-critical structural
    verdict.
    """
    impact = score_lane_impact(
        {
            "title": "Chainlink acquires Atlas; Aave SVR live",
            "body": "",
            "source": "dlnews",
            # mimic classifier enrichment when the LLM is down
            "affected_categories": [],
        }
    )
    assert impact.critical is True
    assert "aave_defi" in impact.lanes


# ─── event-kind detection ───────────────────────────────────────────


def test_detect_event_kinds_acquisition():
    kinds = dict(detect_event_kinds("company a acquires company b"))
    assert "ACQUISITION" in kinds


def test_detect_event_kinds_oracle():
    kinds = dict(detect_event_kinds("new oracle price feed migration with svr"))
    assert "ORACLE" in kinds


def test_detect_event_kinds_regulatory():
    kinds = dict(detect_event_kinds("cftc enforcement action and lawsuit"))
    assert "REGULATORY" in kinds


def test_detect_event_kinds_delisting_outranks_listing():
    delist = dict(detect_event_kinds("exchange delists the market"))
    listing = dict(detect_event_kinds("exchange lists a new market"))
    assert "DELISTING" in delist
    assert "LISTING" in listing
    # delisting severity > listing severity
    assert delist["DELISTING"] > listing["LISTING"]


def test_detect_event_kinds_none_on_benign_text():
    assert detect_event_kinds("bitcoin price goes up today") == []


# ─── lane detection ─────────────────────────────────────────────────


def test_detect_lanes_named_venues():
    assert "aave_defi" in detect_lanes("an aave governance proposal")
    assert "polymarket" in detect_lanes("polymarket launches a market")
    assert "hyperliquid" in detect_lanes("hyperliquid perps update")
    assert "tokenized_eq" in detect_lanes("backed finance tokenized equities")
    assert "oracle_infra" in detect_lanes("chainlink data streams")


def test_detect_lanes_empty_when_no_venue():
    assert detect_lanes("a random defi project does something") == []


# ─── scoring behaviour ──────────────────────────────────────────────


def test_benign_news_is_not_critical():
    impact = score_lane_impact(
        {"title": "Bitcoin hits new all-time high", "body": "", "source": "dlnews"}
    )
    assert impact.critical is False
    assert impact.severity == 0.0
    assert impact.lanes == ()


def test_structural_event_with_no_venue_is_not_critical():
    impact = score_lane_impact(
        {"title": "Some random DAO changes its fee parameter", "body": "vote", "source": "x"}
    )
    # GOVERNANCE detected, but no lane touched → not critical, no builds.
    assert "GOVERNANCE" in impact.event_kinds
    assert impact.critical is False
    assert impact.builds == ()


def test_regulatory_action_on_polymarket_is_critical():
    impact = score_lane_impact(
        {"title": "CFTC opens enforcement action against Polymarket", "body": "", "source": "gdelt"}
    )
    assert impact.critical is True
    assert "polymarket" in impact.lanes
    assert "REGULATORY" in impact.event_kinds


def test_delisting_on_traded_venue_is_critical():
    impact = score_lane_impact(
        {"title": "Hyperliquid delists three perp markets", "body": "", "source": "gdelt"}
    )
    assert impact.critical is True
    assert "hyperliquid" in impact.lanes


def test_new_listing_below_floor_by_default():
    impact = score_lane_impact(
        {"title": "Aave lists a new GHO market", "body": "launch", "source": "snapshot:aavedao.eth"}
    )
    # A *new* listing is a low-severity structural event; below the 0.6 floor.
    assert "LISTING" in impact.event_kinds
    assert impact.critical is False


def test_indirect_oracle_hop_reaches_dependent_lanes():
    """An oracle event naming an oracle provider propagates to oracle-dependent
    lanes (aave_defi, polymarket, oracle_infra) even when those lanes aren't
    named — the indirect hop the Atlas signal needed."""
    impact = score_lane_impact(
        {
            "title": "Chainlink deprecates a legacy data feed",
            "body": "price feed migration",
            "source": "chainlink_blog",
        }
    )
    assert impact.critical is True
    # oracle_infra named directly; aave_defi + polymarket via the hop.
    assert "oracle_infra" in impact.lanes
    assert "aave_defi" in impact.lanes
    assert "polymarket" in impact.lanes


def test_custom_severity_floor_is_respected():
    sig = {"title": "Aave lists a new GHO market", "body": "launch", "source": "x"}
    # With a low floor, even a new listing becomes critical.
    impact = score_lane_impact(sig, critical_severity=0.4)
    assert impact.critical is True


def test_body_only_structural_words_are_discounted():
    """A routine governance post that merely *mentions* launch/merge/oracle in
    a long body (benign title) must NOT trip a critical alert — the real
    false-positive observed against live Snapshot data."""
    impact = score_lane_impact(
        {
            "title": "[closed] [ARFC] TokenLogic Phase II - Extension",
            "body": (
                "In response to the launch of Aave V4 and Aave Labs role as "
                "innovator, this proposal presents TokenLogic to manage finances. "
                "We will merge operations and consult on oracle integrations."
            ),
            "source": "snapshot:aavedao.eth",
        }
    )
    # lane is still detected (aave), but body-only mechanisms are discounted
    # below the 0.6 floor → not a structural alert.
    assert "aave_defi" in impact.lanes
    assert impact.critical is False


def test_title_structural_event_is_not_discounted():
    """The same mechanism in the *title* keeps full severity."""
    impact = score_lane_impact(
        {
            "title": "Aave to merge with a competing lending protocol",
            "body": "details to follow",
            "source": "theblock",
        }
    )
    assert "ACQUISITION" in impact.event_kinds
    assert impact.critical is True


def test_body_only_oracle_does_not_fire_indirect_hop():
    """The indirect oracle hop requires the mechanism to be salient (in the
    headline). An oracle provider named with the mechanism only in body prose
    must not blanket-flag all oracle-dependent lanes."""
    impact = score_lane_impact(
        {
            "title": "Weekly community call recap",
            "body": "We discussed chainlink and a possible oracle migration someday.",
            "source": "x",
        }
    )
    # no lane is directly named in a way that clears the floor; the hop is
    # suppressed because ORACLE is body-only.
    assert impact.critical is False


def test_affected_categories_feed_lane_detection():
    """The scorer reads affected_categories (LLM enrichment) too."""
    impact = score_lane_impact(
        {
            "title": "Major oracle change announced",
            "body": "",
            "source": "theblock",
            "affected_categories": ["aave", "liquidation"],
        }
    )
    assert "aave_defi" in impact.lanes
    assert impact.critical is True


def test_per_lane_and_builds_are_consistent():
    impact = score_lane_impact(
        {"title": "Chainlink acquires Atlas; Aave SVR live", "body": "", "source": "x"}
    )
    # every lane in .lanes appears in per_lane
    assert set(impact.lanes) == set(impact.per_lane)
    # builds are the union of the matched lanes' builds, deduped
    expected: list[str] = []
    for lane in impact.lanes:
        for b in LANES[lane]["builds"]:
            if b not in expected:
                expected.append(b)
    assert set(impact.builds) == set(expected)


def test_as_dict_round_trips_fields():
    impact = score_lane_impact(
        {"title": "CFTC sues Polymarket", "body": "", "source": "gdelt"}
    )
    d = impact.as_dict()
    assert d["critical"] is True
    assert "polymarket" in d["lanes"]
    assert isinstance(d["per_lane"], dict)
    assert isinstance(d["builds"], list)
