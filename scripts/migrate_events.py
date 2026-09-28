#!/usr/bin/env python3
"""
Migrate Parcel Scout v2 DuckDB EVENT tables → v3 PostgreSQL.

Tables migrated:
  building_permits   → permit_event        (236,989 rows)
  code_violations    → code_event          (131,367 rows)
  crime_incidents    → crime_event         (413,269 rows)
  service_requests   → service_request     (449,895 rows)
  foreclosures       → filing_node         (  5,753 rows)
  ucc_filings        → filing_node         (  4,854 rows)
  court_records      → filing_node         (  1,260 rows)
  environmental_sites→ environmental_site  (  2,616 rows)
  transit_stops      → transit_stop        (  6,992 rows)
  highways           → highway             (  5,290 rows)

Usage:
  source /path/to/.venv/bin/activate
  python3 scripts/migrate_events.py

Environment variables:
  DUCKDB_PATH  — path to v2 database
  PG_DSN       — PostgreSQL DSN
"""

import os
import sys
import uuid
import time
from datetime import datetime
from pathlib import Path

import duckdb
import psycopg2
from psycopg2.extras import execute_values

# ─── Config ───────────────────────────────────────────────────────────────────

DUCKDB_PATH = os.environ.get(
    "DUCKDB_PATH",
    str(Path(__file__).resolve().parent.parent.parent / "parcel-scout" / "data" / "parcel_scout.duckdb"),
)
PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://parcelscout:parcelscout_dev@localhost:5432/parcelscout",
)
BATCH_SIZE = 5000

# ─── Helpers ──────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def make_uuid():
    return str(uuid.uuid4())


def safe_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def safe_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def build_property_map(pg_conn):
    """Load source_id → property_id (UUID) mapping from PostgreSQL."""
    log("Loading source_property_ref mapping…")
    cur = pg_conn.cursor()
    cur.execute("SELECT source_id, property_id FROM source_property_ref")
    mapping = {row[0]: str(row[1]) for row in cur.fetchall()}
    log(f"  Loaded {len(mapping):,} parcel_id → property_id mappings")
    return mapping


def batch_insert(pg_conn, table, columns, rows, conflict_clause="ON CONFLICT DO NOTHING"):
    """Insert rows in batches using execute_values; returns count inserted."""
    if not rows:
        return 0
    col_str = ", ".join(columns)
    sql = f"INSERT INTO {table} ({col_str}) VALUES %s {conflict_clause}"
    inserted = 0
    for start in range(0, len(rows), BATCH_SIZE):
        chunk = rows[start: start + BATCH_SIZE]
        with pg_conn.cursor() as cur:
            execute_values(cur, sql, chunk)
        pg_conn.commit()
        inserted += len(chunk)
    return inserted


def migrate_table(label, duck_conn, pg_conn, duck_sql, pg_table, pg_columns, row_transform,
                  conflict_clause="ON CONFLICT DO NOTHING", progress_every=50_000):
    """
    Generic streaming migration helper.

    duck_sql        — SELECT query to run against DuckDB
    pg_table        — target PostgreSQL table name
    pg_columns      — list of column names to insert
    row_transform   — callable(duck_row) → tuple or None (None rows are skipped)
    """
    log(f"--- Migrating {label} ---")
    t0 = time.time()
    result = duck_conn.execute(duck_sql)
    rows_out = []
    processed = 0
    skipped = 0
    total_inserted = 0

    while True:
        batch = result.fetchmany(BATCH_SIZE)
        if not batch:
            break
        for raw in batch:
            transformed = row_transform(raw)
            if transformed is None:
                skipped += 1
                continue
            rows_out.append(transformed)
            processed += 1

        # Flush when we have enough
        if len(rows_out) >= BATCH_SIZE:
            total_inserted += batch_insert(pg_conn, pg_table, pg_columns, rows_out, conflict_clause)
            rows_out = []

        if (processed + skipped) % progress_every < BATCH_SIZE:
            log(f"  …{processed + skipped:,} read, {processed:,} mapped, {skipped:,} skipped, {total_inserted:,} inserted")

    # Flush remainder
    if rows_out:
        total_inserted += batch_insert(pg_conn, pg_table, pg_columns, rows_out, conflict_clause)

    elapsed = time.time() - t0
    log(f"  Done: {processed:,} rows inserted, {skipped:,} skipped  [{elapsed:.1f}s]")
    return total_inserted


