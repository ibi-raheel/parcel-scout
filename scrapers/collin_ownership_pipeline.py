"""
Collin County LLC Ownership Pipeline.

Resolves beneficial owners behind LLCs by:
  1. Querying TX Comptroller franchise-tax API for officers/registered agents
  2. Searching Collin County Clerk portal by officer name for deed transactions
  3. Cross-linking all properties a person controls through any LLC

Usage:
  python3 scrapers/collin_ownership_pipeline.py run   [--batch-size 100] [--headless]
  python3 scrapers/collin_ownership_pipeline.py stats
  python3 scrapers/collin_ownership_pipeline.py lookup --llc "WY LADERA LLC"
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional, List, Dict, Tuple, Any

import psycopg2
import psycopg2.extras
import requests
from playwright.sync_api import sync_playwright, Page, Browser, TimeoutError as PwTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_DSN = "postgresql://parcelscout:parcelscout_dev@localhost:5432/parcelscout"
COMPTROLLER_SEARCH_URL = "https://comptroller.texas.gov/data-search/franchise-tax"
CLERK_BASE_URL = "https://collin.tx.publicsearch.us"
REQUEST_DELAY_SEC = 2.0
DEFAULT_BATCH_SIZE = 100
LOG_FMT = "%(asctime)s  %(levelname)-8s  %(message)s"

logging.basicConfig(format=LOG_FMT, level=logging.INFO, stream=sys.stdout)
log = logging.getLogger("ownership_pipeline")

# Deed types we care about for property ownership
DEED_TYPES = {
    "WARRANTY DEED", "GENERAL WARRANTY DEED", "SPECIAL WARRANTY DEED",
    "DEED", "QUIT CLAIM DEED", "QUITCLAIM DEED", "DEED OF TRUST",
    "RELEASE OF DEED OF TRUST", "RELEASE OF LIEN",
}

DOC_TYPE_MAP = {
    "DEED OF TRUST": ("deed_of_trust", "financing"),
    "DEED": ("warranty_deed", "transfer"),
    "WARRANTY DEED": ("warranty_deed", "transfer"),
    "SPECIAL WARRANTY DEED": ("special_warranty", "transfer"),
    "GENERAL WARRANTY DEED": ("warranty_deed", "transfer"),
    "QUIT CLAIM DEED": ("quit_claim", "transfer"),
    "QUITCLAIM DEED": ("quit_claim", "transfer"),
    "RELEASE OF LIEN": ("lien_release", "financing"),
    "RELEASE OF DEED OF TRUST": ("lien_release", "financing"),
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class OfficerInfo:
    """An officer/governing person found via TX Comptroller."""
    name: str
    title: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    source: str = "comptroller"


@dataclass
class EntityInfo:
    """TX Comptroller entity record."""
    name: str
    taxpayer_id: str = ""
    sos_file_number: str = ""
    formation_state: str = ""
    sos_status: str = ""
    right_to_transact: str = ""
    effective_date: str = ""
    registered_agent: str = ""
    registered_address: str = ""
    mailing_address: str = ""
    officers: List[OfficerInfo] = field(default_factory=list)
    raw_data: dict = field(default_factory=dict)


@dataclass
class ClerkDeedRecord:
    """A deed record from Collin County Clerk portal."""
    instrument_number: str = ""
    doc_type: str = ""
    recording_date: Optional[date] = None
    grantors: List[str] = field(default_factory=list)
    grantees: List[str] = field(default_factory=list)
    legal_description: str = ""
    consideration: Optional[float] = None
    detail_url: str = ""


@dataclass
class PipelineResult:
    """Result of processing one LLC."""
    llc_name: str
    llc_id: str
    property_count: int = 0
    entity_info: Optional[EntityInfo] = None
    officers_found: List[OfficerInfo] = field(default_factory=list)
    clerk_records: Dict[str, List[ClerkDeedRecord]] = field(default_factory=dict)
    linked_properties: int = 0
    linked_via_other_llcs: int = 0
    total_portfolio: int = 0
    error: str = ""


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_connection():
    return psycopg2.connect(DB_DSN)


def get_top_collin_llcs(conn, limit: int = 100) -> List[Tuple[str, str, int]]:
    """Return top Collin County LLCs by property count."""
    cur = conn.cursor()
    cur.execute("""
        SELECT op.raw_name, op.id::text, COUNT(DISTINCT po.property_id) as props
        FROM owner_party op
        JOIN property_ownership po ON po.owner_party_id = op.id
        JOIN property p ON po.property_id = p.id
        WHERE p.county = 'Collin' AND op.party_type = 'llc'
        GROUP BY op.raw_name, op.id
        ORDER BY COUNT(DISTINCT po.property_id) DESC
        LIMIT %s
    """, (limit,))
    rows = cur.fetchall()
    log.info("Found %d Collin County LLCs to process", len(rows))
    return [(r[0], r[1], r[2]) for r in rows]


def get_already_resolved(conn) -> set:
    """Return set of owner_party IDs that already have resolved_entity_id."""
    cur = conn.cursor()
    cur.execute("""
        SELECT id::text FROM owner_party
        WHERE resolved_entity_id IS NOT NULL
          AND party_type = 'llc'
    """)
    return {r[0] for r in cur.fetchall()}


def upsert_entity_record(conn, info: EntityInfo) -> str:
    """Insert or update entity_record, return its UUID."""
    cur = conn.cursor()

    # Check if exists by SOS file number or name
    if info.sos_file_number:
        cur.execute(
            "SELECT id FROM entity_record WHERE sos_file_number = %s",
            (info.sos_file_number,)
        )
    else:
        cur.execute(
            "SELECT id FROM entity_record WHERE entity_name = %s AND entity_type = 'llc'",
            (info.name,)
        )
    row = cur.fetchone()

    formation_date = None
    if info.effective_date:
        try:
            formation_date = datetime.strptime(info.effective_date, "%m/%d/%Y").date()
        except ValueError:
            pass

    if row:
        entity_id = str(row[0])
        cur.execute("""
            UPDATE entity_record SET
                entity_name = COALESCE(NULLIF(%s, ''), entity_name),
                sos_file_number = COALESCE(NULLIF(%s, ''), sos_file_number),
                formation_date = COALESCE(%s, formation_date),
                sos_status = COALESCE(NULLIF(%s, ''), sos_status),
                registered_agent = COALESCE(NULLIF(%s, ''), registered_agent),
                registered_address = COALESCE(NULLIF(%s, ''), registered_address),
                updated_at = NOW()
            WHERE id = %s
        """, (
            info.name, info.sos_file_number, formation_date,
            info.sos_status, info.registered_agent, info.registered_address,
            entity_id,
        ))
    else:
        entity_id = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO entity_record (id, entity_name, entity_type, sos_file_number,
                formation_date, sos_status, registered_agent, registered_address,
                created_at, updated_at)
            VALUES (%s, %s, 'llc', %s, %s, %s, %s, %s, NOW(), NOW())
        """, (
            entity_id, info.name, info.sos_file_number, formation_date,
            info.sos_status, info.registered_agent, info.registered_address,
        ))

    conn.commit()
    return entity_id


