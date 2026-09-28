"""
Pre-Foreclosure Risk Scoring Service

Implements a tiered signal model:
  Tier 1 (Strong Leads)     — any single signal = HIGH alert
  Tier 2 (Supporting Stress) — amplifies Tier 1 signals
  Tier 3 (Contextual)       — background context signals

Final risk levels: CRITICAL / HIGH / ELEVATED / MODERATE / LOW
"""
from datetime import date
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text
import uuid
import json

TODAY = date.today()

# Score weights
TIER1_BASE_SCORE = 50
TIER2_SCORE = 10
TIER3_SCORE = 5

RISK_LEVELS = {
    (80, 100): "CRITICAL",
    (60, 79): "HIGH",
    (40, 59): "ELEVATED",
    (20, 39): "MODERATE",
    (0, 19): "LOW",
}


def _risk_level(score: int) -> str:
    for (low, high), level in RISK_LEVELS.items():
        if low <= score <= high:
            return level
    return "LOW"


# ── Tier 1 signal detectors ──────────────────────────────────────────────────

def _check_maturing_debt(db: Session, property_id: str) -> Optional[dict]:
    """Debt maturing within 12 months with no visible refinance."""
    sql = text("""
        SELECT
            di.id::text,
            di.maturity_date,
            di.lender_name,
            di.has_subsequent_financing,
            di.original_amount,
            di.is_released,
            de.recording_date
        FROM debt_instrument di
        LEFT JOIN document_event de ON de.id = di.document_event_id
        WHERE di.property_id = :pid
          AND di.maturity_date BETWEEN :today AND :future
          AND di.has_subsequent_financing = false
          AND di.is_released = false
        ORDER BY di.maturity_date ASC
        LIMIT 1
    """)
    future = date(TODAY.year + 1, TODAY.month, TODAY.day)
    row = db.execute(sql, {"pid": property_id, "today": TODAY, "future": future}).fetchone()

    if row:
        delta_days = (row.maturity_date - TODAY).days
        months_remaining = round(delta_days / 30.44)
        return {
            "signal": "debt_maturing_within_12m",
            "label": "Loan Maturing Within 12 Months",
            "maturity_date": row.maturity_date.isoformat(),
            "months_remaining": months_remaining,
            "lender_name": row.lender_name,
            "original_amount": float(row.original_amount) if row.original_amount else None,
            "no_refinance": True,
        }
    return None


def _check_past_due_debt(db: Session, property_id: str) -> Optional[dict]:
    """Debt already past maturity with no release — balloon payment risk."""
    sql = text("""
        SELECT
            di.id::text,
            di.maturity_date,
            di.lender_name,
            di.original_amount,
            di.has_subsequent_financing
        FROM debt_instrument di
        WHERE di.property_id = :pid
          AND di.maturity_date < :today
          AND di.is_released = false
          AND di.status = 'active'
          AND di.has_subsequent_financing = false
        ORDER BY di.maturity_date DESC
        LIMIT 1
    """)
    row = db.execute(sql, {"pid": property_id, "today": TODAY}).fetchone()

    if row:
        months_past = round((TODAY - row.maturity_date).days / 30.44)
        return {
            "signal": "debt_past_maturity_unreleased",
            "label": "Loan Past Maturity — Not Released",
            "maturity_date": row.maturity_date.isoformat(),
            "months_past_due": months_past,
            "lender_name": row.lender_name,
            "original_amount": float(row.original_amount) if row.original_amount else None,
        }
    return None


