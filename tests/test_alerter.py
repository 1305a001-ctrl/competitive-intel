"""Tests for the alerter — pure gating + formatter + structural-alert layer."""
from __future__ import annotations

import json
from typing import Any

import pytest

from competitive_intel import alerter
from competitive_intel.alerter import (
    build_alert_payload,
    build_structural_alert,
    dispatch_structural_alert,
    emit_alert,
    format_alert,
    format_structural_alert,
    persist_structural_alert,
    should_alert,
    structural_stream_key,
)
from competitive_intel.lane_impact import score_lane_impact


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


# ─── structural-alert layer (lane-viability radar) ──────────────────


class _FakeRedis:
    """Records xadd streams and set keys for assertions."""

    def __init__(self) -> None:
        self.streams: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.sets: dict[str, str] = {}
        self._n = 0

    async def xadd(self, stream, fields, **kwargs):
        self._n += 1
        self.streams.append((stream, fields, kwargs))
        return f"id-{self._n}"

    async def set(self, key, value):
        self.sets[key] = value
        return True


@pytest.fixture
def atlas_impact():
    return score_lane_impact(
        {
            "title": "Chainlink acquires Atlas; Aave SVR live",
            "body": "",
            "source": "theblock",
        }
    )


def test_structural_stream_key_format():
    assert structural_stream_key("aave_defi") == "news:structural_alert:aave_defi"


def test_format_structural_alert_contains_fields(atlas_impact):
    sig = {
        "source": "theblock",
        "title": "Chainlink acquires Atlas; Aave SVR live",
        "url": "https://theblock.co/x",
    }
    text = format_structural_alert(sig, atlas_impact)
    assert "Lane-Viability Radar" in text
    assert "Aave" in text  # lane label
    assert "T1.02" in text  # affected build
    assert "theblock.co" in text
    assert "ACQUISITION" in text or "ORACLE" in text


def test_build_structural_alert_shape(atlas_impact):
    sig = {"source": "theblock", "title": "t", "url": "u", "published_at": "p"}
    rec = build_structural_alert(
        sig, atlas_impact, classification={"type": "REGIME", "confidence": 0.0}
    )
    assert rec["kind"] == "lane_structural_alert"
    assert rec["url"] == "u"
    assert rec["impact"]["critical"] is True
    assert "aave_defi" in rec["impact"]["lanes"]
    # LLM view attached for cross-reference, even though it's low-confidence
    assert rec["llm_type"] == "REGIME"
    assert rec["llm_confidence"] == 0.0


async def test_persist_structural_alert_writes_per_lane_and_latest(atlas_impact):
    fake = _FakeRedis()
    rec = build_structural_alert(
        {"source": "x", "title": "t", "url": "u"}, atlas_impact
    )
    ids = await persist_structural_alert(fake, rec)
    # one stream per lane
    assert len(ids) == len(atlas_impact.lanes)
    written_streams = {s for s, _, _ in fake.streams}
    for lane in atlas_impact.lanes:
        assert f"news:structural_alert:{lane}" in written_streams
    # latest string set
    assert "news:structural_alert:latest" in fake.sets
    decoded = json.loads(fake.sets["news:structural_alert:latest"])
    assert decoded["impact"]["critical"] is True


async def test_persist_structural_alert_fails_open():
    class _Broken:
        async def xadd(self, *a, **k):
            raise RuntimeError("down")

        async def set(self, *a, **k):
            raise RuntimeError("down")

    impact = score_lane_impact({"title": "CFTC sues Polymarket", "body": "", "source": "g"})
    rec = build_structural_alert({"source": "g", "title": "t", "url": "u"}, impact)
    ids = await persist_structural_alert(_Broken(), rec)
    assert ids == []  # fail-OPEN, no raise


async def test_dispatch_dry_run_persists_but_no_outbound(atlas_impact, monkeypatch):
    """Default (gate OFF): persist + log, but NO pa:notifications outbound."""
    monkeypatch.setattr(
        alerter.settings, "structural_alert_outbound_enabled", False
    )
    fake = _FakeRedis()
    rec = build_structural_alert({"source": "x", "title": "t", "url": "u"}, atlas_impact)
    status = await dispatch_structural_alert(fake, rec)
    assert status["outbound_sent"] is False
    assert status["outbound_id"] is None
    assert len(status["persisted"]) == len(atlas_impact.lanes)
    # nothing was XADDed to the pa-agent notifications stream
    assert all(s != "pa:notifications" for s, _, _ in fake.streams)


async def test_dispatch_outbound_when_flag_on(atlas_impact, monkeypatch):
    """Gate ON: persist AND emit to pa:notifications."""
    monkeypatch.setattr(
        alerter.settings, "structural_alert_outbound_enabled", True
    )
    fake = _FakeRedis()
    rec = build_structural_alert({"source": "x", "title": "t", "url": "u"}, atlas_impact)
    status = await dispatch_structural_alert(fake, rec)
    assert status["outbound_sent"] is True
    assert status["outbound_id"] is not None
    # pa:notifications got the outbound alert
    assert any(s == "pa:notifications" for s, _, _ in fake.streams)