def upsert_person_node(conn, full_name: str, roles: list = None) -> str:
    """Insert or update person_node, return its UUID."""
    cur = conn.cursor()
    normalized = normalize_person_name(full_name)

    cur.execute(
        "SELECT id FROM person_node WHERE normalized_name = %s",
        (normalized,)
    )
    row = cur.fetchone()

    if row:
        person_id = str(row[0])
        if roles:
            cur.execute("""
                UPDATE person_node
                SET roles = roles || %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s
            """, (json.dumps(roles), person_id))
    else:
        person_id = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO person_node (id, full_name, normalized_name, roles, created_at, updated_at)
            VALUES (%s, %s, %s, %s, NOW(), NOW())
        """, (person_id, full_name, normalized, json.dumps(roles or [])))

    conn.commit()
    return person_id


def link_entity_person(conn, entity_id: str, person_id: str, position: str,
                       source: str = "comptroller", confidence: float = 0.9):
    """Create entity_person link if not exists."""
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM entity_person
        WHERE entity_id = %s AND person_id = %s AND position = %s
    """, (entity_id, person_id, position))

    if not cur.fetchone():
        cur.execute("""
            INSERT INTO entity_person (id, entity_id, person_id, position, source,
                confidence, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
        """, (str(uuid.uuid4()), entity_id, person_id, position, source, confidence))
        conn.commit()


def update_owner_party_resolution(conn, owner_party_id: str, entity_id: str,
                                  person_id: Optional[str] = None,
                                  confidence: float = 0.85,
                                  method: str = "comptroller_pipeline"):
    """Update owner_party with resolved entity/person."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE owner_party SET
            resolved_entity_id = %s,
            resolved_person_id = COALESCE(%s, resolved_person_id),
            resolution_confidence = %s,
            resolution_method = %s,
            updated_at = NOW()
        WHERE id = %s
    """, (entity_id, person_id, confidence, method, owner_party_id))
    conn.commit()


