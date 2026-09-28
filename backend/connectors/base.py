"""
Base connector contract for all Parcel Scout data sources.

Every connector follows the four-stage pipeline:
    discover → fetch → parse → normalize → publish

plus a health() method for observability.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health dataclass
# ---------------------------------------------------------------------------

@dataclass
class ConnectorHealth:
    """Health snapshot returned by every connector's health() method."""

    source_name: str
    last_success: Optional[datetime]
    last_failure: Optional[datetime]
    records_fetched: int
    records_parsed: int
    records_failed: int
    freshness_hours: float
    status: str  # healthy | degraded | stale | error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_name": self.source_name,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_failure": self.last_failure.isoformat() if self.last_failure else None,
            "records_fetched": self.records_fetched,
            "records_parsed": self.records_parsed,
            "records_failed": self.records_failed,
            "freshness_hours": round(self.freshness_hours, 2),
            "status": self.status,
        }


# ---------------------------------------------------------------------------
# Sync log helper (writes to the existing sync_log table)
# ---------------------------------------------------------------------------

class SyncLogWriter:
    """
    Thin wrapper around the sync_log table.

    The table columns are:
        source, connector, status,
        records_fetched, records_parsed, records_written,
        error_message, started_at, completed_at, created_at
    """

    def __init__(self, dsn: str):
        self._dsn = dsn

    def start(self, source_name: str, connector: str) -> str:
        """Insert a 'running' row and return its UUID."""
        import psycopg2
        import psycopg2.extras

        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sync_log
                        (source, connector, status, started_at)
                    VALUES (%s, %s, 'running', NOW())
                    RETURNING id
                    """,
                    (source_name, connector),
                )
                row_id = str(cur.fetchone()[0])
            conn.commit()
        return row_id

    def finish(
        self,
        row_id: str,
        *,
        status: str,
        records_fetched: int = 0,
        records_parsed: int = 0,
        records_written: int = 0,
        error_message: Optional[str] = None,
    ) -> None:
        import psycopg2

        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sync_log
                    SET status           = %s,
                        records_fetched  = %s,
                        records_parsed   = %s,
                        records_written  = %s,
                        error_message    = %s,
                        completed_at     = NOW()
                    WHERE id = %s
                    """,
                    (
                        status,
                        records_fetched,
                        records_parsed,
                        records_written,
                        error_message,
                        row_id,
                    ),
                )
            conn.commit()

    def last_run(self, source_name: str) -> Optional[Dict[str, Any]]:
        """Return the most recent completed row for a given source."""
        import psycopg2
        import psycopg2.extras

        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM sync_log
                    WHERE source = %s
                      AND status IN ('success', 'partial', 'error')
                    ORDER BY completed_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    (source_name,),
                )
                row = cur.fetchone()
        return dict(row) if row else None

    def last_success(self, source_name: str) -> Optional[Dict[str, Any]]:
        """Return the most recent successful row for a given source."""
        import psycopg2
        import psycopg2.extras

        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM sync_log
                    WHERE source = %s
                      AND status IN ('success', 'partial')
                    ORDER BY completed_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    (source_name,),
                )
                row = cur.fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------

