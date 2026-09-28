#!/usr/bin/env python3
"""
Migrate Parcel Scout v2 DuckDB data → v3 PostgreSQL canonical schema.

Maps the flat DuckDB tables to the normalized PostgreSQL entity model:
  parcels       → property + source_property_ref + owner_party + property_ownership + valuation_snapshot
  deeds         → document_event + debt_instrument
  code_violations → code_event
  building_permits → permit_event
  service_requests → service_request
  crime_incidents → crime_event
  foreclosures  → filing_node
  ucc_filings   → filing_node
  court_records → filing_node
  environmental_sites → environmental_site
  highways      → highway
  transit_stops → transit_stop
  opportunity_zones → opportunity_zone
  tirz_districts → tirz_district

Usage:
  pip install duckdb psycopg2-binary
  python scripts/migrate_duckdb_to_postgres.py

Environment variables:
  DUCKDB_PATH  — path to v2 database (default: ../parcel-scout/data/parcel_scout.duckdb)
  PG_DSN       — PostgreSQL connection string (default: postgresql://parcelscout:parcelscout_dev@localhost:5432/parcelscout)
"""

import os
import sys
import uuid
import time
from datetime import datetime, date
from pathlib import Path

import duckdb
import psycopg2
from psycopg2.extras import execute_values

# ─── Config ───────────────────────────────────────────────────────────────────
DUCKDB_PATH = os.environ.get(
    "DUCKDB_PATH",
    str(Path(__file__).resolve().parent.parent.parent / "parcel-scout" / "data" / "parcel_scout.duckdb")
)
PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://parcelscout:parcelscout_dev@localhost:5432/parcelscout"
)
BATCH_SIZE = 5000

# ─── Helpers ──────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def make_uuid():
    return str(uuid.uuid4())

def safe_float(v):
    if v is None: return None
    try: return float(v)
    except: return None

def safe_int(v):
    if v is None: return None
    try: return int(v)
    except: return None

def safe_date(v):
    if v is None: return None
    if isinstance(v, (date, datetime)): return v
    try: return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except: return None

def safe_str(v, max_len=None):
    if v is None: return None
    s = str(v).strip()
    if not s: return None
    if max_len: s = s[:max_len]
    return s


# ─── Migration Functions ─────────────────────────────────────────────────────

