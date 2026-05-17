"""Two-stage classifier.

Stage 1 — call ai-edge:8030 /classify-news to get a generic news label
  (asset list + urgency). This is shared with news-consolidator's
  prescreen pipeline; we keep using it because it's already calibrated.

Stage 2 — call ai-edge:8030 /sentiment with a competitive-intel-framework
  prompt riding in the body. The response is parsed into:
    {type: DISPLACE|UNLOCK|SUBSTITUTE|REGIME,
     confidence: 0..1,
     affected_categories: list[str],
     rationale: str}

Pure helpers (prompt build + JSON parse) are testable without a live LLM.
The async I/O wrapper is fail-OPEN: on any LLM error we return a low-
confidence default so the rest of the pipeline can continue (and the
scanner just won't fire an alert on this signal).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from competitive_intel.settings import settings

log = logging.getLogger(__name__)


VALID_TYPES: tuple[str, ...] = ("DISPLACE", "UNLOCK", "SUBSTITUTE", "REGIME")


COMP_INTEL_PROMPT_TEMPLATE = """\
You are a competitive intelligence analyst. Classify the following news
or governance signal into ONE of:
  DISPLACE - an incumbent infrastructure layer captures value previously available
  UNLOCK - a new primitive opens a market that didn't exist
  SUBSTITUTE - a competing product launches in our lane
  REGIME - the base rate of our edge changes (volatility, flows, regulation)

Output JSON ONLY with keys:
  type: one of those four
  confidence: 0-1
  affected_categories: list of strings (e.g. "liquidation", "polymarket", "hyperliquid")
  rationale: one short clause

Signal:
  source: {source}
  title: {title}
  body: {body}
"""


# ─── pure helpers ────────────────────────────────────────────────────


def build_comp_intel_prompt(source: str, title: str, body: str) -> str:
    """Pure: format the framework-classifier prompt. No I/O.

    Body is truncated to 1500 chars — most RSS summaries fit in <500.
    """
    return COMP_INTEL_PROMPT_TEMPLATE.format(
        source=(source or "")[:80],
        title=(title or "")[:300],
        body=(body or "")[:1500],
    )


_JSON_OBJ_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)


def parse_comp_intel_response(raw: str | dict | None) -> dict[str, Any]:
    """Pure: LLM raw output → normalised dict. Always returns the same
    shape, even on garbage input.

    Accepts:
      - dict (e.g. when LLM already returned JSON-parsed by upstream)
      - str  (raw model text — extracts first {...} substring)
      - None (treated as empty)
    """
    default = {
        "type": "REGIME",
        "confidence": 0.0,
        "affected_categories": [],
        "rationale": "",
    }
    if raw is None:
        return default

    obj: dict[str, Any] | None = None
    if isinstance(raw, dict):
        obj = raw
    elif isinstance(raw, str):
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                obj = None
        except (json.JSONDecodeError, ValueError):
            # try to find an embedded JSON object
            m = _JSON_OBJ_RE.search(raw)
            if m:
                try:
                    obj = json.loads(m.group(0))
                except (json.JSONDecodeError, ValueError):
                    obj = None
    if not isinstance(obj, dict):
        return default

    t = str(obj.get("type", "")).upper().strip()
    if t not in VALID_TYPES:
        t = "REGIME"

    try:
        conf = float(obj.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))

    cats_raw = obj.get("affected_categories") or []
    if isinstance(cats_raw, str):
        cats = [c.strip() for c in cats_raw.split(",") if c.strip()]
    elif isinstance(cats_raw, list):
        cats = [str(c).strip() for c in cats_raw if str(c).strip()]
    else:
        cats = []

    rationale = str(obj.get("rationale", "") or "").strip()[:280]

    return {
        "type": t,
        "confidence": conf,
        "affected_categories": cats,
        "rationale": rationale,
    }


# ─── LLM I/O (fail-OPEN) ────────────────────────────────────────────


async def call_classify_news(
    client: httpx.AsyncClient, title: str, body: str,
) -> dict[str, Any]:
    """Stage 1 — call /classify-news.

    Returns the parsed JSON from the endpoint, or a fail-OPEN default.
    The default has empty affected_assets and urgency=low so it can't
    trigger alerts on its own.
    """
    url = f"{settings.local_llm_base_url.rstrip('/')}/classify-news"
    try:
        r = await client.post(
            url,
            json={"headline": title[:500], "body": body[:2000]},
            timeout=settings.local_llm_timeout_sec,
        )
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("classify_news.failed err=%s", exc)
        return {"type": "other", "affected_assets": [], "urgency": "low", "ok": False}


async def call_comp_intel(
    client: httpx.AsyncClient, source: str, title: str, body: str,
) -> dict[str, Any]:
    """Stage 2 — call /sentiment with the comp-intel prompt embedded.

    The /sentiment endpoint already wraps Ollama with a JSON-mode
    response_format, so we ship the framework prompt as the headline+
    body and parse the resulting JSON object with our own parser.

    Fail-OPEN: returns a REGIME / confidence=0.0 stub on any error.
    """
    url = f"{settings.local_llm_base_url.rstrip('/')}/sentiment"
    prompt = build_comp_intel_prompt(source=source, title=title, body=body)
    try:
        r = await client.post(
            url,
            json={
                "headline": prompt[:500],
                "body": prompt[500:2500] if len(prompt) > 500 else "",
                "asset": "competitive_intel",
            },
            timeout=settings.local_llm_timeout_sec,
        )
        r.raise_for_status()
        raw = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("comp_intel.llm_failed err=%s", exc)
        return parse_comp_intel_response(None)
    # The /sentiment endpoint returns parsed sentiment dict — but when we
    # send a comp-intel prompt the model returns our schema; we look for
    # comp-intel keys, and fall back to the raw payload for parsing.
    candidate: str | dict[str, Any] | None = None
    if isinstance(raw, dict):
        # If the model output the comp-intel schema directly, parse will
        # pick it up. Otherwise try the rationale or raw fields.
        if any(k in raw for k in ("type", "affected_categories")):
            candidate = raw
        else:
            candidate = raw.get("rationale") or raw.get("raw") or raw
    else:
        candidate = raw  # type: ignore[assignment]
    return parse_comp_intel_response(candidate)


async def classify_signal(
    source: str, title: str, body: str,
) -> dict[str, Any]:
    """End-to-end: run both stages, merge into one classification.

    Returns:
        {
          "stage1": {...generic news classifier...},
          "stage2": {type, confidence, affected_categories, rationale},
          "type": str,         # alias of stage2.type
          "confidence": float, # alias of stage2.confidence
        }
    """
    async with httpx.AsyncClient(
        timeout=settings.local_llm_timeout_sec,
        follow_redirects=True,
    ) as client:
        stage1 = await call_classify_news(client, title, body)
        stage2 = await call_comp_intel(client, source, title, body)
    return {
        "stage1": stage1,
        "stage2": stage2,
        "type": stage2["type"],
        "confidence": stage2["confidence"],
    }
