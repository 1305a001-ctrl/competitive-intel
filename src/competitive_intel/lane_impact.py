"""Lane-impact scoring — the "lane-viability radar".

The original scanner only fired alerts when the *LLM* (ai-edge:8030) returned
a high-confidence DISPLACE/SUBSTITUTE label. That LLM is fail-OPEN: on any
error it returns ``confidence=0.0`` / ``parse_failed``. So a structural,
lane-killing event (e.g. "Chainlink acquires Atlas / Aave SVR goes live")
could be *collected* yet *never alerted* — exactly the Jan-2026 failure that
silently closed the Aave-liquidations lane.

This module is the fix. It is a **deterministic, rule-based** classifier that
does NOT depend on the LLM. Given a signal's text, it asks one question:

    Does this structural event threaten (or benefit) a venue we actually
    trade on, and through which mechanism?

Mechanisms ("event kinds") we recognise:
  - ORACLE      — an oracle/price-feed change (the Atlas/SVR mechanism)
  - ACQUISITION — an M&A / merger that can capture or close a value stream
  - REGULATORY  — a regulator / lawsuit / enforcement action against a venue
  - DELISTING   — a delisting / deprecation / sunset of a market we trade
  - GOVERNANCE  — a governance parameter / risk-param / listing-policy change
  - LISTING     — a new listing on a venue we trade

Lanes are the venues the stack actually trades, keyed to the watchlist
build IDs they map to:
  - aave_defi      → Aave / GMX / Morpho liquidations + funding (T1.02, T1.06, T3.05)
  - polymarket     → Polymarket prediction markets (T1.01, T1.07, T2.05)
  - hyperliquid    → HYPE / Hyperliquid perps + basis (T1.10, T2.03, T3.01)
  - tokenized_eq   → tokenized equities (T1.04, T3.04)
  - oracle_infra   → oracle-infra dependencies shared across lanes (T1.03, T1.05, T2.01)

The output is a :class:`LaneImpact` verdict. ``critical=True`` means at least
one lane is threatened with a severity at/above the alert floor — that is what
the structural-alert layer fires on, *independently* of the LLM gate.

Everything here is pure (no I/O) so it is exhaustively unit-testable, including
the real Atlas example.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ─── lane catalogue ─────────────────────────────────────────────────
# A lane = a venue we trade. ``aliases`` are the venue/protocol names that, if
# they appear in a signal, mean "this touches that lane". ``builds`` are the
# watchlist build IDs the lane maps to (so an alert can name the affected work).

LANES: dict[str, dict[str, Any]] = {
    "aave_defi": {
        "label": "Aave/GMX DeFi (liquidations + funding)",
        "aliases": (
            "aave", "aavedao", "gmx", "morpho", "compound", "fluid", "silo",
            "spark protocol", "spark lend",
        ),
        "builds": ("T1.02", "T1.06", "T3.05"),
    },
    "polymarket": {
        "label": "Polymarket prediction markets",
        "aliases": ("polymarket", "uma oracle", "uma optimistic oracle", "polygon clob"),
        "builds": ("T1.01", "T1.07", "T2.05"),
    },
    "hyperliquid": {
        "label": "HYPE / Hyperliquid perps",
        "aliases": ("hyperliquid", "hyperevm", "hype token", "$hype", "hypercore"),
        "builds": ("T1.10", "T2.03", "T3.01"),
    },
    "tokenized_eq": {
        "label": "Tokenized equities",
        "aliases": (
            "xstocks", "backed finance", "tokenized equit", "tokenized stock",
            "tokenised equit", "tokenised stock", "ondo", "dinari",
        ),
        "builds": ("T1.04", "T3.04"),
    },
    "oracle_infra": {
        "label": "Shared oracle infrastructure",
        "aliases": (
            "chainlink", "pyth", "redstone", "data streams", "data feed",
            "price feed", "ncfx", "lwba",
        ),
        "builds": ("T1.03", "T1.05", "T2.01"),
    },
}

# Lanes for which an oracle-infra change is *existential* (their edge is built
# directly on the oracle). An ORACLE event mentioning an oracle provider
# propagates to these lanes even if the lane's own name is absent — this is the
# "indirect" hop that the Atlas signal needed (CL acquisition → Aave lane).
ORACLE_DEPENDENT_LANES: tuple[str, ...] = ("aave_defi", "polymarket", "oracle_infra")


# ─── event-kind detectors (pure, keyword/regex) ─────────────────────
# Each kind has a severity weight in [0,1]; higher = more likely lane-altering.

_ORACLE_PATTERNS = (
    r"\bsvr\b",
    r"smart\s+value\s+recapture",
    r"\boracle\b",
    r"price\s+feed",
    r"data\s+stream",
    r"data\s+feed",
    r"oev\b",
    r"oracle\s+extractable",
    r"feed\s+migration",
    r"\bdepeg",
)
_ACQUISITION_PATTERNS = (
    r"\bacquir(?:e|es|ed|ing)\b",
    r"\bacquisition\b",
    r"\bmerg(?:e|es|er|ed|ing)\b",
    r"\bto\s+buy\b",
    r"\bbuyout\b",
    r"\btakeover\b",
)
_GOVERNANCE_PATTERNS = (
    r"\barfc\b",
    r"\btemp\s*check\b",
    r"\bgovernance\s+proposal\b",
    r"\bproposal\b",
    r"risk\s+param",
    r"parameter\s+(?:change|update)",
    r"\bonboard(?:ing|ed)?\b",
    r"\bvote\b",
    r"\bquorum\b",
)
# A *delisting* / deprecation / sunset removes a market we may be trading and
# is materially more lane-critical than a *new* listing. Split the two so the
# severity reflects that.
_DELISTING_PATTERNS = (
    r"\bdelist(?:s|ed|ing)?\b",
    r"\bde-list",
    r"\bdeprecat(?:e|es|ed|ing|ion)\b",
    r"\bsunset(?:s|ting)?\b",
    r"\bwind(?:s|ing)?\s+down\b",
    r"\bremov(?:e|es|ed|ing)\s+(?:the\s+)?market",
)
_LISTING_PATTERNS = (
    r"\blist(?:s|ed|ing)?\b",
    r"\bnew\s+market\b",
    r"\blaunch(?:es|ed|ing)?\b",
)
_REGULATORY_PATTERNS = (
    r"\bsec\b",
    r"\bcftc\b",
    r"\bmas\b",
    r"\blawsuit\b",
    r"\benforcement\b",
    r"\bsubpoena\b",
    r"\bregulat(?:e|es|ed|ion|or|ory)\b",
    r"\bsanction(?:s|ed)?\b",
    r"\bcease\s+and\s+desist\b",
    r"\bban(?:s|ned|ning)?\b",
)

# kind -> (compiled patterns, base severity)
_EVENT_KINDS: dict[str, tuple[tuple[re.Pattern[str], ...], float]] = {
    "ACQUISITION": (tuple(re.compile(p, re.I) for p in _ACQUISITION_PATTERNS), 0.9),
    "ORACLE": (tuple(re.compile(p, re.I) for p in _ORACLE_PATTERNS), 0.8),
    "REGULATORY": (tuple(re.compile(p, re.I) for p in _REGULATORY_PATTERNS), 0.75),
    "DELISTING": (tuple(re.compile(p, re.I) for p in _DELISTING_PATTERNS), 0.7),
    "GOVERNANCE": (tuple(re.compile(p, re.I) for p in _GOVERNANCE_PATTERNS), 0.5),
    "LISTING": (tuple(re.compile(p, re.I) for p in _LISTING_PATTERNS), 0.45),
}

# Default severity floor at/above which a lane impact is treated as "critical"
# (alert-worthy). Overridable via settings.
DEFAULT_CRITICAL_SEVERITY: float = 0.6


@dataclass(frozen=True)
class LaneImpact:
    """Verdict for one signal.

    Attributes
    ----------
    lanes:
        Lane keys this signal touches, sorted by descending severity.
    event_kinds:
        Detected structural mechanisms (e.g. ["ACQUISITION", "ORACLE"]).
    severity:
        Max per-lane severity in [0,1].
    critical:
        ``severity >= floor`` and at least one lane matched.
    builds:
        Watchlist build IDs implicated by the matched lanes (deduped, stable).
    rationale:
        Short human-readable clause for the alert.
    per_lane:
        ``{lane: severity}`` for transparency / persistence.
    """

    lanes: tuple[str, ...] = ()
    event_kinds: tuple[str, ...] = ()
    severity: float = 0.0
    critical: bool = False
    builds: tuple[str, ...] = ()
    rationale: str = ""
    per_lane: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lanes": list(self.lanes),
            "event_kinds": list(self.event_kinds),
            "severity": self.severity,
            "critical": self.critical,
            "builds": list(self.builds),
            "rationale": self.rationale,
            "per_lane": dict(self.per_lane),
        }


# ─── pure helpers ───────────────────────────────────────────────────


# Body-only structural matches are discounted: a lane-killing event announces
# itself in the *headline*, whereas long governance bodies frequently contain
# words like "launch" / "merge" / "oracle" incidentally. A structural mechanism
# found only in the body (not the title/categories) is scaled by this factor.
BODY_ONLY_DISCOUNT: float = 0.6


def _salient_text(signal: dict[str, Any]) -> str:
    """Lowercase title + source + affected_categories.

    This is the "headline" surface where structural events announce
    themselves. The LLM's ``affected_categories`` count as salient because
    they are a distilled judgement, not incidental prose.
    """
    parts: list[str] = []
    for k in ("title", "source"):
        v = signal.get(k)
        if v:
            parts.append(str(v))
    cats = signal.get("affected_categories")
    if isinstance(cats, list):
        parts.extend(str(c) for c in cats)
    elif isinstance(cats, str):
        parts.append(cats)
    return "\n".join(parts).lower()


def _haystack(signal: dict[str, Any]) -> str:
    """Lowercase text blob: title + body + source + affected_categories."""
    parts: list[str] = []
    for k in ("title", "body", "source"):
        v = signal.get(k)
        if v:
            parts.append(str(v))
    cats = signal.get("affected_categories")
    if isinstance(cats, list):
        parts.extend(str(c) for c in cats)
    elif isinstance(cats, str):
        parts.append(cats)
    return "\n".join(parts).lower()


def detect_event_kinds(text: str) -> list[tuple[str, float]]:
    """Pure: which structural mechanisms appear in ``text``.

    Returns ``[(kind, base_severity), ...]`` sorted by descending severity.
    """
    hits: list[tuple[str, float]] = []
    for kind, (patterns, sev) in _EVENT_KINDS.items():
        if any(p.search(text) for p in patterns):
            hits.append((kind, sev))
    hits.sort(key=lambda kv: kv[1], reverse=True)
    return hits


def detect_lanes(text: str) -> list[str]:
    """Pure: which lanes are named (directly) in ``text``.

    Sorted by lane-catalogue insertion order (stable).
    """
    matched: list[str] = []
    for lane, spec in LANES.items():
        for alias in spec["aliases"]:
            if alias in text:
                matched.append(lane)
                break
    return matched


def score_lane_impact(
    signal: dict[str, Any],
    *,
    critical_severity: float | None = None,
) -> LaneImpact:
    """Pure: score one signal for lane impact.

    Algorithm
    ---------
    1. Detect structural event kinds (ACQUISITION/ORACLE/REGULATORY/...).
       Kinds found in the *title/categories* score at full weight; kinds found
       only in the *body* are discounted (long governance bodies contain
       "launch"/"merge"/"oracle" incidentally). No structural kind → severity 0.
    2. Detect directly-named lanes.
    3. **Indirect hop**: if the event is an ORACLE/ACQUISITION event that names
       an oracle provider (Chainlink/Pyth/...), propagate to the
       oracle-dependent lanes even when those lanes are not named. This is the
       hop the Atlas headline needs: "Chainlink acquires Atlas (SVR)" names no
       lane explicitly, yet kills the Aave-liquidations lane.
    4. Per-lane severity = max effective severity of the matched kinds, with a
       small boost when an oracle event hits an oracle-dependent lane (the edge
       sits directly on the oracle) and a small penalty for purely-indirect hits.
    5. ``critical`` iff max severity ≥ floor and ≥1 lane matched.
    """
    floor = (
        critical_severity if critical_severity is not None else DEFAULT_CRITICAL_SEVERITY
    )
    salient = _salient_text(signal)
    text = _haystack(signal)

    # Kinds in the headline/categories at full weight; body-only at a discount.
    salient_kinds = dict(detect_event_kinds(salient))
    all_kinds = dict(detect_event_kinds(text))
    if not all_kinds:
        return LaneImpact(rationale="no structural event detected")

    effective: dict[str, float] = {}
    for kind, base in all_kinds.items():
        if kind in salient_kinds:
            effective[kind] = base
        else:
            effective[kind] = round(base * BODY_ONLY_DISCOUNT, 3)

    # event_kinds ordered by effective severity (desc) for stable, useful output
    kind_names = tuple(
        sorted(effective, key=lambda k: effective[k], reverse=True)
    )
    max_kind_sev = max(effective.values())
    # an oracle/acquisition event is only a credible *indirect-hop* trigger when
    # the mechanism is salient (in the headline), not buried in body prose.
    is_oracle_event = "ORACLE" in salient_kinds or "ACQUISITION" in salient_kinds
    names_oracle_provider = any(a in text for a in LANES["oracle_infra"]["aliases"])

    direct_lanes = detect_lanes(text)
    per_lane: dict[str, float] = {}

    # direct hits get full kind severity
    for lane in direct_lanes:
        boost = 0.1 if (is_oracle_event and lane in ORACLE_DEPENDENT_LANES) else 0.0
        per_lane[lane] = min(1.0, max_kind_sev + boost)

    # indirect oracle hop: oracle event + named oracle provider → propagate to
    # oracle-dependent lanes not already hit directly.
    if is_oracle_event and names_oracle_provider:
        for lane in ORACLE_DEPENDENT_LANES:
            if lane in per_lane:
                continue
            # purely-indirect → slight discount, but still alert-worthy for the
            # high-severity kinds (acquisition/oracle) so Atlas clears the floor.
            per_lane[lane] = min(1.0, max_kind_sev - 0.05)

    if not per_lane:
        return LaneImpact(
            event_kinds=kind_names,
            severity=0.0,
            rationale=(
                f"structural event ({', '.join(kind_names)}) but no lane touched"
            ),
        )

    lanes_sorted = tuple(
        sorted(per_lane, key=lambda lane: per_lane[lane], reverse=True)
    )
    severity = max(per_lane.values())

    builds: list[str] = []
    seen: set[str] = set()
    for lane in lanes_sorted:
        for bid in LANES[lane]["builds"]:
            if bid not in seen:
                seen.add(bid)
                builds.append(bid)

    top_lane = lanes_sorted[0]
    rationale = (
        f"{'/'.join(kind_names)} event impacts {LANES[top_lane]['label']}"
        f" (severity {severity:.2f})"
    )

    return LaneImpact(
        lanes=lanes_sorted,
        event_kinds=kind_names,
        severity=round(severity, 3),
        critical=severity >= floor,
        builds=tuple(builds),
        rationale=rationale,
        per_lane={k: round(v, 3) for k, v in per_lane.items()},
    )
