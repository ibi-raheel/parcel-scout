"""
Debt Intelligence and Pre-Foreclosure Risk API Routes

Endpoints:
  GET /api/properties/{id}/debt  — loan chronology + maturity analysis
  GET /api/properties/{id}/risk  — pre-foreclosure risk assessment
  GET /api/risk/high             — all HIGH/CRITICAL risk properties
  GET /api/risk/summary          — aggregate risk stats
"""
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.orm import Session
from ..database import get_db
from ..services.debt_service import (
    get_loan_chronology,
    get_maturity_analysis,
    detect_release_gaps,
)
from ..services.risk_service import (
    compute_pre_foreclosure_risk,
    get_high_risk_properties,
    get_risk_summary,
    score_all_properties,
)
from ..config import DEFAULT_LIMIT, MAX_LIMIT

router = APIRouter(tags=["debt & risk"])


# ── Property-level debt endpoints ────────────────────────────────────────────

@router.get("/properties/{property_id}/debt")
def property_debt(property_id: str, db: Session = Depends(get_db)):
    """
    Full debt intelligence for a property:
    - Loan chronology (ordered financing event timeline)
    - Maturity analysis (active debts, nearest maturity, refinance risk)
    - Release gaps (financing events with no corresponding release)
    """
    chronology = get_loan_chronology(db, property_id)
    maturity = get_maturity_analysis(db, property_id)
    gaps = detect_release_gaps(db, property_id)

    return {
        "property_id": property_id,
        "loan_chronology": chronology,
        "maturity_analysis": maturity,
        "release_gaps": gaps,
        "summary": {
            "total_events": len(chronology),
            "unreleased_gaps": len(gaps),
            "nearest_maturity": maturity["nearest_maturity"],
            "months_to_maturity": maturity["months_to_maturity"],
            "refinance_risk": maturity["refinance_risk"],
        },
    }


@router.get("/properties/{property_id}/risk")
def property_risk(property_id: str, db: Session = Depends(get_db)):
    """
    Pre-foreclosure risk assessment for a single property.

    Returns tiered signal analysis, risk score (0-100),
    risk level (CRITICAL/HIGH/ELEVATED/MODERATE/LOW),
    and plain-English explanation.
    """
    result = compute_pre_foreclosure_risk(db, property_id)
    result["property_id"] = property_id
    return result


# ── Portfolio-level risk endpoints ───────────────────────────────────────────

@router.get("/risk/high")
def high_risk_properties(
    min_score: int = Query(60, ge=0, le=100, description="Minimum risk score (0-100)"),
    limit: int = Query(DEFAULT_LIMIT, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """
    List all HIGH and CRITICAL risk properties (score >= 60 by default),
    ordered by risk score descending.

    Query params:
      - min_score: override threshold (default 60 = HIGH)
      - limit / offset: pagination
    """
    results = get_high_risk_properties(db, min_score=min_score, limit=limit, offset=offset)
    return {
        "min_score": min_score,
        "count": len(results),
        "offset": offset,
        "properties": results,
    }


@router.get("/risk/summary")
def risk_summary(db: Session = Depends(get_db)):
    """
    Aggregate pre-foreclosure risk statistics across all scored properties.

    Returns:
      - total_scored: number of properties with a risk score
      - high_risk_count: properties scoring >= 60
      - elevated_plus_count: properties scoring >= 40
      - distribution_by_risk_level: per-level counts and averages
      - tier1_signal_count: properties with at least one Tier 1 signal
    """
    return get_risk_summary(db)


@router.post("/risk/score-all")
def trigger_batch_scoring(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Trigger batch pre-foreclosure risk scoring for all properties.
    Runs synchronously (may take several minutes for 76k properties).
    Returns summary statistics when complete.
    """
    stats = score_all_properties(db)
    return {
        "status": "completed",
        "statistics": stats,
    }
