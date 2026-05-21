"""Competitive intelligence service.

Continuously scans news/governance sources, classifies signals into the
competitive-intelligence-framework taxonomy (DISPLACE/UNLOCK/SUBSTITUTE/REGIME),
matches against per-build watchlists, and emits Telegram alerts when a
high-impact signal touches an active build.

It also runs a deterministic "lane-viability radar" (lane_impact.py): a
structural-event scorer — independent of the LLM — that catches lane-killing
events (oracle changes, acquisitions, governance/listing changes, regulatory
actions) on the venues we trade and emits structured alerts to the
`news:structural_alert:*` Redis namespace.

v0.2.0 — daily skim (4h loop); 12 sources incl. Snapshot/Discourse/GDELT
venue coverage; 25 builds watched; lane-viability radar (outbound gated off).
"""

__version__ = "0.2.0"