def _check_substitute_trustee(db: Session, property_id: str) -> Optional[dict]:
    """Substitute trustee deed in document history — strong foreclosure indicator."""
    sql = text("""
        SELECT id::text, doc_type, recording_date, grantor_raw, grantee_raw
        FROM document_event
        WHERE property_id = :pid
          AND doc_type IN ('substitute_trustee_deed', 'trustee_deed', 'foreclosure_deed')
        ORDER BY recording_date DESC
        LIMIT 1
    """)
    row = db.execute(sql, {"pid": property_id}).fetchone()

    if row:
        return {
            "signal": "substitute_trustee_deed",
            "label": "Substitute Trustee Deed Recorded",
            "doc_type": row.doc_type,
            "recording_date": row.recording_date.isoformat() if row.recording_date else None,
            "grantor": row.grantor_raw,
            "grantee": row.grantee_raw,
        }
    return None


def _check_bankruptcy(db: Session, property_id: str) -> Optional[dict]:
    """Bankruptcy filing linked to property."""
    sql = text("""
        SELECT id::text, filing_type, filing_date, defendant, debtor, case_number, status
        FROM filing_node
        WHERE property_id = :pid
          AND filing_type IN ('bankruptcy', 'chapter_7', 'chapter_11', 'chapter_13')
        ORDER BY filing_date DESC
        LIMIT 1
    """)
    row = db.execute(sql, {"pid": property_id}).fetchone()

    if row:
        return {
            "signal": "bankruptcy_filing",
            "label": "Bankruptcy Filing",
            "filing_type": row.filing_type,
            "filing_date": row.filing_date.isoformat() if row.filing_date else None,
            "party": row.defendant or row.debtor,
            "case_number": row.case_number,
            "status": row.status,
        }
    return None


def _check_lender_litigation(db: Session, property_id: str) -> Optional[dict]:
    """Lis pendens or foreclosure filing — lender initiated legal action."""
    sql = text("""
        SELECT id::text, filing_type, filing_date, plaintiff, defendant,
               case_number, status, description, sale_date
        FROM filing_node
        WHERE property_id = :pid
          AND filing_type IN ('lis_pendens', 'foreclosure')
        ORDER BY filing_date DESC NULLS LAST
        LIMIT 1
    """)
    row = db.execute(sql, {"pid": property_id}).fetchone()

    if row:
        return {
            "signal": "lender_litigation",
            "label": "Lender Litigation / Lis Pendens",
            "filing_type": row.filing_type,
            "filing_date": row.filing_date.isoformat() if row.filing_date else None,
            "plaintiff": row.plaintiff,
            "defendant": row.defendant,
            "case_number": row.case_number,
            "sale_date": row.sale_date.isoformat() if row.sale_date else None,
            "description": row.description,
        }
    return None


# ── Tier 2 signal detectors ──────────────────────────────────────────────────

def _check_tax_delinquent(db: Session, property_id: str) -> Optional[dict]:
    """Tax delinquency from valuation_snapshot."""
    sql = text("""
        SELECT tax_status, tax_delinquent_amount, tax_delinquent_years, tax_year
        FROM valuation_snapshot
        WHERE property_id = :pid
          AND tax_status IN ('DELINQUENT', 'LIKELY_DELINQUENT')
        ORDER BY tax_year DESC
        LIMIT 1
    """)
    row = db.execute(sql, {"pid": property_id}).fetchone()

    if row:
        return {
            "signal": "tax_delinquent",
            "label": "Tax Delinquency",
            "tax_status": row.tax_status,
            "tax_year": row.tax_year,
            "delinquent_amount": float(row.tax_delinquent_amount) if row.tax_delinquent_amount else None,
            "delinquent_years": row.tax_delinquent_years,
        }
    return None


def _check_mechanic_lien(db: Session, property_id: str) -> Optional[dict]:
    """Mechanic's lien in filing_node."""
    sql = text("""
        SELECT id::text, filing_type, filing_date, plaintiff, amount, status
        FROM filing_node
        WHERE property_id = :pid
          AND filing_type = 'mechanic_lien'
        ORDER BY filing_date DESC
        LIMIT 1
    """)
    row = db.execute(sql, {"pid": property_id}).fetchone()

    if row:
        return {
            "signal": "mechanic_lien",
            "label": "Mechanic's Lien",
            "filing_date": row.filing_date.isoformat() if row.filing_date else None,
            "claimant": row.plaintiff,
            "amount": float(row.amount) if row.amount else None,
            "status": row.status,
        }
    return None