def store_deed_as_document_event(conn, deed: ClerkDeedRecord, person_name: str):
    """Store a clerk deed record as a document_event if it doesn't already exist."""
    if not deed.instrument_number:
        return

    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM document_event
        WHERE instrument_number = %s
          AND source LIKE 'collin_clerk%%'
    """, (deed.instrument_number,))
    if cur.fetchone():
        return  # Already exists

    mapped = DOC_TYPE_MAP.get(
        deed.doc_type.upper(),
        (deed.doc_type.lower().replace(" ", "_") if deed.doc_type else "unknown", "other")
    )

    cur.execute("""
        INSERT INTO document_event (
            id, instrument_number, doc_type, doc_category,
            recording_date, grantor_raw, grantee_raw, consideration,
            source, raw_payload, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
    """, (
        str(uuid.uuid4()),
        deed.instrument_number,
        mapped[0],
        mapped[1],
        deed.recording_date,
        "; ".join(deed.grantors) if deed.grantors else None,
        "; ".join(deed.grantees) if deed.grantees else None,
        deed.consideration,
        f"collin_clerk_name_search:{person_name}",
        json.dumps({
            "legal_description": deed.legal_description,
            "detail_url": deed.detail_url,
            "search_person": person_name,
        }),
    ))
    conn.commit()


def find_matching_owner_parties(conn, person_name: str) -> List[Tuple[str, str, int]]:
    """Find owner_party records where the person appears as grantor/grantee.
    Returns list of (owner_party_id, raw_name, property_count)."""
    normalized = normalize_person_name(person_name)
    # Search for LLCs where this person might be an officer via entity_person links
    cur = conn.cursor()
    cur.execute("""
        SELECT op.id::text, op.raw_name, COUNT(DISTINCT po.property_id) as props
        FROM owner_party op
        JOIN property_ownership po ON po.owner_party_id = op.id
        JOIN property p ON po.property_id = p.id
        WHERE p.county = 'Collin'
          AND op.party_type = 'llc'
          AND op.resolved_person_id IN (
              SELECT id FROM person_node WHERE normalized_name = %s
          )
        GROUP BY op.id, op.raw_name
        ORDER BY props DESC
    """, (normalized,))
    return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def find_llcs_in_deeds(conn, person_name: str, deed_records: List[ClerkDeedRecord]) -> List[Tuple[str, str, int]]:
    """Find LLC owner_parties that appear in deed records as grantor/grantee.
    Cross-links person to LLCs they transact with."""
    llc_names = set()
    for deed in deed_records:
        for name in deed.grantors + deed.grantees:
            upper = name.upper()
            if any(kw in upper for kw in ("LLC", "LP", "INC", "CORP", "LTD", "TRUST")):
                llc_names.add(upper.strip())

    if not llc_names:
        return []

    cur = conn.cursor()
    results = []
    for llc_name in llc_names:
        # Fuzzy match against owner_party
        cur.execute("""
            SELECT op.id::text, op.raw_name, COUNT(DISTINCT po.property_id) as props
            FROM owner_party op
            JOIN property_ownership po ON po.owner_party_id = op.id
            JOIN property p ON po.property_id = p.id
            WHERE p.county = 'Collin'
              AND op.party_type = 'llc'
              AND UPPER(op.raw_name) = %s
            GROUP BY op.id, op.raw_name
        """, (llc_name,))
        for r in cur.fetchall():
            results.append((r[0], r[1], r[2]))

    return results


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

def normalize_person_name(name: str) -> str:
    """Normalize a person name for matching."""
    name = name.upper().strip()
    # Remove common suffixes
    for suffix in (",", " JR", " SR", " II", " III", " IV"):
        name = name.replace(suffix, "")
    # Remove extra whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


def is_person_name(name: str) -> bool:
    """Heuristic: is this a person name vs a company name?"""
    upper = name.upper()
    company_indicators = ("LLC", "LP", "INC", "CORP", "LTD", "TRUST", "COMPANY",
                          "PARTNERS", "ASSOCIATION", "FOUNDATION", "HOLDINGS",
                          "INVESTMENTS", "PROPERTIES", "REALTY", "DEVELOPMENT",
                          "CORPORATION", "ECONOMIC")
    return not any(kw in upper for kw in company_indicators)


def extract_person_names_from_officers(officers: List[OfficerInfo]) -> List[OfficerInfo]:
    """Filter officer list to only actual person names (not nested LLCs)."""
    persons = []
    for off in officers:
        if is_person_name(off.name):
            persons.append(off)
    return persons


def clean_llc_name_for_search(raw_name: str) -> str:
    """Clean an LLC name for Comptroller search.
    Remove common suffixes that might prevent matching."""
    name = raw_name.strip()
    # The comptroller search is fuzzy enough, but let's try exact first
    return name


# ---------------------------------------------------------------------------
# Step 1: TX Comptroller search
# ---------------------------------------------------------------------------

def search_comptroller_by_name(llc_name: str) -> List[dict]:
    """Search TX Comptroller franchise-tax API by entity name.
    Returns list of matching entity dicts (taxpayerId, name, mailingAddressZip)."""
    clean_name = clean_llc_name_for_search(llc_name)
    url = f"{COMPTROLLER_SEARCH_URL}?name={urllib.parse.quote(clean_name)}"

    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        })
        resp.raise_for_status()
        data = resp.json()
        if data.get("success") and data.get("data"):
            return data["data"] if isinstance(data["data"], list) else [data["data"]]
    except Exception as e:
        log.warning("Comptroller search failed for %s: %s", llc_name, e)

    return []


def get_comptroller_detail(taxpayer_id: str) -> Optional[dict]:
    """Get full entity detail from TX Comptroller by taxpayerID."""
    url = f"{COMPTROLLER_SEARCH_URL}/{taxpayer_id}"

    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        })
        resp.raise_for_status()
        data = resp.json()
        if data.get("success") and data.get("data"):
            return data["data"]
    except Exception as e:
        log.warning("Comptroller detail failed for %s: %s", taxpayer_id, e)

    return None


def resolve_llc_via_comptroller(llc_name: str) -> Optional[EntityInfo]:
    """Full Comptroller lookup: search + detail.
    Returns EntityInfo with officers, or None."""

    # Step 1: Search by name — try without suffix first (better hit rate)
    simplified = re.sub(
        r"[\s,.]*(LLC|L\.L\.C\.|LP|L\.P\.|INC|CORP|LTD|CORPORATION|COMPANY|CO)\s*\.?\s*$",
        "", llc_name.strip(), flags=re.I
    ).strip()

    matches = []
    if simplified and simplified != llc_name.strip():
        matches = search_comptroller_by_name(simplified)

    if not matches:
        matches = search_comptroller_by_name(llc_name)

    if not matches:
        log.info("  No Comptroller match for: %s", llc_name)
        return None

    # Find best match: exact name match preferred
    best_match = None
    llc_upper = llc_name.upper().strip()
    for m in matches:
        if m.get("name", "").upper().strip() == llc_upper:
            best_match = m
            break
    if not best_match:
        # Take the first partial match
        best_match = matches[0]
        log.info("  No exact match; using closest: %s", best_match.get("name"))

    taxpayer_id = best_match.get("taxpayerId")
    if not taxpayer_id:
        return None

    # Step 2: Get detail page (has officers)
    time.sleep(REQUEST_DELAY_SEC)
    detail = get_comptroller_detail(taxpayer_id)
    if not detail:
        log.info("  No Comptroller detail for taxpayerID: %s", taxpayer_id)
        return None

    # Parse into EntityInfo
    reg_addr_parts = [
        detail.get("registeredOfficeAddressStreet", ""),
        detail.get("registeredOfficeAddressCity", ""),
        detail.get("registeredOfficeAddressState", ""),
        detail.get("registeredOfficeAddressZip", ""),
    ]
    reg_addr = ", ".join(p for p in reg_addr_parts if p)

    mail_parts = [
        detail.get("mailingAddressStreet", ""),
        detail.get("mailingAddressCity", ""),
        detail.get("mailingAddressState", ""),
        detail.get("mailingAddressZip", ""),
    ]
    mail_addr = ", ".join(p for p in mail_parts if p)

    entity = EntityInfo(
        name=detail.get("name", llc_name),
        taxpayer_id=str(taxpayer_id),
        sos_file_number=detail.get("sosFileNumber", ""),
        formation_state=detail.get("stateOfFormation", ""),
        sos_status=detail.get("sosRegistrationStatus", ""),
        right_to_transact=detail.get("rightToTransactTX", ""),
        effective_date=detail.get("effectiveSosRegistrationDate", ""),
        registered_agent=detail.get("registeredAgentName", ""),
        registered_address=reg_addr,
        mailing_address=mail_addr,
        raw_data=detail,
    )

    # Parse officers
    for off in detail.get("officerInfo", []):
        officer = OfficerInfo(
            name=off.get("AGNT_NM", "").strip(),
            title=off.get("AGNT_TITL_TX", "").strip(),
            address=off.get("AD_STR_POB_TX", "").strip(),
            city=off.get("CITY_NM", "").strip(),
            state=off.get("ST_CD", "").strip(),
            zip_code=off.get("AD_ZP", "").strip(),
            source="comptroller",
        )
        if officer.name:
            entity.officers.append(officer)

    # Also treat registered agent as a potential person
    if entity.registered_agent and is_person_name(entity.registered_agent):
        entity.officers.append(OfficerInfo(
            name=entity.registered_agent,
            title="REGISTERED AGENT",
            source="comptroller",
        ))

    return entity


# ---------------------------------------------------------------------------
# Step 2: Collin County Clerk name search (Playwright)
# ---------------------------------------------------------------------------

def parse_date_str(s: str) -> Optional[date]:
    """Parse various date formats from the clerk portal."""
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%b %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_consideration(s: str) -> Optional[float]:
    """Parse dollar amount."""
    if not s:
        return None
    cleaned = re.sub(r"[^\d.]", "", s)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def search_clerk_by_name(page: Page, person_name: str,
                         delay: float = REQUEST_DELAY_SEC) -> List[ClerkDeedRecord]:
    """Search Collin County Clerk portal for all deed records matching a person's name.
    Uses the quickSearch endpoint. Returns list of ClerkDeedRecord."""

    encoded = urllib.parse.quote_plus(person_name)
    search_url = (
        f"{CLERK_BASE_URL}/results?department=RP"
        f"&limit=50"
        f"&searchOfficialRecords=true"
        f"&searchOcrText=false"
        f"&searchType=quickSearch"
        f"&searchValue={encoded}"
    )

    log.info("  Clerk search for: %s", person_name)

    # Retry logic — the Kofile React SPA can be slow to render
    rows_found = False
    for attempt in range(3):
        page.goto(search_url, timeout=30000)
        try:
            page.wait_for_selector(
                'table tbody tr[role="row"]',
                timeout=20000
            )
            rows_found = True
            break
        except PwTimeout:
            # Check if it's a genuine "no results" vs still loading
            body_text = page.inner_text("body")
            if "0 results" in body_text or "no results" in body_text.lower()[:300]:
                log.info("    No clerk results for: %s", person_name)
                return []
            if attempt < 2:
                log.debug("    Clerk page still loading, retry %d...", attempt + 1)
                time.sleep(3)

    # Extra settle time for dynamic rendering
    time.sleep(2)

    if not rows_found:
        body_text = page.inner_text("body")
        row_count = page.evaluate(
            'document.querySelectorAll("table tbody tr[role=row]").length'
        )
        if row_count == 0:
            log.info("    Clerk page did not load results for: %s", person_name)
            return []

    body_text = page.inner_text("body")
    if "0 results" in body_text or "No results" in body_text.lower()[:300]:
        log.info("    No clerk results for: %s", person_name)
        return []

    # Extract results count from facets like "2010-2019 (3)"
    facet_counts = re.findall(r"\((\d+)\)", body_text[:1000])
    result_count = sum(int(c) for c in facet_counts) if facet_counts else 0
    if not result_count:
        count_match = re.search(r"(\d+)\s+results?", body_text[:500])
        result_count = int(count_match.group(1)) if count_match else 0
    log.info("    Found ~%d clerk results for: %s", result_count, person_name)

    # Parse all visible result rows
    records = _parse_result_rows(page)

    # If there are more results, try to paginate
    if result_count > 50 and len(records) < result_count:
        records.extend(_paginate_results(page, delay, max_pages=4))

    return records


def _parse_result_rows(page: Page) -> List[ClerkDeedRecord]:
    """Parse visible result rows from the Collin clerk search results page.

    Collin County Kofile table columns (0-indexed):
      [0] checkbox  [1] actions  [2] status icons
      [3] Grantor   [4] Grantee  [5] Doc Type
      [6] Recorded Date  [7] Doc Number  [8] Book/Volume/Page
      [9] Legal Description
    """
    records = []

    rows_data = page.evaluate("""() => {
        const rows = Array.from(document.querySelectorAll('table tbody tr[role="row"]'));
        return rows.map(row => {
            const cells = Array.from(row.querySelectorAll('td'));
            const link = row.querySelector('a[href*="/doc/"]');
            const href = link ? link.getAttribute('href') : '';
            return {
                cells: cells.map(td => td.innerText.trim()),
                href: href,
            };
        });
    }""")

    for rd in rows_data:
        cells = rd.get("cells", [])
        if not cells or len(cells) < 8:
            continue

        record = ClerkDeedRecord()

        # Column 3: Grantor
        if cells[3]:
            record.grantors = [g.strip() for g in cells[3].split("\n") if g.strip()]

        # Column 4: Grantee
        if cells[4]:
            record.grantees = [g.strip() for g in cells[4].split("\n") if g.strip()]

        # Column 5: Doc Type
        record.doc_type = cells[5].strip()

        # Column 6: Recorded Date
        record.recording_date = parse_date_str(cells[6])

        # Column 7: Doc Number (instrument number)
        record.instrument_number = cells[7].strip()

        # Column 9: Legal Description (if present)
        if len(cells) > 9 and cells[9]:
            record.legal_description = cells[9].strip()

        # Detail URL
        href = rd.get("href", "")
        if href:
            record.detail_url = f"{CLERK_BASE_URL}{href}" if href.startswith("/") else href

        if record.instrument_number:
            records.append(record)

    return records


def _paginate_results(page: Page, delay: float, max_pages: int = 4) -> List[ClerkDeedRecord]:
    """Click through pagination to get more results."""
    all_records = []
    for _ in range(max_pages):
        try:
            next_btn = page.query_selector('button[aria-label="next page"], .pagination-next, [class*="next"]')
            if not next_btn or not next_btn.is_enabled():
                break
            next_btn.click()
            time.sleep(delay + 2)
            page.wait_for_selector('tr[role="row"]', timeout=10000)
            time.sleep(1)
            new_records = _parse_result_rows(page)
            if not new_records:
                break
            all_records.extend(new_records)
        except (PwTimeout, Exception) as e:
            log.debug("    Pagination stopped: %s", e)
            break
    return all_records


def extract_deed_detail(page: Page, detail_url: str,
                        delay: float = REQUEST_DELAY_SEC) -> Optional[ClerkDeedRecord]:
    """Navigate to a deed detail page and extract full info (parties, legal desc)."""
    try:
        page.goto(detail_url, timeout=30000)
        time.sleep(delay + 1)

        try:
            page.wait_for_selector(".doc-preview__summary", timeout=10000)
        except PwTimeout:
            return None

        record = ClerkDeedRecord(detail_url=detail_url)

        # Doc type
        header = page.query_selector(".doc-preview__summary-header")
        if header:
            record.doc_type = header.inner_text().strip()

        # Summary fields
        items = page.query_selector_all(".doc-preview-summary__column-list-item")
        for item in items:
            spans = item.query_selector_all(".doc-preview-summary__column-span")
            if len(spans) >= 2:
                label = spans[0].inner_text().strip().rstrip(":")
                value = spans[1].inner_text().strip()

                if label == "Recorded Date":
                    record.recording_date = parse_date_str(value)
                elif label == "Consideration":
                    record.consideration = parse_consideration(value)
                elif "Instrument" in label and "Number" in label:
                    record.instrument_number = value

        # Parties
        party_data = page.evaluate("""() => {
            const el = document.querySelector('[data-testid="docPreviewParty"]');
            if (!el) return {grantors: [], grantees: []};

            const grantors = [];
            const grantees = [];
            const children = el.children;
            let currentRole = '';

            for (let i = 0; i < children.length; i++) {
                const child = children[i];
                if (child.classList.contains('doc-preview-group__summary-group-label')) {
                    currentRole = child.textContent.trim().toUpperCase();
                } else if (child.tagName === 'A') {
                    const name = child.textContent.trim();
                    if (currentRole.includes('GRANTOR')) {
                        grantors.push(name);
                    } else if (currentRole.includes('GRANTEE')) {
                        grantees.push(name);
                    }
                }
            }
            return {grantors, grantees};
        }""")

        record.grantors = party_data.get("grantors", [])
        record.grantees = party_data.get("grantees", [])

        # Legal description
        legal_el = page.query_selector(".doc-preview__legal-description, [data-testid='legalDescription']")
        if legal_el:
            record.legal_description = legal_el.inner_text().strip()

        return record

    except Exception as e:
        log.warning("    Failed to extract deed detail from %s: %s", detail_url, e)
        return None


def search_and_extract_deeds(page: Page, person_name: str,
                             delay: float = REQUEST_DELAY_SEC,
                             max_details: int = 25) -> List[ClerkDeedRecord]:
    """Full clerk workflow: search by name, then extract details for top results."""
    # First get search results (list view)
    records = search_clerk_by_name(page, person_name, delay)

    if not records:
        return []

    # Filter to deed-type records
    deed_records = [r for r in records if r.doc_type.upper() in DEED_TYPES or not r.doc_type]

    # For records that have detail URLs, extract full info (up to max_details)
    detailed = []
    detail_urls = [r.detail_url for r in deed_records if r.detail_url][:max_details]

    if detail_urls:
        log.info("    Extracting details for %d deed records...", len(detail_urls))
        for url in detail_urls:
            time.sleep(delay)
            det = extract_deed_detail(page, url, delay)
            if det:
                detailed.append(det)

    # Merge: use detailed records where available, keep list-only records for the rest
    detailed_instruments = {d.instrument_number for d in detailed if d.instrument_number}
    merged = list(detailed)
    for r in deed_records:
        if r.instrument_number and r.instrument_number not in detailed_instruments:
            merged.append(r)

    return merged


# ---------------------------------------------------------------------------
# Step 3: Cross-link and store
# ---------------------------------------------------------------------------

def cross_link_person_to_llcs(conn, person_id: str, person_name: str,
                              entity_id: str, original_llc_id: str,
                              deed_records: List[ClerkDeedRecord]) -> Tuple[int, int]:
    """Cross-link a person to all LLCs found in their deed records.
    Returns (linked_via_other_llcs_count, total_portfolio_properties)."""

    other_llcs = find_llcs_in_deeds(conn, person_name, deed_records)
    other_llc_count = 0
    total_props = 0

    for llc_op_id, llc_name, prop_count in other_llcs:
        if llc_op_id == original_llc_id:
            continue

        # Check if this LLC already has an entity_record
        cur = conn.cursor()
        cur.execute("SELECT resolved_entity_id FROM owner_party WHERE id = %s", (llc_op_id,))
        row = cur.fetchone()
        other_entity_id = str(row[0]) if row and row[0] else None

        if other_entity_id:
            # Create entity_relationship between the two LLCs via shared person
            cur.execute("""
                SELECT id FROM entity_relationship
                WHERE entity_a_id = %s AND entity_b_id = %s AND relationship = 'shared_officer'
            """, (entity_id, other_entity_id))
            if not cur.fetchone():
                cur.execute("""
                    INSERT INTO entity_relationship (id, entity_a_id, entity_b_id,
                        relationship, confidence, evidence, created_at)
                    VALUES (%s, %s, %s, 'shared_officer', 0.8, %s, NOW())
                """, (
                    str(uuid.uuid4()), entity_id, other_entity_id,
                    json.dumps({"person": person_name, "source": "collin_clerk_deeds"}),
                ))
                conn.commit()

        # Link person to this LLC's owner_party
        update_owner_party_resolution(
            conn, llc_op_id, other_entity_id or entity_id,
            person_id, confidence=0.7,
            method="comptroller_pipeline_crosslink"
        )

        other_llc_count += 1
        total_props += prop_count

    return other_llc_count, total_props


def upsert_ownership_resolution(conn, property_id: str, direct_owner_id: str,
                                person_id: str, entity_id: str,
                                confidence: float = 0.85):
    """Create/update ownership_resolution linking property -> person."""
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM ownership_resolution
        WHERE property_id = %s AND direct_owner_id = %s
    """, (property_id, direct_owner_id))
    row = cur.fetchone()

    resolution_chain = json.dumps({
        "steps": [
            {"type": "llc", "entity_id": entity_id, "source": "owner_party"},
            {"type": "officer", "person_id": person_id, "source": "comptroller"},
        ]
    })

    if row:
        cur.execute("""
            UPDATE ownership_resolution SET
                beneficial_person_id = %s,
                confidence = %s,
                resolution_chain = %s,
                computed_at = NOW()
            WHERE id = %s
        """, (person_id, confidence, resolution_chain, str(row[0])))
    else:
        cur.execute("""
            INSERT INTO ownership_resolution (id, property_id, direct_owner_id,
                beneficial_owner_id, beneficial_person_id, confidence,
                resolution_chain, computed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
        """, (
            str(uuid.uuid4()), property_id, direct_owner_id,
            entity_id, person_id, confidence, resolution_chain,
        ))

    conn.commit()


