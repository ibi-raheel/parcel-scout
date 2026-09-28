"""
Dallas County Clerk document scraper.

Scrapes deed and mortgage data from the Dallas County Clerk's Kofile/GovOS
publicsearch platform at https://dallas.tx.publicsearch.us/

Workflow:
  1. Extract instrument numbers from Dallas property legal_description fields
  2. Search each instrument number on the clerk portal
  3. Click into the detail page to extract full document metadata
  4. Update the corresponding document_event rows with financial details
  5. Create/update debt_instrument records for Deeds of Trust

Address-search workflow (for the 21,613 deeds with no instrument number):
  1. Query Dallas properties that have no grantor_raw yet
  2. For each property address, perform a quickSearch on the clerk portal
  3. From the result list pick the most recent Warranty Deed or Deed of Trust
  4. Extract grantor/grantee names from the detail page
  5. Update the matching document_event row

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
BASE_URL = "https://dallas.tx.publicsearch.us"
REQUEST_DELAY_SEC = 2.0  # minimum seconds between page loads
DEFAULT_BATCH_SIZE = 100
ADDRESS_BATCH_SIZE = 500
LOG_FMT = "%(asctime)s  %(levelname)-8s  %(message)s"

# Progress file for address-search resume support
ADDRESS_PROGRESS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "dallas_address_search_progress.json",
)

# Deed types we want to capture for address searches (prefer transfers, accept financing)
ADDRESS_SEARCH_DOC_PRIORITIES = [
    "WARRANTY DEED",
    "GENERAL WARRANTY DEED",
    "SPECIAL WARRANTY DEED",
    "DEED",
    "QUIT CLAIM DEED",
    "QUITCLAIM DEED",
    "DEED OF TRUST",
]

# Street suffix normalisation map (USPS standard abbreviations)
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
log = logging.getLogger("dallas_clerk")

# Map from clerk doc types to our internal types
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
    """Parsed document from the Dallas County Clerk portal."""
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


def get_dallas_instrument_numbers(conn, limit: int = None, offset: int = 0):
    """
    Extract instrument numbers embedded in Dallas property legal descriptions.
    Returns list of (property_id, document_event_id, instrument_number).
    """
    sql = """
        SELECT p.id AS property_id,
               de.id AS document_event_id,
               p.legal_description
        FROM document_event de
        JOIN property p ON de.property_id = p.id
        JOIN source_property_ref spr ON spr.property_id = p.id
        WHERE spr.source = 'dallas_cad'
          AND de.source = 'dcad_bulk'
          AND de.instrument_number IS NULL
          AND p.legal_description LIKE '%%INT%%'
          AND de.raw_payload IS NULL
        ORDER BY de.created_at
    """
    if limit:
        sql += f" LIMIT {limit} OFFSET {offset}"

    cur = conn.cursor()
    cur.execute(sql)
    rows = cur.fetchall()

    results = []
    for prop_id, de_id, legal_desc in rows:
        # Extract instrument number from legal description (e.g., INT201500191531)
        match = re.search(r'INT(\d+)', legal_desc)
        if match:
            results.append((prop_id, de_id, match.group(1)))

    log.info("Found %d instrument numbers to look up", len(results))
    return results


def update_document_event(conn, de_id: str, doc: ClerkDocument):
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


def upsert_debt_instrument(conn, property_id: str, de_id: str, doc: ClerkDocument):
    """
    For Deeds of Trust, create or update a debt_instrument record.
    The lender is identified from the grantee list (excluding the borrower).
    """
    if doc.doc_category != "financing":
        return

    lender_name = None
    for g in doc.grantees:
        # Heuristic: lender names often contain BANK, MORTGAGE, CREDIT, etc.
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
    # Check if debt_instrument already exists for this document_event
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
                source = 'dallas_clerk',
                updated_at = NOW()
            WHERE id = %s
        """, (lender_name, doc.consideration, existing[0]))
    else:
        cur.execute("""
            INSERT INTO debt_instrument (id, property_id, document_event_id,
                lender_name, original_amount, source, status, data_quality)
            VALUES (%s, %s, %s, %s, %s, 'dallas_clerk', 'active', 'clerk_index')
        """, (str(uuid.uuid4()), property_id, de_id, lender_name, doc.consideration))

    conn.commit()


