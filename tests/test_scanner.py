"""Integration test for scanner.run_once — the full ingest→classify→radar path.

External I/O (sources, LLM) is mocked. The key assertion: an Atlas-style
headline flows end-to-end into a persisted structural alert on the new Redis
namespace, even though the (mocked) LLM returns a fail-OPEN low-confidence
classification — exactly the case the v0.1 pipeline missed.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from competitive_intel import alerter, scanner
from competitive_intel.sources import RawSignal
from competitive_intel.watchlist import Build


class _FakeRedis:
    def __init__(self) -> None:
        self.streams: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.sets: dict[str, str] = {}
        self.seen: set[str] = set()
        self._n = 0

    # idempotency SET
    def pipeline(self):
        return _FakePipe(self)

    async def sadd(self, key, *vals):
        self.seen.update(vals)
        return len(vals)

    async def scard(self, key):
        return len(self.seen)

    async def srandmember(self, key, n):
        return []

    async def srem(self, key, *vals):
        return 0

    # streams + strings
    async def xadd(self, stream, fields, **kwargs):
        self._n += 1
        self.streams.append((stream, fields, kwargs))
        return f"id-{self._n}"

    async def set(self, key, value):
        self.sets[key] = value
        return True


class _FakePipe:
    def __init__(self, parent: _FakeRedis) -> None:
        self.parent = parent
        self.calls: list[str] = []

    def sismember(self, key, val):
        self.calls.append(val)
        return self

    async def execute(self):
        return [val in self.parent.seen for val in self.calls]


@pytest.fixture
def radar_watchlist() -> dict[str, Build]:
    return {
        "T1.02": Build(
            id="T1.02",
            name="Liquidation Bot",
            track=1,
            status="degraded",
            upstream=("aave v3",),
            substitutes=("atlas",),
            keywords=("svr", "liquidation"),
            existential=("svr expansion",),
        ),
    }


async def _fake_classify_failopen(source, title, body):
    """Mimic the LLM being down: REGIME / confidence 0.0 (fail-OPEN default)."""
    return {
        "stage1": {"type": "other", "affected_assets": [], "urgency": "low", "ok": False},
        "stage2": {"type": "REGIME", "confidence": 0.0, "affected_categories": [], "rationale": ""},
        "type": "REGIME",
        "confidence": 0.0,
    }


async def _fake_fetch_atlas():
    return [
        RawSignal(
            source="theblock",
            title="Chainlink acquires Atlas; Aave SVR goes live",
            body="Oracle MEV capture for Aave liquidations.",
            url="https://theblock.co/atlas",
            published_at="2026-01-15T00:00:00+00:00",
        ),
        # a benign one that must NOT produce a structural alert
        RawSignal(
            source="dlnews",
            title="Bitcoin hits a new all-time high",
            body="",
            url="https://dlnews.com/btc",
            published_at="2026-01-15T00:00:00+00:00",
        ),
    ]


async def test_run_once_atlas_triggers_structural_alert_despite_failed_llm(
    radar_watchlist, monkeypatch, tmp_path
):
    fake = _FakeRedis()
    monkeypatch.setattr(scanner.sources, "fetch_all", _fake_fetch_atlas)
    monkeypatch.setattr(scanner.classifier, "classify_signal", _fake_classify_failopen)
    monkeypatch.setattr(scanner.settings, "signal_log_path", str(tmp_path / "log.jsonl"))
    # outbound stays gated off (dry-run) — default, but be explicit
    monkeypatch.setattr(alerter.settings, "structural_alert_outbound_enabled", False)

    counters = await scanner.run_once(fake, radar_watchlist)

    # exactly one structural alert (the Atlas headline), benign one ignored
    assert counters["structural_alerted"] == 1
    # the LLM gate fired zero classic alerts (confidence 0.0)
    assert counters["alerted"] == 0

    # persisted to the aave_defi structural stream + latest key
    written = {s for s, _, _ in fake.streams}
    assert "news:structural_alert:aave_defi" in written
    assert "news:structural_alert:latest" in fake.sets
    # dry-run: nothing on pa:notifications
    assert "pa:notifications" not in written

    latest = json.loads(fake.sets["news:structural_alert:latest"])
    assert latest["impact"]["critical"] is True
    assert "aave_defi" in latest["impact"]["lanes"]
    assert "T1.02" in latest["impact"]["builds"]


async def test_run_once_logs_lane_impact_rows(radar_watchlist, monkeypatch, tmp_path):
    from competitive_intel import signal_log

    fake = _FakeRedis()
    log_path = tmp_path / "log.jsonl"
    monkeypatch.setattr(scanner.sources, "fetch_all", _fake_fetch_atlas)
    monkeypatch.setattr(scanner.classifier, "classify_signal", _fake_classify_failopen)
    monkeypatch.setattr(scanner.settings, "signal_log_path", str(log_path))

    await scanner.run_once(fake, radar_watchlist)

    rows = signal_log.read_all(log_path)
    assert len(rows) == 2
    by_url = {r["url"]: r for r in rows}
    atlas = by_url["https://theblock.co/atlas"]
    assert atlas["structural_critical"] is True
    assert atlas["structural_alerted"] is True
    assert "aave_defi" in atlas["lane_impact"]["lanes"]
    benign = by_url["https://dlnews.com/btc"]
    assert benign["structural_critical"] is False