def resolve_properties_for_person(conn, entity_id: str, person_id: str,
                                  owner_party_id: str):
    """Create ownership_resolution for all properties owned by this owner_party."""
    cur = conn.cursor()
    cur.execute("""
        SELECT po.property_id::text
        FROM property_ownership po
        WHERE po.owner_party_id = %s AND po.is_current = true
    """, (owner_party_id,))

    count = 0
    for (prop_id,) in cur.fetchall():
        upsert_ownership_resolution(conn, prop_id, owner_party_id,
                                    person_id, entity_id)
        count += 1

    return count


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_one_llc(conn, llc_name: str, llc_id: str, prop_count: int,
                    page: Optional[Page] = None,
                    delay: float = REQUEST_DELAY_SEC) -> PipelineResult:
    """Process a single LLC through the full pipeline."""
    result = PipelineResult(
        llc_name=llc_name,
        llc_id=llc_id,
        property_count=prop_count,
    )

    # Step 1: TX Comptroller search
    log.info("Processing: %s (%d properties)", llc_name, prop_count)
    entity_info = resolve_llc_via_comptroller(llc_name)

    if not entity_info:
        result.error = "no_comptroller_match"
        log.info("  SKIP: No Comptroller match for %s", llc_name)
        return result

    result.entity_info = entity_info
    log.info("  Comptroller: %s (SOS: %s, Status: %s)",
             entity_info.name, entity_info.sos_file_number, entity_info.sos_status)

    # Store entity record
    entity_id = upsert_entity_record(conn, entity_info)
    update_owner_party_resolution(conn, llc_id, entity_id,
                                  method="comptroller_pipeline")

    # Get person names from officers
    person_officers = extract_person_names_from_officers(entity_info.officers)
    result.officers_found = entity_info.officers

    if not person_officers:
        # All officers are other LLCs, not people
        log.info("  Officers are all entities (no people): %s",
                 [o.name for o in entity_info.officers])
        # Still store entity-level officers
        for off in entity_info.officers:
            if off.name:
                # These are entity officers (other LLCs)
                log.info("    Officer (entity): %s (%s)", off.name, off.title)

        if entity_info.registered_agent and is_person_name(entity_info.registered_agent):
            person_officers = [OfficerInfo(
                name=entity_info.registered_agent,
                title="REGISTERED AGENT",
                source="comptroller",
            )]
        else:
            result.error = "no_person_officers"
            return result

    # Store person nodes and links
    primary_person_id = None
    for off in person_officers:
        log.info("  Officer (person): %s (%s)", off.name, off.title)
        person_id = upsert_person_node(conn, off.name, roles=[off.title])
        link_entity_person(conn, entity_id, person_id, off.title)

        if primary_person_id is None:
            primary_person_id = person_id

        # Update owner_party with the first person
        update_owner_party_resolution(
            conn, llc_id, entity_id, person_id,
            confidence=0.85, method="comptroller_pipeline"
        )

    # Resolve properties -> person
    resolved_count = resolve_properties_for_person(
        conn, entity_id, primary_person_id, llc_id
    )
    result.linked_properties = resolved_count

    # Step 2: Collin County Clerk search (if Playwright page available)
    if page and person_officers:
        for off in person_officers[:2]:  # Search top 2 persons
            time.sleep(delay)
            deeds = search_and_extract_deeds(page, off.name, delay, max_details=15)
            result.clerk_records[off.name] = deeds

            if deeds:
                log.info("  Clerk: %d deed records for %s", len(deeds), off.name)

                # Store deed records
                for deed in deeds:
                    store_deed_as_document_event(conn, deed, off.name)

                # Step 3: Cross-link
                other_count, other_props = cross_link_person_to_llcs(
                    conn, primary_person_id, off.name,
                    entity_id, llc_id, deeds
                )
                result.linked_via_other_llcs += other_count
                result.total_portfolio = result.linked_properties + other_props
            else:
                log.info("  Clerk: No deed records for %s", off.name)

    if not result.total_portfolio:
        result.total_portfolio = result.linked_properties

    return result