# ---------------------------------------------------------------------------
# Scraping logic
# ---------------------------------------------------------------------------

def parse_date(text: str) -> Optional[date]:
    """Parse dates like '1/14/2025 8:01 AM', '10/29/2007 0:00 AM', or '01/10/2025'."""
    if not text or not text.strip():
        return None
    text = text.strip()
    # Strip time portion if present -- we only need the date
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


def search_instrument(page: Page, instrument_number: str) -> Optional[str]:
    """
    Search for a document by instrument number on the clerk portal.
    Returns the detail page URL (/doc/{id}) if found, None otherwise.
    """
    search_url = (
        f"{BASE_URL}/results?department=RP"
        f"&searchOcrText=false"
        f"&searchType=quickSearch"
        f"&searchValue={instrument_number}"
        f"&recordedDateRange=18000101,{datetime.now().strftime('%Y%m%d')}"
    )

    page.goto(search_url, timeout=30000)
    time.sleep(3)  # Wait for HTMX/JS rendering

    # Wait for either results or "no results" indicator
    try:
        page.wait_for_selector('tr[role="row"], .no-results, [class*="empty"]', timeout=10000)
    except PwTimeout:
        pass

    body_text = page.inner_text("body")

    # Check for no results
    if "0 results" in body_text or "No results" in body_text:
        return None

    # Find result rows and look for our instrument number
    rows = page.query_selector_all('tr[role="row"]')
    for row in rows:
        row_text = row.inner_text()
        if instrument_number in row_text:
            row.click()
            time.sleep(2)

            # The URL should now be /doc/{id}
            if "/doc/" in page.url:
                return page.url

            # Try waiting for navigation
            try:
                page.wait_for_url(f"**/doc/**", timeout=5000)
                return page.url
            except PwTimeout:
                pass

    return None


def extract_document_detail(page: Page, instrument_number: str) -> ClerkDocument:
    """Extract all document metadata from the detail page."""
    doc = ClerkDocument(instrument_number=instrument_number)
    doc.detail_url = page.url

    try:
        # Wait for the summary section to render
        page.wait_for_selector(".doc-preview__summary", timeout=10000)
    except PwTimeout:
        doc.error = "detail_page_timeout"
        return doc

    # Capture full body text for raw storage
    doc.raw_text = page.inner_text("body")

    # -- Doc type (header)
    header = page.query_selector(".doc-preview__summary-header")
    if header:
        doc.doc_type_raw = header.inner_text().strip()
        mapped = DOC_TYPE_MAP.get(doc.doc_type_raw.upper(), (doc.doc_type_raw.lower().replace(" ", "_"), "other"))
        doc.doc_type = mapped[0]
        doc.doc_category = mapped[1]

    # -- Summary fields (key-value pairs in list items)
    items = page.query_selector_all(".doc-preview-summary__column-list-item")
    for item in items:
        spans = item.query_selector_all(".doc-preview-summary__column-span")
        if len(spans) >= 2:
            label = spans[0].inner_text().strip().rstrip(":")
            value = spans[1].inner_text().strip()

            if label == "Document Number":
                pass  # We already have instrument_number
            elif label == "Number of Pages":
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

    # -- Parties (grantors and grantees)
    # Structure: <div data-testid="docPreviewParty">
    #   <a>NAME1</a><span class="...label">GRANTOR</span>
    #   <a>NAME2</a><span class="...label">GRANTEE</span>
    # All parties are in a single container as alternating name/role pairs.
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
    """
    Scrape a batch of instrument numbers from the Dallas County Clerk portal.

    Args:
        instrument_numbers: list of (property_id, document_event_id, instrument_number)
        conn: psycopg2 connection
        headless: run browser in headless mode
        delay: seconds between requests
    """
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

        # Initial load to get cookies/session
        log.info("Loading clerk portal homepage...")
        page.goto(BASE_URL, timeout=30000)
        time.sleep(2)

        last_request_time = time.time()

        for i, (prop_id, de_id, inst_num) in enumerate(instrument_numbers):
            log.info(
                "[%d/%d] Searching for instrument %s (property=%s)",
                i + 1, len(instrument_numbers), inst_num, prop_id,
            )

            # Rate limiting
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

                # Rate limit before detail page extraction
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

                # Update database
                update_document_event(conn, de_id, doc)

                # Create debt instrument for financing docs
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
# Date-range search (alternative to instrument number lookup)
# ---------------------------------------------------------------------------