# ─── 1. building_permits → permit_event ───────────────────────────────────────

def migrate_building_permits(duck_conn, pg_conn, prop_map):
    duck_sql = """
        SELECT permit_id, parcel_id, permit_number, permit_type, permit_date,
               status, description, estimated_cost, contractor,
               address, latitude, longitude, source, fetched_at
        FROM building_permits
    """
    pg_columns = [
        "id", "property_id", "permit_number", "permit_type", "permit_date",
        "status", "description", "estimated_cost", "contractor",
        "address", "latitude", "longitude", "source", "fetched_at",
    ]

    def transform(r):
        permit_id, parcel_id, permit_number, permit_type, permit_date, \
            status, description, estimated_cost, contractor, \
            address, latitude, longitude, source, fetched_at = r
        property_id = prop_map.get(str(parcel_id)) if parcel_id else None
        return (
            make_uuid(),
            property_id,
            permit_number,
            permit_type,
            permit_date,
            status,
            description,
            safe_float(estimated_cost),
            contractor,
            address,
            safe_float(latitude),
            safe_float(longitude),
            source,
            fetched_at,
        )

    return migrate_table(
        "building_permits → permit_event",
        duck_conn, pg_conn, duck_sql,
        "permit_event", pg_columns, transform,
    )


# ─── 2. code_violations → code_event ──────────────────────────────────────────

def migrate_code_violations(duck_conn, pg_conn, prop_map):
    duck_sql = """
        SELECT parcel_id, case_number, violation_type, violation_date,
               status, description, address, latitude, longitude, source, fetched_at
        FROM code_violations
    """
    pg_columns = [
        "id", "property_id", "case_number", "violation_type", "violation_date",
        "status", "description", "address", "latitude", "longitude", "source", "fetched_at",
    ]

    def transform(r):
        parcel_id, case_number, violation_type, violation_date, \
            status, description, address, latitude, longitude, source, fetched_at = r
        property_id = prop_map.get(str(parcel_id)) if parcel_id else None
        return (
            make_uuid(),
            property_id,
            case_number,
            violation_type,
            violation_date,
            status,
            description,
            address,
            safe_float(latitude),
            safe_float(longitude),
            source,
            fetched_at,
        )

    return migrate_table(
        "code_violations → code_event",
        duck_conn, pg_conn, duck_sql,
        "code_event", pg_columns, transform,
    )


# ─── 3. crime_incidents → crime_event ─────────────────────────────────────────

def migrate_crime_incidents(duck_conn, pg_conn, prop_map):
    duck_sql = """
        SELECT incident_id, parcel_id, incident_type, incident_date,
               severity, address, latitude, longitude, source, fetched_at
        FROM crime_incidents
    """
    pg_columns = [
        "id", "incident_id", "property_id", "incident_type", "incident_date",
        "severity", "address", "latitude", "longitude", "source", "fetched_at",
    ]

    def transform(r):
        incident_id, parcel_id, incident_type, incident_date, \
            severity, address, latitude, longitude, source, fetched_at = r
        property_id = prop_map.get(str(parcel_id)) if parcel_id else None
        return (
            make_uuid(),
            incident_id,
            property_id,
            incident_type,
            incident_date,
            severity,
            address,
            safe_float(latitude),
            safe_float(longitude),
            source,
            fetched_at,
        )

    return migrate_table(
        "crime_incidents → crime_event",
        duck_conn, pg_conn, duck_sql,
        "crime_event", pg_columns, transform,
    )


# ─── 4. service_requests → service_request ────────────────────────────────────