def run_pipeline(batch_size: int = DEFAULT_BATCH_SIZE, headless: bool = True,
                 delay: float = REQUEST_DELAY_SEC, skip_resolved: bool = True):
    """Run the full ownership pipeline for top Collin County LLCs."""
    conn = get_connection()

    llcs = get_top_collin_llcs(conn, limit=batch_size)

    if skip_resolved:
        resolved = get_already_resolved(conn)
        llcs = [(n, i, c) for n, i, c in llcs if i not in resolved]
        log.info("After filtering already-resolved: %d LLCs to process", len(llcs))

    if not llcs:
        log.info("No LLCs to process. Use --no-skip to reprocess all.")
        conn.close()
        return

    results = []
    stats = {
        "total": len(llcs),
        "comptroller_found": 0,
        "officers_found": 0,
        "clerk_searched": 0,
        "clerk_deeds_found": 0,
        "properties_resolved": 0,
        "cross_linked": 0,
        "errors": 0,
    }

    # Launch Playwright for clerk searches
    log.info("Launching Playwright browser (headless=%s)...", headless)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        for idx, (llc_name, llc_id, prop_count) in enumerate(llcs):
            log.info("=" * 70)
            log.info("[%d/%d] %s (%d properties)",
                     idx + 1, len(llcs), llc_name, prop_count)

            try:
                result = process_one_llc(
                    conn, llc_name, llc_id, prop_count,
                    page=page, delay=delay,
                )
                results.append(result)

                # Update stats
                if result.entity_info:
                    stats["comptroller_found"] += 1
                if result.officers_found:
                    stats["officers_found"] += 1
                total_deeds = sum(len(d) for d in result.clerk_records.values())
                if total_deeds:
                    stats["clerk_searched"] += 1
                    stats["clerk_deeds_found"] += total_deeds
                stats["properties_resolved"] += result.linked_properties
                stats["cross_linked"] += result.linked_via_other_llcs
                if result.error:
                    stats["errors"] += 1

                # Print result summary
                _print_result(result)

            except Exception as e:
                log.error("FAILED processing %s: %s", llc_name, e, exc_info=True)
                stats["errors"] += 1

            # Rate limit between LLCs
            time.sleep(delay)

        browser.close()

    conn.close()

    # Final summary
    log.info("=" * 70)
    log.info("PIPELINE COMPLETE")
    log.info("=" * 70)
    for k, v in stats.items():
        log.info("  %-25s %s", k, v)