def search_by_date_range(
    start_date: str,
    end_date: str,
    doc_type_filter: str = "Deed of Trust",
    max_results: int = 500,
    headless: bool = True,
    delay: float = REQUEST_DELAY_SEC,
):
    """
    Search the clerk portal by date range and document type.
    Useful for discovering new documents not yet in our database.

    Args:
        start_date: YYYYMMDD format
        end_date: YYYYMMDD format
        doc_type_filter: text to search for (e.g., "Deed of Trust")
        max_results: maximum results to process
        headless: run headless
        delay: rate limit delay

    Returns:
        list of ClerkDocument
    """
    documents = []

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

        search_url = (
            f"{BASE_URL}/results?department=RP"
            f"&searchOcrText=false"
            f"&searchType=quickSearch"
            f"&searchValue={doc_type_filter.replace(' ', '%20')}"
            f"&recordedDateRange={start_date},{end_date}"
        )

        log.info("Searching: %s", search_url)
        page.goto(search_url, timeout=60000, wait_until="domcontentloaded")
        time.sleep(8)

        body_text = page.inner_text("body")

        # Extract total count
        count_match = re.search(r'(\d+(?:,\d+)*)\s+results?\b', body_text)
        total = int(count_match.group(1).replace(",", "")) if count_match else 0
        log.info("Total results: %d", total)

        # Get all result rows from the current page
        rows = page.query_selector_all('tr[role="row"]')
        log.info("Result rows on page: %d", len(rows))

        for idx, row in enumerate(rows):
            if idx >= max_results:
                break

            row_text = row.inner_text()
            # Extract doc number from the row
            doc_num_match = re.search(r'\d{12,15}', row_text)
            if not doc_num_match:
                continue

            inst_num = doc_num_match.group(0)
            log.info("[%d] Clicking result for %s", idx + 1, inst_num)

            # Rate limit
            time.sleep(delay)

            row.click()
            time.sleep(3)

            if "/doc/" in page.url:
                doc = extract_document_detail(page, inst_num)
                documents.append(doc)
                log.info(
                    "  Extracted: %s | %s | %s | consideration=%s",
                    doc.doc_type_raw, inst_num, doc.recording_date, doc.consideration,
                )

                # Go back to results
                page.go_back()
                time.sleep(3)
            else:
                log.warning("  Did not navigate to detail page")

        browser.close()

    return documents


# ---------------------------------------------------------------------------
# Address normalisation helpers
# ---------------------------------------------------------------------------

def normalise_address(raw: str) -> str:
    """
    Normalise a property address for clerk portal search.

    Steps:
      1. Upper-case everything
      2. Strip unit/suite/apt designators (e.g. "STE 200", "APT 3B", "UNIT A")
      3. Expand or abbreviate common street suffixes (prefer abbreviated form)
      4. Collapse multiple spaces

    Returns a clean string ready to be URL-encoded.
    """
    if not raw:
        return ""

    addr = raw.upper().strip()

    # Remove unit/suite/apt/floor designators including everything after them
    addr = re.sub(
        r"\s+(?:STE|SUITE|APT|APARTMENT|UNIT|#|FL|FLOOR|BLDG|BUILDING)\s*[\w-]*$",
        "",
        addr,
        flags=re.IGNORECASE,
    )

    # Remove trailing comma-separated parts (sometimes city sneaks in)
    addr = addr.split(",")[0].strip()

    # Replace written-out suffixes with abbreviated forms
    tokens = addr.split()
    if len(tokens) >= 2:
        last = tokens[-1]
        if last in STREET_SUFFIX_MAP:
            tokens[-1] = STREET_SUFFIX_MAP[last]
        addr = " ".join(tokens)

    # Collapse multiple spaces
    addr = re.sub(r"\s+", " ", addr).strip()

    return addr


