from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from ..database import get_db
from ..models.filing import FilingNode

router = APIRouter(prefix="/filings", tags=["filings"])


@router.get("")
def list_filings(
    property_id: Optional[str] = Query(None),
    filing_type: Optional[str] = Query(None, description="ucc, lis_pendens, mechanic_lien, tax_lien, judgment, foreclosure, bankruptcy"),
    status: Optional[str] = Query(None, description="active, terminated, dismissed, satisfied, sold"),
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(FilingNode)
    if property_id:
        q = q.filter(FilingNode.property_id == property_id)
    if filing_type:
        q = q.filter(FilingNode.filing_type == filing_type)
    if status:
        q = q.filter(FilingNode.status == status)

    filings = q.order_by(FilingNode.filing_date.desc()).limit(limit).offset(offset).all()

    return {
        "count": len(filings),
        "filings": [
            {
                "id": str(f.id),
                "property_id": str(f.property_id) if f.property_id else None,
                "filing_type": f.filing_type,
                "case_number": f.case_number,
                "filing_date": f.filing_date.isoformat() if f.filing_date else None,
                "plaintiff": f.plaintiff,
                "defendant": f.defendant,
                "secured_party": f.secured_party,
                "debtor": f.debtor,
                "amount": float(f.amount) if f.amount else None,
                "status": f.status,
                "description": f.description,
                "sale_date": f.sale_date.isoformat() if f.sale_date else None,
                "source": f.source,
            }
            for f in filings
        ],
    }