def _print_result(result: PipelineResult):
    """Print a formatted result for one LLC."""
    print(f"\n  LLC: {result.llc_name} ({result.property_count} properties)")

    if result.entity_info:
        for off in result.officers_found:
            label = "Person" if is_person_name(off.name) else "Entity"
            print(f"    TX Comptroller Officer: {off.name} ({off.title}) [{label}]")
        if result.entity_info.registered_agent:
            print(f"    Registered Agent: {result.entity_info.registered_agent}")
        print(f"    SOS Status: {result.entity_info.sos_status}")

    for person_name, deeds in result.clerk_records.items():
        print(f"    Collin Clerk: {len(deeds)} deed transactions found for {person_name}")

    if result.linked_properties:
        print(f"    Properties linked: {result.linked_properties} through {result.llc_name}")

    if result.linked_via_other_llcs:
        print(f"    + {result.linked_via_other_llcs} additional LLCs cross-linked")

    print(f"    Total portfolio: {result.total_portfolio} properties")

    if result.error:
        print(f"    Error: {result.error}")

    print()


# ---------------------------------------------------------------------------
# Stats command
# ---------------------------------------------------------------------------

def show_stats():
    """Show pipeline resolution statistics."""
    conn = get_connection()
    cur = conn.cursor()

    print("\n" + "=" * 70)
    print("COLLIN COUNTY OWNERSHIP PIPELINE - STATISTICS")
    print("=" * 70)

    # Total LLCs in Collin
    cur.execute("""
        SELECT COUNT(DISTINCT op.id)
        FROM owner_party op
        JOIN property_ownership po ON po.owner_party_id = op.id
        JOIN property p ON po.property_id = p.id
        WHERE p.county = 'Collin' AND op.party_type = 'llc'
    """)
    total_llcs = cur.fetchone()[0]
    print(f"\n  Total Collin County LLCs: {total_llcs}")

    # Resolved LLCs
    cur.execute("""
        SELECT COUNT(DISTINCT op.id)
        FROM owner_party op
        JOIN property_ownership po ON po.owner_party_id = op.id
        JOIN property p ON po.property_id = p.id
        WHERE p.county = 'Collin' AND op.party_type = 'llc'
          AND op.resolved_entity_id IS NOT NULL
    """)
    resolved_llcs = cur.fetchone()[0]
    print(f"  Resolved LLCs: {resolved_llcs} ({resolved_llcs*100//max(total_llcs,1)}%)")

    # Entity records
    cur.execute("SELECT COUNT(*) FROM entity_record WHERE entity_type = 'llc'")
    print(f"  Entity records: {cur.fetchone()[0]}")

    # Person nodes
    cur.execute("SELECT COUNT(*) FROM person_node")
    print(f"  Person nodes: {cur.fetchone()[0]}")

    # Entity-person links
    cur.execute("SELECT COUNT(*) FROM entity_person")
    print(f"  Entity-person links: {cur.fetchone()[0]}")

    # Entity relationships
    cur.execute("SELECT COUNT(*) FROM entity_relationship WHERE relationship = 'shared_officer'")
    print(f"  Shared-officer relationships: {cur.fetchone()[0]}")

    # Ownership resolutions with beneficial person
    cur.execute("""
        SELECT COUNT(*) FROM ownership_resolution
        WHERE beneficial_person_id IS NOT NULL
    """)
    print(f"  Properties with beneficial person resolved: {cur.fetchone()[0]}")

    # Top resolved persons
    cur.execute("""
        SELECT pn.full_name,
               COUNT(DISTINCT ep.entity_id) as entities,
               COUNT(DISTINCT ores.property_id) as properties
        FROM person_node pn
        JOIN entity_person ep ON ep.person_id = pn.id
        LEFT JOIN ownership_resolution ores ON ores.beneficial_person_id = pn.id
        GROUP BY pn.id, pn.full_name
        ORDER BY properties DESC
        LIMIT 20
    """)
    rows = cur.fetchall()
    if rows:
        print(f"\n  Top beneficial owners:")
        print(f"  {'Person':<40} {'LLCs':>6} {'Properties':>12}")
        print(f"  {'-'*40} {'-'*6} {'-'*12}")
        for name, ents, props in rows:
            print(f"  {name:<40} {ents:>6} {props:>12}")

    # Document events from pipeline
    cur.execute("""
        SELECT COUNT(*) FROM document_event
        WHERE source LIKE 'collin_clerk_name_search:%'
    """)
    print(f"\n  Deed records from name searches: {cur.fetchone()[0]}")

    print("\n" + "=" * 70)
    conn.close()


