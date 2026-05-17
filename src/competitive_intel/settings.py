"""Env-driven settings.

Per-source URLs are configurable so a unit test or staging instance can
swap RSS endpoints. The defaults match the production targets from §5 of
the framework.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- scanner cadence ---
    # daily skim interval (seconds). default 4h per spec.
    skim_interval_sec: int = 4 * 60 * 60
    # max articles per source per scan (defensive cap)
    max_per_source: int = 50
    # per-request HTTP timeout
    http_timeout_sec: float = 15.0

    # --- LLM (ai-edge:8030) ---
    local_llm_base_url: str = "http://ai-edge:8030"
    local_llm_timeout_sec: float = 30.0

    # --- watchlist / log paths ---
    watchlist_path: str = "/app/data/watchlist.yaml"
    signal_log_path: str = "/var/lib/competitive-intel/signal-log.jsonl"

    # --- Redis ---
    redis_url: str = "redis://localhost:6379"
    # pa-agent notification stream — the channel pa-agent already drains
    pa_notifications_stream: str = "pa:notifications"
    # idempotency: seen-url SET (we use a Redis SET, not a stream)
    seen_urls_key: str = "competitive_intel:seen_urls"
    # cap on the SET size (LRU-style trim is approximate via SRANDMEMBER scan)
    seen_urls_max: int = 50_000

    # --- alert gating ---
    alert_min_confidence: float = 0.7
    alert_types: tuple[str, ...] = ("DISPLACE", "SUBSTITUTE")

    # --- Source URLs (RSS / HTTP) ---
    # Aave governance forum (Discourse instance — Discourse exposes .rss
    # on every category URL).
    src_aave_governance: str = "https://governance.aave.com/c/development/26.rss"
    # Chainlink blog
    src_chainlink_blog: str = "https://blog.chain.link/rss/"
    # GMX governance forum
    src_gmx_governance: str = "https://gov.gmx.io/latest.rss"
    # Hyperliquid Twitter via a nitter instance (configurable; nitter
    # availability varies — fail-OPEN handles outages).
    src_hyperliquid_twitter: str = "https://nitter.net/HyperliquidX/rss"
    # DLNews
    src_dlnews: str = "https://www.dlnews.com/arc/outboundfeeds/rss/"
    # The Block
    src_theblock: str = "https://www.theblock.co/rss.xml"
    # Polymarket Twitter (nitter)
    src_polymarket_twitter: str = "https://nitter.net/Polymarket/rss"
    # DefiLlama TVL changes — protocol pages for tracked venues. We hit
    # the public protocols API and surface large 24h TVL deltas as a
    # synthetic signal.
    src_defillama_protocols: str = "https://api.llama.fi/protocols"
    # Protocols we follow (DefiLlama slug). Sub-set of upstream deps.
    defillama_protocols: tuple[str, ...] = (
        "aave-v3", "morpho", "fluid", "compound-v3", "silo",
        "gmx-v2-perps", "hyperliquid", "polymarket",
    )
    # |delta24h| in % to surface as a signal
    defillama_tvl_delta_pct: float = 10.0


settings = Settings()  # type: ignore[call-arg]
