"""
Parcel Scout v3 — Source Connector Orchestrator.

Runs one or more source connectors in sequence, logs everything to
sync_log, and prints a summary report.

Usage:
    # Run all connectors
    python3 -m backend.orchestrator

    # Run specific connectors
    python3 -m backend.orchestrator --source dallas_permits --source dallas_crime

    # Dry-run (discover only, no write)
    python3 -m backend.orchestrator --dry-run

    # List available connectors
    python3 -m backend.orchestrator --list
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Type

# ---------------------------------------------------------------------------
# Logging setup (before any other imports so submodules pick it up)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("parcel_scout.orchestrator")

# ---------------------------------------------------------------------------
# Connector imports
# ---------------------------------------------------------------------------
from .config import DATABASE_URL
from .connectors.base import SourceConnector
from .connectors.dallas_cad import DallasCADConnector
from .connectors.tarrant_cad import TarrantCADConnector
from .connectors.dallas_permits import DallasPermitsConnector
from .connectors.dallas_violations import DallasViolationsConnector
from .connectors.dallas_crime import DallasCrimeConnector
from .connectors.dallas_clerk import DallasClerkConnector
from .connectors.epa_environmental import EPAEnvironmentalConnector
from .connectors.fema_flood import FEMAFloodConnector

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ALL_CONNECTORS: Dict[str, Type[SourceConnector]] = {
    "dallas_cad": DallasCADConnector,
    "tarrant_cad": TarrantCADConnector,
    "dallas_permits": DallasPermitsConnector,
    "dallas_violations": DallasViolationsConnector,
    "dallas_crime": DallasCrimeConnector,
    "dallas_clerk": DallasClerkConnector,
    "epa_environmental": EPAEnvironmentalConnector,
    "fema_flood": FEMAFloodConnector,
}

# Default run order (dependencies first)
DEFAULT_ORDER = [
    "dallas_cad",
    "tarrant_cad",
    "dallas_permits",
    "dallas_violations",
    "dallas_crime",
    "epa_environmental",
    "fema_flood",
    "dallas_clerk",  # Last: Playwright scraper is slowest
]


# ---------------------------------------------------------------------------
# Orchestrator core
# ---------------------------------------------------------------------------

def run_connectors(
    source_names: List[str],
    dsn: str,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """
    Run each named connector in sequence.

    Args:
        source_names: ordered list of SOURCE_NAME strings to run
        dsn: PostgreSQL connection string
        dry_run: if True, call discover() only (no fetch/parse/publish)

    Returns:
        List of result dicts, one per connector.
    """
    results: List[Dict[str, Any]] = []
    total_start = time.monotonic()

    for name in source_names:
        connector_cls = ALL_CONNECTORS.get(name)
        if connector_cls is None:
            logger.warning("Unknown connector '%s' — skipping", name)
            results.append(
                {
                    "source": name,
                    "status": "skipped",
                    "error": f"connector '{name}' not registered",
                    "duration_sec": 0,
                }
            )
            continue

        connector = connector_cls(dsn)
        logger.info("=" * 60)
        logger.info("Starting connector: %s", name)
        start = time.monotonic()

        if dry_run:
            try:
                targets = connector.discover()
                result: Dict[str, Any] = {
                    "source": name,
                    "status": "dry_run",
                    "targets_discovered": len(targets),
                    "duration_sec": round(time.monotonic() - start, 2),
                }
                logger.info(
                    "[DRY-RUN] %s: discovered %d targets in %.1fs",
                    name,
                    len(targets),
                    result["duration_sec"],
                )
            except Exception as exc:
                result = {
                    "source": name,
                    "status": "error",
                    "error": str(exc),
                    "duration_sec": round(time.monotonic() - start, 2),
                }
                logger.exception("Dry-run discover() failed for %s", name)
        else:
            result = connector.run()
            result["duration_sec"] = round(time.monotonic() - start, 2)
            logger.info(
                "Finished %s: status=%s fetched=%d parsed=%d written=%d (%.1fs)",
                name,
                result.get("status"),
                result.get("records_fetched", 0),
                result.get("records_parsed", 0),
                result.get("records_written", 0),
                result["duration_sec"],
            )

        results.append(result)

    # ---------------------------------------------------------------------------
    # Summary report
    # ---------------------------------------------------------------------------
    total_sec = round(time.monotonic() - total_start, 2)
    succeeded = [r for r in results if r.get("status") in ("success", "partial", "dry_run")]
    failed = [r for r in results if r.get("status") == "error"]
    skipped = [r for r in results if r.get("status") == "skipped"]

    logger.info("=" * 60)
    logger.info("ORCHESTRATOR SUMMARY  (%.1fs total)", total_sec)
    logger.info(
        "  Succeeded: %d  |  Failed: %d  |  Skipped: %d",
        len(succeeded),
        len(failed),
        len(skipped),
    )

    if succeeded:
        logger.info("  Succeeded:")
        for r in succeeded:
            logger.info(
                "    ✓  %-25s  %s",
                r["source"],
                (
                    f"fetched={r.get('records_fetched',0)} "
                    f"written={r.get('records_written',0)}"
                    if r.get("status") != "dry_run"
                    else f"targets={r.get('targets_discovered',0)}"
                ),
            )

    if failed:
        logger.info("  Failed:")
        for r in failed:
            logger.warning(
                "    ✗  %-25s  %s", r["source"], r.get("error", "unknown error")
            )

    if skipped:
        logger.info("  Skipped:")
        for r in skipped:
            logger.info("    -  %s", r["source"])

    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m backend.orchestrator",
        description="Parcel Scout v3 — Source Connector Orchestrator",
    )
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        metavar="NAME",
        help=(
            "Connector to run (can repeat: --source dallas_permits --source dallas_crime). "
            "Defaults to all connectors in dependency order."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover targets only; do not fetch or write to DB.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available connectors and exit.",
    )
    parser.add_argument(
        "--dsn",
        default=DATABASE_URL,
        help="PostgreSQL DSN (defaults to DATABASE_URL env var).",
    )

    args = parser.parse_args()

    if args.list:
        print("Available connectors (default run order):")
        for name in DEFAULT_ORDER:
            cls = ALL_CONNECTORS[name]
            print(f"  {name:<25}  {cls.SOURCE_URL}")
        return 0

    sources_to_run = args.sources or DEFAULT_ORDER

    # Validate
    unknown = [s for s in sources_to_run if s not in ALL_CONNECTORS]
    if unknown:
        logger.error(
            "Unknown connector(s): %s. Available: %s",
            unknown,
            sorted(ALL_CONNECTORS.keys()),
        )
        return 1

    logger.info(
        "Orchestrator starting — %d connector(s)%s",
        len(sources_to_run),
        " [DRY RUN]" if args.dry_run else "",
    )
    logger.info("Sources: %s", ", ".join(sources_to_run))

    results = run_connectors(sources_to_run, dsn=args.dsn, dry_run=args.dry_run)

    # Exit code: 1 if any connector errored
    any_error = any(r.get("status") == "error" for r in results)
    return 1 if any_error else 0


if __name__ == "__main__":
    sys.exit(main())