def migrate_service_requests(duck_conn, pg_conn, prop_map):
    duck_sql = """
        SELECT request_id, parcel_id, request_type, request_date,
               status, description, address, latitude, longitude, source, fetched_at
        FROM service_requests
    """
    pg_columns = [
        "id", "request_id", "property_id", "request_type", "request_date",
        "status", "description", "address", "latitude", "longitude", "source", "fetched_at",
    ]

    def transform(r):
        request_id, parcel_id, request_type, request_date, \
            status, description, address, latitude, longitude, source, fetched_at = r
        property_id = prop_map.get(str(parcel_id)) if parcel_id else None
        return (
            make_uuid(),
            request_id,
            property_id,
            request_type,
            request_date,
            status,
            description,
            address,
            safe_float(latitude),
            safe_float(longitude),
            source,
            fetched_at,
        )

    return migrate_table(
        "service_requests → service_request",
        duck_conn, pg_conn, duck_sql,
        "service_request", pg_columns, transform,
    )


# ─── 5. foreclosures → filing_node ────────────────────────────────────────────

def migrate_foreclosures(duck_conn, pg_conn, prop_map):
    duck_sql = """
        SELECT parcel_id, filing_type, filing_date, sale_date,
               plaintiff, defendant, amount, address,
               source, fetched_at
        FROM foreclosures
    """
    pg_columns = [
        "id", "property_id", "filing_type", "filing_date", "sale_date",
        "plaintiff", "defendant", "amount", "description",
        "source", "fetched_at",
    ]

    def transform(r):
        parcel_id, filing_type, filing_date, sale_date, \
            plaintiff, defendant, amount, address, \
            source, fetched_at = r
        property_id = prop_map.get(str(parcel_id)) if parcel_id else None
        ft = filing_type or "foreclosure"
        return (
            make_uuid(),
            property_id,
            ft,
            filing_date,
            sale_date,
            plaintiff,
            defendant,
            safe_float(amount),
            address,       # address → description
            source,
            fetched_at,
        )

    return migrate_table(
        "foreclosures → filing_node",
        duck_conn, pg_conn, duck_sql,
        "filing_node", pg_columns, transform,
    )


# ─── 6. ucc_filings → filing_node ─────────────────────────────────────────────

def migrate_ucc_filings(duck_conn, pg_conn, prop_map):
    duck_sql = """
        SELECT parcel_id, filing_date, secured_party, debtor_name,
               collateral_description, filing_number, status,
               source, fetched_at
        FROM ucc_filings
    """
    pg_columns = [
        "id", "property_id", "filing_type", "filing_date",
        "secured_party", "debtor", "case_number", "status",
        "description", "source", "fetched_at",
    ]

    def transform(r):
        parcel_id, filing_date, secured_party, debtor_name, \
            collateral_description, filing_number, status, \
            source, fetched_at = r
        property_id = prop_map.get(str(parcel_id)) if parcel_id else None
        return (
            make_uuid(),
            property_id,
            "ucc",          # filing_type → 'ucc'
            filing_date,
            secured_party,
            debtor_name,    # debtor_name → debtor
            filing_number,  # filing_number → case_number
            status,
            collateral_description,
            source,
            fetched_at,
        )

    return migrate_table(
        "ucc_filings → filing_node",
        duck_conn, pg_conn, duck_sql,
        "filing_node", pg_columns, transform,
    )


# ─── 7. court_records → filing_node ───────────────────────────────────────────

def migrate_court_records(duck_conn, pg_conn, prop_map):
    duck_sql = """
        SELECT parcel_id, record_type, case_number, filing_date,
               plaintiff, defendant, amount, status,
               source, fetched_at
        FROM court_records
    """
    pg_columns = [
        "id", "property_id", "filing_type", "case_number", "filing_date",
        "plaintiff", "defendant", "amount", "status",
        "source", "fetched_at",
    ]

    def transform(r):
        parcel_id, record_type, case_number, filing_date, \
            plaintiff, defendant, amount, status, \
            source, fetched_at = r
        property_id = prop_map.get(str(parcel_id)) if parcel_id else None
        ft = record_type or "court_record"
        return (
            make_uuid(),
            property_id,
            ft,             # record_type → filing_type
            case_number,
            filing_date,
            plaintiff,
            defendant,
            safe_float(amount),
            status,
            source,
            fetched_at,
        )

    return migrate_table(
        "court_records → filing_node",
        duck_conn, pg_conn, duck_sql,
        "filing_node", pg_columns, transform,
    )


