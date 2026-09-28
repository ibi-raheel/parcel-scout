"""
Dallas Code Violations connector — Socrata / SODA API.

Source: City of Dallas Open Data
Dataset: https://www.dallasopendata.com/Code-Compliance/Code-Violations/g9e8-yetx
API endpoint: https://www.dallasopendata.com/resource/g9e8-yetx.json

Fetches code compliance violations and writes them to code_event.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import psycopg2
import psycopg2.extras

from .base import ConnectorHealth, SourceConnector

_SODA_URL = "https://www.dallasopendata.com/resource/g9e8-yetx.json"
_DEFAULT_LOOKBACK_DAYS = 30
_PAGE_SIZE = 1000


class DallasViolationsConnector(SourceConnector):
    """
    Dallas code compliance violations via Socrata SODA API.

    Fetches violations opened/updated in the last 30 days and writes
    them to the code_event table.
    """

    SOURCE_NAME = "dallas_violations"
    SOURCE_URL = (
        "https://www.dallasopendata.com/Code-Compliance/Code-Violations/g9e8-yetx"
    )
    STALE_AFTER_HOURS = 24.0

    def __init__(self, dsn: str, lookback_days: int = _DEFAULT_LOOKBACK_DAYS):
        super().__init__(dsn)
        self._lookback_days = lookback_days

    def _cutoff_iso(self) -> str:
        return (
            datetime.now(tz=timezone.utc) - timedelta(days=self._lookback_days)
        ).strftime("%Y-%m-%dT%H:%M:%S")

    def discover(self) -> List[str]:
        """Return first-page SODA URL; fetch() handles pagination."""
        cutoff = self._cutoff_iso()
        params = urllib.parse.urlencode(
            {
                "$where": f"service_request_open_dt >= '{cutoff}'",
                "$limit": _PAGE_SIZE,
                "$offset": 0,
                "$order": "service_request_open_dt DESC",
            }
        )
        return [f"{_SODA_URL}?{params}"]

    def fetch(self, targets: List[str]) -> List[dict]:
        """Download all pages of violations from SODA."""
        all_records: List[dict] = []
        cutoff = self._cutoff_iso()
        offset = 0

        while True:
            params = urllib.parse.urlencode(
                {
                    "$where": f"service_request_open_dt >= '{cutoff}'",
                    "$limit": _PAGE_SIZE,
                    "$offset": offset,
                    "$order": "service_request_open_dt DESC",
                }
            )
            url = f"{_SODA_URL}?{params}"
            try:
                req = urllib.request.Request(
                    url, headers={"Accept": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    page = json.loads(resp.read().decode())
            except Exception as exc:
                self.logger.error(
                    "SODA violations fetch failed at offset %d: %s", offset, exc
                )
                break

            if not page:
                break
            all_records.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

        self.logger.info(
            "Fetched %d violation records from SODA", len(all_records)
        )
        return all_records

    def parse(self, raw: List[dict]) -> List[dict]:
        """Type-cast SODA violation records."""
        parsed: List[dict] = []
        for r in raw:
            try:
                case_number = (
                    r.get("service_request_number")
                    or r.get("case_number")
                    or r.get("complaint_number")
                    or ""
                )
                violation_type = r.get("violation_description") or r.get("code") or ""
                address = (
                    r.get("service_request_address")
                    or r.get("address")
                    or ""
                ).strip().upper()
                city = (r.get("city") or "DALLAS").strip().upper()
                status = r.get("service_request_status") or r.get("status") or ""

                opened_raw = (
                    r.get("service_request_open_dt")
                    or r.get("open_date")
                    or ""
                )
                opened_date: Optional[datetime] = None
                if opened_raw:
                    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                        try:
                            opened_date = datetime.strptime(
                                opened_raw[:19], fmt
                            ).replace(tzinfo=timezone.utc)
                            break
                        except ValueError:
                            continue

                parsed.append(
                    {
                        "case_number": case_number,
                        "violation_type": violation_type,
                        "address": address,
                        "city": city,
                        "status": status,
                        "opened_date": opened_date,
                    }
                )
            except Exception as exc:
                self.logger.debug("Parse error: %s", exc)
        return parsed

    def normalize(self, parsed: List[dict]) -> List[dict]:
        """Match violations to properties and build canonical code_event dicts."""
        canonical: List[dict] = []
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                for rec in parsed:
                    if not rec.get("case_number"):
                        continue
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
                            "case_number": rec["case_number"],
                            "violation_type": rec["violation_type"],
                            "address": rec["address"],
                            "status": rec["status"],
                            "opened_date": rec["opened_date"],
                        }
                    )
        return canonical

    def publish(self, canonical: List[dict]) -> int:
        """Insert or update code_event rows."""
        if not canonical:
            return 0
        written = 0
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                for rec in canonical:
                    try:
                        # code_event columns: property_id, case_number, violation_type,
                        # violation_date (date), status, description, address, source, fetched_at
                        violation_date = (
                            rec["opened_date"].date()
                            if rec.get("opened_date")
                            else None
                        )
                        cur.execute(
                            """
                            INSERT INTO code_event
                                (property_id, case_number, violation_type,
                                 violation_date, status, address,
                                 source, fetched_at)
                            VALUES (%s, %s, %s, %s, %s, %s, 'dallas_violations', NOW())
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                rec["property_id"],
                                rec["case_number"],
                                rec["violation_type"],
                                violation_date,
                                rec["status"],
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
