"""
FEMA National Flood Hazard Layer (NFHL) connector.

Source: FEMA Map Service Center / ArcGIS REST API
URL: https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer

Queries the FEMA NFHL ArcGIS REST endpoint for DFW area flood zones
and updates the property table's flood_zone / flood_risk / in_floodplain
columns for all properties that lack this data.

FEMA flood zones:
    A*, AE, AO, AH, A1-A30  — High risk (100-year floodplain)
    B, X (shaded)             — Moderate risk
    C, X                      — Minimal risk
    D                         — Undetermined
"""

from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

from .base import ConnectorHealth, SourceConnector

# FEMA NFHL ArcGIS REST endpoint — layer 28 = Flood Hazard Zones
_FEMA_API = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)

# DFW bounding box (EPSG:4326)
_DFW_BBOX = "-97.5,32.5,-96.5,33.3"
_PAGE_SIZE = 1000

# Risk classification mapping
_ZONE_RISK: Dict[str, Tuple[str, bool]] = {
    # High risk — in floodplain
    "A": ("high", True),
    "AE": ("high", True),
    "AO": ("high", True),
    "AH": ("high", True),
    "AR": ("high", True),
    "A99": ("high", True),
    "VE": ("high", True),
    "V": ("high", True),
    # Moderate risk
    "X": ("moderate", False),  # shaded X = moderate
    "B": ("moderate", False),
    # Minimal / undetermined
    "C": ("minimal", False),
    "D": ("undetermined", False),
}


def _classify_zone(flood_zone: str) -> Tuple[str, bool]:
    """Return (flood_risk label, in_floodplain) for a FEMA zone string."""
    zone = (flood_zone or "").strip().upper()
    # Exact match
    if zone in _ZONE_RISK:
        return _ZONE_RISK[zone]
    # Prefix match (A1-A30 etc.)
    for prefix, classification in _ZONE_RISK.items():
        if zone.startswith(prefix):
            return classification
    return ("undetermined", False)


