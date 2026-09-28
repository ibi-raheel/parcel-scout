"""
Multi-Source Ownership Resolver — Collin County LLC Properties
==============================================================
Resolves beneficial ownership of LLC-held properties using five evidence sources:

  Source 1: TX Comptroller officers/agents (entity_person + person_node + entity_record)
  Source 2: Permit contractors (person names on permits filed against the property)
  Source 3: Deed grantors/grantees (document_event grantor_raw / grantee_raw)
  Source 4: OpenCorporates data embedded in entity_record
  Source 5: Mailing address cross-reference (LLC mailing address shared with a known person)

Confidence tiers:
  1 source  → 0.40  (possible)
  2 sources → 0.70  (likely)
  3 sources → 0.90  (very likely)
  4+ sources → 0.98 (confirmed)

Results are written back to:
  - ownership_resolution.beneficial_person_id
  - ownership_resolution.resolution_chain  (JSONB showing contributing sources)
  - ownership_resolution.confidence
  - owner_party.resolution_confidence
  - owner_party.resolution_method
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from collections import defaultdict
from typing import Any

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

DB_URL = "postgresql://parcelscout:parcelscout_dev@localhost:5432/parcelscout"

# Confidence scoring
CONF_BY_SOURCE_COUNT = {1: 0.40, 2: 0.70, 3: 0.90}
CONF_4_PLUS = 0.98

# Minimum name-word length to be considered a person name (not a company acronym)
MIN_NAME_WORDS = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def _normalize(s: str) -> str:
    """Upper-case, strip punctuation/extra whitespace for comparison."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Z0-9 ]", " ", s.upper())
    return re.sub(r"\s+", " ", s).strip()


_COMPANY_SUFFIXES = re.compile(
    r"\b(LLC|LP|LTD|INC|CORP|CO|HOLDINGS|ENTERPRISES|PROPERTIES|REALTY|TRUST|"
    r"FOUNDATION|ASSOCIATION|ASSOC|PARTNERSHIP|GROUP|INVESTMENTS?|DEVELOPMENT|"
    r"MANAGEMENT|SERVICES?|RESOURCES?|SOLUTIONS?|SYSTEMS?|TECHNOLOGIES?|TECH|"
    r"CONSULTANTS?|ADVISORS?|CAPITAL|VENTURES?|PARTNERS?|FUNDING|FINANCE|"
    r"MORTGAGE|BANK|FINANCIAL|TITLE|ESCROW|REAL ESTATE)\b",
    re.IGNORECASE,
)


def _looks_like_person(name: str) -> bool:
    """Heuristic: name that has 2+ words and no obvious company keywords."""
    if not name or len(name) < 4:
        return False
    words = name.strip().split()
    if len(words) < MIN_NAME_WORDS:
        return False
    if _COMPANY_SUFFIXES.search(name):
        return False
    # Names like "JOHN W DOE" or "DOE JOHN" are plausible
    return True


def _confidence_for_count(n: int) -> float:
    if n >= 4:
        return CONF_4_PLUS
    return CONF_BY_SOURCE_COUNT.get(n, 0.40)


# ---------------------------------------------------------------------------
# Source 1 — TX Comptroller officers/agents
# ---------------------------------------------------------------------------

def _source_comptroller(cur, owner_party_id: str) -> list[dict]:
    """
    Find person_node rows linked via entity_person → entity_record → owner_party.
    The entity_record is matched to owner_party by normalized name comparison.
    """
    cur.execute(
        """
        SELECT DISTINCT
            pn.id        AS person_id,
            pn.full_name AS person_name,
            ep.position  AS role,
            er.entity_name
        FROM entity_record er
        JOIN entity_person ep ON ep.entity_id = er.id
        JOIN person_node   pn ON pn.id = ep.person_id
        JOIN owner_party   op ON op.id = %(owner_id)s
        WHERE
            -- Match by normalized name (strip punctuation/spaces)
            REGEXP_REPLACE(UPPER(TRIM(er.entity_name)), '[^A-Z0-9 ]', '', 'g')
            = REGEXP_REPLACE(UPPER(TRIM(op.raw_name)),  '[^A-Z0-9 ]', '', 'g')
        """,
        {"owner_id": owner_party_id},
    )
    rows = cur.fetchall()
    results = []
    for r in rows:
        results.append(
            {
                "source": "tx_comptroller",
                "person_id": str(r["person_id"]),
                "name": r["person_name"],
                "role": r["role"],
                "entity": r["entity_name"],
            }
        )
    return results