def _check_judgment_lien(db: Session, property_id: str) -> Optional[dict]:
    """Judgment lien in filing_node."""
    sql = text("""
        SELECT id::text, filing_type, filing_date, plaintiff, defendant, amount, status
        FROM filing_node
        WHERE property_id = :pid
          AND filing_type = 'judgment'
        ORDER BY filing_date DESC
        LIMIT 1
    """)
    row = db.execute(sql, {"pid": property_id}).fetchone()

    if row:
        return {
            "signal": "judgment_lien",
            "label": "Judgment Lien",
            "filing_date": row.filing_date.isoformat() if row.filing_date else None,
            "plaintiff": row.plaintiff,
            "defendant": row.defendant,
            "amount": float(row.amount) if row.amount else None,
            "status": row.status,
        }
    return None


def _check_ucc_concentration(db: Session, property_id: str) -> Optional[dict]:
    """UCC filings > 2 against same entity (same debtor)."""
    sql = text("""
        SELECT debtor, COUNT(*) as cnt
        FROM filing_node
        WHERE property_id = :pid
          AND filing_type = 'ucc'
          AND debtor IS NOT NULL
        GROUP BY debtor
        HAVING COUNT(*) > 2
        ORDER BY cnt DESC
        LIMIT 1
    """)
    row = db.execute(sql, {"pid": property_id}).fetchone()

    if row:
        return {
            "signal": "ucc_concentration",
            "label": "Multiple UCC Filings (>2 against same entity)",
            "debtor": row.debtor,
            "ucc_count": row.cnt,
        }
    return None


def _check_code_violations(db: Session, property_id: str) -> Optional[dict]:
    """Code violations > 3 against the property."""
    sql = text("""
        SELECT COUNT(*) as cnt
        FROM code_event
        WHERE property_id = :pid
    """)
    row = db.execute(sql, {"pid": property_id}).fetchone()

    if row and row.cnt > 3:
        return {
            "signal": "code_violations",
            "label": f"Multiple Code Violations ({row.cnt} total)",
            "violation_count": row.cnt,
        }
    return None


# ── Tier 3 signal detectors ──────────────────────────────────────────────────

def _check_recent_ownership_change(db: Session, property_id: str) -> Optional[dict]:
    """Ownership change in last 2 years via document_event."""
    cutoff = date(TODAY.year - 2, TODAY.month, TODAY.day)
    sql = text("""
        SELECT recording_date, doc_type, grantor_raw, grantee_raw, consideration
        FROM document_event
        WHERE property_id = :pid
          AND doc_category = 'transfer'
          AND recording_date >= :cutoff
        ORDER BY recording_date DESC
        LIMIT 1
    """)
    row = db.execute(sql, {"pid": property_id, "cutoff": cutoff}).fetchone()

    if row:
        return {
            "signal": "recent_ownership_change",
            "label": "Recent Ownership Change (< 2 years)",
            "recording_date": row.recording_date.isoformat() if row.recording_date else None,
            "doc_type": row.doc_type,
            "from_owner": row.grantor_raw,
            "to_owner": row.grantee_raw,
            "consideration": float(row.consideration) if row.consideration else None,
        }
    return None