class FEMAFloodConnector(SourceConnector):
    """
    FEMA NFHL flood zone connector.

    Queries the FEMA ArcGIS REST service for flood zone polygons in DFW,
    then updates property rows with flood_zone / flood_risk / in_floodplain.

    Point-in-polygon matching is done via the FEMA API's spatial query
    against each property's lat/lon.  For efficiency, properties are batched
    and queried in groups using a bounding-box pre-filter.
    """

    SOURCE_NAME = "fema_flood"
    SOURCE_URL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"
    STALE_AFTER_HOURS = 720.0  # FEMA NFHL updates are infrequent

    def discover(self) -> List[str]:
        """Return property IDs that need flood zone data."""
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id
                    FROM property
                    WHERE latitude IS NOT NULL
                      AND longitude IS NOT NULL
                      AND (flood_zone IS NULL OR flood_zone = '')
                    ORDER BY created_at DESC
                    LIMIT 2000
                    """
                )
                rows = cur.fetchall()
        self.logger.info(
            "Found %d properties needing flood zone data", len(rows)
        )
        return [str(r[0]) for r in rows]

    def fetch(self, targets: List[str]) -> List[dict]:
        """
        Query FEMA ArcGIS for flood zone polygons across the DFW bbox.

        We query the zone layer once for the full bounding box and cache
        all returned features.  Point-in-polygon assignment happens in
        normalize().
        """
        all_features: List[dict] = []
        result_offset = 0

        while True:
            params = urllib.parse.urlencode(
                {
                    "f": "json",
                    "geometry": _DFW_BBOX,
                    "geometryType": "esriGeometryEnvelope",
                    "inSR": "4326",
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": "FLD_ZONE,ZONE_SUBTY,DFIRM_ID",
                    "returnGeometry": "true",
                    "resultOffset": result_offset,
                    "resultRecordCount": _PAGE_SIZE,
                }
            )
            url = f"{_FEMA_API}?{params}"
            try:
                req = urllib.request.Request(
                    url, headers={"Accept": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode())
            except Exception as exc:
                self.logger.error(
                    "FEMA fetch failed at offset %d: %s", result_offset, exc
                )
                break

            features = data.get("features", [])
            if not features:
                break
            all_features.extend(features)
            self.logger.debug(
                "FEMA: fetched %d features (offset=%d)", len(features), result_offset
            )
            if len(features) < _PAGE_SIZE:
                break
            result_offset += _PAGE_SIZE

        self.logger.info("FEMA: %d flood zone features fetched", len(all_features))
        # Store property IDs as context alongside features
        return [{"features": all_features, "property_ids": targets}]

    def parse(self, raw: List[dict]) -> List[dict]:
        """
        For each feature, extract flood zone attributes and ring coordinates.

        Returns simplified zone dicts for use in normalize().
        """
        if not raw:
            return []
        payload = raw[0]
        features = payload.get("features", [])
        property_ids = payload.get("property_ids", [])

        zones: List[dict] = []
        for feat in features:
            attrs = feat.get("attributes", {})
            geom = feat.get("geometry", {})
            flood_zone = str(attrs.get("FLD_ZONE") or "").strip()
            if not flood_zone:
                continue
            # Collect rings from polygon geometry
            rings = geom.get("rings", [])
            zones.append({"flood_zone": flood_zone, "rings": rings})

        return [{"zones": zones, "property_ids": property_ids}]

    def normalize(self, parsed: List[dict]) -> List[dict]:
        """
        Point-in-polygon: for each property, find which zone contains it.

        Returns a list of update dicts: {property_id, flood_zone, flood_risk, in_floodplain}.
        """
        if not parsed:
            return []
        payload = parsed[0]
        zones = payload.get("zones", [])
        property_ids = payload.get("property_ids", [])

        if not property_ids or not zones:
            return []

        # Load property lat/lons
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, latitude, longitude FROM property
                    WHERE id = ANY(%s::uuid[])
                    """,
                    (property_ids,),
                )
                props = [dict(r) for r in cur.fetchall()]

        updates: List[dict] = []
        for prop in props:
            lat = float(prop["latitude"] or 0)
            lon = float(prop["longitude"] or 0)
            if not lat or not lon:
                continue

            matched_zone = "X"  # default: minimal risk
            for zone_rec in zones:
                fz = zone_rec["flood_zone"]
                rings = zone_rec["rings"]
                for ring in rings:
                    if _point_in_ring(lon, lat, ring):
                        matched_zone = fz
                        break
                else:
                    continue
                break

            risk, in_floodplain = _classify_zone(matched_zone)
            updates.append(
                {
                    "property_id": str(prop["id"]),
                    "flood_zone": matched_zone,
                    "flood_risk": risk,
                    "in_floodplain": in_floodplain,
                }
            )

        return updates

    def publish(self, canonical: List[dict]) -> int:
        """Update property.flood_zone, flood_risk, in_floodplain."""
        if not canonical:
            return 0
        written = 0
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                for rec in canonical:
                    try:
                        cur.execute(
                            """
                            UPDATE property
                            SET flood_zone    = %s,
                                flood_risk    = %s,
                                in_floodplain = %s,
                                updated_at    = NOW()
                            WHERE id = %s
                            """,
                            (
                                rec["flood_zone"],
                                rec["flood_risk"],
                                rec["in_floodplain"],
                                rec["property_id"],
                            ),
                        )
                        written += cur.rowcount
                    except Exception as exc:
                        self.logger.debug("FEMA publish skip: %s", exc)
            conn.commit()
        return written

    def health(self) -> ConnectorHealth:
        return self._build_health()


# ---------------------------------------------------------------------------
# Point-in-polygon (ray casting)
# ---------------------------------------------------------------------------

def _point_in_ring(x: float, y: float, ring: List) -> bool:
    """
    Ray-casting point-in-polygon test.

    ring is a list of [x, y] pairs from an ArcGIS polygon ring.
    """
    n = len(ring)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        intersect = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi
        )
        if intersect:
            inside = not inside
        j = i
    return inside
