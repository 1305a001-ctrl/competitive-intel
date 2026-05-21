# competitive-intel

Continuous competitive intelligence scanner. Reads governance forums,
oracle blogs, prediction-market Twitter, DefiLlama TVL deltas, and DeFi
news; classifies each signal into the four-type taxonomy from the
[Competitive Intelligence Framework](./docs/framework.md) (DISPLACE /
UNLOCK / SUBSTITUTE / REGIME); matches against per-build watchlists; and
alerts via the `pa:notifications` Redis stream when a high-impact signal
touches an active build.

Built to prevent the failure mode that nearly hit the operator on the
Aave SVR/Atlas expansion: a structural shift was public on the
governance forum for 6+ weeks before it caught attention. This service
runs that check continuously.

## Architecture

```
[12 sources]─fetch_all─▶[classifier two-stage]──▶[watchlist match]──▶[LLM gate]──▶[XADD pa:notifications]
      │                       │                                                          │
      │                       ▼                                                          ▼
      │                ai-edge:8030                                            pa-agent → Telegram
      │                /classify-news                                              (existing)
      │                /sentiment
      │                       │
      │                       ▼
      │            [signal-log.jsonl append-only]  ◀── + lane_impact verdict per row
      │
      └──▶[lane-impact scorer (deterministic, no LLM)]──critical?──▶[news:structural_alert:<lane>]
                                                                     [news:structural_alert:latest]
                                                                              │
                                                                     (outbound GATED OFF
                                                                      by default — dry-run)
```

Sources currently watched:

Generic / blog (v0.1):

- Chainlink blog (RSS)
- Hyperliquid Twitter (nitter RSS)
- DLNews (RSS)
- The Block (RSS)
- Polymarket Twitter (nitter RSS)
- Aave + GMX governance forums (legacy per-category `.rss`)
- DefiLlama `/protocols` — synthetic signal when |Δ24h| > 10% TVL for
  any tracked protocol

Venue coverage (slice a — the stack's OWN trading venues, free APIs only):

- **Snapshot GraphQL** (`hub.snapshot.org/graphql`) — Aave (`aavedao.eth`)
  + GMX (`gmx.eth`) governance proposals, the moment they're posted.
- **Discourse `latest.json`** — Aave (`governance.aave.com`) + GMX
  (`gov.gmx.io`) forums (stable JSON feed; more robust than `.rss`).
- **GDELT DOC 2.0** (`api.gdeltproject.org/api/v2/doc/doc`) — free global
  news index, queried for structural events (M&A / regulatory / oracle /
  delisting) naming one of our venues.

Toggle venue coverage with `VENUE_COVERAGE_ENABLED` (default on; additive —
it only adds sources, never changes the original 8).

Each source is fail-OPEN: a timeout or 500 logs a warning and the rest
of the scan continues.

## Lane-viability radar (structural-event alerts)

The v0.1 alert gate only fired when the **LLM** returned a high-confidence
DISPLACE/SUBSTITUTE label. That LLM is fail-OPEN (`confidence=0.0` on any
error), so a structural, lane-killing event — e.g. *"Chainlink acquires
Atlas / Aave SVR goes live"* — could be **collected yet never alerted**.
That is the exact Jan-2026 failure that silently closed the Aave-liquidations
lane.

`lane_impact.py` is the fix: a **deterministic, rule-based** scorer (no LLM
dependency) that asks *"does this structural event threaten a venue we
trade, and through which mechanism?"* Mechanisms: `ACQUISITION`, `ORACLE`,
`REGULATORY`, `DELISTING`, `GOVERNANCE`, `LISTING`. Lanes map to watchlist
build IDs. An oracle/acquisition event naming an oracle provider
(Chainlink/Pyth/...) propagates to oracle-dependent lanes even when the lane
isn't named — the indirect hop the Atlas headline needed.

When a verdict clears the severity floor (`STRUCTURAL_ALERT_MIN_SEVERITY`,
default 0.6) the scanner:

1. persists a structured record to `news:structural_alert:<lane>` (stream)
   + `news:structural_alert:latest` (string),
2. annotates the JSONL signal-log row with the lane-impact verdict,
3. **only** sends an outbound pa-agent/Telegram notification if
   `STRUCTURAL_ALERT_OUTBOUND_ENABLED=true` (default **false** — dry-run, so
   deploying this can't spam the operator until the radar is observed live).

## Scope

- Daily skim only (4h interval) — weekly deep-read + monthly cycle are
  not yet implemented.
- 25 builds watched (T1.01–T1.10, T2.01–T2.06, T3.01–T3.05). See
  `data/watchlist.yaml`.
- LLM alert gate: type ∈ {DISPLACE, SUBSTITUTE}, confidence ≥ 0.7, and at
  least one matched build → `pa:notifications`.
- Lane-viability radar (slice a): deterministic structural-event scorer →
  `news:structural_alert:*`. Outbound notification gated off by default.
- Idempotency: Redis SET `competitive_intel:seen_urls` dedupes URLs
  across scans. Soft cap 50k entries.

## Running

```bash
pip install -e .[dev]
pytest -q
python -m competitive_intel.main
```

Required env vars (see `src/competitive_intel/settings.py`):

- `REDIS_URL` — must point at the Redis instance pa-agent consumes
- `LOCAL_LLM_BASE_URL` — defaults to `http://ai-edge:8030`
- `WATCHLIST_PATH` — defaults to `/app/data/watchlist.yaml`
- `SIGNAL_LOG_PATH` — defaults to `/var/lib/competitive-intel/signal-log.jsonl`
- `VENUE_COVERAGE_ENABLED` — add Snapshot/Discourse/GDELT venue sources
  (default `true`)
- `STRUCTURAL_ALERT_MIN_SEVERITY` — lane-impact alert floor (default `0.6`)
- `STRUCTURAL_ALERT_OUTBOUND_ENABLED` — send structural alerts outbound to
  pa-agent/Telegram (default `false` — dry-run; persist + log only)

## Deploy

The `docker/docker-compose.snippet.yml` slots into ai-primary's stack
alongside the existing 24 containers. Image rolls out via:

```bash
docker compose pull competitive-intel && docker compose up -d competitive-intel
```

After deploy, pin to digest with the standard helper:

```bash
/Users/benedict/CommandCenter/_skills/_helpers/pin-compose-to-digest.sh \
    --service competitive-intel
```

## Watchlist update workflow

The framework expects watchlists to be updated weekly. To add or modify
a build:

1. Edit `data/watchlist.yaml`.
2. The scanner reloads on every cycle (no restart).
3. Commit the change; the container's volume mount picks up the new
   file automatically because we bind the YAML in read-only.

## Signal log

Every signal — alerted or not — appends one JSON row to
`signal-log.jsonl`. Use this for the weekly framework review (§1).
`signal_log.read_all(path)` returns the parsed list for tooling.
