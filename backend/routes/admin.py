"""
Admin API — source connector health dashboard.

Endpoints:
    GET  /api/admin/sources               — list all connectors with health
    GET  /api/admin/sources/{name}/health — detailed health for one connector
    POST /api/admin/sources/{name}/refresh — trigger manual refresh
    GET  /api/admin/freshness             — summary of stale sources (>24h)
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

from ..config import DATABASE_URL
from ..connectors.base import ConnectorHealth, SourceConnector
from ..connectors.dallas_cad import DallasCADConnector
from ..connectors.tarrant_cad import TarrantCADConnector
from ..connectors.dallas_permits import DallasPermitsConnector
from ..connectors.dallas_violations import DallasViolationsConnector
from ..connectors.dallas_crime import DallasCrimeConnector
from ..connectors.dallas_clerk import DallasClerkConnector
from ..connectors.epa_environmental import EPAEnvironmentalConnector
from ..connectors.fema_flood import FEMAFloodConnector

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# ---------------------------------------------------------------------------
# Connector registry
# ---------------------------------------------------------------------------

def _build_registry(dsn: str) -> Dict[str, SourceConnector]:
    """Instantiate all connectors keyed by SOURCE_NAME."""
    connectors: List[SourceConnector] = [
        DallasCADConnector(dsn),
        TarrantCADConnector(dsn),
        DallasPermitsConnector(dsn),
        DallasViolationsConnector(dsn),
        DallasCrimeConnector(dsn),
        DallasClerkConnector(dsn),
        EPAEnvironmentalConnector(dsn),
        FEMAFloodConnector(dsn),
    ]
    return {c.SOURCE_NAME: c for c in connectors}


def _get_registry() -> Dict[str, SourceConnector]:
    return _build_registry(DATABASE_URL)


# ---------------------------------------------------------------------------
# Background refresh state (simple in-process tracker)
# ---------------------------------------------------------------------------

_refresh_state: Dict[str, Dict[str, Any]] = {}
_state_lock = threading.Lock()


def _run_refresh(source_name: str, connector: SourceConnector) -> None:
    """Execute connector.run() in a background thread and track state."""
    with _state_lock:
        _refresh_state[source_name] = {
            "status": "running",
            "started_at": datetime.now(tz=timezone.utc).isoformat(),
            "result": None,
        }
    try:
        result = connector.run()
        with _state_lock:
            _refresh_state[source_name] = {
                "status": result.get("status", "unknown"),
                "started_at": _refresh_state[source_name]["started_at"],
                "completed_at": datetime.now(tz=timezone.utc).isoformat(),
                "result": result,
            }
    except Exception as exc:
        logger.exception("Background refresh failed for %s", source_name)
        with _state_lock:
            _refresh_state[source_name] = {
                "status": "error",
                "started_at": _refresh_state[source_name]["started_at"],
                "completed_at": datetime.now(tz=timezone.utc).isoformat(),
                "error": str(exc),
            }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/sources", summary="List all connectors with health status")
def list_sources() -> JSONResponse:
    """
    Return health snapshots for all registered source connectors.

    Each entry includes:
    - source_name
    - status (healthy / degraded / stale / error)
    - last_success, last_failure timestamps
    - records_fetched, records_parsed, records_failed (last run)
    - freshness_hours (hours since last successful run; -1 = never run)
    - source_url
    """
    registry = _get_registry()
    results: List[Dict[str, Any]] = []

    for source_name, connector in registry.items():
        try:
            h = connector.health()
            entry = h.to_dict()
        except Exception as exc:
            logger.warning("health() failed for %s: %s", source_name, exc)
            entry = {
                "source_name": source_name,
                "status": "error",
                "error": str(exc),
                "last_success": None,
                "last_failure": None,
                "records_fetched": 0,
                "records_parsed": 0,
                "records_failed": 0,
                "freshness_hours": -1.0,
            }
        entry["source_url"] = connector.SOURCE_URL

        # Attach any in-progress refresh state
        with _state_lock:
            refresh = _refresh_state.get(source_name)
        if refresh:
            entry["refresh"] = refresh

        results.append(entry)

    # Sort: errors first, then stale, then healthy
    _order = {"error": 0, "degraded": 1, "stale": 2, "healthy": 3}
    results.sort(key=lambda r: _order.get(r.get("status", "error"), 99))

    return JSONResponse(
        content={
            "sources": results,
            "total": len(results),
            "healthy": sum(1 for r in results if r.get("status") == "healthy"),
            "degraded": sum(1 for r in results if r.get("status") == "degraded"),
            "stale": sum(1 for r in results if r.get("status") == "stale"),
            "error": sum(1 for r in results if r.get("status") == "error"),
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    )


@router.get(
    "/sources/{name}/health",
    summary="Detailed health for one connector",
)
def source_health(name: str) -> JSONResponse:
    """
    Return the health snapshot plus the last 10 sync_log entries for the
    named connector.
    """
    registry = _get_registry()
    if name not in registry:
        raise HTTPException(
            status_code=404,
            detail=f"Connector '{name}' not found. "
                   f"Available: {sorted(registry.keys())}",
        )

    connector = registry[name]

    try:
        h = connector.health()
        health_dict = h.to_dict()
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"health() failed: {exc}"
        )

    # Fetch last 10 sync_log rows for this source
    history: List[Dict[str, Any]] = []
    try:
        import psycopg2
        import psycopg2.extras

        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute(
                    """
                    SELECT id::text, source, connector, status,
                           records_fetched, records_parsed, records_written,
                           error_message,
                           started_at, completed_at, created_at
                    FROM sync_log
                    WHERE source = %s
                    ORDER BY created_at DESC
                    LIMIT 10
                    """,
                    (name,),
                )
                for row in cur.fetchall():
                    row_dict = dict(row)
                    # Serialize datetimes
                    for key in ("started_at", "completed_at", "created_at"):
                        if row_dict.get(key):
                            row_dict[key] = row_dict[key].isoformat()
                    history.append(row_dict)
    except Exception as exc:
        logger.warning("Could not fetch sync_log history for %s: %s", name, exc)

    with _state_lock:
        refresh = _refresh_state.get(name)

    return JSONResponse(
        content={
            "health": health_dict,
            "source_url": connector.SOURCE_URL,
            "stale_after_hours": connector.STALE_AFTER_HOURS,
            "history": history,
            "refresh": refresh,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    )


@router.post(
    "/sources/{name}/refresh",
    summary="Trigger a manual refresh for one connector",
)
def trigger_refresh(name: str, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Queue a background refresh for the named connector.

    Returns immediately with a 202 Accepted.  Poll
    GET /api/admin/sources/{name}/health to check progress.
    """
    registry = _get_registry()
    if name not in registry:
        raise HTTPException(
            status_code=404,
            detail=f"Connector '{name}' not found. "
                   f"Available: {sorted(registry.keys())}",
        )

    # Prevent concurrent refreshes for the same source
    with _state_lock:
        current = _refresh_state.get(name, {})
    if current.get("status") == "running":
        return JSONResponse(
            status_code=409,
            content={
                "message": f"Refresh for '{name}' is already running.",
                "refresh": current,
            },
        )

    connector = registry[name]
    background_tasks.add_task(_run_refresh, name, connector)

    return JSONResponse(
        status_code=202,
        content={
            "message": f"Refresh for '{name}' queued.",
            "source": name,
            "poll": f"/api/admin/sources/{name}/health",
        },
    )


