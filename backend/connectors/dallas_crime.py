"""
Dallas PD Crime Data connector — Socrata / SODA API.

Source: City of Dallas Open Data — Dallas Police Department
Dataset: https://www.dallasopendata.com/Public-Safety/Police-Incidents/qv6i-rri7
API endpoint: https://www.dallasopendata.com/resource/qv6i-rri7.json

Fetches police incidents (crime reports) from the last N days and
writes them to the crime_event table.
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

_SODA_URL = "https://www.dallasopendata.com/resource/qv6i-rri7.json"
_DEFAULT_LOOKBACK_DAYS = 30
_PAGE_SIZE = 1000


class DallasCrimeConnector(SourceConnector):
    """
    Dallas PD incident/crime data via Socrata SODA API.

    Fetches incidents from the last 30 days and writes them to crime_event,
    linking to property records where a matching address exists.
    """

    SOURCE_NAME = "dallas_crime"
    SOURCE_URL = (
        "https://www.dallasopendata.com/Public-Safety/Police-Incidents/qv6i-rri7"
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
        """Return first-page URL; fetch() handles pagination."""
        cutoff = self._cutoff_iso()
        params = urllib.parse.urlencode(
            {
                "$where": f"date1 >= '{cutoff}'",
                "$limit": _PAGE_SIZE,
                "$offset": 0,
                "$order": "date1 DESC",
            }
        )
        return [f"{_SODA_URL}?{params}"]

    def fetch(self, targets: List[str]) -> List[dict]:
        """Download all pages of crime incidents from SODA."""
        all_records: List[dict] = []
        cutoff = self._cutoff_iso()
        offset = 0

        while True:
            params = urllib.parse.urlencode(
                {
                    "$where": f"date1 >= '{cutoff}'",
                    "$limit": _PAGE_SIZE,
                    "$offset": offset,
                    "$order": "date1 DESC",
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
                    "SODA crime fetch failed at offset %d: %s", offset, exc
                )
                break

            if not page:
                break
            all_records.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

        self.logger.info("Fetched %d crime records from SODA", len(all_records))
        return all_records

    def parse(self, raw: List[dict]) -> List[dict]:
        """Type-cast raw SODA crime records."""
        parsed: List[dict] = []
        for r in raw:
            try:
                incident_number = (
                    r.get("incidentnum") or r.get("incident_number") or ""
                )
                offense_type = (
                    r.get("typeofincident")
                    or r.get("nibrs_code_description")
                    or r.get("offensedescription")
                    or ""
                )
                address = (
                    r.get("block") or r.get("location_description") or ""
                ).strip().upper()
                city = "DALLAS"

                date_raw = r.get("date1") or r.get("incident_date") or ""
                incident_date: Optional[datetime] = None
                if date_raw:
                    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                        try:
                            incident_date = datetime.strptime(
                                date_raw[:19], fmt
                            ).replace(tzinfo=timezone.utc)
                            break
                        except ValueError:
                            continue

                lat: Optional[float] = None
                lon: Optional[float] = None
                try:
                    lat = float(r.get("y_cordinate") or r.get("latitude") or 0) or None
                    lon = float(r.get("x_coordinate") or r.get("longitude") or 0) or None
                except (ValueError, TypeError):
                    pass

                parsed.append(
                    {
                        "incident_number": incident_number,
                        "offense_type": offense_type,
                        "address": address,
                        "city": city,
                        "incident_date": incident_date,
                        "latitude": lat,
                        "longitude": lon,
                    }
                )
            except Exception as exc:
                self.logger.debug("Parse error: %s", exc)
        return parsed

    def normalize(self, parsed: List[dict]) -> List[dict]:
        """Match incidents to properties; build canonical crime_event dicts."""
        canonical: List[dict] = []
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                for rec in parsed:
                    if not rec.get("incident_number"):
                        continue
                    property_id: Optional[str] = None
                    if rec["address"] and len(rec["address"]) > 5:
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
                            "incident_number": rec["incident_number"],
                            "offense_type": rec["offense_type"],
                            "address": rec["address"],
                            "incident_date": rec["incident_date"],
                            "latitude": rec["latitude"],
                            "longitude": rec["longitude"],
                        }
                    )
        return canonical

    def publish(self, canonical: List[dict]) -> int:
        """Insert or update crime_event rows."""
        if not canonical:
            return 0
        written = 0
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                for rec in canonical:
                    try:
                        # crime_event columns: property_id, incident_id, incident_type,
                        # incident_date (date), severity, address, latitude, longitude,
                        # source, fetched_at
                        incident_date = (
                            rec["incident_date"].date()
                            if rec.get("incident_date")
                            else None
                        )
                        cur.execute(
                            """
                            INSERT INTO crime_event
                                (property_id, incident_id, incident_type,
                                 incident_date, address, latitude, longitude,
                                 source, fetched_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, 'dallas_crime', NOW())
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                rec["property_id"],
                                rec["incident_number"],
                                rec["offense_type"],
                                incident_date,
                                rec["address"],
                                rec["latitude"],
                                rec["longitude"],
                            ),
                        )
                        written += 1
                    except Exception as exc:
                        self.logger.debug("publish skip: %s", exc)
            conn.commit()
        return written

    def health(self) -> ConnectorHealth:
        return self._build_health()
