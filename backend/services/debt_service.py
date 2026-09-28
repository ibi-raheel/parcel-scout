"""
Debt Intelligence Service
Provides loan chronology, maturity analysis, and release gap detection
for properties using debt_instrument and document_event data.
"""
from datetime import date, datetime
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text


TODAY = date.today()


def get_loan_chronology(db: Session, property_id: str) -> list:
    """
    Returns ordered list of financing events for a property:
    origination (deed_of_trust), transfers, and debt instrument records.
    """
    sql = text("""
        SELECT
            de.id::text          AS event_id,
            de.doc_type,
            de.doc_category,
            de.recording_date,
            de.effective_date,
            de.grantor_raw       AS grantor,
            de.grantee_raw       AS grantee,
            de.loan_amount,
            de.lender_name       AS doc_lender,
            de.maturity_date     AS doc_maturity,
            di.id::text          AS debt_id,
            di.original_amount,
            di.current_balance_est,
            di.interest_rate,
            di.maturity_date     AS debt_maturity,
            di.lender_name       AS debt_lender,
            di.loan_type,
            di.status            AS debt_status,
            di.is_released,
            di.release_date,
            di.has_subsequent_financing,
            di.data_quality
        FROM document_event de
        LEFT JOIN debt_instrument di ON di.document_event_id = de.id
        WHERE de.property_id = :pid
        ORDER BY COALESCE(de.recording_date, de.effective_date) ASC
    """)

    rows = db.execute(sql, {"pid": property_id}).fetchall()

    # Also get debt instruments not linked to doc events
    debt_sql = text("""
        SELECT
            di.id::text          AS debt_id,
            di.property_id::text,
            di.original_amount,
            di.current_balance_est,
            di.interest_rate,
            di.maturity_date     AS debt_maturity,
            di.lender_name,
            di.loan_type,
            di.status,
            di.is_released,
            di.release_date,
            di.has_subsequent_financing,
            di.data_quality,
            di.source,
            di.created_at
        FROM debt_instrument di
        WHERE di.property_id = :pid
          AND di.document_event_id IS NULL
        ORDER BY di.created_at ASC
    """)
    orphan_debts = db.execute(debt_sql, {"pid": property_id}).fetchall()

    chronology = []

    for row in rows:
        event = {
            "event_id": row.event_id,
            "event_type": _classify_doc_event(row.doc_type, row.doc_category),
            "doc_type": row.doc_type,
            "doc_category": row.doc_category,
            "recording_date": row.recording_date.isoformat() if row.recording_date else None,
            "effective_date": row.effective_date.isoformat() if row.effective_date else None,
            "grantor": row.grantor,
            "grantee": row.grantee,
            "loan_amount": float(row.loan_amount) if row.loan_amount else None,
            "lender": row.doc_lender or row.debt_lender,
        }
        if row.debt_id:
            event["debt"] = {
                "debt_id": row.debt_id,
                "original_amount": float(row.original_amount) if row.original_amount else None,
                "current_balance_est": float(row.current_balance_est) if row.current_balance_est else None,
                "interest_rate": float(row.interest_rate) if row.interest_rate else None,
                "maturity_date": row.debt_maturity.isoformat() if row.debt_maturity else None,
                "lender_name": row.debt_lender,
                "loan_type": row.loan_type,
                "status": row.debt_status,
                "is_released": row.is_released,
                "release_date": row.release_date.isoformat() if row.release_date else None,
                "has_subsequent_financing": row.has_subsequent_financing,
                "data_quality": row.data_quality,
            }
        else:
            event["debt"] = None
        chronology.append(event)

    # Append orphan debt instruments as synthetic events
    for row in orphan_debts:
        chronology.append({
            "event_id": None,
            "event_type": "debt_record",
            "doc_type": None,
            "doc_category": None,
            "recording_date": None,
            "effective_date": None,
            "grantor": None,
            "grantee": None,
            "loan_amount": float(row.original_amount) if row.original_amount else None,
            "lender": row.lender_name,
            "debt": {
                "debt_id": row.debt_id,
                "original_amount": float(row.original_amount) if row.original_amount else None,
                "current_balance_est": float(row.current_balance_est) if row.current_balance_est else None,
                "interest_rate": float(row.interest_rate) if row.interest_rate else None,
                "maturity_date": row.debt_maturity.isoformat() if row.debt_maturity else None,
                "lender_name": row.lender_name,
                "loan_type": row.loan_type,
                "status": row.status,
                "is_released": row.is_released,
                "release_date": row.release_date.isoformat() if row.release_date else None,
                "has_subsequent_financing": row.has_subsequent_financing,
                "data_quality": row.data_quality,
            },
        })

    return chronology


def _classify_doc_event(doc_type: Optional[str], doc_category: Optional[str]) -> str:
    """Map doc_type/category to a human-readable event classification."""
    if doc_type == "deed_of_trust":
        return "loan_origination"
    if doc_category == "financing":
        return "financing_event"
    if doc_category == "transfer":
        return "ownership_transfer"
    if doc_type in ("judgment", "mechanic_lien"):
        return "lien_recorded"
    if doc_category == "notice":
        return "notice_recorded"
    return doc_type or "unknown"


