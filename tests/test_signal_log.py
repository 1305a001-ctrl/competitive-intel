"""Tests for the append-only signal log."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from competitive_intel.signal_log import append_row, build_log_row, read_all


def _sample_classification(t="DISPLACE", conf=0.83):
    return {
        "stage1": {"type": "oracle", "affected_assets": ["BTC"], "urgency": "high"},
        "stage2": {
            "type": t,
            "confidence": conf,
            "affected_categories": ["liquidation"],
            "rationale": "test",
        },
        "type": t,
        "confidence": conf,
    }


def test_build_log_row_normalises_shape():
    sig = {
        "source": "aave_governance",
        "title": "x",
        "url": "https://example.com/1",
        "published_at": "2026-05-17T00:00:00+00:00",
    }
    row = build_log_row(
        signal=sig,
        classification=_sample_classification(),
        matched_builds=["T1.02"],
        alerted=True,
        now=datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC),
    )
    assert row["ts"] == "2026-05-17T12:00:00+00:00"
    assert row["url"] == "https://example.com/1"
    assert row["type"] == "DISPLACE"
    assert row["confidence"] == 0.83
    assert row["matched_builds"] == ["T1.02"]
    assert row["alerted"] is True
    assert row["affected_categories"] == ["liquidation"]


def test_append_row_writes_one_jsonl_line(tmp_path):
    path = tmp_path / "log.jsonl"
    row = build_log_row(
        signal={"source": "x", "title": "y", "url": "u", "published_at": ""},
        classification=_sample_classification(),
        matched_builds=[],
        alerted=False,
    )
    append_row(path, row)
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert text.count("\n") == 1
    parsed = json.loads(text.strip())
    assert parsed["url"] == "u"


def test_append_row_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "deeper" / "log.jsonl"
    row = build_log_row(
        signal={"source": "x", "title": "y", "url": "u", "published_at": ""},
        classification=_sample_classification(),
        matched_builds=[],
        alerted=False,
    )
    append_row(path, row)
    assert path.exists()


def test_append_row_is_append_only(tmp_path):
    path = tmp_path / "log.jsonl"
    for i in range(5):
        row = build_log_row(
            signal={"source": "x", "title": f"y{i}", "url": f"u{i}", "published_at": ""},
            classification=_sample_classification(),
            matched_builds=[],
            alerted=False,
        )
        append_row(path, row)
    rows = read_all(path)
    assert len(rows) == 5
    assert [r["url"] for r in rows] == [f"u{i}" for i in range(5)]


def test_read_all_skips_malformed_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(
        '{"ts": "a", "url": "u1"}\n'
        "this is not json\n"
        '{"ts": "b", "url": "u2"}\n',
        encoding="utf-8",
    )
    rows = read_all(path)
    assert len(rows) == 2
    assert [r["url"] for r in rows] == ["u1", "u2"]


def test_read_all_handles_missing_file(tmp_path):
    assert read_all(tmp_path / "absent.jsonl") == []