@router.get(
    "/freshness",
    summary="Summary of sources that are stale or errored",
)
def freshness_summary() -> JSONResponse:
    """
    Return a quick summary of which sources are stale (>24h since last
    successful fetch) or have never successfully run.

    Useful for alerting / cron health checks.
    """
    registry = _get_registry()
    stale_threshold_hours = 24.0

    summary: List[Dict[str, Any]] = []
    for source_name, connector in registry.items():
        try:
            h = connector.health()
        except Exception as exc:
            summary.append(
                {
                    "source_name": source_name,
                    "status": "error",
                    "freshness_hours": -1.0,
                    "is_stale": True,
                    "error": str(exc),
                }
            )
            continue

        is_stale = (
            h.freshness_hours < 0
            or h.freshness_hours > stale_threshold_hours
        )
        summary.append(
            {
                "source_name": source_name,
                "status": h.status,
                "freshness_hours": h.freshness_hours,
                "last_success": (
                    h.last_success.isoformat() if h.last_success else None
                ),
                "is_stale": is_stale,
            }
        )

    stale_sources = [s for s in summary if s["is_stale"]]
    ok_sources = [s for s in summary if not s["is_stale"]]

    return JSONResponse(
        content={
            "stale_threshold_hours": stale_threshold_hours,
            "stale_count": len(stale_sources),
            "ok_count": len(ok_sources),
            "stale_sources": sorted(
                stale_sources, key=lambda s: s.get("freshness_hours", -1)
            ),
            "ok_sources": sorted(
                ok_sources, key=lambda s: s.get("freshness_hours", 0)
            ),
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    )
