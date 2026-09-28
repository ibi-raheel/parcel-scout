"""
EPA Environmental connector — TRI (Toxics Release Inventory) and FRS
(Facility Registry Service).

Sources:
    TRI: https://enviro.epa.gov/enviro/efservice/
    FRS: https://ofmpub.epa.gov/frs_public2/fii_query_det.disp_program_facility

API: EPA Envirofacts REST API
    https://enviro.epa.gov/enviro/efservice/

Fetches hazardous-waste / toxic-release facilities in DFW and writes
them to the environmental_site table.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

from .base import ConnectorHealth, SourceConnector

# EPA Envirofacts base
_FRS_API = "https://data.epa.gov/efservice"
# FRS facility table: EZ_FACILITY_DETAIL
_FRS_TABLE = "EZ_FACILITY_DETAIL"

# Geographic bounding box: DFW metro (approx)
_LAT_MIN = 32.5
_LAT_MAX = 33.3
_LON_MIN = -97.5
_LON_MAX = -96.5

_PAGE_SIZE = 500


class EPAEnvironmentalConnector(SourceConnector):
    """
    EPA FRS/TRI environmental sites connector.

    Fetches hazardous facilities within the DFW bounding box from the
    EPA Envirofacts REST API and writes them to environmental_site.
    """

    SOURCE_NAME = "epa_environmental"
    SOURCE_URL = "https://enviro.epa.gov/enviro/efservice/"
    STALE_AFTER_HOURS = 720.0  # EPA data changes infrequently; monthly refresh

    def discover(self) -> List[str]:
        """
        Build paginated EPA Envirofacts query URLs for DFW-area facilities.

        Returns a list of fetch URLs.
        """
        pages: List[str] = []
        row_start = 0
        while row_start < 5000:  # cap at 5000 records
            url = (
                f"{_FRS_API}/{_FRS_TABLE}"
                f"/LATITUDE83/>{_LAT_MIN}"
                f"/LATITUDE83/<{_LAT_MAX}"
                f"/LONGITUDE83/>{_LON_MIN}"
                f"/LONGITUDE83/<{_LON_MAX}"
                f"/rows/{row_start}:{_PAGE_SIZE}/JSON"
            )
            pages.append(url)
            if row_start == 0:
                break  # fetch() will paginate
            row_start += _PAGE_SIZE
        return pages

    def fetch(self, targets: List[str]) -> List[dict]:
        """Download all DFW environmental facilities from EPA Envirofacts."""
        all_records: List[dict] = []
        row_start = 0

        while True:
            url = (
                f"{_FRS_API}/{_FRS_TABLE}"
                f"/LATITUDE83/>{_LAT_MIN}"
                f"/LATITUDE83/<{_LAT_MAX}"
                f"/LONGITUDE83/>{_LON_MIN}"
                f"/LONGITUDE83/<{_LON_MAX}"
                f"/rows/{row_start}:{_PAGE_SIZE}/JSON"
            )
            try:
                req = urllib.request.Request(
                    url,
                    headers={"Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode())
            except Exception as exc:
                self.logger.error(
                    "EPA fetch failed at row %d: %s", row_start, exc
                )
                break

            if not data or not isinstance(data, list):
                break

            all_records.extend(data)
            self.logger.debug("EPA: fetched %d records (row_start=%d)", len(data), row_start)
            if len(data) < _PAGE_SIZE:
                break
            row_start += _PAGE_SIZE

        self.logger.info("EPA: fetched %d total facility records", len(all_records))
        return all_records

    def parse(self, raw: List[dict]) -> List[dict]:
        """Extract and type-cast EPA facility records."""
        parsed: List[dict] = []
        for r in raw:
            try:
                registry_id = str(r.get("REGISTRY_ID") or r.get("FRS_FACILITY_DETAIL_ID") or "")
                site_name = (r.get("PRIMARY_NAME") or r.get("FACILITY_NAME") or "").strip()
                site_type = (r.get("INTEREST_TYPES") or r.get("SITE_TYPE_NAME") or "").strip()

                lat: Optional[float] = None
                lon: Optional[float] = None
                try:
                    lat = float(r.get("LATITUDE83") or 0) or None
                    lon = float(r.get("LONGITUDE83") or 0) or None
                except (ValueError, TypeError):
                    pass

                if not lat or not lon:
                    continue  # skip records with no coordinates

                parsed.append(
                    {
                        "registry_id": registry_id,
                        "site_name": site_name[:255] if site_name else "",
                        "site_type": site_type[:50] if site_type else "",
                        "latitude": lat,
                        "longitude": lon,
                    }
                )
            except Exception as exc:
                self.logger.debug("EPA parse error: %s", exc)
        return parsed

    def normalize(self, parsed: List[dict]) -> List[dict]:
        """Canonical form matches environmental_site columns directly."""
        return [
            {
                "site_name": r["site_name"],
                "site_type": r["site_type"],
                "latitude": r["latitude"],
                "longitude": r["longitude"],
                "registry_id": r["registry_id"],
            }
            for r in parsed
            if r.get("site_name") and r.get("latitude")
        ]

    def publish(self, canonical: List[dict]) -> int:
        """Upsert environmental_site rows (match on lat/lon + site_name)."""
        if not canonical:
            return 0
        written = 0
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                for rec in canonical:
                    try:
                        cur.execute(
                            """
                            INSERT INTO environmental_site
                                (site_name, site_type, latitude, longitude,
                                 source, fetched_at)
                            VALUES (%s, %s, %s, %s, 'epa_environmental', NOW())
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                rec["site_name"],
                                rec["site_type"],
                                rec["latitude"],
                                rec["longitude"],
                            ),
                        )
                        written += 1
                    except Exception as exc:
                        self.logger.debug("EPA publish skip: %s", exc)
            conn.commit()
        return written

    def health(self) -> ConnectorHealth:
        return self._build_health()