def _check_absentee_owner(db: Session, property_id: str) -> Optional[dict]:
    """Mailing address differs from property address (absentee owner)."""
    sql = text("""
        SELECT
            op.mailing_address,
            op.mailing_city,
            op.mailing_state,
            op.mailing_zip,
            op.mailing_type,
            op.is_out_of_state,
            p.property_address,
            p.city,
            p.state,
            p.zip
        FROM property_ownership po
        JOIN owner_party op ON op.id = po.owner_party_id
        JOIN property p ON p.id = po.property_id
        WHERE po.property_id = :pid
          AND po.is_current = true
          AND op.mailing_address IS NOT NULL
        LIMIT 1
    """)
    row = db.execute(sql, {"pid": property_id}).fetchone()

    if not row:
        return None

    mailing_addr = (row.mailing_address or "").strip().lower()
    prop_addr = (row.property_address or "").strip().lower()

    is_absentee = (
        row.is_out_of_state
        or row.mailing_type == "po_box"
        or (mailing_addr and prop_addr and mailing_addr != prop_addr)
    )

    if is_absentee:
        return {
            "signal": "absentee_owner",
            "label": "Absentee Owner (mailing differs from property)",
            "mailing_address": row.mailing_address,
            "mailing_city": row.mailing_city,
            "mailing_state": row.mailing_state,
            "property_address": row.property_address,
            "is_out_of_state": row.is_out_of_state,
            "mailing_type": row.mailing_type,
        }
    return None


def _check_portfolio_stress(db: Session, property_id: str) -> Optional[dict]:
    """Multiple properties by the same owner showing stress signals."""
    sql = text("""
        WITH owner AS (
            SELECT po.owner_party_id
            FROM property_ownership po
            WHERE po.property_id = :pid
              AND po.is_current = true
            LIMIT 1
        ),
        owner_properties AS (
            SELECT DISTINCT po2.property_id
            FROM property_ownership po2
            JOIN owner ON po2.owner_party_id = owner.owner_party_id
            WHERE po2.property_id::text != :pid
        ),
        stressed AS (
            SELECT vs.property_id
            FROM valuation_snapshot vs
            JOIN owner_properties op ON op.property_id = vs.property_id
            WHERE vs.tax_status IN ('DELINQUENT', 'LIKELY_DELINQUENT')
            UNION
            SELECT fn.property_id
            FROM filing_node fn
            JOIN owner_properties op ON op.property_id = fn.property_id
            WHERE fn.filing_type IN ('lis_pendens', 'foreclosure', 'judgment', 'tax_lien')
        )
        SELECT COUNT(DISTINCT property_id) as cnt FROM stressed
    """)
    row = db.execute(sql, {"pid": property_id}).fetchone()

    if row and row.cnt > 0:
        return {
            "signal": "portfolio_stress",
            "label": f"Owner Portfolio Stress ({row.cnt} other properties with stress signals)",
            "stressed_property_count": row.cnt,
        }
    return None


# ── Main risk scorer ─────────────────────────────────────────────────────────

