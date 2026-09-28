"""
Dallas CAD (DCAD) appraisal data connector.

Source: Dallas Central Appraisal District bulk data files
URL: https://www.dallascad.org/DataSets.aspx

DCAD publishes annual CSV/MDB exports.  This connector downloads the
property/owner/value CSV exports and upserts into the canonical schema.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import zipfile
from datetime import datetime, timezone
from typing import List, Optional
from urllib.request import urlretrieve

import psycopg2
import psycopg2.extras

from .base import ConnectorHealth, SourceConnector

logger = logging.getLogger(__name__)

# DCAD publishes data sets for the current year; adjust URL per release cycle.
DCAD_BASE_URL = "https://www.dallascad.org/ViewHTM.aspx"


class DallasCADConnector(SourceConnector):
    """
    Dallas Central Appraisal District — property appraisal data.

    Pipeline:
        discover  — query DB for accounts that need refresh (or all on first run)
        fetch     — call DCAD search API per account (stubbed; replace with bulk)
        parse     — extract field values from raw API response
        normalize — map to property / valuation_snapshot canonical columns
        publish   — upsert into property + valuation_snapshot
    """

    SOURCE_NAME = "dallas_cad"
    SOURCE_URL = "https://www.dallascad.org/DataSets.aspx"
    STALE_AFTER_HOURS = 168.0  # weekly refresh is fine for CAD data

    # DCAD's public account-detail JSON endpoint (used for individual lookups)
    _API_ROOT = "https://www.dallascad.org/AcctDetailRes.aspx"

    def discover(self) -> List[str]:
        """Return up to 500 Dallas property account IDs that need refresh."""
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT spr.source_id
                    FROM source_property_ref spr
                    WHERE spr.source = 'dallas_cad'
                      AND (
                          spr.fetched_at IS NULL
                          OR spr.fetched_at < NOW() - INTERVAL '7 days'
                      )
                    LIMIT 500
                    """
                )
                rows = cur.fetchall()
        if not rows:
            # No accounts in DB yet — nothing to refresh on this run
            self.logger.info("No stale Dallas CAD accounts found")
            return []
        return [r[0] for r in rows]

    def fetch(self, targets: List[str]) -> List[dict]:
        """
        Fetch account details from DCAD.

        In production, replace with bulk CSV download.  Here we call the
        DCAD public HTML/JSON detail page for each account and extract
        embedded JSON data.
        """
        import urllib.request

        results: List[dict] = []
        for account_id in targets:
            url = f"{self._API_ROOT}?ID={account_id}"
            try:
                with urllib.request.urlopen(url, timeout=15) as resp:
                    raw_html = resp.read().decode("utf-8", errors="replace")
                results.append({"account_id": account_id, "raw_html": raw_html})
            except Exception as exc:
                self.logger.warning("Failed to fetch account %s: %s", account_id, exc)
        return results

    def parse(self, raw: List[dict]) -> List[dict]:
        """Extract structured fields from raw HTML responses."""
        import re

        parsed: List[dict] = []
        for record in raw:
            account_id = record.get("account_id", "")
            html = record.get("raw_html", "")

            # Attempt to extract market value using DCAD's known HTML pattern.
            # Pattern: "Market Value</td><td ...>$NNN,NNN</td>"
            market_value: Optional[float] = None
            mv_match = re.search(
                r"Market Value.*?\$([\d,]+)", html, re.IGNORECASE | re.DOTALL
            )
            if mv_match:
                try:
                    market_value = float(mv_match.group(1).replace(",", ""))
                except ValueError:
                    pass

            # Owner name
            owner_match = re.search(
                r"Owner Name.*?<td[^>]*>(.*?)</td>", html, re.IGNORECASE | re.DOTALL
            )
            owner_raw = owner_match.group(1).strip() if owner_match else None

            parsed.append(
                {
                    "account_id": account_id,
                    "market_value": market_value,
                    "owner_raw": owner_raw,
                    "tax_year": datetime.now(tz=timezone.utc).year,
                }
            )
        return parsed

    def normalize(self, parsed: List[dict]) -> List[dict]:
        """Map parsed records to (property_id, valuation) pairs."""
        canonical: List[dict] = []
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                for rec in parsed:
                    cur.execute(
                        """
                        SELECT property_id FROM source_property_ref
                        WHERE source = 'dallas_cad' AND source_id = %s
                        """,
                        (rec["account_id"],),
                    )
                    row = cur.fetchone()
                    if not row:
                        continue
                    canonical.append(
                        {
                            "property_id": str(row[0]),
                            "tax_year": rec["tax_year"],
                            "market_value": rec["market_value"],
                            "source": "dallas_cad",
                        }
                    )
        return canonical

    def publish(self, canonical: List[dict]) -> int:
        """Upsert valuation snapshots."""
        if not canonical:
            return 0
        written = 0
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                for rec in canonical:
                    if not rec.get("market_value"):
                        continue
                    cur.execute(
                        """
                        INSERT INTO valuation_snapshot
                            (property_id, tax_year, market_value, source, fetched_at)
                        VALUES (%s, %s, %s, %s, NOW())
                        ON CONFLICT (property_id, tax_year, source)
                        DO UPDATE SET
                            market_value = EXCLUDED.market_value,
                            fetched_at   = NOW()
                        """,
                        (
                            rec["property_id"],
                            rec["tax_year"],
                            rec["market_value"],
                            rec["source"],
                        ),
                    )
                    written += 1
            conn.commit()
        return written

    def health(self) -> ConnectorHealth:
        return self._build_health()