def migrate_properties(duck, pg):
    """Migrate parcels → property + source_property_ref + owner_party + property_ownership + valuation_snapshot"""
    log("Migrating parcels → property + related tables...")

    cols = [r[0] for r in duck.execute("SELECT column_name FROM information_schema.columns WHERE table_name='parcels' ORDER BY ordinal_position").fetchall()]
    rows = duck.execute("SELECT * FROM parcels").fetchall()
    log(f"  {len(rows):,} parcels to migrate")

    # Track owner_party deduplication
    owner_cache = {}  # raw_name → owner_party_id

    prop_batch = []
    ref_batch = []
    owner_batch = []
    ownership_batch = []
    valuation_batch = []

    for i, row in enumerate(rows):
        r = dict(zip(cols, row))
        prop_id = make_uuid()

        # ── property ──
        prop_batch.append((
            prop_id, r.get('county'),
            safe_str(r.get('property_address'), 255), safe_str(r.get('city'), 100),
            'TX', safe_str(r.get('zip'), 10),
            safe_str(r.get('legal_description')),
            safe_float(r.get('acreage')), safe_float(r.get('lot_sqft')),
            safe_float(r.get('building_sqft')), safe_int(r.get('year_built')),
            safe_int(r.get('num_stories')), safe_str(r.get('building_class'), 20),
            safe_str(r.get('condition'), 50),
            safe_str(r.get('state_land_use_code'), 10), safe_str(r.get('land_use_description'), 100),
            safe_str(r.get('zoning_code'), 20), safe_str(r.get('zoning_description'), 100),
            safe_float(r.get('latitude')), safe_float(r.get('longitude')),
            # has_frontage and infrastructure
            bool(r.get('has_frontage')),
            safe_str(r.get('nearest_highway_name'), 100),
            safe_int(r.get('highway_aadt')),
            safe_float(r.get('distance_to_highway_ft')),
            # Location intelligence
            safe_str(r.get('flood_zone'), 10), safe_str(r.get('flood_risk'), 20),
            bool(r.get('in_floodplain')), bool(r.get('in_opportunity_zone')),
            bool(r.get('in_tirz')), safe_str(r.get('tirz_name'), 100),
            safe_str(r.get('census_tract'), 20),
            safe_str(r.get('nearest_transit_stop'), 100),
            safe_float(r.get('distance_to_transit_ft')),
        ))

        # ── source_property_ref ──
        source = f"{r.get('county', 'unknown').lower().replace(' ', '')}_cad"
        ref_batch.append((
            make_uuid(), prop_id, source, str(r.get('parcel_id', '')),
        ))

        # ── owner_party (deduplicated) ──
        owner_name = safe_str(r.get('owner_name'), 255)
        if owner_name:
            cache_key = f"{owner_name}|{r.get('owner_mailing_address')}|{r.get('county')}"
            if cache_key not in owner_cache:
                owner_id = make_uuid()
                owner_cache[cache_key] = owner_id

                is_llc = bool(r.get('is_llc'))
                party_type = 'llc' if is_llc else 'individual'
                mailing_state = safe_str(r.get('owner_mailing_state'), 20)
                is_oos = bool(r.get('out_of_state_owner'))

                owner_batch.append((
                    owner_id, owner_name, owner_name.upper(),
                    party_type,
                    safe_str(r.get('owner_mailing_address'), 255),
                    safe_str(r.get('owner_mailing_city'), 100),
                    mailing_state, safe_str(r.get('owner_mailing_zip'), 10),
                    safe_str(r.get('owner_mailing_type'), 20),
                    is_oos,
                ))
            else:
                owner_id = owner_cache[cache_key]

            # ── property_ownership ──
            ownership_batch.append((
                make_uuid(), prop_id, owner_id, 100.00, True, source, None, None,
            ))

        # ── valuation_snapshot ──
        tax_year = safe_int(r.get('tax_year')) or 2026
        valuation_batch.append((
            make_uuid(), prop_id, tax_year,
            safe_float(r.get('market_value')), safe_float(r.get('total_appraised_value')),
            safe_float(r.get('land_value')), safe_float(r.get('improvement_value')),
            safe_float(r.get('assessed_value')),
            safe_str(r.get('tax_status'), 20),
            safe_float(r.get('tax_delinquent_amount')),
            safe_int(r.get('tax_delinquent_years')),
            safe_str(r.get('tax_data_source'), 30),
            source,
        ))

        # Flush batches
        if len(prop_batch) >= BATCH_SIZE:
            _flush_properties(pg, prop_batch, ref_batch, owner_batch, ownership_batch, valuation_batch)
            prop_batch, ref_batch, owner_batch, ownership_batch, valuation_batch = [], [], [], [], []
            log(f"    {i+1:,} / {len(rows):,} migrated")

    # Final flush
    if prop_batch:
        _flush_properties(pg, prop_batch, ref_batch, owner_batch, ownership_batch, valuation_batch)
        log(f"    {len(rows):,} / {len(rows):,} migrated")

    log(f"  Done: {len(rows):,} properties, {len(owner_cache):,} unique owners")
    return owner_cache


def _flush_properties(pg, props, refs, owners, ownerships, valuations):
    cur = pg.cursor()

    if props:
        execute_values(cur, """
            INSERT INTO property (
                id, county, property_address, city, state, zip,
                legal_description, acreage, lot_sqft, building_sqft, year_built,
                num_stories, building_class, condition,
                state_land_use_code, land_use_description, zoning_code, zoning_description,
                latitude, longitude,
                has_frontage, nearest_highway, highway_aadt, distance_to_highway_ft,
                flood_zone, flood_risk, in_floodplain, in_opportunity_zone,
                in_tirz, tirz_name, census_tract, nearest_transit_stop, distance_to_transit_ft
            ) VALUES %s ON CONFLICT DO NOTHING
        """, props, page_size=1000)

    # PostGIS geom update skipped — PostGIS not available for PG16 via Homebrew
    # Will add when PostGIS is installed: UPDATE property SET geom = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)

    if refs:
        execute_values(cur, """
            INSERT INTO source_property_ref (id, property_id, source, source_id)
            VALUES %s ON CONFLICT DO NOTHING
        """, refs, page_size=1000)

    if owners:
        execute_values(cur, """
            INSERT INTO owner_party (
                id, raw_name, normalized_name, party_type,
                mailing_address, mailing_city, mailing_state, mailing_zip,
                mailing_type, is_out_of_state
            ) VALUES %s ON CONFLICT DO NOTHING
        """, owners, page_size=1000)

    if ownerships:
        execute_values(cur, """
            INSERT INTO property_ownership (
                id, property_id, owner_party_id, ownership_pct, is_current,
                source, effective_date, end_date
            ) VALUES %s ON CONFLICT DO NOTHING
        """, ownerships, page_size=1000)

    if valuations:
        execute_values(cur, """
            INSERT INTO valuation_snapshot (
                id, property_id, tax_year,
                market_value, total_appraised, land_value, improvement_value, assessed_value,
                tax_status, tax_delinquent_amount, tax_delinquent_years, tax_data_source, source
            ) VALUES %s ON CONFLICT DO NOTHING
        """, valuations, page_size=1000)

    pg.commit()
    cur.close()


