"""Competitive intelligence service.

Continuously scans news/governance sources, classifies signals into the
competitive-intelligence-framework taxonomy (DISPLACE/UNLOCK/SUBSTITUTE/REGIME),
matches against per-build watchlists, and emits Telegram alerts when a
high-impact signal touches an active build.

v0.1.0 — daily skim only (4h scanner loop), 8 sources, 25 builds watched.
"""

__version__ = "0.1.0"
