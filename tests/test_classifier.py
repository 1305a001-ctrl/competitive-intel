"""Tests for the classifier — pure prompt + parse helpers only.

The async LLM I/O is exercised indirectly via the fail-OPEN parse path.
"""
from __future__ import annotations

import json

from competitive_intel.classifier import (
    COMP_INTEL_PROMPT_TEMPLATE,
    VALID_TYPES,
    build_comp_intel_prompt,
    parse_comp_intel_response,
)


def test_prompt_includes_all_four_types():
    p = build_comp_intel_prompt(source="x", title="y", body="z")
    for t in VALID_TYPES:
        assert t in p
    assert "JSON" in p


def test_prompt_truncates_long_inputs():
    long = "a" * 5000
    p = build_comp_intel_prompt(source=long, title=long, body=long)
    # template + truncated source(80) + title(300) + body(1500) << 5000
    assert len(p) < len(COMP_INTEL_PROMPT_TEMPLATE) + 80 + 300 + 1500 + 50


def test_parse_handles_clean_dict_input():
    res = parse_comp_intel_response({
        "type": "DISPLACE",
        "confidence": 0.85,
        "affected_categories": ["liquidation"],
        "rationale": "Aave + CL capturing MEV",
    })
    assert res["type"] == "DISPLACE"
    assert res["confidence"] == 0.85
    assert res["affected_categories"] == ["liquidation"]
    assert "Aave" in res["rationale"]


def test_parse_handles_json_string_input():
    raw = json.dumps({"type": "UNLOCK", "confidence": 0.6, "affected_categories": []})
    res = parse_comp_intel_response(raw)
    assert res["type"] == "UNLOCK"
    assert res["confidence"] == 0.6


def test_parse_extracts_json_from_noisy_text():
    raw = (
        "Sure! Here is the classification: "
        '{"type": "SUBSTITUTE", "confidence": 0.9, "affected_categories": ["nav"], '
        '"rationale": "RedStone moved down-market"}'
        " — hope this helps."
    )
    res = parse_comp_intel_response(raw)
    assert res["type"] == "SUBSTITUTE"
    assert res["confidence"] == 0.9
    assert "RedStone" in res["rationale"]


def test_parse_returns_default_on_none():
    res = parse_comp_intel_response(None)
    assert res["type"] == "REGIME"
    assert res["confidence"] == 0.0
    assert res["affected_categories"] == []


def test_parse_returns_default_on_garbage():
    res = parse_comp_intel_response("this is not json at all")
    assert res["type"] == "REGIME"
    assert res["confidence"] == 0.0


def test_parse_clamps_confidence():
    res = parse_comp_intel_response({"type": "REGIME", "confidence": 5.0})
    assert res["confidence"] == 1.0
    res2 = parse_comp_intel_response({"type": "REGIME", "confidence": -3})
    assert res2["confidence"] == 0.0


def test_parse_normalises_invalid_type():
    res = parse_comp_intel_response({"type": "BAFFLEGAB", "confidence": 0.5})
    assert res["type"] == "REGIME"


def test_parse_handles_categories_as_comma_string():
    res = parse_comp_intel_response({
        "type": "DISPLACE",
        "confidence": 0.7,
        "affected_categories": "liquidation, oracle, polymarket",
    })
    assert res["affected_categories"] == ["liquidation", "oracle", "polymarket"]


def test_parse_truncates_rationale():
    long_rationale = "x" * 1000
    res = parse_comp_intel_response({
        "type": "REGIME",
        "confidence": 0.5,
        "rationale": long_rationale,
    })
    assert len(res["rationale"]) <= 280