# ---------------------------------------------------------------------------
# Single LLC lookup
# ---------------------------------------------------------------------------

def lookup_llc(llc_name: str, headless: bool = True, delay: float = REQUEST_DELAY_SEC):
    """Lookup a single LLC and display full details."""
    conn = get_connection()

    # Find in DB
    cur = conn.cursor()
    cur.execute("""
        SELECT op.id::text, op.raw_name, COUNT(DISTINCT po.property_id) as props
        FROM owner_party op
        JOIN property_ownership po ON po.owner_party_id = op.id
        JOIN property p ON po.property_id = p.id
        WHERE p.county = 'Collin' AND op.party_type = 'llc'
          AND UPPER(op.raw_name) = %s
        GROUP BY op.id, op.raw_name
    """, (llc_name.upper(),))
    row = cur.fetchone()

    if not row:
        log.error("LLC not found in database: %s", llc_name)
        conn.close()
        return

    llc_id, raw_name, prop_count = row[0], row[1], row[2]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        result = process_one_llc(conn, raw_name, llc_id, prop_count,
                                 page=page, delay=delay)

        print("\n" + "=" * 70)
        _print_result(result)

        # Show deed details
        for person_name, deeds in result.clerk_records.items():
            if deeds:
                print(f"\n  Deed records for {person_name}:")
                for d in deeds[:20]:
                    print(f"    {d.recording_date or '???'} | {d.doc_type or 'UNKNOWN':<25} | "
                          f"Instr: {d.instrument_number}")
                    if d.grantors:
                        print(f"      Grantors: {', '.join(d.grantors)}")
                    if d.grantees:
                        print(f"      Grantees: {', '.join(d.grantees)}")

        browser.close()

    conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Collin County LLC Ownership Pipeline"
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # run command
    run_p = sub.add_parser("run", help="Run the pipeline for top LLCs")
    run_p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                       help="Number of LLCs to process (default: 100)")
    run_p.add_argument("--headless", action="store_true", default=True,
                       help="Run browser in headless mode (default)")
    run_p.add_argument("--no-headless", dest="headless", action="store_false",
                       help="Show browser window")
    run_p.add_argument("--delay", type=float, default=REQUEST_DELAY_SEC,
                       help="Delay between requests in seconds (default: 2)")
    run_p.add_argument("--no-skip", dest="skip_resolved", action="store_false",
                       help="Reprocess already-resolved LLCs")

    # stats command
    sub.add_parser("stats", help="Show pipeline statistics")

    # lookup command
    lookup_p = sub.add_parser("lookup", help="Lookup a single LLC")
    lookup_p.add_argument("--llc", required=True, help="LLC name to look up")
    lookup_p.add_argument("--no-headless", dest="headless", action="store_false",
                          default=True, help="Show browser window")
    lookup_p.add_argument("--delay", type=float, default=REQUEST_DELAY_SEC,
                          help="Delay between requests")

    args = parser.parse_args()

    if args.command == "run":
        run_pipeline(
            batch_size=args.batch_size,
            headless=args.headless,
            delay=args.delay,
            skip_resolved=args.skip_resolved,
        )
    elif args.command == "stats":
        show_stats()
    elif args.command == "lookup":
        lookup_llc(args.llc, headless=args.headless, delay=args.delay)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
