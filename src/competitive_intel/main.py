"""Service entrypoint.

Run with `python -m competitive_intel.main`.
"""
from __future__ import annotations

import asyncio
import logging

from competitive_intel import scanner


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> None:
    _setup_logging()
    log = logging.getLogger("competitive_intel.main")
    log.info("competitive-intel starting")
    asyncio.run(scanner.run_loop())


if __name__ == "__main__":
    main()
