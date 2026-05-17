"""Tests for the alerter — pure gating + formatter only."""
from __future__ import annotations

import json
from typing import Any

import pytest

from competitive_intel.alerter import (
    build_alert_payload,
    emit_alert,
    format_alert,
    should_alert,
)


@pytest.fixture
def displace_classification() -> dict[str, Any]:
    return {
        "type": "DISPLACE",
        "confidence": 0.85,
        "stage2": {
            "type": "DISPLACE",
            "confidence": 0.85,
            "affected_categories": ["liquidation"],
            "rationale": "Aave + Chainlink capturing liquidation MEV",
        },
    }


def test_should_alert_passes_canonical_case(displace_classification):
    assert should_alert(displace_classification, ["T1.02"]) is True


def test_should_alert_blocks_low_confidence(displace_classification):
    c = {**displace_classification, "confidence": 0.5}
    assert should_alert(c, ["T1.02"]) is False


def test_should_alert_blocks_unlock_by_default():
    c = {"type": "UNLOCK", "confidence": 0.9}
    assert should_alert(c, ["T1.02"]) is False


def test_should_alert_blocks_when_no_builds_matched(displace_classification):
    assert should_alert(displace_classification, []) is False


def test_should_alert_honours_explicit_types(displace_classification):
    # UNLOCK should pass if explicitly enabled
    c = {"type": "UNLOCK", "confidence": 0.95}
    assert should_alert(c, ["T1.05"], alert_types=("UNLOCK",)) is True


def test_should_alert_handles_bogus_confidence():
    c = {"type": "DISPLACE", "confidence": "not a number"}
    assert should_alert(c, ["T1.02"]) is False


def test_format_alert_contains_all_required_fields(
    displace_classification, sample_watchlist,
):
    sig = {
        "source": "aave_governance",
        "title": "Aave SVR expansion to Arbitrum/Base",
        "url": "https://governance.aave.com/t/123",
    }
    text = format_alert(sig, displace_classification, ["T1.02"], sample_watchlist)
    assert "Competitive Intel Alert" in text
    assert "DISPLACE" in text
    assert "85%" in text
    assert "aave_governance" in text
    assert "T1.02" in text
    assert "Liquidation Bot" in text  # name lookup
    assert "Aave SVR expansion" in text
    assert "governance.aave.com" in text
    assert "Aave + Chainlink" in text  # rationale present
    assert "framework" in text  # decision-window reminder


def test_format_alert_works_without_build_lookup(displace_classification):
    sig = {"source": "x", "title": "y", "url": "z"}
    text = format_alert(sig, displace_classification, ["T1.02"], None)
    assert "T1.02" in text


def test_build_alert_payload_has_text_kind_meta(
    displace_classification, sample_watchlist,
):
    sig = {"source": "aave", "title": "t", "url": "u"}
    payload = build_alert_payload(
        sig, displace_classification, ["T1.02"], sample_watchlist,
    )
    assert "text" in payload
    assert payload["kind"] == "competitive_intel"
    assert payload["meta"]["url"] == "u"
    assert payload["meta"]["matched_builds"] == ["T1.02"]
    assert payload["meta"]["type"] == "DISPLACE"


async def test_emit_alert_serialises_to_xadd(displace_classification):
    """emit_alert should XADD a JSON-encoded `data` field."""
    captured: dict[str, Any] = {}

    class _FakeRedis:
        async def xadd(self, stream, fields, **kwargs):
            captured["stream"] = stream
            captured["fields"] = fields
            captured["kwargs"] = kwargs
            return "fake-id-1"

    payload = {"text": "hi", "kind": "competitive_intel", "meta": {}}
    entry_id = await emit_alert(_FakeRedis(), payload)
    assert entry_id == "fake-id-1"
    assert captured["stream"] == "pa:notifications"
    assert "data" in captured["fields"]
    decoded = json.loads(captured["fields"]["data"])
    assert decoded["text"] == "hi"
    assert captured["kwargs"]["maxlen"] == 10_000
    assert captured["kwargs"]["approximate"] is True


async def test_emit_alert_fails_open_on_redis_error():
    class _BrokenRedis:
        async def xadd(self, *a, **k):
            raise RuntimeError("redis down")

    res = await emit_alert(_BrokenRedis(), {"text": "x", "kind": "y", "meta": {}})
    assert res is None