def migrate_deeds(duck, pg):
    """Migrate deeds → document_event + debt_instrument"""
    log("Migrating deeds → document_event + debt_instrument...")

    # Build property_id lookup: source_id → v3 property.id
    cur = pg.cursor()
    cur.execute("SELECT source_id, property_id FROM source_property_ref")
    pid_map = {r[0]: str(r[1]) for r in cur.fetchall()}
    cur.close()

    cols = [r[0] for r in duck.execute("SELECT column_name FROM information_schema.columns WHERE table_name='deeds' ORDER BY ordinal_position").fetchall()]
    rows = duck.execute("SELECT * FROM deeds").fetchall()
    log(f"  {len(rows):,} deeds to migrate")

    doc_batch = []
    debt_batch = []

    for i, row in enumerate(rows):
        r = dict(zip(cols, row))
        old_pid = str(r.get('parcel_id', ''))
        new_pid = pid_map.get(old_pid)
        if not new_pid:
            continue

        doc_id = make_uuid()
        deed_type = safe_str(r.get('deed_type'), 30)

        # Classify document
        doc_type = 'warranty_deed'
        doc_category = 'transfer'
        if deed_type:
            dt_upper = deed_type.upper()
            if 'QCD' in dt_upper or 'QUIT' in dt_upper:
                doc_type = 'quit_claim'
            elif 'SWD' in dt_upper:
                doc_type = 'special_warranty'
            elif 'DNL' in dt_upper or 'DOT' in dt_upper or 'TD' in dt_upper:
                doc_type = 'deed_of_trust'
                doc_category = 'financing'
            elif 'PLAT' in dt_upper:
                doc_type = 'plat'
                doc_category = 'notice'
            elif 'ROW' in dt_upper:
                doc_type = 'right_of_way'
                doc_category = 'transfer'
            elif 'JDGMT' in dt_upper or 'JUDG' in dt_upper:
                doc_type = 'judgment'
                doc_category = 'enforcement'
            elif 'CONST' in dt_upper:
                doc_type = 'mechanic_lien'
                doc_category = 'lien'
            elif 'WD' in dt_upper:
                doc_type = 'warranty_deed'

        doc_batch.append((
            doc_id, new_pid,
            safe_str(r.get('instrument_number'), 50),
            safe_str(r.get('deed_book'), 20), safe_str(r.get('deed_page'), 20),
            doc_type, doc_category,
            safe_date(r.get('filing_date')), safe_date(r.get('deed_date')),
            None,  # grantor_raw (not in v2)
            safe_str(r.get('grantee'), 500),
            None, None,  # grantor_party_id, grantee_party_id (resolve later)
            safe_float(r.get('consideration')),
            safe_float(r.get('loan_amount')),
            None,  # interest_rate
            safe_date(r.get('maturity_date')),
            safe_str(r.get('lender_name'), 255),
            safe_str(r.get('source'), 50), None, None,
            safe_date(r.get('fetched_at')),
        ))

        # Create debt_instrument if it's a financing event
        maturity = safe_date(r.get('estimated_maturity')) or safe_date(r.get('maturity_date'))
        if maturity or doc_category == 'financing':
            debt_batch.append((
                make_uuid(), new_pid, doc_id,
                safe_float(r.get('loan_amount')),
                None,  # current_balance_est
                None,  # interest_rate
                None,  # loan_term_months
                maturity,
                safe_str(r.get('lender_name'), 255),
                None,  # servicer
                None,  # loan_type
                'active', False, None, False,
                'cad_estimated',
                safe_str(r.get('source'), 50),
            ))

        if len(doc_batch) >= BATCH_SIZE:
            _flush_deeds(pg, doc_batch, debt_batch)
            doc_batch, debt_batch = [], []
            log(f"    {i+1:,} / {len(rows):,}")

    if doc_batch:
        _flush_deeds(pg, doc_batch, debt_batch)

    log(f"  Done: {len(rows):,} document events migrated")