def address_variants(raw: str) -> List[str]:
    """
    Return a list of address strings to try, from most specific to least.

    Variants:
      1. Normalised full address (e.g. "1234 MAIN ST")
      2. Just the street number + first token of street name (broader match)
    """
    base = normalise_address(raw)
    if not base:
        return []

    variants = [base]

    # Broader variant: house number + first two significant tokens of street name.
    # Skip single-letter directional prefixes (N, S, E, W, NE, NW, SE, SW) when
    # building the broader variant so we don't end up with "4500 S".
    parts = base.split()
    if len(parts) >= 3 and parts[0].isdigit():
        # Find the index of the first non-directional token after the house number
        directionals = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}
        street_tokens = []
        for tok in parts[1:]:
            if tok in directionals and not street_tokens:
                continue  # skip leading directionals
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
    Return Dallas properties that have no grantor_raw populated yet.

    Returns list of (property_id, document_event_id, property_address).
    Ordered by market value descending so high-value properties come first.
    We skip any property whose document_event already has
    raw_payload->>'address_search_attempted' set (resume support).
    """
    sql = """
        SELECT DISTINCT ON (p.id)
               p.id            AS property_id,
               de.id           AS document_event_id,
               p.property_address
        FROM property p
        JOIN document_event de ON de.property_id = p.id
        LEFT JOIN valuation_snapshot vs ON vs.property_id = p.id
        WHERE p.county = 'Dallas'
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
    log.info("Loaded %d properties to search by address", len(rows))
    return rows


def mark_address_search_attempted(
    conn, de_id: str, address_searched: str, result_status: str, notes: str = ""
):
    """
    Record that we attempted an address search for this document_event.

    result_status: 'found' | 'not_found' | 'no_deed_result' | 'error'
    This prevents re-processing on the next run.
    """
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
    """
    Update an existing document_event with data found via address search.
    Merges address-search metadata into raw_payload.
    """
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
# Progress file helpers (crash-safe resume)
# ---------------------------------------------------------------------------

def load_progress() -> dict:
    """Load the progress file, returning an empty dict if it doesn't exist."""
    if os.path.exists(ADDRESS_PROGRESS_FILE):
        try:
            with open(ADDRESS_PROGRESS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"completed": [], "stats": {"found": 0, "not_found": 0, "no_deed": 0, "errors": 0}}


def save_progress(progress: dict):
    """Atomically write the progress file."""
    tmp = ADDRESS_PROGRESS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(progress, f, indent=2)
    os.replace(tmp, ADDRESS_PROGRESS_FILE)


# ---------------------------------------------------------------------------
# Address-search Playwright logic
# ---------------------------------------------------------------------------

