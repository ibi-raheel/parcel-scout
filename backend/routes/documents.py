from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from ..database import get_db
from ..models.document import DocumentEvent, DebtInstrument

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get("")
def list_documents(
    property_id: Optional[str] = Query(None),
    doc_type: Optional[str] = Query(None),
    doc_category: Optional[str] = Query(None),
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(DocumentEvent)
    if property_id:
        q = q.filter(DocumentEvent.property_id == property_id)
    if doc_type:
        q = q.filter(DocumentEvent.doc_type == doc_type)
    if doc_category:
        q = q.filter(DocumentEvent.doc_category == doc_category)

    docs = q.order_by(DocumentEvent.recording_date.desc()).limit(limit).offset(offset).all()

    return {
        "count": len(docs),
        "documents": [
            {
                "id": str(d.id),
                "property_id": str(d.property_id) if d.property_id else None,
                "instrument_number": d.instrument_number,
                "doc_type": d.doc_type,
                "doc_category": d.doc_category,
                "recording_date": d.recording_date.isoformat() if d.recording_date else None,
                "effective_date": d.effective_date.isoformat() if d.effective_date else None,
                "grantor_raw": d.grantor_raw,
                "grantee_raw": d.grantee_raw,
                "consideration": float(d.consideration) if d.consideration else None,
                "loan_amount": float(d.loan_amount) if d.loan_amount else None,
                "interest_rate": float(d.interest_rate) if d.interest_rate else None,
                "maturity_date": d.maturity_date.isoformat() if d.maturity_date else None,
                "lender_name": d.lender_name,
                "source": d.source,
            }
            for d in docs
        ],
    }


@router.get("/debts")
def list_debts(
    property_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(DebtInstrument)
    if property_id:
        q = q.filter(DebtInstrument.property_id == property_id)
    if status:
        q = q.filter(DebtInstrument.status == status)

    debts = q.order_by(DebtInstrument.created_at.desc()).limit(limit).offset(offset).all()

    return {
        "count": len(debts),
        "debts": [
            {
                "id": str(d.id),
                "property_id": str(d.property_id),
                "original_amount": float(d.original_amount) if d.original_amount else None,
                "current_balance_est": float(d.current_balance_est) if d.current_balance_est else None,
                "interest_rate": float(d.interest_rate) if d.interest_rate else None,
                "loan_term_months": int(d.loan_term_months) if d.loan_term_months else None,
                "maturity_date": d.maturity_date.isoformat() if d.maturity_date else None,
                "lender_name": d.lender_name,
                "servicer_name": d.servicer_name,
                "loan_type": d.loan_type,
                "status": d.status,
                "is_released": d.is_released,
                "release_date": d.release_date.isoformat() if d.release_date else None,
                "data_quality": d.data_quality,
            }
            for d in debts
        ],
    }
