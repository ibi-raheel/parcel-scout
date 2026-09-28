"""
Dallas Building Permits connector — Socrata / SODA API.

Source: City of Dallas Open Data
Dataset: https://www.dallasopendata.com/Services/Building-Permits/tb7q-k64x
API endpoint: https://www.dallasopendata.com/resource/tb7q-k64x.json

Fetches building permits issued in the last N days and links them to
matching properties via address lookup.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

from .base import ConnectorHealth, SourceConnector

# Socrata endpoint (Dallas building permits)
_SODA_URL = "https://www.dallasopendata.com/resource/tb7q-k64x.json"
_DEFAULT_LOOKBACK_DAYS = 30
_PAGE_SIZE = 1000


class DallasPermitsConnector(SourceConnector):
    """
    Dallas City building permits via Socrata SODA API.

    Fetches permits issued in the last 30 days and writes them to
    permit_event, linking to a property record where addressable.
    """

    SOURCE_NAME = "dallas_permits"
    SOURCE_URL = "https://www.dallasopendata.com/Services/Building-Permits/tb7q-k64x"
    STALE_AFTER_HOURS = 24.0

    def __init__(self, dsn: str, lookback_days: int = _DEFAULT_LOOKBACK_DAYS):
        super().__init__(dsn)
        self._lookback_days = lookback_days

    def discover(self) -> List[str]:
        """
        Build paginated SODA query URLs for permits issued in the last N days.

        Returns a list of fetch URLs (one per page).
        """
        cutoff = (
            datetime.now(tz=timezone.utc) - timedelta(days=self._lookback_days)
        ).strftime("%Y-%m-%dT%H:%M:%S")

        pages: List[str] = []
        offset = 0
        while True:
            params = urllib.parse.urlencode(
                {
                    "$where": f"issued_date >= '{cutoff}'",
                    "$limit": _PAGE_SIZE,
                    "$offset": offset,
                    "$order": "issued_date DESC",
                }
            )
            pages.append(f"{_SODA_URL}?{params}")
            # We don't know total count yet; stop after first batch estimate.
            # fetch() will handle pagination by checking response length.
            if offset == 0:
                break
            offset += _PAGE_SIZE
        return pages

    def fetch(self, targets: List[str]) -> List[dict]:
        """Download all pages from SODA, following pagination."""
        all_records: List[dict] = []
        base_url = targets[0] if targets else ""

        offset = 0
        while True:
            # Reconstruct URL with updated offset
            params = urllib.parse.urlencode(
                {
                    "$where": f"issued_date >= '{self._cutoff_iso()}'",
                    "$limit": _PAGE_SIZE,
                    "$offset": offset,
                    "$order": "issued_date DESC",
                }
            )
            url = f"{_SODA_URL}?{params}"
            try:
                req = urllib.request.Request(
                    url,
                    headers={"Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    page = json.loads(resp.read().decode())
            except Exception as exc:
                self.logger.error("SODA fetch failed at offset %d: %s", offset, exc)
                break

            if not page:
                break
            all_records.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

        self.logger.info("Fetched %d permit records from SODA", len(all_records))
        return all_records

    def _cutoff_iso(self) -> str:
        return (
            datetime.now(tz=timezone.utc) - timedelta(days=self._lookback_days)
        ).strftime("%Y-%m-%dT%H:%M:%S")

    def parse(self, raw: List[dict]) -> List[dict]:
        """Clean and type-cast raw SODA permit records."""
        parsed: List[dict] = []
        for r in raw:
            try:
                permit_number = r.get("permit_number") or r.get("permit_num") or ""
                permit_type = r.get("permit_type") or r.get("type_of_work") or ""
                address = (r.get("address") or r.get("site_address") or "").strip().upper()
                city = (r.get("city") or "DALLAS").strip().upper()
                zip_code = str(r.get("zip") or r.get("zip_code") or "").strip()[:10]

                issued_raw = r.get("issued_date") or r.get("issue_date") or ""
                issued_date: Optional[datetime] = None
                if issued_raw:
                    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                        try:
                            issued_date = datetime.strptime(
                                issued_raw[:19], fmt
                            ).replace(tzinfo=timezone.utc)
                            break
                        except ValueError:
                            continue

                valuation: Optional[float] = None
                val_raw = r.get("declared_valuation") or r.get("valuation") or ""
                if val_raw:
                    try:
                        valuation = float(str(val_raw).replace(",", "").replace("$", ""))
                    except ValueError:
                        pass

                parsed.append(
                    {
                        "permit_number": permit_number,
                        "permit_type": permit_type,
                        "address": address,
                        "city": city,
                        "zip": zip_code,
                        "issued_date": issued_date,
                        "valuation": valuation,
                        "raw": r,
                    }
                )
            except Exception as exc:
                self.logger.debug("Parse error on record: %s", exc)
        return parsed

    def normalize(self, parsed: List[dict]) -> List[dict]:
        """
        Attempt to match each permit to a property via address lookup.
        Returns canonical permit_event dicts.
        """
        canonical: List[dict] = []
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                for rec in parsed:
                    if not rec.get("permit_number"):
                        continue

                    # Address match (case-insensitive exact)
                    property_id: Optional[str] = None
                    if rec["address"]:
                        cur.execute(
                            """
                            SELECT id FROM property
                            WHERE UPPER(property_address) = %s
                            LIMIT 1
                            """,
                            (rec["address"],),
                        )
                        row = cur.fetchone()
                        if row:
                            property_id = str(row[0])

                    canonical.append(
                        {
                            "property_id": property_id,
                            "permit_number": rec["permit_number"],
                            "permit_type": rec["permit_type"],
                            "address": rec["address"],
                            "issued_date": rec["issued_date"],
                            "valuation": rec["valuation"],
                        }
                    )
        return canonical

    def publish(self, canonical: List[dict]) -> int:
        """Insert or update permit_event rows."""
        if not canonical:
            return 0
        written = 0
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                for rec in canonical:
                    try:
                        # permit_event columns: property_id, permit_number, permit_type,
                        # permit_date (date), status, description, estimated_cost,
                        # address, source, fetched_at
                        permit_date = (
                            rec["issued_date"].date()
                            if rec.get("issued_date")
                            else None
                        )
                        cur.execute(
                            """
                            INSERT INTO permit_event
                                (property_id, permit_number, permit_type,
                                 permit_date, estimated_cost, address,
                                 source, fetched_at)
                            VALUES (%s, %s, %s, %s, %s, %s, 'dallas_permits', NOW())
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                rec["property_id"],
                                rec["permit_number"],
                                rec["permit_type"],
                                permit_date,
                                rec["valuation"],
                                rec["address"],
                            ),
                        )
                        written += 1
                    except Exception as exc:
                        self.logger.debug("publish skip: %s", exc)
            conn.commit()
        return written

    def health(self) -> ConnectorHealth:
        return self._build_health()
