from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from ..services.search_service import global_search

router = APIRouter(prefix="/search", tags=["search"])


@router.get("")
def search(
    q: str = Query(..., min_length=2, description="Search term"),
    limit: int = Query(20, le=100),
    db: Session = Depends(get_db),
):
    results = global_search(db, q=q, limit=limit)
    return results