class SourceConnector(ABC):
    """
    Contract that every data-source connector must implement.

    Subclasses MUST set:
        SOURCE_NAME  — stable identifier used in sync_log (e.g. "dallas_permits")
        SOURCE_URL   — canonical URL / API root for documentation purposes
        STALE_AFTER_HOURS — how many hours before the source is considered stale

    The run() helper executes the full pipeline and handles sync_log bookkeeping.
    """

    SOURCE_NAME: str = "unknown"
    SOURCE_URL: str = ""
    STALE_AFTER_HOURS: float = 24.0

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._log = SyncLogWriter(dsn)
        self.logger = logging.getLogger(
            f"parcel_scout.connector.{self.SOURCE_NAME}"
        )

    # ------------------------------------------------------------------
    # Pipeline stages — must be implemented by every connector
    # ------------------------------------------------------------------

    @abstractmethod
    def discover(self) -> List[str]:
        """
        Identify candidate record identifiers to fetch.

        Returns a list of opaque target strings (URLs, IDs, account numbers, etc.)
        that will be passed directly to fetch().
        """

    @abstractmethod
    def fetch(self, targets: List[str]) -> List[dict]:
        """
        Retrieve raw data for the given targets.

        Returns a list of raw record dicts (as received from the source,
        with minimal transformation).
        """

    @abstractmethod
    def parse(self, raw: List[dict]) -> List[dict]:
        """
        Parse raw records into an intermediate representation.

        Handles format normalization (types, dates, unit conversions) but does
        NOT yet map to the canonical schema.
        """

    @abstractmethod
    def normalize(self, parsed: List[dict]) -> List[dict]:
        """
        Map intermediate records to the canonical Parcel Scout schema.

        Returns a list of dicts whose keys match the target DB table columns.
        """

    @abstractmethod
    def publish(self, canonical: List[dict]) -> int:
        """
        Write canonical records to the database.

        Returns the number of rows successfully written.
        """

    @abstractmethod
    def health(self) -> ConnectorHealth:
        """Return the current health snapshot for this connector."""

    # ------------------------------------------------------------------
    # Convenience: build a health snapshot from sync_log data
    # ------------------------------------------------------------------

    def _build_health(self) -> ConnectorHealth:
        """
        Read the last two sync_log rows (most recent success + most recent any)
        and compute a ConnectorHealth dataclass.  Connectors can call this from
        their health() implementation.
        """
        now = datetime.now(tz=timezone.utc)

        last_ok = self._log.last_success(self.SOURCE_NAME)
        last_any = self._log.last_run(self.SOURCE_NAME)

        last_success_dt: Optional[datetime] = None
        last_failure_dt: Optional[datetime] = None
        records_fetched = 0
        records_parsed = 0
        records_failed = 0

        if last_ok and last_ok.get("completed_at"):
            last_success_dt = last_ok["completed_at"]
            if not isinstance(last_success_dt, datetime):
                last_success_dt = datetime.fromisoformat(str(last_success_dt))
            records_fetched = last_ok.get("records_fetched") or 0
            records_parsed = last_ok.get("records_parsed") or 0
            records_failed = (records_fetched or 0) - (
                last_ok.get("records_written") or 0
            )

        if last_any and last_any.get("status") == "error":
            if last_any.get("completed_at"):
                last_failure_dt = last_any["completed_at"]
                if not isinstance(last_failure_dt, datetime):
                    last_failure_dt = datetime.fromisoformat(
                        str(last_failure_dt)
                    )

        # Freshness
        if last_success_dt:
            delta = now - last_success_dt.replace(tzinfo=timezone.utc) if last_success_dt.tzinfo is None else now - last_success_dt
            freshness_hours = delta.total_seconds() / 3600.0
        else:
            freshness_hours = float("inf")

        # Status classification
        if freshness_hours == float("inf"):
            status = "error"
        elif freshness_hours <= self.STALE_AFTER_HOURS:
            status = "healthy"
        elif freshness_hours <= self.STALE_AFTER_HOURS * 2:
            status = "stale"
        else:
            status = "error"

        # Degrade if last run was an error even if we have a recent success
        if last_failure_dt and last_success_dt and last_failure_dt > last_success_dt:
            if status == "healthy":
                status = "degraded"

        return ConnectorHealth(
            source_name=self.SOURCE_NAME,
            last_success=last_success_dt,
            last_failure=last_failure_dt,
            records_fetched=records_fetched,
            records_parsed=records_parsed,
            records_failed=max(0, records_failed),
            freshness_hours=freshness_hours if freshness_hours != float("inf") else -1.0,
            status=status,
        )

    # ------------------------------------------------------------------
    # Full pipeline runner with sync_log bookkeeping
    # ------------------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        """
        Execute the full pipeline: discover → fetch → parse → normalize → publish.

        Returns a summary dict.  All errors are caught; the connector never
        raises — callers check the returned status.
        """
        row_id = self._log.start(self.SOURCE_NAME, self.__class__.__name__)
        result: Dict[str, Any] = {
            "source": self.SOURCE_NAME,
            "status": "error",
            "records_fetched": 0,
            "records_parsed": 0,
            "records_written": 0,
            "error": None,
        }

        try:
            self.logger.info("discover()")
            targets = self.discover()
            self.logger.info("fetch(%d targets)", len(targets))
            raw = self.fetch(targets)
            result["records_fetched"] = len(raw)

            self.logger.info("parse(%d records)", len(raw))
            parsed = self.parse(raw)
            result["records_parsed"] = len(parsed)

            self.logger.info("normalize(%d records)", len(parsed))
            canonical = self.normalize(parsed)

            self.logger.info("publish(%d records)", len(canonical))
            written = self.publish(canonical)
            result["records_written"] = written

            result["status"] = "success" if written == len(canonical) else "partial"
            self.logger.info(
                "Finished: %s — fetched=%d parsed=%d written=%d",
                result["status"],
                result["records_fetched"],
                result["records_parsed"],
                written,
            )

        except Exception as exc:
            result["error"] = str(exc)
            self.logger.exception("Pipeline failed for %s", self.SOURCE_NAME)

        finally:
            self._log.finish(
                row_id,
                status=result["status"],
                records_fetched=result["records_fetched"],
                records_parsed=result["records_parsed"],
                records_written=result["records_written"],
                error_message=result.get("error"),
            )

        return result