def compute_pre_foreclosure_risk(db: Session, property_id: str) -> dict:
    """
    Compute pre-foreclosure risk for a property.

    Returns:
      - risk_level: CRITICAL / HIGH / ELEVATED / MODERATE / LOW
      - risk_score: 0-100
      - tier1_signals: list of active Tier 1 signals with evidence
      - tier2_signals: list
      - tier3_signals: list
      - explanation: plain English reason
      - confidence: 0.0 - 1.0 based on data completeness
    """
    # Gather all signals
    tier1_signals = []
    tier2_signals = []
    tier3_signals = []

    # --- Tier 1 ---
    maturing = _check_maturing_debt(db, property_id)
    if maturing:
        tier1_signals.append(maturing)

    past_due = _check_past_due_debt(db, property_id)
    if past_due:
        tier1_signals.append(past_due)

    trustee = _check_substitute_trustee(db, property_id)
    if trustee:
        tier1_signals.append(trustee)

    bankruptcy = _check_bankruptcy(db, property_id)
    if bankruptcy:
        tier1_signals.append(bankruptcy)

    litigation = _check_lender_litigation(db, property_id)
    if litigation:
        tier1_signals.append(litigation)

    # --- Tier 2 ---
    tax = _check_tax_delinquent(db, property_id)
    if tax:
        tier2_signals.append(tax)

    mechanic = _check_mechanic_lien(db, property_id)
    if mechanic:
        tier2_signals.append(mechanic)

    judgment = _check_judgment_lien(db, property_id)
    if judgment:
        tier2_signals.append(judgment)

    ucc = _check_ucc_concentration(db, property_id)
    if ucc:
        tier2_signals.append(ucc)

    violations = _check_code_violations(db, property_id)
    if violations:
        tier2_signals.append(violations)

    # --- Tier 3 ---
    ownership_change = _check_recent_ownership_change(db, property_id)
    if ownership_change:
        tier3_signals.append(ownership_change)

    absentee = _check_absentee_owner(db, property_id)
    if absentee:
        tier3_signals.append(absentee)

    portfolio = _check_portfolio_stress(db, property_id)
    if portfolio:
        tier3_signals.append(portfolio)

    # --- Score calculation ---
    score = 0
    t1_count = len(tier1_signals)
    t2_count = len(tier2_signals)
    t3_count = len(tier3_signals)

    if t1_count > 0:
        # Base score for first Tier 1 signal
        score += TIER1_BASE_SCORE
        # Each additional Tier 1 signal adds weight
        score += (t1_count - 1) * 15
        # Tier 2 amplifies
        score += t2_count * TIER2_SCORE
        # Tier 3 adds context
        score += t3_count * TIER3_SCORE
    else:
        # No Tier 1: score based on Tier 2 and 3 only
        score += t2_count * TIER2_SCORE
        score += t3_count * TIER3_SCORE

    score = min(score, 100)

    # Override: HIGH risk requires T1 + at least one T2 (per PRD)
    risk_level = _risk_level(score)
    if t1_count > 0 and t2_count >= 1 and risk_level not in ("CRITICAL", "HIGH"):
        risk_level = "HIGH"
        score = max(score, 60)

    # --- Explanation ---
    explanation = _build_explanation(tier1_signals, tier2_signals, tier3_signals, risk_level)

    # --- Confidence based on data completeness ---
    confidence = _compute_confidence(db, property_id)

    return {
        "risk_level": risk_level,
        "risk_score": score,
        "tier1_signals": tier1_signals,
        "tier2_signals": tier2_signals,
        "tier3_signals": tier3_signals,
        "explanation": explanation,
        "confidence": confidence,
        "computed_at": TODAY.isoformat(),
    }


def _build_explanation(tier1: list, tier2: list, tier3: list, risk_level: str) -> str:
    """Generate plain-English explanation of why this property is at risk."""
    parts = []

    if not tier1 and not tier2 and not tier3:
        return "No significant pre-foreclosure signals detected."

    if tier1:
        t1_labels = [s["label"] for s in tier1]
        parts.append(f"Primary stress indicators: {', '.join(t1_labels)}.")

    if tier2:
        t2_labels = [s["label"] for s in tier2]
        parts.append(f"Supporting stress factors: {', '.join(t2_labels)}.")

    if tier3:
        t3_labels = [s["label"] for s in tier3]
        parts.append(f"Contextual factors: {', '.join(t3_labels)}.")

    if risk_level == "CRITICAL":
        parts.append(
            "This property exhibits multiple strong indicators of imminent distress and requires immediate attention."
        )
    elif risk_level == "HIGH":
        parts.append(
            "This property has both a primary stress trigger and supporting distress factors — high probability of financial distress."
        )
    elif risk_level == "ELEVATED":
        parts.append(
            "This property shows moderate stress signals that warrant monitoring."
        )

    return " ".join(parts)


