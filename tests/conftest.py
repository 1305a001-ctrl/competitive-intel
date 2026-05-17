"""Shared pytest fixtures."""
from __future__ import annotations

import pytest

from competitive_intel.watchlist import Build


@pytest.fixture
def sample_watchlist() -> dict[str, Build]:
    return {
        "T1.01": Build(
            id="T1.01",
            name="Polymarket Compound Engine",
            track=1,
            status="active",
            upstream=("polymarket", "uma oracle", "usdc peg"),
            substitutes=("kalshi", "limitless"),
            keywords=("prediction market",),
            existential=("polymarket 5m",),
        ),
        "T1.02": Build(
            id="T1.02",
            name="Liquidation Bot",
            track=1,
            status="degraded",
            upstream=("aave v3", "chainlink data streams"),
            substitutes=("atlas", "hexagate"),
            keywords=("svr", "liquidation"),
            existential=("svr expansion",),
        ),
        "T2.03": Build(
            id="T2.03",
            name="HYPE Infrastructure Layer",
            track=2,
            status="re_evaluating",
            upstream=("hyperliquid labs", "hyperevm"),
            substitutes=("felix protocol",),
            keywords=("hyperliquid native lending",),
            existential=(),
        ),
    }