# ---------------------------------------------------------------------------
# Source 2 — Permit contractors
# ---------------------------------------------------------------------------

def _source_permit_contractors(cur, property_id: str) -> list[dict]:
    """
    Person names that pulled permits on this property.
    Filters out obvious company names using heuristic.
    """
    cur.execute(
        """
        SELECT DISTINCT contractor
        FROM permit_event
        WHERE property_id = %(pid)s
          AND contractor IS NOT NULL
          AND contractor <> ''
        """,
        {"pid": property_id},
    )
    rows = cur.fetchall()
    results = []
    seen = set()
    for r in rows:
        name = (r["contractor"] or "").strip()
        if not name or not _looks_like_person(name):
            continue
        norm = _normalize(name)
        if norm in seen:
            continue
        seen.add(norm)
        # Count how many permits
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM permit_event
            WHERE property_id = %(pid)s AND UPPER(TRIM(contractor)) = %(name)s
            """,
            {"pid": property_id, "name": name.upper().strip()},
        )
        cnt = (cur.fetchone() or {}).get("cnt", 1)
        results.append(
            {
                "source": "permit_contractor",
                "person_id": None,
                "name": name,
                "permit_count": int(cnt),
            }
        )
    return results


# ---------------------------------------------------------------------------
# Source 3 — Deed grantors / grantees
# ---------------------------------------------------------------------------

def _source_deed_parties(cur, property_id: str, owner_raw_name: str) -> list[dict]:
    """
    For deeds recorded on this property:
    - Grantor who transferred the property INTO the LLC (the LLC appears as grantee)
    - Grantee who the LLC bought from (LLC appears as grantor elsewhere)
    Both directions reveal the human behind the entity.
    """
    cur.execute(
        """
        SELECT DISTINCT grantor_raw, grantee_raw, doc_type, recording_date
        FROM document_event
        WHERE property_id = %(pid)s
          AND doc_type IN (
              'warranty_deed','special_warranty','quit_claim',
              'WD','DEED','SPECIAL WARRANTY DEED','WARRANTY DEED',
              'QUIT CLAIM DEED'
          )
          AND (grantor_raw IS NOT NULL OR grantee_raw IS NOT NULL)
        ORDER BY recording_date DESC
        """,
        {"pid": property_id},
    )
    rows = cur.fetchall()
    owner_norm = _normalize(owner_raw_name)
    results = []
    seen = set()

    for r in rows:
        grantor = (r["grantor_raw"] or "").strip()
        grantee = (r["grantee_raw"] or "").strip()
        rec_date = r["recording_date"]

        # When current LLC owner appears as grantee, the grantor is the prior human owner
        if grantee and _normalize(grantee) == owner_norm and grantor:
            for part in re.split(r"[;,]", grantor):
                part = part.strip()
                if part and _looks_like_person(part):
                    norm = _normalize(part)
                    if norm not in seen:
                        seen.add(norm)
                        results.append(
                            {
                                "source": "deed_grantor",
                                "person_id": None,
                                "name": part,
                                "deed_date": rec_date.isoformat() if rec_date else None,
                                "role": "grantor_to_llc",
                            }
                        )

        # When LLC appears as grantor, check if grantee is a person (less common but valid)
        if grantor and _normalize(grantor) == owner_norm and grantee:
            for part in re.split(r"[;,]", grantee):
                part = part.strip()
                if part and _looks_like_person(part):
                    norm = _normalize(part)
                    if norm not in seen:
                        seen.add(norm)
                        results.append(
                            {
                                "source": "deed_grantor",
                                "person_id": None,
                                "name": part,
                                "deed_date": rec_date.isoformat() if rec_date else None,
                                "role": "llc_grantee_person",
                            }
                        )
    return results


# ---------------------------------------------------------------------------
# Source 4 — OpenCorporates / entity_record extra data
# ---------------------------------------------------------------------------

def _source_opencorporates(cur, owner_party_id: str) -> list[dict]:
    """
    Checks entity_record for any registered_agent that looks like a person name,
    and treats that as an OpenCorporates-style source if the record has sos_file_number
    indicating it came from a structured registry.
    """
    cur.execute(
        """
        SELECT er.registered_agent, er.entity_name, er.sos_file_number
        FROM entity_record er
        JOIN owner_party op ON op.id = %(owner_id)s
        WHERE
            REGEXP_REPLACE(UPPER(TRIM(er.entity_name)), '[^A-Z0-9 ]', '', 'g')
            = REGEXP_REPLACE(UPPER(TRIM(op.raw_name)),  '[^A-Z0-9 ]', '', 'g')
          AND er.registered_agent IS NOT NULL
          AND er.registered_agent <> ''
        """,
        {"owner_id": owner_party_id},
    )
    rows = cur.fetchall()
    results = []
    seen = set()
    for r in rows:
        agent = (r["registered_agent"] or "").strip()
        if not agent or not _looks_like_person(agent):
            continue
        norm = _normalize(agent)
        if norm in seen:
            continue
        seen.add(norm)
        results.append(
            {
                "source": "opencorporates",
                "person_id": None,
                "name": agent,
                "role": "registered_agent",
                "sos_file_number": r["sos_file_number"],
            }
        )
    return results


# ---------------------------------------------------------------------------
# Source 5 — Mailing address cross-reference
# ---------------------------------------------------------------------------

def _source_mailing_crossref(cur, owner_party_id: str) -> list[dict]:
    """
    If the LLC's mailing address matches another owner_party record that looks
    like a person's name (individual), that person is likely the beneficial owner.
    Also check document_event grantors at the same mailing address.
    """
    cur.execute(
        """
        SELECT mailing_address, mailing_city, mailing_state, mailing_zip
        FROM owner_party
        WHERE id = %(owner_id)s
          AND mailing_address IS NOT NULL
          AND mailing_address NOT ILIKE '%%PO BOX%%'
        """,
        {"owner_id": owner_party_id},
    )
    addr_row = cur.fetchone()
    if not addr_row or not addr_row["mailing_address"]:
        return []

    mailing_addr = addr_row["mailing_address"].strip()
    mailing_city = (addr_row["mailing_city"] or "").strip()

    # Find other owner_party records at the same address that look like individuals
    cur.execute(
        """
        SELECT DISTINCT op.id, op.raw_name, op.party_type
        FROM owner_party op
        WHERE op.id <> %(owner_id)s
          AND UPPER(TRIM(op.mailing_address)) = UPPER(%(addr)s)
          AND (UPPER(TRIM(op.mailing_city)) = UPPER(%(city)s) OR %(city)s = '')
          AND op.raw_name IS NOT NULL
        """,
        {"owner_id": owner_party_id, "addr": mailing_addr, "city": mailing_city},
    )
    rows = cur.fetchall()
    results = []
    seen = set()
    for r in rows:
        name = (r["raw_name"] or "").strip()
        if not name:
            continue
        # Either explicitly individual or looks like a person name
        if r["party_type"] == "individual" or _looks_like_person(name):
            norm = _normalize(name)
            if norm in seen:
                continue
            seen.add(norm)
            # NOTE: person_id is None here — mailing crossref yields an owner_party,
            # not a person_node. Set to None so FK constraint is not violated.
            results.append(
                {
                    "source": "mailing_crossref",
                    "person_id": None,
                    "name": name,
                    "mailing_address": mailing_addr,
                }
            )
    return results


# ---------------------------------------------------------------------------
# Name agreement: group sources by the person name they point to
# ---------------------------------------------------------------------------

def _best_candidate(
    sources: list[dict],
) -> tuple[str | None, list[dict], float]:
    """
    Given evidence from multiple sources (each a dict with 'name' and optionally 'person_id'),
    find the name that appears most frequently across sources (using normalized comparison).
    Returns (best_name, contributing_source_records, confidence).
    """
    if not sources:
        return None, [], 0.0

    # Group by normalized name
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in sources:
        norm = _normalize(s.get("name", ""))
        if norm:
            groups[norm].append(s)

    if not groups:
        return None, [], 0.0

    # Pick the group with the most distinct source types
    best_norm = max(groups, key=lambda k: len({s["source"] for s in groups[k]}))
    best_group = groups[best_norm]
    distinct_source_types = len({s["source"] for s in best_group})
    confidence = _confidence_for_count(distinct_source_types)

    # Use the first raw name spelling from the group
    best_name = best_group[0]["name"]
    return best_name, best_group, confidence


# ---------------------------------------------------------------------------
# Resolve one property
# ---------------------------------------------------------------------------

def _resolve_property(
    cur_read,
    cur_write,
    property_id: str,
    owner_party_id: str,
    owner_raw_name: str,
) -> dict | None:
    """
    Gather evidence from all five sources, determine best candidate, and write results.
    Returns resolution dict or None if no evidence found.
    """
    all_evidence: list[dict] = []

    # Source 1: TX Comptroller
    try:
        comptroller = _source_comptroller(cur_read, owner_party_id)
        all_evidence.extend(comptroller)
    except Exception as exc:
        log.debug("comptroller source error for %s: %s", property_id, exc)

    # Source 2: Permit contractors
    try:
        permits = _source_permit_contractors(cur_read, property_id)
        all_evidence.extend(permits)
    except Exception as exc:
        log.debug("permit source error for %s: %s", property_id, exc)

    # Source 3: Deed parties
    try:
        deeds = _source_deed_parties(cur_read, property_id, owner_raw_name)
        all_evidence.extend(deeds)
    except Exception as exc:
        log.debug("deed source error for %s: %s", property_id, exc)

    # Source 4: OpenCorporates / registered agent
    try:
        oc = _source_opencorporates(cur_read, owner_party_id)
        all_evidence.extend(oc)
    except Exception as exc:
        log.debug("opencorporates source error for %s: %s", property_id, exc)

    # Source 5: Mailing address cross-ref
    try:
        mail = _source_mailing_crossref(cur_read, owner_party_id)
        all_evidence.extend(mail)
    except Exception as exc:
        log.debug("mailing source error for %s: %s", property_id, exc)

    if not all_evidence:
        return None

    best_name, contributors, confidence = _best_candidate(all_evidence)
    if not best_name or confidence < 0.20:
        return None

    # Determine person_id from contributors (prefer tx_comptroller or mailing_crossref)
    person_id = None
    for c in contributors:
        if c.get("person_id"):
            person_id = c["person_id"]
            if c["source"] == "tx_comptroller":
                break  # highest-quality source wins

    # Build resolution chain
    resolution_chain = {
        "beneficial_person_name": best_name,
        "confidence": confidence,
        "source_count": len({c["source"] for c in contributors}),
        "sources": contributors,
    }

    return {
        "property_id": property_id,
        "owner_party_id": owner_party_id,
        "person_id": person_id,
        "best_name": best_name,
        "confidence": confidence,
        "resolution_chain": resolution_chain,
        "source_count": len({c["source"] for c in contributors}),
    }


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_collin_county_resolution(dry_run: bool = False) -> dict:
    """
    Resolve beneficial ownership for ALL Collin County LLC properties.
    Updates ownership_resolution and owner_party tables.
    Returns a summary dict.
    """
    t0 = time.time()
    log.info("Starting Collin County multi-source ownership resolution …")

    conn = _conn()
    conn.autocommit = False
    cur = conn.cursor()

    # Fetch all Collin County LLC properties with their current owner_party
    cur.execute(
        """
        SELECT
            p.id             AS property_id,
            p.property_address,
            po.owner_party_id,
            op.raw_name      AS owner_raw_name,
            or2.id           AS resolution_id
        FROM property p
        JOIN property_ownership po ON po.property_id = p.id AND po.is_current = TRUE
        JOIN owner_party op        ON op.id = po.owner_party_id
        LEFT JOIN ownership_resolution or2 ON or2.property_id = p.id
        WHERE p.county = 'Collin'
          AND op.party_type = 'llc'
        ORDER BY p.id
        """
    )
    properties = cur.fetchall()
    total = len(properties)
    log.info("Found %d Collin County LLC properties to process", total)

    stats = {
        "total": total,
        "resolved": 0,
        "unresolved": 0,
        "confidence_breakdown": {
            "confirmed_0.98": 0,
            "very_likely_0.90": 0,
            "likely_0.70": 0,
            "possible_0.40": 0,
        },
        "source_breakdown": defaultdict(int),
        "elapsed_seconds": 0,
    }

    batch = []
    owner_updates = []

    for i, row in enumerate(properties):
        property_id = str(row["property_id"])
        owner_party_id = str(row["owner_party_id"])
        owner_raw_name = row["owner_raw_name"] or ""
        resolution_id = row["resolution_id"]

        resolution = _resolve_property(
            cur_read=cur,
            cur_write=cur,
            property_id=property_id,
            owner_party_id=owner_party_id,
            owner_raw_name=owner_raw_name,
        )

        if resolution:
            stats["resolved"] += 1
            conf = resolution["confidence"]
            if conf >= 0.98:
                stats["confidence_breakdown"]["confirmed_0.98"] += 1
            elif conf >= 0.90:
                stats["confidence_breakdown"]["very_likely_0.90"] += 1
            elif conf >= 0.70:
                stats["confidence_breakdown"]["likely_0.70"] += 1
            else:
                stats["confidence_breakdown"]["possible_0.40"] += 1

            for c in resolution["resolution_chain"]["sources"]:
                stats["source_breakdown"][c["source"]] += 1

            chain_json = json.dumps(resolution["resolution_chain"])
            method = "multi_source_" + "_".join(
                sorted({c["source"] for c in resolution["resolution_chain"]["sources"]})
            )

            if resolution_id:
                # Update existing resolution
                batch.append(
                    (
                        resolution["person_id"],
                        conf,
                        chain_json,
                        property_id,
                    )
                )
            else:
                # Insert new resolution
                batch.append(
                    (
                        property_id,
                        owner_party_id,
                        resolution["person_id"],
                        conf,
                        chain_json,
                    )
                )

            owner_updates.append(
                (conf, method, owner_party_id)
            )
        else:
            stats["unresolved"] += 1

        # Flush in batches
        if (i + 1) % 500 == 0 or (i + 1) == total:
            log.info("  Progress: %d/%d (%.0f%%)", i + 1, total, (i + 1) / total * 100)
            if not dry_run and batch:
                _flush_batch(cur, conn, batch, owner_updates, properties, i)
                batch = []
                owner_updates = []

    if not dry_run and batch:
        _flush_remaining(cur, conn, batch, owner_updates, properties, total)

    if not dry_run:
        conn.commit()

    conn.close()

    elapsed = time.time() - t0
    stats["elapsed_seconds"] = round(elapsed, 1)
    stats["source_breakdown"] = dict(stats["source_breakdown"])
    log.info("Resolution complete in %.1fs: %s", elapsed, stats)
    return stats


def _flush_remaining(cur, conn, batch, owner_updates, properties, total):
    """Flush remaining records."""
    _flush_batch_raw(cur, conn, batch, owner_updates)


def _flush_batch(cur, conn, batch, owner_updates, properties, idx):
    """Flush a batch of upserts."""
    _flush_batch_raw(cur, conn, batch, owner_updates)


def _flush_batch_raw(cur, conn, batch, owner_updates):
    """Execute upserts for a batch."""
    if not batch:
        return

    # We mixed update/insert rows — we'll just do an upsert via ON CONFLICT
    for item in batch:
        if len(item) == 4:
            # Update existing: (person_id, confidence, chain_json, property_id)
            person_id, conf, chain_json, property_id = item
            cur.execute(
                """
                UPDATE ownership_resolution
                SET beneficial_person_id = %s,
                    confidence = %s,
                    resolution_chain = %s::jsonb,
                    computed_at = NOW()
                WHERE property_id = %s
                """,
                (person_id, conf, chain_json, property_id),
            )
        else:
            # Insert: (property_id, owner_party_id, person_id, confidence, chain_json)
            property_id, owner_party_id, person_id, conf, chain_json = item
            cur.execute(
                """
                INSERT INTO ownership_resolution
                    (property_id, direct_owner_id, beneficial_person_id, confidence,
                     resolution_chain, computed_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (property_id) DO UPDATE SET
                    beneficial_person_id = EXCLUDED.beneficial_person_id,
                    confidence           = EXCLUDED.confidence,
                    resolution_chain     = EXCLUDED.resolution_chain,
                    computed_at          = NOW()
                """,
                (property_id, owner_party_id, person_id, conf, chain_json),
            )

    for conf, method, owner_party_id in owner_updates:
        cur.execute(
            """
            UPDATE owner_party
            SET resolution_confidence = %s,
                resolution_method     = %s,
                updated_at            = NOW()
            WHERE id = %s
            """,
            (conf, method, owner_party_id),
        )

    conn.commit()


# ---------------------------------------------------------------------------
# Single-property lookup for the API endpoint
# ---------------------------------------------------------------------------

def get_ownership_chain(property_id: str) -> dict | None:
    """
    Returns the full ownership chain for a single property, suitable for
    the GET /api/properties/{id}/ownership-chain endpoint.
    """
    conn = _conn()
    cur = conn.cursor()

    try:
        # Basic property + current owner
        cur.execute(
            """
            SELECT
                p.id,
                p.property_address,
                p.city,
                p.county,
                op.id         AS owner_party_id,
                op.raw_name   AS cad_owner,
                op.party_type,
                op.mailing_address,
                op.mailing_city,
                op.mailing_state,
                op.resolution_confidence,
                op.resolution_method
            FROM property p
            JOIN property_ownership po ON po.property_id = p.id AND po.is_current = TRUE
            JOIN owner_party op        ON op.id = po.owner_party_id
            WHERE p.id = %s
            """,
            (property_id,),
        )
        prop = cur.fetchone()
        if not prop:
            return None

        owner_party_id = str(prop["owner_party_id"])

        # Existing resolution
        cur.execute(
            """
            SELECT
                or2.beneficial_person_id,
                or2.confidence,
                or2.resolution_chain,
                pn.full_name AS beneficial_person_name
            FROM ownership_resolution or2
            LEFT JOIN person_node pn ON pn.id = or2.beneficial_person_id
            WHERE or2.property_id = %s
            ORDER BY or2.computed_at DESC
            LIMIT 1
            """,
            (property_id,),
        )
        res = cur.fetchone()

        # Source 1: TX Comptroller officers
        comptroller_evidence = _source_comptroller(cur, owner_party_id)

        # Source 2: Permit contractors
        permit_evidence = _source_permit_contractors(cur, property_id)

        # Source 3: Deed parties
        deed_evidence = _source_deed_parties(
            cur, property_id, prop["cad_owner"] or ""
        )

        # Source 4: OpenCorporates
        oc_evidence = _source_opencorporates(cur, owner_party_id)

        # Source 5: Mailing crossref
        mail_evidence = _source_mailing_crossref(cur, owner_party_id)

        all_evidence = (
            comptroller_evidence
            + permit_evidence
            + deed_evidence
            + oc_evidence
            + mail_evidence
        )

        # Build sources list for response
        sources_out = []
        for e in comptroller_evidence:
            sources_out.append(
                {
                    "source": "tx_comptroller",
                    "name": e["name"],
                    "role": e.get("role"),
                    "entity": e.get("entity"),
                }
            )
        for e in permit_evidence:
            sources_out.append(
                {
                    "source": "permit_contractor",
                    "name": e["name"],
                    "permits": e.get("permit_count", 1),
                }
            )
        for e in deed_evidence:
            sources_out.append(
                {
                    "source": "deed_grantor",
                    "name": e["name"],
                    "deed_date": e.get("deed_date"),
                    "role": e.get("role"),
                }
            )
        for e in oc_evidence:
            sources_out.append(
                {
                    "source": "opencorporates",
                    "name": e["name"],
                    "role": e.get("role"),
                }
            )
        for e in mail_evidence:
            sources_out.append(
                {
                    "source": "mailing_crossref",
                    "name": e["name"],
                    "mailing_address": e.get("mailing_address"),
                }
            )

        # Determine best candidate from live evidence
        best_name, contributors, live_confidence = _best_candidate(all_evidence)

        # If we have a stored resolution, prefer it (higher authority); fall back to live
        beneficial_person_name = None
        confidence = 0.0
        if res:
            beneficial_person_name = res["beneficial_person_name"] or best_name
            confidence = float(res["confidence"] or live_confidence)
        elif best_name:
            beneficial_person_name = best_name
            confidence = live_confidence

        # Related LLCs — other LLCs sharing the same person (via entity_person)
        related_entities: list[str] = []
        if beneficial_person_name:
            cur.execute(
                """
                SELECT DISTINCT er2.entity_name
                FROM entity_record er2
                JOIN entity_person ep2 ON ep2.entity_id = er2.id
                JOIN entity_person ep1 ON ep1.person_id = ep2.person_id
                JOIN entity_record er1 ON er1.id = ep1.entity_id
                JOIN owner_party op2 ON op2.id = %(op_id)s
                WHERE REGEXP_REPLACE(UPPER(TRIM(er1.entity_name)), '[^A-Z0-9 ]', '', 'g')
                      = REGEXP_REPLACE(UPPER(TRIM(op2.raw_name)), '[^A-Z0-9 ]', '', 'g')
                  AND er2.entity_name <> er1.entity_name
                LIMIT 20
                """,
                {"op_id": owner_party_id},
            )
            related_entities = [r["entity_name"] for r in cur.fetchall()]

        # Portfolio stats — all properties owned by LLCs sharing the same controlling person
        # (simplified: count properties of this owner_party)
        cur.execute(
            """
            SELECT COUNT(DISTINCT po2.property_id) AS prop_count,
                   COALESCE(SUM(vs.market_value), 0) AS total_value
            FROM property_ownership po2
            JOIN (
                SELECT property_id, MAX(tax_year) AS max_year
                FROM valuation_snapshot
                GROUP BY property_id
            ) latest ON latest.property_id = po2.property_id
            JOIN valuation_snapshot vs
                 ON vs.property_id = latest.property_id
                AND vs.tax_year   = latest.max_year
            WHERE po2.owner_party_id = %s AND po2.is_current = TRUE
            """,
            (owner_party_id,),
        )
        port = cur.fetchone() or {}

        return {
            "property_address": prop["property_address"],
            "city": prop["city"],
            "county": prop["county"],
            "cad_owner": prop["cad_owner"],
            "resolution": {
                "beneficial_person": beneficial_person_name,
                "confidence": confidence,
                "sources": sources_out,
            }
            if beneficial_person_name
            else None,
            "related_entities": related_entities,
            "total_portfolio": {
                "properties": int(port.get("prop_count") or 0),
                "total_value": float(port.get("total_value") or 0),
            },
        }

    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    dry = "--dry-run" in sys.argv
    if dry:
        print("DRY RUN — no database writes")

    result = run_collin_county_resolution(dry_run=dry)
    print("\n=== Resolution Summary ===")
    print(f"Total LLC properties:  {result['total']}")
    print(f"Resolved:              {result['resolved']}")
    print(f"Unresolved:            {result['unresolved']}")
    print(f"Elapsed:               {result['elapsed_seconds']}s")
    print("\nConfidence breakdown:")
    for tier, count in result["confidence_breakdown"].items():
        print(f"  {tier}: {count}")
    print("\nSource contribution:")
    for source, count in sorted(result["source_breakdown"].items(), key=lambda x: -x[1]):
        print(f"  {source}: {count}")
