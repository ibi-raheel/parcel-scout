from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text, func, select
from ..models.property import Property
from ..models.owner import PropertyOwnership, OwnerParty
from ..models.signal import ValuationSnapshot, SignalResult
from ..models.document import DocumentEvent, DebtInstrument
from ..models.filing import FilingNode


def get_properties(
    db: Session,
    county: Optional[str] = None,
    city: Optional[str] = None,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
    min_score: Optional[int] = None,
    zoning_code: Optional[str] = None,
    land_use: Optional[str] = None,
    in_opportunity_zone: Optional[bool] = None,
    in_tirz: Optional[bool] = None,
    tax_status: Optional[str] = None,
    limit: int = 5000,
    offset: int = 0,
):
    """Return properties with optional filters. Values come from the latest valuation snapshot."""
    # Build a CTE for the latest market value per property
    latest_val = (
        db.query(
            ValuationSnapshot.property_id,
            func.max(ValuationSnapshot.tax_year).label("max_year"),
        )
        .group_by(ValuationSnapshot.property_id)
        .subquery("latest_val")
    )

    val_join = (
        db.query(ValuationSnapshot)
        .join(
            latest_val,
            (ValuationSnapshot.property_id == latest_val.c.property_id)
            & (ValuationSnapshot.tax_year == latest_val.c.max_year),
        )
        .subquery("val_join")
    )

    # Score subquery: sum of score_delta for active signals
    score_sq = (
        db.query(
            SignalResult.property_id,
            func.coalesce(func.sum(SignalResult.score_delta), 0).label("total_score"),
        )
        .filter(SignalResult.status == "active")
        .group_by(SignalResult.property_id)
        .subquery("score_sq")
    )

    q = (
        db.query(
            Property,
            val_join.c.market_value,
            val_join.c.tax_year,
            val_join.c.tax_status,
            val_join.c.tax_delinquent_amount,
            func.coalesce(score_sq.c.total_score, 0).label("score"),
        )
        .outerjoin(val_join, Property.id == val_join.c.property_id)
        .outerjoin(score_sq, Property.id == score_sq.c.property_id)
    )

    if county:
        q = q.filter(func.lower(Property.county) == county.lower())
    if city:
        q = q.filter(func.lower(Property.city).like(f"%{city.lower()}%"))
    if min_value is not None:
        q = q.filter(val_join.c.market_value >= min_value)
    if max_value is not None:
        q = q.filter(val_join.c.market_value <= max_value)
    if min_score is not None:
        q = q.filter(score_sq.c.total_score >= min_score)
    if zoning_code:
        q = q.filter(func.lower(Property.zoning_code).like(f"%{zoning_code.lower()}%"))
    if land_use:
        q = q.filter(func.lower(Property.land_use_description).like(f"%{land_use.lower()}%"))
    if in_opportunity_zone is not None:
        q = q.filter(Property.in_opportunity_zone == in_opportunity_zone)
    if in_tirz is not None:
        q = q.filter(Property.in_tirz == in_tirz)
    if tax_status:
        q = q.filter(val_join.c.tax_status == tax_status)

    q = q.limit(limit).offset(offset)
    return q.all()


def get_property_detail(db: Session, property_id: str):
    """Return a property and all its related records."""
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop:
        return None

    # Current owner
    current_ownership = (
        db.query(PropertyOwnership, OwnerParty)
        .join(OwnerParty, PropertyOwnership.owner_party_id == OwnerParty.id)
        .filter(
            PropertyOwnership.property_id == property_id,
            PropertyOwnership.is_current == True,
        )
        .first()
    )

    # Valuation history
    valuations = (
        db.query(ValuationSnapshot)
        .filter(ValuationSnapshot.property_id == property_id)
        .order_by(ValuationSnapshot.tax_year.desc())
        .all()
    )

    # Documents
    documents = (
        db.query(DocumentEvent)
        .filter(DocumentEvent.property_id == property_id)
        .order_by(DocumentEvent.recording_date.desc())
        .all()
    )

    # Debt instruments
    debts = (
        db.query(DebtInstrument)
        .filter(DebtInstrument.property_id == property_id)
        .order_by(DebtInstrument.created_at.desc())
        .all()
    )

    # Filings
    filings = (
        db.query(FilingNode)
        .filter(FilingNode.property_id == property_id)
        .order_by(FilingNode.filing_date.desc())
        .all()
    )

    # Active signals / score
    signals = (
        db.query(SignalResult)
        .filter(
            SignalResult.property_id == property_id,
            SignalResult.status == "active",
        )
        .all()
    )

    # Event counts from raw SQL for speed
    counts_sql = text("""
        SELECT
            (SELECT COUNT(*) FROM permit_event WHERE property_id = :pid)   AS permits,
            (SELECT COUNT(*) FROM code_event   WHERE property_id = :pid)   AS violations,
            (SELECT COUNT(*) FROM crime_event  WHERE property_id = :pid)   AS crimes,
            (SELECT COUNT(*) FROM service_request WHERE property_id = :pid) AS service_requests
    """)
    counts = db.execute(counts_sql, {"pid": property_id}).fetchone()

    return {
        "property": prop,
        "current_ownership": current_ownership,
        "valuations": valuations,
        "documents": documents,
        "debts": debts,
        "filings": filings,
        "signals": signals,
        "event_counts": {
            "permits": counts.permits if counts else 0,
            "violations": counts.violations if counts else 0,
            "crimes": counts.crimes if counts else 0,
            "service_requests": counts.service_requests if counts else 0,
        },
    }
