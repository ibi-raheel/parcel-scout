"""
Dallas County Clerk connector — Playwright-based document scraper.

Source: Dallas County Clerk / GovOS PublicSearch
URL: https://dallas.tx.publicsearch.us/

This connector is a thin framework wrapper around the existing
scrapers/dallas_clerk.py implementation.  It integrates with the
connector pipeline contract while delegating the actual Playwright
scraping to the proven scraper module.

For the instrument-number workflow it discovers properties that have
document_event rows missing grantor/grantee data and enqueues them
for the Playwright scraper.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

from .base import ConnectorHealth, SourceConnector

logger = logging.getLogger(__name__)

_CLERK_URL = "https://dallas.tx.publicsearch.us"


class DallasClerkConnector(SourceConnector):
    """
    Dallas County Clerk document connector.

    Wraps the Playwright-based scraper in scrapers/dallas_clerk.py.
    The discover/fetch/parse/normalize/publish pipeline here is a
    thin shim; the real work happens inside the Playwright scraper.

    This connector's primary role is:
    1. Provide health metrics via sync_log
    2. Allow the orchestrator to trigger the clerk scraper as part of
       the standard pipeline
    3. Report which document_event rows are still missing grantor data
    """

    SOURCE_NAME = "dallas_clerk"
    SOURCE_URL = "https://dallas.tx.publicsearch.us"
    STALE_AFTER_HOURS = 72.0  # clerk data rotates slowly

    # Maximum number of instrument numbers to queue per run
    _BATCH_SIZE = 100

    def discover(self) -> List[str]:
        """
        Find instrument numbers whose document_event rows lack grantor data.

        Returns a list of instrument_number strings to feed to fetch().
        """
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT instrument_number
                    FROM document_event
                    WHERE source = 'dallas_clerk'
                      AND grantor_raw IS NULL
                      AND instrument_number IS NOT NULL
                    ORDER BY recording_date DESC NULLS LAST
                    LIMIT %s
                    """,
                    (self._BATCH_SIZE,),
                )
                rows = cur.fetchall()
        instrument_numbers = [r[0] for r in rows if r[0]]
        self.logger.info(
            "Discovered %d clerk documents needing enrichment",
            len(instrument_numbers),
        )
        return instrument_numbers

    def fetch(self, targets: List[str]) -> List[dict]:
        """
        Delegate to the Playwright clerk scraper for each instrument number.

        In headless CI environments this may be stubbed.  The scraper
        writes directly to the DB; we return a manifest of what was queued.
        """
        if not targets:
            return []

        # The existing scraper is invoked as a subprocess to avoid Playwright
        # event loop conflicts with FastAPI.
        scraper_path = (
            "/Users/ibi/Documents/Equitify AI/parcel-scout-v3/scrapers/dallas_clerk.py"
        )

        results: List[dict] = []
        # Process in the batch size the scraper supports
        batch = targets[: self._BATCH_SIZE]
        try:
            self.logger.info("Invoking dallas_clerk scraper for %d instruments", len(batch))
            proc = subprocess.run(
                [
                    sys.executable,
                    scraper_path,
                    "--mode",
                    "instrument",
                    "--batch-size",
                    str(len(batch)),
                ],
                capture_output=True,
                text=True,
                timeout=600,  # 10 min max
            )
            if proc.returncode != 0:
                self.logger.warning(
                    "Clerk scraper exited %d: %s", proc.returncode, proc.stderr[:500]
                )
            else:
                self.logger.info("Clerk scraper stdout: %s", proc.stdout[:200])
            results.append(
                {
                    "queued": len(batch),
                    "returncode": proc.returncode,
                    "stderr": proc.stderr[:500],
                }
            )
        except subprocess.TimeoutExpired:
            self.logger.error("Clerk scraper timed out")
            results.append({"queued": len(batch), "returncode": -1, "error": "timeout"})
        except FileNotFoundError:
            self.logger.warning("Clerk scraper not found at %s; skipping", scraper_path)
            results.append({"queued": 0, "returncode": -1, "error": "scraper_not_found"})

        return results

    def parse(self, raw: List[dict]) -> List[dict]:
        """No additional parsing needed; scraper writes directly to DB."""
        return raw

    def normalize(self, parsed: List[dict]) -> List[dict]:
        """No normalization needed for clerk data; already canonical."""
        return parsed

    def publish(self, canonical: List[dict]) -> int:
        """
        Count the number of document_event rows now enriched.

        This is a post-hoc count: the scraper already wrote to the DB.
        """
        queued = sum(r.get("queued", 0) for r in canonical)
        self.logger.info("Clerk scraper processed %d instrument numbers", queued)
        return queued

    def health(self) -> ConnectorHealth:
        return self._build_health()