def _compute_confidence(db: Session, property_id: str) -> float:
    """
    Confidence score based on data availability:
    - Has debt instrument: +0.3
    - Has valuation snapshot with known tax status: +0.2
    - Has filing nodes: +0.2
    - Has document events: +0.2
    - Has ownership record: +0.1
    Max = 1.0
    """
    sql = text("""
        SELECT
            (SELECT COUNT(*) FROM debt_instrument WHERE property_id = :pid)      AS has_debt,
            (SELECT COUNT(*) FROM valuation_snapshot
             WHERE property_id = :pid AND tax_status != 'UNKNOWN')               AS has_valuation,
            (SELECT COUNT(*) FROM filing_node WHERE property_id = :pid)          AS has_filings,
            (SELECT COUNT(*) FROM document_event WHERE property_id = :pid)       AS has_docs,
            (SELECT COUNT(*) FROM property_ownership WHERE property_id = :pid)   AS has_ownership
    """)
    row = db.execute(sql, {"pid": property_id}).fetchone()

    if not row:
        return 0.1

    confidence = 0.0
    if row.has_debt > 0:
        confidence += 0.3
    if row.has_valuation > 0:
        confidence += 0.2
    if row.has_filings > 0:
        confidence += 0.2
    if row.has_docs > 0:
        confidence += 0.2
    if row.has_ownership > 0:
        confidence += 0.1

    return round(min(confidence, 1.0), 2)


# ── Batch scoring ─────────────────────────────────────────────────────────────

def score_all_properties(db: Session, batch_size: int = 500) -> dict:
    """
    Batch compute pre-foreclosure risk for all properties and store in signal_result.

    Returns summary statistics.
    """
    # Delete existing pre_foreclosure_risk signals to avoid duplicates
    db.execute(text("DELETE FROM signal_result WHERE signal_key = 'pre_foreclosure_risk'"))
    db.commit()

    # Fetch all property IDs
    rows = db.execute(text("SELECT id::text FROM property ORDER BY id")).fetchall()
    total = len(rows)

    stats = {
        "total_processed": 0,
        "total_errors": 0,
        "risk_distribution": {"CRITICAL": 0, "HIGH": 0, "ELEVATED": 0, "MODERATE": 0, "LOW": 0},
        "tier1_signal_count": 0,
    }

    insert_sql = text("""
        INSERT INTO signal_result
            (id, property_id, signal_key, status, score_delta, confidence, evidence, explanation, signal_version, computed_at)
        VALUES
            (:id, :property_id, 'pre_foreclosure_risk', 'active', :score_delta, :confidence, :evidence, :explanation, 1, now())
    """)

    batch = []

    for i, row in enumerate(rows):
        pid = row[0]
        try:
            result = compute_pre_foreclosure_risk(db, pid)

            evidence = {
                "tier1_signals": result["tier1_signals"],
                "tier2_signals": result["tier2_signals"],
                "tier3_signals": result["tier3_signals"],
                "risk_level": result["risk_level"],
                "computed_at": result["computed_at"],
            }

            batch.append({
                "id": str(uuid.uuid4()),
                "property_id": pid,
                "score_delta": result["risk_score"],
                "confidence": result["confidence"],
                "evidence": json.dumps(evidence),
                "explanation": result["explanation"],
            })

            stats["risk_distribution"][result["risk_level"]] += 1
            if result["tier1_signals"]:
                stats["tier1_signal_count"] += 1

        except Exception as e:
            stats["total_errors"] += 1

        stats["total_processed"] += 1

        # Commit in batches
        if len(batch) >= batch_size:
            db.execute(insert_sql, batch)
            db.commit()
            batch = []

        if (i + 1) % 5000 == 0:
            print(f"  Progress: {i + 1}/{total} ({round((i+1)/total*100, 1)}%)")

    # Flush remaining
    if batch:
        db.execute(insert_sql, batch)
        db.commit()

    print(f"Completed: {stats['total_processed']} properties scored.")
    return stats


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_high_risk_properties(
    db: Session,
    min_score: int = 60,
    limit: int = 100,
    offset: int = 0,
) -> list:
    """Return properties with risk_score >= min_score, ordered by score desc."""
    sql = text("""
        SELECT
            sr.property_id::text,
            sr.score_delta          AS risk_score,
            sr.confidence,
            sr.explanation,
            sr.evidence,
            sr.computed_at,
            p.property_address,
            p.city,
            p.state,
            p.zip,
            p.county,
            p.land_use_description,
            p.latitude,
            p.longitude,
            (sr.evidence->>'risk_level') AS risk_level
        FROM signal_result sr
        JOIN property p ON p.id = sr.property_id
        WHERE sr.signal_key = 'pre_foreclosure_risk'
          AND sr.status = 'active'
          AND sr.score_delta >= :min_score
        ORDER BY sr.score_delta DESC, sr.confidence DESC
        LIMIT :limit OFFSET :offset
    """)
    rows = db.execute(sql, {"min_score": min_score, "limit": limit, "offset": offset}).fetchall()

    results = []
    for row in rows:
        evidence = row.evidence if isinstance(row.evidence, dict) else {}
        results.append({
            "property_id": row.property_id,
            "property_address": row.property_address,
            "city": row.city,
            "state": row.state,
            "zip": row.zip,
            "county": row.county,
            "land_use_description": row.land_use_description,
            "latitude": float(row.latitude) if row.latitude else None,
            "longitude": float(row.longitude) if row.longitude else None,
            "risk_level": row.risk_level or _risk_level(row.risk_score),
            "risk_score": row.risk_score,
            "confidence": float(row.confidence) if row.confidence else None,
            "explanation": row.explanation,
            "tier1_signals": evidence.get("tier1_signals", []),
            "tier2_signals": evidence.get("tier2_signals", []),
            "tier3_signals": evidence.get("tier3_signals", []),
            "computed_at": row.computed_at.isoformat() if row.computed_at else None,
        })

    return results


