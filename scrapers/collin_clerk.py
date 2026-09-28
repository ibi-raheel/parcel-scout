"""
Collin County Clerk document scraper.

Scrapes deed and mortgage data from the Collin County Clerk's Kofile/GovOS
publicsearch platform at https://collin.tx.publicsearch.us/

Same Kofile platform as Dallas — identical UI structure.

Collin County has instrument numbers already stored in document_event rows
(format: 20211217002548110 — 17-digit YYYYMMDD + sequence).

Workflow:
  1. Look up Collin document_event rows that have instrument_number but no fetched_at
  2. For each, search the clerk portal and extract full document metadata
  3. Update document_event rows with grantor/grantee/consideration data

Address-search workflow:
  1. Query Collin properties with no grantor_raw
  2. For each, perform a quickSearch on the clerk portal by address
  3. Extract grantor/grantee from the best matching deed result

Rate limit: 1 request per 2 seconds (configurable).
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
from typing import Optional, List, Tuple

import psycopg2
import psycopg2.extras
from playwright.sync_api import sync_playwright, Page, Browser, TimeoutError as PwTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_DSN = "postgresql://parcelscout:parcelscout_dev@localhost:5432/parcelscout"
BASE_URL = "https://collin.tx.publicsearch.us"
COUNTY_NAME = "Collin"
REQUEST_DELAY_SEC = 2.0
DEFAULT_BATCH_SIZE = 100
ADDRESS_BATCH_SIZE = 500
LOG_FMT = "%(asctime)s  %(levelname)-8s  %(message)s"

ADDRESS_PROGRESS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "collin_address_search_progress.json",
)

STREET_SUFFIX_MAP = {
    "AVENUE": "AVE",
    "BOULEVARD": "BLVD",
    "CIRCLE": "CIR",
    "COURT": "CT",
    "DRIVE": "DR",
    "EXPRESSWAY": "EXPY",
    "FREEWAY": "FWY",
    "HIGHWAY": "HWY",
    "LANE": "LN",
    "PARKWAY": "PKWY",
    "PLACE": "PL",
    "ROAD": "RD",
    "SQUARE": "SQ",
    "STREET": "ST",
    "TERRACE": "TER",
    "TRAIL": "TRL",
    "WAY": "WAY",
}

logging.basicConfig(format=LOG_FMT, level=logging.INFO, stream=sys.stdout)
log = logging.getLogger("collin_clerk")

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
    "MECHANIC LIEN": ("mechanic_lien", "lien"),
    "MECHANICS LIEN": ("mechanic_lien", "lien"),
    "LIS PENDENS": ("lis_pendens", "enforcement"),
    "JUDGMENT": ("judgment", "enforcement"),
    "ABSTRACT OF JUDGMENT": ("judgment", "enforcement"),
    "PLAT": ("plat", "notice"),
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ClerkDocument:
    """Parsed document from the Collin County Clerk portal."""
    instrument_number: str
    doc_type_raw: str = ""
    doc_type: str = ""
    doc_category: str = ""
    recording_date: Optional[date] = None
    instrument_date: Optional[date] = None
    num_pages: Optional[int] = None
    book_volume_page: str = ""
    consideration: Optional[float] = None
    grantors: list = field(default_factory=list)
    grantees: list = field(default_factory=list)
    legal_description: str = ""
    detail_url: str = ""
    raw_text: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_connection():
    return psycopg2.connect(DB_DSN)


def get_collin_instrument_numbers(conn, limit: int = None, offset: int = 0):
    """
    Return Collin document_event rows that have an instrument_number but no
    fetched_at yet.

    Returns list of (property_id, document_event_id, instrument_number).
    """
    sql = """
        SELECT p.id AS property_id,
               de.id AS document_event_id,
               de.instrument_number
        FROM document_event de
        JOIN property p ON de.property_id = p.id
        WHERE p.county = 'Collin'
          AND de.instrument_number IS NOT NULL
          AND de.instrument_number != ''
          AND de.fetched_at IS NULL
          AND (de.raw_payload IS NULL OR de.raw_payload->>'error' IS NULL)
        ORDER BY de.created_at
    """
    if limit:
        sql += f" LIMIT {limit} OFFSET {offset}"

    cur = conn.cursor()
    cur.execute(sql)
    rows = cur.fetchall()
    log.info("Found %d Collin instrument numbers to look up", len(rows))
    return rows


def update_document_event(conn, de_id: str, doc: "ClerkDocument"):
    """Update an existing document_event row with data from the clerk portal."""
    mapped = DOC_TYPE_MAP.get(doc.doc_type_raw.upper(), (None, None))
    doc_type = mapped[0] or doc.doc_type
    doc_category = mapped[1] or doc.doc_category

    cur = conn.cursor()
    cur.execute("""
        UPDATE document_event
        SET instrument_number = %s,
            doc_type = COALESCE(%s, doc_type),
            doc_category = COALESCE(%s, doc_category),
            recording_date = COALESCE(%s, recording_date),
            effective_date = COALESCE(%s, effective_date),
            grantor_raw = COALESCE(%s, grantor_raw),
            grantee_raw = COALESCE(%s, grantee_raw),
            consideration = COALESCE(%s, consideration),
            source_url = %s,
            raw_payload = %s,
            fetched_at = NOW()
        WHERE id = %s
    """, (
        doc.instrument_number,
        doc_type,
        doc_category,
        doc.recording_date,
        doc.instrument_date,
        "; ".join(doc.grantors) if doc.grantors else None,
        "; ".join(doc.grantees) if doc.grantees else None,
        doc.consideration,
        doc.detail_url,
        json.dumps(asdict(doc), default=str),
        de_id,
    ))
    conn.commit()


def mark_not_found(conn, de_id: str, instrument_number: str):
    """Mark a document_event as fetched but not found on the clerk portal."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE document_event
        SET instrument_number = %s,
            fetched_at = NOW(),
            raw_payload = %s
        WHERE id = %s
    """, (
        instrument_number,
        json.dumps({"error": "not_found_on_clerk_portal"}),
        de_id,
    ))
    conn.commit()


def upsert_debt_instrument(conn, property_id: str, de_id: str, doc: "ClerkDocument"):
    """For Deeds of Trust, create or update a debt_instrument record."""
    if doc.doc_category != "financing":
        return

    lender_name = None
    for g in doc.grantees:
        upper = g.upper()
        if any(kw in upper for kw in [
            "BANK", "MORTGAGE", "CREDIT", "LENDING", "FINANCIAL",
            "SAVINGS", "TRUST CO", "CAPITAL", "FUNDING", "LOAN",
            "SECRETARY OF HOUSING", "HUD", "FHA", "VA ", "FANNIE",
            "FREDDIE", "WELLS FARGO", "CHASE", "NATIONSTAR",
        ]):
            lender_name = g
            break
    if not lender_name and doc.grantees:
        lender_name = doc.grantees[0]

    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM debt_instrument WHERE document_event_id = %s",
        (de_id,),
    )
    existing = cur.fetchone()

    if existing:
        cur.execute("""
            UPDATE debt_instrument
            SET lender_name = COALESCE(%s, lender_name),
                original_amount = COALESCE(%s, original_amount),
                source = 'collin_clerk',
                updated_at = NOW()
            WHERE id = %s
        """, (lender_name, doc.consideration, existing[0]))
    else:
        cur.execute("""
            INSERT INTO debt_instrument (id, property_id, document_event_id,
                lender_name, original_amount, source, status, data_quality)
            VALUES (%s, %s, %s, %s, %s, 'collin_clerk', 'active', 'clerk_index')
        """, (str(uuid.uuid4()), property_id, de_id, lender_name, doc.consideration))

    conn.commit()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_date(text: str) -> Optional[date]:
    """Parse dates like '1/14/2025 8:01 AM', '10/29/2007 0:00 AM', or '01/10/2025'."""
    if not text or not text.strip():
        return None
    text = text.strip()
    date_part = re.split(r'\s+\d{1,2}:\d{2}', text)[0].strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(date_part, fmt).date()
        except ValueError:
            continue
    return None


def parse_consideration(text: str) -> Optional[float]:
    """Parse dollar amounts like '$1,234,567.89' or '1234567.89'."""
    if not text or not text.strip():
        return None
    cleaned = re.sub(r'[,$\s]', '', text.strip())
    if not cleaned or cleaned == '0' or cleaned == '0.00':
        return None
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Scraping logic
# ---------------------------------------------------------------------------

def search_instrument(page: Page, instrument_number: str) -> Optional[str]:
    """
    Search for a document by instrument number on the Collin clerk portal.
    Returns the detail page URL (/doc/{id}) if found, None otherwise.

    Collin instrument numbers look like: 20211217002548110 (17 digits)
    or older format: 0056251 (7 digits).
    """
    search_url = (
        f"{BASE_URL}/results?department=RP"
        f"&searchOcrText=false"
        f"&searchType=quickSearch"
        f"&searchValue={instrument_number}"
        f"&recordedDateRange=18000101,{datetime.now().strftime('%Y%m%d')}"
    )

    page.goto(search_url, timeout=30000)
    time.sleep(3)

    try:
        page.wait_for_selector('tr[role="row"], .no-results, [class*="empty"]', timeout=10000)
    except PwTimeout:
        pass

    body_text = page.inner_text("body")

    if "0 results" in body_text or "No results" in body_text:
        return None

    rows = page.query_selector_all('tr[role="row"]')
    for row in rows:
        row_text = row.inner_text()
        if instrument_number in row_text:
            row.click()
            time.sleep(2)

            if "/doc/" in page.url:
                return page.url

            try:
                page.wait_for_url(f"**/doc/**", timeout=5000)
                return page.url
            except PwTimeout:
                pass

    # If no exact match found in row text, click the first result row
    if rows:
        rows[0].click()
        time.sleep(2)
        if "/doc/" in page.url:
            return page.url

    return None


def extract_document_detail(page: Page, instrument_number: str) -> ClerkDocument:
    """Extract all document metadata from the detail page."""
    doc = ClerkDocument(instrument_number=instrument_number)
    doc.detail_url = page.url

    try:
        page.wait_for_selector(".doc-preview__summary", timeout=10000)
    except PwTimeout:
        doc.error = "detail_page_timeout"
        return doc

    doc.raw_text = page.inner_text("body")

    # -- Doc type (header)
    header = page.query_selector(".doc-preview__summary-header")
    if header:
        doc.doc_type_raw = header.inner_text().strip()
        mapped = DOC_TYPE_MAP.get(doc.doc_type_raw.upper(), (doc.doc_type_raw.lower().replace(" ", "_"), "other"))
        doc.doc_type = mapped[0]
        doc.doc_category = mapped[1]

    # -- Summary fields
    items = page.query_selector_all(".doc-preview-summary__column-list-item")
    for item in items:
        spans = item.query_selector_all(".doc-preview-summary__column-span")
        if len(spans) >= 2:
            label = spans[0].inner_text().strip().rstrip(":")
            value = spans[1].inner_text().strip()

            if label == "Number of Pages":
                try:
                    doc.num_pages = int(value)
                except ValueError:
                    pass
            elif label == "Recorded Date":
                doc.recording_date = parse_date(value)
            elif label == "Book/Volume/Page":
                doc.book_volume_page = value
            elif label == "Instrument Date":
                doc.instrument_date = parse_date(value)
            elif label == "Consideration":
                doc.consideration = parse_consideration(value)

    # -- Parties
    party_div = page.query_selector('[data-testid="docPreviewParty"]')
    if party_div:
        parties = page.evaluate("""(el) => {
            const result = [];
            const children = el.children;
            for (let i = 0; i < children.length; i++) {
                const child = children[i];
                if (child.tagName === 'A') {
                    const name = child.textContent.trim();
                    const nextSib = children[i + 1];
                    const role = nextSib && nextSib.classList.contains('doc-preview-group__summary-group-label')
                        ? nextSib.textContent.trim().toUpperCase()
                        : '';
                    if (name && role) {
                        result.push({name, role});
                    }
                }
            }
            return result;
        }""", party_div)

        for p in parties:
            if p["role"] == "GRANTOR":
                doc.grantors.append(p["name"])
            elif p["role"] == "GRANTEE":
                doc.grantees.append(p["name"])

    # -- Legal description
    legal_el = page.query_selector('[data-testid="docPreviewLegalDescription"]')
    if legal_el:
        doc.legal_description = legal_el.inner_text().strip()

    return doc


def scrape_batch(
    instrument_numbers: list,
    conn,
    headless: bool = True,
    delay: float = REQUEST_DELAY_SEC,
):
    """Scrape a batch of instrument numbers from the Collin County Clerk portal."""
    stats = {"found": 0, "not_found": 0, "errors": 0, "skipped": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
        )
        page = context.new_page()

        log.info("Loading Collin clerk portal homepage...")
        page.goto(BASE_URL, timeout=30000)
        time.sleep(2)

        last_request_time = time.time()

        for i, (prop_id, de_id, inst_num) in enumerate(instrument_numbers):
            log.info(
                "[%d/%d] Searching for instrument %s (property=%s)",
                i + 1, len(instrument_numbers), inst_num, prop_id,
            )

            elapsed = time.time() - last_request_time
            if elapsed < delay:
                time.sleep(delay - elapsed)

            try:
                last_request_time = time.time()
                detail_url = search_instrument(page, inst_num)

                if not detail_url:
                    log.warning("  Not found on clerk portal: %s", inst_num)
                    mark_not_found(conn, de_id, inst_num)
                    stats["not_found"] += 1
                    continue

                elapsed = time.time() - last_request_time
                if elapsed < delay:
                    time.sleep(delay - elapsed)
                last_request_time = time.time()

                doc = extract_document_detail(page, inst_num)

                if doc.error:
                    log.error("  Error extracting %s: %s", inst_num, doc.error)
                    stats["errors"] += 1
                    continue

                log.info(
                    "  Found: %s | %s | rec=%s | consideration=%s | "
                    "grantors=%s | grantees=%s",
                    doc.doc_type_raw,
                    doc.instrument_number,
                    doc.recording_date,
                    doc.consideration,
                    doc.grantors[:2],
                    doc.grantees[:2],
                )

                update_document_event(conn, de_id, doc)

                if doc.doc_category == "financing":
                    upsert_debt_instrument(conn, prop_id, de_id, doc)

                stats["found"] += 1

            except PwTimeout as e:
                log.error("  Timeout for instrument %s: %s", inst_num, e)
                stats["errors"] += 1
            except Exception as e:
                log.error("  Unexpected error for %s: %s", inst_num, e, exc_info=True)
                stats["errors"] += 1

        browser.close()

    return stats


# ---------------------------------------------------------------------------
# Address normalisation helpers
# ---------------------------------------------------------------------------

def normalise_address(raw: str) -> str:
    """
    Normalise a Collin property address for clerk portal search.

    Collin addresses often include city/state/zip (e.g. "1630 COIT RD , PLANO, TX 75075").
    We strip everything after the first comma to get just the street address.
    """
    if not raw:
        return ""

    addr = raw.upper().strip()

    # Remove unit/suite/apt/floor designators
    addr = re.sub(
        r"\s+(?:STE|SUITE|APT|APARTMENT|UNIT|#|FL|FLOOR|BLDG|BUILDING)\s*[\w-]*",
        "",
        addr,
        flags=re.IGNORECASE,
    )

    # Strip city/state/zip (everything after the first comma)
    addr = addr.split(",")[0].strip()

    # Replace written-out suffixes with abbreviated forms
    tokens = addr.split()
    if len(tokens) >= 2:
        last = tokens[-1]
        if last in STREET_SUFFIX_MAP:
            tokens[-1] = STREET_SUFFIX_MAP[last]
        addr = " ".join(tokens)

    addr = re.sub(r"\s+", " ", addr).strip()
    return addr


def address_variants(raw: str) -> List[str]:
    """Return a list of address strings to try, from most specific to least."""
    base = normalise_address(raw)
    if not base:
        return []

    variants = [base]

    parts = base.split()
    if len(parts) >= 3 and parts[0].isdigit():
        directionals = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}
        street_tokens = []
        for tok in parts[1:]:
            if tok in directionals and not street_tokens:
                continue
            street_tokens.append(tok)
            if len(street_tokens) == 1:
                break

        if street_tokens:
            broader = f"{parts[0]} {' '.join(street_tokens)}"
            if broader != base:
                variants.append(broader)

    return variants


# ---------------------------------------------------------------------------
# Address-search DB helpers
# ---------------------------------------------------------------------------

def get_properties_without_grantor(
    conn, limit: int = ADDRESS_BATCH_SIZE
) -> List[Tuple[str, str, str]]:
    """
    Return Collin properties that have no grantor_raw populated yet.
    Returns list of (property_id, document_event_id, property_address).
    """
    sql = """
        SELECT DISTINCT ON (p.id)
               p.id            AS property_id,
               de.id           AS document_event_id,
               p.property_address
        FROM property p
        JOIN document_event de ON de.property_id = p.id
        LEFT JOIN valuation_snapshot vs ON vs.property_id = p.id
        WHERE p.county = 'Collin'
          AND p.property_address IS NOT NULL
          AND (de.grantor_raw IS NULL OR de.grantor_raw = '')
          AND (de.raw_payload IS NULL
               OR de.raw_payload->>'address_search_attempted' IS NULL)
        ORDER BY p.id, vs.market_value DESC NULLS LAST
        LIMIT %s
    """
    cur = conn.cursor()
    cur.execute(sql, (limit,))
    rows = cur.fetchall()
    log.info("Loaded %d Collin properties to search by address", len(rows))
    return rows


def mark_address_search_attempted(
    conn, de_id: str, address_searched: str, result_status: str, notes: str = ""
):
    """Record that we attempted an address search for this document_event."""
    payload = {
        "address_search_attempted": True,
        "address_searched": address_searched,
        "result_status": result_status,
        "notes": notes,
        "attempted_at": datetime.utcnow().isoformat(),
    }
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE document_event
        SET raw_payload = COALESCE(raw_payload, '{}'::jsonb) || %s::jsonb,
            fetched_at  = NOW()
        WHERE id = %s
        """,
        (json.dumps(payload), de_id),
    )
    conn.commit()