# Preference order for selecting the best result from the clerk portal
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
    Search the clerk portal for documents matching *address*.

    Strategy:
      - Use the quickSearch endpoint with the normalised address
      - Parse the result list for rows whose address column contains the
        search string
      - Among matching rows pick the row with the highest-priority deed type
        and most recent recording date
      - Return the detail page URL if found, None otherwise

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
    time.sleep(delay + 1)  # extra wait for JS rendering

    try:
        page.wait_for_selector('tr[role="row"], .no-results, [class*="empty"]', timeout=12000)
    except PwTimeout:
        pass

    body_text = page.inner_text("body")

    if "0 results" in body_text or "No results" in body_text.lower()[:200]:
        return None

    # Parse result rows
    rows = page.query_selector_all('tr[role="row"]')
    if not rows:
        return None

    # Evaluate all rows in one JS call for efficiency
    rows_data = page.evaluate("""() => {
        const rows = Array.from(document.querySelectorAll('tr[role="row"]'));
        return rows.map(row => ({
            text: row.innerText,
            cells: Array.from(row.querySelectorAll('td')).map(td => td.innerText.trim()),
        }));
    }""")

    # Score each row: lower = better
    best_score = 9999
    best_idx = -1
    best_date = None

    for idx, rd in enumerate(rows_data):
        cells = rd["cells"]
        text = rd["text"].upper()

        # Skip header rows (no cells or first cell is a column heading)
        if not cells:
            continue

        # Identify doc type from row text
        matched_priority = None
        for dtype, priority in _DEED_PRIORITY.items():
            if dtype in text:
                matched_priority = priority
                break

        if matched_priority is None:
            continue  # Not a deed type we care about

        # Try to parse recording date from last few cells (format M/D/YYYY)
        rec_date = None
        for cell in reversed(cells):
            d = parse_date(cell)
            if d:
                rec_date = d
                break

        # Prefer: lower deed priority, then more recent date
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

    # Click the winning row
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
    Scrape the clerk portal for grantor/grantee names using property addresses.

    Args:
        properties: list of (property_id, document_event_id, property_address)
        conn: psycopg2 connection
        headless: run browser headless
        delay: seconds between requests
        progress: dict loaded from progress file (modified in-place)

    Returns stats dict.
    """
    if progress is None:
        progress = load_progress()

    completed_set = set(progress.get("completed", []))
    stats = progress.get("stats", {"found": 0, "not_found": 0, "no_deed": 0, "errors": 0})

    # Filter out already-completed properties
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

        log.info("Loading clerk portal homepage...")
        page.goto(BASE_URL, timeout=30000)
        time.sleep(3)

        last_request_time = time.time()

        for i, (prop_id, de_id, raw_address) in enumerate(todo):
            log.info(
                "[%d/%d] Property %s | address: %s",
                i + 1, len(todo), prop_id, raw_address,
            )

            # Rate limiting
            elapsed = time.time() - last_request_time
            if elapsed < delay:
                time.sleep(delay - elapsed)
            last_request_time = time.time()

            # Try address variants (full → broader)
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
                    # Small pause between variants
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

            # We landed on a detail page — extract the document
            elapsed = time.time() - last_request_time
            if elapsed < delay:
                time.sleep(delay - elapsed)
            last_request_time = time.time()

            try:
                # Extract instrument number from URL (/doc/{instrument_number})
                url_match = re.search(r"/doc/(\d+)", detail_url)
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
                    # Create debt instrument for financing docs
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

            # Mark property as completed and save progress every iteration
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
        description="Scrape Dallas County Clerk deed/mortgage records"
    )
    sub = parser.add_subparsers(dest="command")

    # --- instrument lookup ---
    inst_cmd = sub.add_parser(
        "lookup",
        help="Look up instrument numbers extracted from Dallas CAD legal descriptions",
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

    # --- date range search ---
    date_cmd = sub.add_parser(
        "search",
        help="Search clerk portal by date range and doc type",
    )
    date_cmd.add_argument("--start", required=True, help="Start date YYYYMMDD")
    date_cmd.add_argument("--end", required=True, help="End date YYYYMMDD")
    date_cmd.add_argument(
        "--doc-type", default="Deed of Trust",
        help="Document type to search (default: '%(default)s')",
    )
    date_cmd.add_argument("--max", type=int, default=50, help="Max results")
    date_cmd.add_argument("--headful", action="store_true")
    date_cmd.add_argument(
        "--delay", type=float, default=REQUEST_DELAY_SEC,
    )

    # --- address-search ---
    addr_cmd = sub.add_parser(
        "address-search",
        help=(
            "Search clerk portal by property address for properties that "
            "have no grantor_raw yet (the 21k+ deeds with no instrument number)"
        ),
    )
    addr_cmd.add_argument(
        "--batch-size", type=int, default=ADDRESS_BATCH_SIZE,
        help="Max properties to load from DB per run (default: %(default)s)",
    )
    addr_cmd.add_argument(
        "--test", type=int, default=0, metavar="N",
        help="If > 0, only process the first N properties (dry-run / smoke test)",
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
    stats_cmd = sub.add_parser("stats", help="Show scraping progress statistics")

    args = parser.parse_args()

    if args.command == "lookup":
        conn = get_connection()
        instruments = get_dallas_instrument_numbers(
            conn, limit=args.batch_size, offset=args.offset
        )
        if not instruments:
            log.info("No instrument numbers to process. All done or none found.")
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

    elif args.command == "search":
        documents = search_by_date_range(
            start_date=args.start,
            end_date=args.end,
            doc_type_filter=args.doc_type,
            max_results=args.max,
            headless=not args.headful,
            delay=args.delay,
        )
        log.info("Found %d documents", len(documents))
        for doc in documents:
            print(json.dumps(asdict(doc), default=str, indent=2))

    elif args.command == "stats":
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                COUNT(*) AS total_dallas_docs,
                COUNT(instrument_number) AS has_instrument,
                COUNT(fetched_at) AS fetched,
                COUNT(consideration) AS has_consideration,
                COUNT(grantor_raw) AS has_grantor,
                COUNT(grantee_raw) AS has_grantee
            FROM document_event
            WHERE source = 'dcad_bulk'
        """)
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        print("\n=== Dallas Document Events ===")
        for c, v in zip(cols, row):
            print(f"  {c}: {v}")

        cur.execute("""
            SELECT COUNT(*)
            FROM property p
            JOIN source_property_ref spr ON spr.property_id = p.id
            WHERE spr.source = 'dallas_cad'
              AND p.legal_description LIKE '%%INT%%'
        """)
        print(f"\n  Properties with embedded instrument numbers: {cur.fetchone()[0]}")

        cur.execute("""
            SELECT
                COUNT(*) AS total,
                COUNT(CASE WHEN raw_payload->>'error' = 'not_found_on_clerk_portal' THEN 1 END) AS not_found,
                COUNT(CASE WHEN fetched_at IS NOT NULL AND raw_payload->>'error' IS NULL THEN 1 END) AS successfully_fetched
            FROM document_event
            WHERE source = 'dcad_bulk'
              AND fetched_at IS NOT NULL
        """)
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        print("\n=== Fetch Progress ===")
        for c, v in zip(cols, row):
            print(f"  {c}: {v}")

        cur.execute("""
            SELECT COUNT(*) FROM debt_instrument WHERE source = 'dallas_clerk'
        """)
        print(f"\n  Debt instruments created from clerk data: {cur.fetchone()[0]}")

        # Address-search specific stats
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE raw_payload->>'address_search_attempted' = 'true') AS addr_attempted,
                COUNT(*) FILTER (WHERE raw_payload->>'result_status' = 'found') AS addr_found,
                COUNT(*) FILTER (WHERE raw_payload->>'result_status' = 'not_found') AS addr_not_found,
                COUNT(*) FILTER (WHERE raw_payload->>'result_status' = 'no_deed') AS addr_no_deed,
                COUNT(*) FILTER (WHERE raw_payload->>'result_status' = 'error') AS addr_error
            FROM document_event
            WHERE source = 'dcad_bulk'
        """)
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        print("\n=== Address-Search Progress ===")
        for c, v in zip(cols, row):
            print(f"  {c}: {v}")

        # Properties still needing address search
        cur.execute("""
            SELECT COUNT(DISTINCT p.id)
            FROM property p
            LEFT JOIN document_event de ON de.property_id = p.id
              AND de.grantor_raw IS NOT NULL AND de.grantor_raw != ''
            WHERE p.county = 'Dallas'
              AND de.id IS NULL
              AND p.property_address IS NOT NULL
        """)
        print(f"\n  Properties still needing grantor lookup: {cur.fetchone()[0]}")

        # Progress file summary
        if os.path.exists(ADDRESS_PROGRESS_FILE):
            prog = load_progress()
            print(f"\n  Progress file: {ADDRESS_PROGRESS_FILE}")
            print(f"  Completed (in file): {len(prog.get('completed', []))}")
            print(f"  Cumulative stats:    {prog.get('stats', {})}")

        conn.close()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
