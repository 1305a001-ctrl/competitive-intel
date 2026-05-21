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

    # --- venue coverage (slice a) ---
    # Master switch for the new venue fetchers (Snapshot/Discourse/GDELT).
    # ON by default in code — but additive: it only ADDS sources, never
    # changes the existing 8. Set false to disable venue coverage entirely.
    venue_coverage_enabled: bool = True
    # Snapshot GraphQL hub (free, no key).
    src_snapshot_graphql: str = "https://hub.snapshot.org/graphql"
    # Governance spaces to watch. aavedao.eth = Aave DAO (944 proposals),
    # gmx.eth = GMX (75). Add more space IDs as lanes grow.
    snapshot_spaces: tuple[str, ...] = ("aavedao.eth", "gmx.eth")
    # Aave governance forum (Discourse). latest.json is the stable JSON feed.
    src_aave_discourse_latest: str = "https://governance.aave.com/latest.json"
    src_aave_discourse_base: str = "https://governance.aave.com"
    # GMX governance forum (Discourse).
    src_gmx_discourse_latest: str = "https://gov.gmx.io/latest.json"
    src_gmx_discourse_base: str = "https://gov.gmx.io"
    # GDELT DOC 2.0 free news index. Query targets structural events naming
    # one of our venues; timespan keeps the feed recent.
    src_gdelt_doc: str = "https://api.gdeltproject.org/api/v2/doc/doc"
    gdelt_query: str = (
        '(polymarket OR hyperliquid OR aave OR chainlink OR "tokenized equities") '
        "(acquisition OR oracle OR regulation OR delisting OR lawsuit OR sec)"
    )
    gdelt_timespan: str = "3d"

    # --- structural-event lane-impact alert layer (slice a) ---
    # Severity floor at/above which a lane impact is treated as alert-worthy.
    structural_alert_min_severity: float = 0.6
    # Redis namespaces for structural alerts (registered in signals_bus.md).
    # Per-lane stream of structural alerts.
    structural_alert_stream_prefix: str = "news:structural_alert"
    # Current-state string: the most recent structural alert (any lane).
    structural_alert_latest_key: str = "news:structural_alert:latest"
    # Stream cap (approximate XADD maxlen).
    structural_alert_stream_maxlen: int = 5_000
    # GATE: when False (default) structural alerts are logged + persisted to
    # Redis + the signal log, but NO outbound notification (pa-agent/Telegram)
    # is sent. Flip to true ONLY after the radar has been observed in prod.
    # This is what keeps merging this PR from spamming the operator.
    structural_alert_outbound_enabled: bool = False

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
