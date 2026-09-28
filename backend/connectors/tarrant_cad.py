"""
Tarrant CAD (TAD) appraisal data connector.

Source: Tarrant Appraisal District
URL: https://www.tad.org/data/

TAD publishes bulk CSV exports.  This connector queries properties tagged
county='Tarrant' and refreshes valuations from TAD's public property
search endpoint.
"""

from __future__ import annotations

import re
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

import psycopg2
import psycopg2.extras

from .base import ConnectorHealth, SourceConnector


class TarrantCADConnector(SourceConnector):
    """
    Tarrant Appraisal District — property appraisal data.

    Pipeline mirrors DallasCADConnector; source URL and account ID
    format differ for Tarrant County properties.
    """

    SOURCE_NAME = "tarrant_cad"
    SOURCE_URL = "https://www.tad.org/data/"
    STALE_AFTER_HOURS = 168.0  # weekly

    _API_ROOT = "https://www.tad.org/property-search/"

    def discover(self) -> List[str]:
        """Return up to 500 Tarrant property account IDs needing refresh."""
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT spr.source_id
                    FROM source_property_ref spr
                    WHERE spr.source = 'tarrant_cad'
                      AND (
                          spr.fetched_at IS NULL
                          OR spr.fetched_at < NOW() - INTERVAL '7 days'
                      )
                    LIMIT 500
                    """
                )
                rows = cur.fetchall()
        if not rows:
            self.logger.info("No stale Tarrant CAD accounts found")
            return []
        return [r[0] for r in rows]

    def fetch(self, targets: List[str]) -> List[dict]:
        """
        Fetch account details from TAD public search.

        TAD uses a form-based search; in production replace with bulk
        XML/CSV download from the annual data release.
        """
        results: List[dict] = []
        for account_id in targets:
            url = f"{self._API_ROOT}?acct={account_id}"
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "ParcelScout/3.0 (data research)"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
                results.append({"account_id": account_id, "raw_html": html})
            except Exception as exc:
                self.logger.warning(
                    "Failed to fetch TAD account %s: %s", account_id, exc
                )
        return results

    def parse(self, raw: List[dict]) -> List[dict]:
        """Extract appraisal values from TAD HTML responses."""
        parsed: List[dict] = []
        for record in raw:
            account_id = record.get("account_id", "")
            html = record.get("raw_html", "")

            market_value: Optional[float] = None
            land_value: Optional[float] = None
            improvement_value: Optional[float] = None

            mv_match = re.search(
                r"Appraised Value.*?\$([\d,]+)", html, re.IGNORECASE | re.DOTALL
            )
            if mv_match:
                try:
                    market_value = float(mv_match.group(1).replace(",", ""))
                except ValueError:
                    pass

            lv_match = re.search(
                r"Land Value.*?\$([\d,]+)", html, re.IGNORECASE | re.DOTALL
            )
            if lv_match:
                try:
                    land_value = float(lv_match.group(1).replace(",", ""))
                except ValueError:
                    pass

            iv_match = re.search(
                r"Improvement Value.*?\$([\d,]+)", html, re.IGNORECASE | re.DOTALL
            )
            if iv_match:
                try:
                    improvement_value = float(iv_match.group(1).replace(",", ""))
                except ValueError:
                    pass

            parsed.append(
                {
                    "account_id": account_id,
                    "market_value": market_value,
                    "land_value": land_value,
                    "improvement_value": improvement_value,
                    "tax_year": datetime.now(tz=timezone.utc).year,
                }
            )
        return parsed

    def normalize(self, parsed: List[dict]) -> List[dict]:
        """Resolve property_id and build canonical valuation records."""
        canonical: List[dict] = []
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                for rec in parsed:
                    cur.execute(
                        """
                        SELECT property_id FROM source_property_ref
                        WHERE source = 'tarrant_cad' AND source_id = %s
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
                            "land_value": rec["land_value"],
                            "improvement_value": rec["improvement_value"],
                            "source": "tarrant_cad",
                        }
                    )
        return canonical

    def publish(self, canonical: List[dict]) -> int:
        """Upsert valuation_snapshot rows for Tarrant properties."""
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
                            (property_id, tax_year, market_value,
                             land_value, improvement_value, source, fetched_at)
                        VALUES (%s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (property_id, tax_year, source)
                        DO UPDATE SET
                            market_value       = EXCLUDED.market_value,
                            land_value         = EXCLUDED.land_value,
                            improvement_value  = EXCLUDED.improvement_value,
                            fetched_at         = NOW()
                        """,
                        (
                            rec["property_id"],
                            rec["tax_year"],
                            rec["market_value"],
                            rec["land_value"],
                            rec["improvement_value"],
                            rec["source"],
                        ),
                    )
                    written += 1
            conn.commit()
        return written

    def health(self) -> ConnectorHealth:
        return self._build_health()