# ─── 8. environmental_sites → environmental_site ──────────────────────────────

def migrate_environmental_sites(duck_conn, pg_conn):
    duck_sql = """
        SELECT site_name, site_type, latitude, longitude, source, fetched_at
        FROM environmental_sites
    """
    pg_columns = [
        "id", "site_name", "site_type", "latitude", "longitude", "source", "fetched_at",
    ]

    def transform(r):
        site_name, site_type, latitude, longitude, source, fetched_at = r
        return (
            make_uuid(),
            site_name,
            site_type,      # source → site_type (DuckDB column is named site_type; instruction says source→site_type but column is already site_type)
            safe_float(latitude),
            safe_float(longitude),
            source,
            fetched_at,
        )

    return migrate_table(
        "environmental_sites → environmental_site",
        duck_conn, pg_conn, duck_sql,
        "environmental_site", pg_columns, transform,
    )


# ─── 9. transit_stops → transit_stop ──────────────────────────────────────────

def migrate_transit_stops(duck_conn, pg_conn):
    duck_sql = """
        SELECT stop_name, agency, latitude, longitude
        FROM transit_stops
    """
    pg_columns = [
        "id", "stop_name", "system_name", "latitude", "longitude",
    ]

    def transform(r):
        stop_name, agency, latitude, longitude = r
        return (
            make_uuid(),
            stop_name,
            agency,         # agency → system_name
            safe_float(latitude),
            safe_float(longitude),
        )

    return migrate_table(
        "transit_stops → transit_stop",
        duck_conn, pg_conn, duck_sql,
        "transit_stop", pg_columns, transform,
    )


# ─── 10. highways → highway ───────────────────────────────────────────────────

def migrate_highways(duck_conn, pg_conn):
    duck_sql = """
        SELECT route_name, highway_type, aadt
        FROM highways
    """
    pg_columns = [
        "id", "name", "highway_type", "aadt",
    ]

    def transform(r):
        route_name, highway_type, aadt = r
        return (
            make_uuid(),
            route_name,     # route_name → name
            highway_type,
            safe_int(aadt),
        )

    return migrate_table(
        "highways → highway",
        duck_conn, pg_conn, duck_sql,
        "highway", pg_columns, transform,
    )


# ─── Verify counts ────────────────────────────────────────────────────────────

def verify_counts(pg_conn):
    targets = [
        "permit_event",
        "code_event",
        "crime_event",
        "service_request",
        "filing_node",
        "environmental_site",
        "transit_stop",
        "highway",
    ]
    log("\n=== Final PostgreSQL row counts ===")
    cur = pg_conn.cursor()
    for t in targets:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        count = cur.fetchone()[0]
        log(f"  {t:<25} {count:>10,}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log("Connecting to DuckDB…")
    duck_conn = duckdb.connect(DUCKDB_PATH, read_only=True)

    log("Connecting to PostgreSQL…")
    pg_conn = psycopg2.connect(PG_DSN)
    pg_conn.autocommit = False

    # Build property_id lookup once
    prop_map = build_property_map(pg_conn)

    t_start = time.time()

    # Run migrations in logical order
    migrate_building_permits(duck_conn, pg_conn, prop_map)
    migrate_code_violations(duck_conn, pg_conn, prop_map)
    migrate_crime_incidents(duck_conn, pg_conn, prop_map)
    migrate_service_requests(duck_conn, pg_conn, prop_map)
    migrate_foreclosures(duck_conn, pg_conn, prop_map)
    migrate_ucc_filings(duck_conn, pg_conn, prop_map)
    migrate_court_records(duck_conn, pg_conn, prop_map)
    migrate_environmental_sites(duck_conn, pg_conn)
    migrate_transit_stops(duck_conn, pg_conn)
    migrate_highways(duck_conn, pg_conn)

    total_elapsed = time.time() - t_start
    log(f"\nAll migrations complete in {total_elapsed:.1f}s")

    verify_counts(pg_conn)

    duck_conn.close()
    pg_conn.close()
    log("Done.")


if __name__ == "__main__":
    main()