def get_maturity_analysis(db: Session, property_id: str) -> dict:
    """
    Analyzes debt maturity for a property.

    Returns:
      - active_debts: list of current (non-released) debt instruments
      - nearest_maturity: date of the soonest maturing loan
      - months_to_maturity: int (negative = already past)
      - refinance_risk: HIGH / MEDIUM / LOW
      - has_subsequent_financing: bool (did they refi after latest debt?)
    """
    sql = text("""
        SELECT
            di.id::text,
            di.maturity_date,
            di.lender_name,
            di.original_amount,
            di.current_balance_est,
            di.interest_rate,
            di.loan_type,
            di.status,
            di.is_released,
            di.release_date,
            di.has_subsequent_financing,
            di.data_quality,
            de.recording_date,
            de.doc_type
        FROM debt_instrument di
        LEFT JOIN document_event de ON de.id = di.document_event_id
        WHERE di.property_id = :pid
        ORDER BY di.maturity_date ASC
    """)
    rows = db.execute(sql, {"pid": property_id}).fetchall()

    if not rows:
        return {
            "active_debts": [],
            "nearest_maturity": None,
            "months_to_maturity": None,
            "refinance_risk": "LOW",
            "has_subsequent_financing": False,
        }

    active_debts = []
    for row in rows:
        active_debts.append({
            "debt_id": row[0],
            "maturity_date": row.maturity_date.isoformat() if row.maturity_date else None,
            "lender_name": row.lender_name,
            "original_amount": float(row.original_amount) if row.original_amount else None,
            "current_balance_est": float(row.current_balance_est) if row.current_balance_est else None,
            "interest_rate": float(row.interest_rate) if row.interest_rate else None,
            "loan_type": row.loan_type,
            "status": row.status,
            "is_released": row.is_released,
            "has_subsequent_financing": row.has_subsequent_financing,
            "data_quality": row.data_quality,
            "recording_date": row.recording_date.isoformat() if row.recording_date else None,
        })

    # Find nearest future maturity (or nearest past if all expired)
    future_debts = [r for r in rows if r.maturity_date and r.maturity_date >= TODAY]
    past_debts = [r for r in rows if r.maturity_date and r.maturity_date < TODAY]

    nearest_maturity_date = None
    if future_debts:
        nearest_maturity_date = min(future_debts, key=lambda r: r.maturity_date).maturity_date
    elif past_debts:
        nearest_maturity_date = max(past_debts, key=lambda r: r.maturity_date).maturity_date

    months_to_maturity = None
    if nearest_maturity_date:
        delta_days = (nearest_maturity_date - TODAY).days
        months_to_maturity = round(delta_days / 30.44)

    # has_subsequent_financing: any debt marked has_subsequent_financing=True
    has_subsequent_financing = any(r.has_subsequent_financing for r in rows)

    # Refinance risk
    refinance_risk = _compute_refinance_risk(months_to_maturity, has_subsequent_financing, rows)

    return {
        "active_debts": active_debts,
        "nearest_maturity": nearest_maturity_date.isoformat() if nearest_maturity_date else None,
        "months_to_maturity": months_to_maturity,
        "refinance_risk": refinance_risk,
        "has_subsequent_financing": has_subsequent_financing,
    }


def _compute_refinance_risk(
    months_to_maturity: Optional[int],
    has_subsequent_financing: bool,
    rows,
) -> str:
    """
    HIGH:   Debt matures within 12 months and no subsequent financing recorded.
    MEDIUM: Debt already past maturity (negative months) or matures within 24 months, no refi.
    LOW:    Either there is subsequent financing or maturity is comfortably far out.
    """
    if has_subsequent_financing:
        return "LOW"
    if months_to_maturity is None:
        return "LOW"
    if months_to_maturity <= 0:
        # Already past maturity — still outstanding
        return "HIGH"
    if months_to_maturity <= 12:
        return "HIGH"
    if months_to_maturity <= 24:
        return "MEDIUM"
    return "LOW"


def detect_release_gaps(db: Session, property_id: str) -> list:
    """
    Find financing events with no corresponding release — indicates still-active debt.
    A 'gap' is a deed_of_trust or financing doc event that:
      - has a linked debt_instrument that is NOT released
      - OR has no linked debt_instrument at all (untracked debt)
    Returns list of gap descriptors.
    """
    sql = text("""
        SELECT
            de.id::text          AS event_id,
            de.doc_type,
            de.recording_date,
            de.grantee_raw       AS borrower,
            de.lender_name,
            di.id::text          AS debt_id,
            di.maturity_date,
            di.is_released,
            di.release_date,
            di.lender_name       AS debt_lender
        FROM document_event de
        LEFT JOIN debt_instrument di ON di.document_event_id = de.id
        WHERE de.property_id = :pid
          AND de.doc_category = 'financing'
        ORDER BY de.recording_date ASC
    """)
    rows = db.execute(sql, {"pid": property_id}).fetchall()

    gaps = []
    for row in rows:
        # Gap if: no debt instrument linked OR debt is not released
        is_gap = False
        reason = ""

        if row.debt_id is None:
            is_gap = True
            reason = "financing_event_no_debt_record"
        elif not row.is_released:
            is_gap = True
            past_due = row.maturity_date and row.maturity_date < TODAY
            reason = "debt_unreleased_past_maturity" if past_due else "debt_unreleased_active"

        if is_gap:
            gaps.append({
                "event_id": row.event_id,
                "doc_type": row.doc_type,
                "recording_date": row.recording_date.isoformat() if row.recording_date else None,
                "borrower": row.borrower,
                "lender": row.lender_name or row.debt_lender,
                "debt_id": row.debt_id,
                "maturity_date": row.maturity_date.isoformat() if row.maturity_date else None,
                "is_released": row.is_released,
                "release_date": row.release_date.isoformat() if row.release_date else None,
                "gap_reason": reason,
            })

    return gaps