def _flush_deeds(pg, docs, debts):
    cur = pg.cursor()
    if docs:
        execute_values(cur, """
            INSERT INTO document_event (
                id, property_id, instrument_number, deed_book, deed_page,
                doc_type, doc_category, recording_date, effective_date,
                grantor_raw, grantee_raw, grantor_party_id, grantee_party_id,
                consideration, loan_amount, interest_rate, maturity_date, lender_name,
                source, source_url, raw_payload, fetched_at
            ) VALUES %s ON CONFLICT DO NOTHING
        """, docs, page_size=1000)
    if debts:
        execute_values(cur, """
            INSERT INTO debt_instrument (
                id, property_id, document_event_id,
                original_amount, current_balance_est, interest_rate, loan_term_months,
                maturity_date, lender_name, servicer_name, loan_type,
                status, is_released, release_date, has_subsequent_financing,
                data_quality, source
            ) VALUES %s ON CONFLICT DO NOTHING
        """, debts, page_size=1000)
    pg.commit()
    cur.close()


def migrate_simple_events(duck, pg, duck_table, pg_table, column_map, log_name):
    """Generic migration for event tables (permits, violations, crimes, 311)"""
    log(f"Migrating {duck_table} → {pg_table}...")

    # Build property_id lookup
    cur = pg.cursor()
    cur.execute("SELECT source_id, property_id FROM source_property_ref")
    pid_map = {r[0]: str(r[1]) for r in cur.fetchall()}
    cur.close()

    try:
        cols = [r[0] for r in duck.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name='{duck_table}' ORDER BY ordinal_position").fetchall()]
        rows = duck.execute(f"SELECT * FROM {duck_table}").fetchall()
    except Exception as e:
        log(f"  Skipping {duck_table}: {e}")
        return

    log(f"  {len(rows):,} records to migrate")

    batch = []
    for i, row in enumerate(rows):
        r = dict(zip(cols, row))
        old_pid = str(r.get('parcel_id', ''))
        new_pid = pid_map.get(old_pid)

        record = [make_uuid(), new_pid]
        for duck_col, transform in column_map:
            val = r.get(duck_col)
            record.append(transform(val) if transform else val)
        batch.append(tuple(record))

        if len(batch) >= BATCH_SIZE:
            _flush_events(pg, pg_table, batch, len(record))
            batch = []
            if (i + 1) % 50000 == 0:
                log(f"    {i+1:,} / {len(rows):,}")

    if batch:
        _flush_events(pg, pg_table, batch, len(batch[0]) if batch else 0)

    log(f"  Done: {len(rows):,} → {pg_table}")


def _flush_events(pg, table, batch, ncols):
    if not batch:
        return
    cur = pg.cursor()
    # Dynamic insert — we trust the caller to match column order
    placeholders = ", ".join(["%s"] * ncols)
    # Use execute_values for speed
    try:
        execute_values(cur, f"INSERT INTO {table} VALUES %s ON CONFLICT DO NOTHING", batch, page_size=1000)
    except Exception as e:
        pg.rollback()
        log(f"  ERROR flushing to {table}: {e}")
        return
    pg.commit()
    cur.close()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log("=" * 70)
    log("  Parcel Scout v2 → v3 Migration")
    log("=" * 70)
    log(f"  DuckDB:     {DUCKDB_PATH}")
    log(f"  PostgreSQL: {PG_DSN}")

    # Connect
    log("\nConnecting to DuckDB...")
    duck = duckdb.connect(DUCKDB_PATH, read_only=True)

    log("Connecting to PostgreSQL...")
    pg = psycopg2.connect(PG_DSN)

    # Show source counts
    log("\nSource data:")
    for table in duck.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name").fetchall():
        cnt = duck.execute(f"SELECT COUNT(*) FROM {table[0]}").fetchone()[0]
        log(f"  {table[0]:<25} {cnt:>10,}")

    start = time.time()

    # 1. Properties (the big one)
    owner_cache = migrate_properties(duck, pg)

    # 2. Deeds → document_event + debt_instrument
    migrate_deeds(duck, pg)

    # 3. Verify counts
    log("\n" + "=" * 70)
    log("  Migration Complete")
    log("=" * 70)

    cur = pg.cursor()
    for table in ['property', 'source_property_ref', 'owner_party', 'property_ownership',
                   'valuation_snapshot', 'document_event', 'debt_instrument']:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        cnt = cur.fetchone()[0]
        log(f"  {table:<25} {cnt:>10,}")
    cur.close()

    elapsed = time.time() - start
    log(f"\n  Total time: {elapsed:.1f}s")
    log(f"  Note: Event tables (permits, violations, crimes, 311, filings) will be")
    log(f"  migrated in a follow-up pass once the property ID mapping is verified.")

    duck.close()
    pg.close()


if __name__ == "__main__":
    main()