def get_risk_summary(db: Session) -> dict:
    """Return aggregate risk statistics across all scored properties."""
    sql = text("""
        SELECT
            (sr.evidence->>'risk_level') AS risk_level,
            COUNT(*) AS count,
            AVG(sr.score_delta) AS avg_score,
            AVG(sr.confidence) AS avg_confidence
        FROM signal_result sr
        WHERE sr.signal_key = 'pre_foreclosure_risk'
          AND sr.status = 'active'
        GROUP BY 1
        ORDER BY AVG(sr.score_delta) DESC
    """)
    rows = db.execute(sql).fetchall()

    total_sql = text("""
        SELECT
            COUNT(*) AS total,
            COUNT(CASE WHEN score_delta >= 60 THEN 1 END) AS high_risk_count,
            COUNT(CASE WHEN score_delta >= 40 THEN 1 END) AS elevated_plus_count,
            MAX(score_delta) AS max_score,
            AVG(score_delta) AS avg_score,
            AVG(confidence) AS avg_confidence
        FROM signal_result
        WHERE signal_key = 'pre_foreclosure_risk' AND status = 'active'
    """)
    totals = db.execute(total_sql).fetchone()

    tier1_sql = text("""
        SELECT COUNT(*) FROM signal_result
        WHERE signal_key = 'pre_foreclosure_risk'
          AND status = 'active'
          AND jsonb_array_length(evidence->'tier1_signals') > 0
    """)
    tier1_count = db.execute(tier1_sql).fetchone()

    distribution = {}
    for row in rows:
        level = row.risk_level or "UNKNOWN"
        distribution[level] = {
            "count": row.count,
            "avg_score": round(float(row.avg_score), 1) if row.avg_score else 0,
            "avg_confidence": round(float(row.avg_confidence), 2) if row.avg_confidence else 0,
        }

    return {
        "total_scored": totals.total if totals else 0,
        "high_risk_count": totals.high_risk_count if totals else 0,
        "elevated_plus_count": totals.elevated_plus_count if totals else 0,
        "max_score": totals.max_score if totals else 0,
        "avg_score": round(float(totals.avg_score), 1) if totals and totals.avg_score else 0,
        "avg_confidence": round(float(totals.avg_confidence), 2) if totals and totals.avg_confidence else 0,
        "tier1_signal_count": tier1_count[0] if tier1_count else 0,
        "distribution_by_risk_level": distribution,
    }
