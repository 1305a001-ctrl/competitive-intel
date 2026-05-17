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
[8 sources]──fetch_all──▶[classifier two-stage]──▶[watchlist match]──▶[gate]──▶[XADD pa:notifications]
                              │                                                       │
                              ▼                                                       ▼
                       ai-edge:8030                                            pa-agent → Telegram
                       /classify-news                                              (existing)
                       /sentiment
                              │
                              ▼
                  [signal-log.jsonl  append-only]
```

Sources currently watched:

- Aave governance forum (RSS)
- Chainlink blog (RSS)
- GMX governance forum (RSS)
- Hyperliquid Twitter (nitter RSS)
- DLNews (RSS)
- The Block (RSS)
- Polymarket Twitter (nitter RSS)
- DefiLlama `/protocols` — synthetic signal when |Δ24h| > 10% TVL for
  any tracked protocol

Each source is fail-OPEN: a timeout or 500 logs a warning and the rest
of the scan continues.

## v0.1 scope

- Daily skim only (4h interval) — weekly deep-read + monthly cycle are
  not yet implemented.
- 25 builds watched (T1.01–T1.10, T2.01–T2.06, T3.01–T3.05). See
  `data/watchlist.yaml`.
- Alert gate: type ∈ {DISPLACE, SUBSTITUTE}, confidence ≥ 0.7, and at
  least one matched build.
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