def update_document_event_from_address_search(
    conn, de_id: str, property_id: str, doc: "ClerkDocument", address_searched: str
):
    """Update an existing document_event with data found via address search."""
    mapped = DOC_TYPE_MAP.get(doc.doc_type_raw.upper(), (None, None))
    doc_type = mapped[0] or doc.doc_type
    doc_category = mapped[1] or doc.doc_category

    payload = {
        **asdict(doc),
        "address_search_attempted": True,
        "address_searched": address_searched,
        "result_status": "found",
        "attempted_at": datetime.utcnow().isoformat(),
    }

    cur = conn.cursor()
    cur.execute(
        """
        UPDATE document_event
        SET instrument_number = COALESCE(instrument_number, %s),
            doc_type          = COALESCE(%s, doc_type),
            doc_category      = COALESCE(%s, doc_category),
            recording_date    = COALESCE(%s, recording_date),
            effective_date    = COALESCE(%s, effective_date),
            grantor_raw       = COALESCE(%s, grantor_raw),
            grantee_raw       = COALESCE(%s, grantee_raw),
            consideration     = COALESCE(%s, consideration),
            source_url        = %s,
            raw_payload       = %s::jsonb,
            fetched_at        = NOW()
        WHERE id = %s
        """,
        (
            doc.instrument_number or None,
            doc_type,
            doc_category,
            doc.recording_date,
            doc.instrument_date,
            "; ".join(doc.grantors) if doc.grantors else None,
            "; ".join(doc.grantees) if doc.grantees else None,
            doc.consideration,
            doc.detail_url,
            json.dumps(payload, default=str),
            de_id,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Progress file helpers
# ---------------------------------------------------------------------------

def load_progress() -> dict:
    if os.path.exists(ADDRESS_PROGRESS_FILE):
        try:
            with open(ADDRESS_PROGRESS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"completed": [], "stats": {"found": 0, "not_found": 0, "no_deed": 0, "errors": 0}}


def save_progress(progress: dict):
    tmp = ADDRESS_PROGRESS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(progress, f, indent=2)
    os.replace(tmp, ADDRESS_PROGRESS_FILE)


# ---------------------------------------------------------------------------
# Address-search Playwright logic
# ---------------------------------------------------------------------------

_DEED_PRIORITY = {
    "WARRANTY DEED": 0,
    "GENERAL WARRANTY DEED": 0,
    "SPECIAL WARRANTY DEED": 1,
    "DEED": 2,
    "QUIT CLAIM DEED": 3,
    "QUITCLAIM DEED": 3,
    "DEED OF TRUST": 4,
}


def search_by_address(page: "Page", address: str, delay: float = REQUEST_DELAY_SEC) -> Optional[str]:
    """
    Search the Collin clerk portal for documents matching *address*.
    Returns detail URL string, or None.
    """
    encoded = urllib.parse.quote_plus(address)
    search_url = (
        f"{BASE_URL}/results?department=RP"
        f"&limit=50"
        f"&searchOfficialRecords=true"
        f"&searchOcrText=false"
        f"&searchType=quickSearch"
        f"&searchValue={encoded}"
    )

    log.debug("  Address search URL: %s", search_url)
    page.goto(search_url, timeout=30000)
    time.sleep(delay + 1)

    try:
        page.wait_for_selector('tr[role="row"], .no-results, [class*="empty"]', timeout=12000)
    except PwTimeout:
        pass

    body_text = page.inner_text("body")

    if "0 results" in body_text or "No results" in body_text.lower()[:200]:
        return None

    rows = page.query_selector_all('tr[role="row"]')
    if not rows:
        return None

    rows_data = page.evaluate("""() => {
        const rows = Array.from(document.querySelectorAll('tr[role="row"]'));
        return rows.map(row => ({
            text: row.innerText,
            cells: Array.from(row.querySelectorAll('td')).map(td => td.innerText.trim()),
        }));
    }""")

    best_score = 9999
    best_idx = -1
    best_date = None

    for idx, rd in enumerate(rows_data):
        cells = rd["cells"]
        text = rd["text"].upper()

        if not cells:
            continue

        matched_priority = None
        for dtype, priority in _DEED_PRIORITY.items():
            if dtype in text:
                matched_priority = priority
                break

        if matched_priority is None:
            continue

        rec_date = None
        for cell in reversed(cells):
            d = parse_date(cell)
            if d:
                rec_date = d
                break

        if matched_priority < best_score:
            best_score = matched_priority
            best_idx = idx
            best_date = rec_date
        elif matched_priority == best_score:
            if rec_date and (best_date is None or rec_date > best_date):
                best_idx = idx
                best_date = rec_date

    if best_idx < 0:
        return None

    winning_rows = page.query_selector_all('tr[role="row"]')
    if best_idx >= len(winning_rows):
        return None

    winning_rows[best_idx].click()
    time.sleep(delay + 1)

    if "/doc/" in page.url:
        return page.url

    try:
        page.wait_for_url("**/doc/**", timeout=6000)
        return page.url
    except PwTimeout:
        return None


def scrape_addresses(
    properties: List[Tuple[str, str, str]],
    conn,
    headless: bool = True,
    delay: float = REQUEST_DELAY_SEC,
    progress: dict = None,
):
    """
    Scrape the Collin clerk portal for grantor/grantee names using property addresses.
    Returns stats dict.
    """
    if progress is None:
        progress = load_progress()

    completed_set = set(progress.get("completed", []))
    stats = progress.get("stats", {"found": 0, "not_found": 0, "no_deed": 0, "errors": 0})

    todo = [r for r in properties if r[0] not in completed_set]
    log.info(
        "%d properties to process (%d already completed, %d total)",
        len(todo), len(completed_set), len(properties),
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        log.info("Loading Collin clerk portal homepage...")
        page.goto(BASE_URL, timeout=30000)
        time.sleep(3)

        last_request_time = time.time()

        for i, (prop_id, de_id, raw_address) in enumerate(todo):
            log.info(
                "[%d/%d] Property %s | address: %s",
                i + 1, len(todo), prop_id, raw_address,
            )

            elapsed = time.time() - last_request_time
            if elapsed < delay:
                time.sleep(delay - elapsed)
            last_request_time = time.time()

            variants = address_variants(raw_address)
            detail_url = None
            searched_with = raw_address

            for variant in variants:
                log.debug("  Trying variant: %r", variant)
                try:
                    detail_url = search_by_address(page, variant, delay=delay)
                    last_request_time = time.time()
                    if detail_url:
                        searched_with = variant
                        break
                    time.sleep(max(0.5, delay - 1))
                    last_request_time = time.time()
                except PwTimeout:
                    log.warning("  Timeout on address variant %r", variant)
                    stats["errors"] += 1
                    break
                except Exception as exc:
                    log.error("  Error on variant %r: %s", variant, exc, exc_info=True)
                    stats["errors"] += 1
                    break

            if not detail_url:
                log.info("  -> No deed result for %s", raw_address)
                mark_address_search_attempted(
                    conn, de_id, searched_with, "not_found",
                    notes=f"Tried variants: {variants}",
                )
                stats["not_found"] += 1
                completed_set.add(prop_id)
                progress["completed"] = list(completed_set)
                progress["stats"] = stats
                save_progress(progress)
                continue

            elapsed = time.time() - last_request_time
            if elapsed < delay:
                time.sleep(delay - elapsed)
            last_request_time = time.time()

            try:
                url_match = re.search(r"/doc/(\w+)", detail_url)
                inst_num = url_match.group(1) if url_match else "unknown"

                doc = extract_document_detail(page, inst_num)

                if doc.error:
                    log.warning("  Extraction error for %s: %s", raw_address, doc.error)
                    mark_address_search_attempted(
                        conn, de_id, searched_with, "error", notes=doc.error
                    )
                    stats["errors"] += 1
                elif not doc.grantors and not doc.grantees:
                    log.info("  -> No parties found for %s (doc: %s)", raw_address, inst_num)
                    mark_address_search_attempted(
                        conn, de_id, searched_with, "no_deed",
                        notes=f"inst={inst_num} type={doc.doc_type_raw}",
                    )
                    stats["no_deed"] += 1
                else:
                    log.info(
                        "  -> FOUND %s | %s | rec=%s | grantors=%s | grantees=%s",
                        doc.doc_type_raw, inst_num, doc.recording_date,
                        doc.grantors[:2], doc.grantees[:2],
                    )
                    update_document_event_from_address_search(
                        conn, de_id, prop_id, doc, searched_with
                    )
                    if doc.doc_category == "financing":
                        upsert_debt_instrument(conn, prop_id, de_id, doc)
                    stats["found"] += 1

            except PwTimeout as exc:
                log.error("  Timeout extracting detail for %s: %s", raw_address, exc)
                mark_address_search_attempted(
                    conn, de_id, searched_with, "error", notes=str(exc)
                )
                stats["errors"] += 1
            except Exception as exc:
                log.error("  Unexpected error for %s: %s", raw_address, exc, exc_info=True)
                mark_address_search_attempted(
                    conn, de_id, searched_with, "error", notes=str(exc)
                )
                stats["errors"] += 1

            completed_set.add(prop_id)
            progress["completed"] = list(completed_set)
            progress["stats"] = stats
            save_progress(progress)

        browser.close()

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape Collin County Clerk deed/mortgage records"
    )
    sub = parser.add_subparsers(dest="command")

    # --- instrument lookup ---
    inst_cmd = sub.add_parser(
        "lookup",
        help="Look up instrument numbers for Collin properties",
    )
    inst_cmd.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
        help="Number of instruments per batch (default: %(default)s)",
    )
    inst_cmd.add_argument(
        "--offset", type=int, default=0,
        help="Skip first N instrument numbers",
    )
    inst_cmd.add_argument(
        "--delay", type=float, default=REQUEST_DELAY_SEC,
        help="Seconds between requests (default: %(default)s)",
    )
    inst_cmd.add_argument(
        "--headful", action="store_true",
        help="Run browser in headful (visible) mode for debugging",
    )

    # --- address-search ---
    addr_cmd = sub.add_parser(
        "address-search",
        help="Search clerk portal by property address for Collin properties with no grantor_raw",
    )
    addr_cmd.add_argument(
        "--batch-size", type=int, default=ADDRESS_BATCH_SIZE,
        help="Max properties to load from DB per run (default: %(default)s)",
    )
    addr_cmd.add_argument(
        "--test", type=int, default=0, metavar="N",
        help="If > 0, only process the first N properties (smoke test)",
    )
    addr_cmd.add_argument(
        "--delay", type=float, default=REQUEST_DELAY_SEC,
        help="Seconds between requests (default: %(default)s)",
    )
    addr_cmd.add_argument(
        "--headful", action="store_true",
        help="Run browser in headful (visible) mode for debugging",
    )
    addr_cmd.add_argument(
        "--reset-progress", action="store_true",
        help="Delete the progress file and start fresh",
    )

    # --- stats ---
    sub.add_parser("stats", help="Show scraping progress statistics")

    args = parser.parse_args()

    if args.command == "lookup":
        conn = get_connection()
        instruments = get_collin_instrument_numbers(
            conn, limit=args.batch_size, offset=args.offset
        )
        if not instruments:
            log.info("No instrument numbers to process.")
            return

        stats = scrape_batch(
            instruments,
            conn,
            headless=not args.headful,
            delay=args.delay,
        )
        log.info("Batch complete: %s", stats)
        conn.close()

    elif args.command == "address-search":
        if args.reset_progress and os.path.exists(ADDRESS_PROGRESS_FILE):
            os.remove(ADDRESS_PROGRESS_FILE)
            log.info("Progress file deleted — starting fresh")

        conn = get_connection()

        batch_size = args.batch_size
        if args.test > 0:
            batch_size = args.test
            log.info("TEST MODE: processing only %d properties", args.test)

        properties = get_properties_without_grantor(conn, limit=batch_size)
        if not properties:
            log.info("No properties to process. All done!")
            conn.close()
            return

        progress = load_progress()
        stats = scrape_addresses(
            properties,
            conn,
            headless=not args.headful,
            delay=args.delay,
            progress=progress,
        )
        log.info(
            "Address-search complete: found=%d  not_found=%d  no_deed=%d  errors=%d",
            stats.get("found", 0),
            stats.get("not_found", 0),
            stats.get("no_deed", 0),
            stats.get("errors", 0),
        )
        conn.close()

    elif args.command == "stats":
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                COUNT(*) AS total_collin_docs,
                COUNT(de.instrument_number) AS has_instrument,
                COUNT(de.fetched_at) AS fetched,
                COUNT(de.consideration) AS has_consideration,
                COUNT(de.grantor_raw) AS has_grantor,
                COUNT(de.grantee_raw) AS has_grantee
            FROM document_event de
            JOIN property p ON de.property_id = p.id
            WHERE p.county = 'Collin'
        """)
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        print("\n=== Collin Document Events ===")
        for c, v in zip(cols, row):
            print(f"  {c}: {v}")

        # Show instrument number coverage
        cur.execute("""
            SELECT COUNT(DISTINCT de.instrument_number) AS distinct_instruments
            FROM document_event de
            JOIN property p ON de.property_id = p.id
            WHERE p.county = 'Collin'
              AND de.instrument_number IS NOT NULL
              AND de.instrument_number != ''
        """)
        row2 = cur.fetchone()
        print(f"  distinct_instrument_numbers: {row2[0]}")

        # Progress file stats
        progress = load_progress()
        print("\n=== Address Search Progress ===")
        print(f"  Completed properties: {len(progress.get('completed', []))}")
        pstats = progress.get("stats", {})
        for k, v in pstats.items():
            print(f"  {k}: {v}")
        conn.close()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
