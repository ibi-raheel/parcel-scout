from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from ..database import get_db
from ..services.owner_service import get_owner_profile, list_owners

router = APIRouter(prefix="/owners", tags=["owners"])


@router.get("")
def get_owners(
    party_type: Optional[str] = Query(None, description="individual, llc, corporation, trust, government, unknown"),
    is_out_of_state: Optional[bool] = Query(None),
    search: Optional[str] = Query(None, description="Fuzzy name search"),
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    owners = list_owners(
        db,
        party_type=party_type,
        is_out_of_state=is_out_of_state,
        search=search,
        limit=limit,
        offset=offset,
    )
    return {
        "count": len(owners),
        "owners": [
            {
                "id": str(o.id),
                "raw_name": o.raw_name,
                "normalized_name": o.normalized_name,
                "party_type": o.party_type,
                "mailing_address": o.mailing_address,
                "mailing_city": o.mailing_city,
                "mailing_state": o.mailing_state,
                "mailing_zip": o.mailing_zip,
                "is_out_of_state": o.is_out_of_state,
            }
            for o in owners
        ],
    }


@router.get("/{owner_id}")
def owner_profile(owner_id: str, db: Session = Depends(get_db)):
    profile = get_owner_profile(db, owner_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Owner not found")

    owner = profile["owner"]
    entity = profile["entity"]

    ownerships_out = []
    for row in profile["ownerships"]:
        own, prop = row
        ownerships_out.append(
            {
                "property_id": str(prop.id),
                "property_address": prop.property_address,
                "city": prop.city,
                "county": prop.county,
                "is_current": own.is_current,
                "ownership_pct": float(own.ownership_pct) if own.ownership_pct else None,
                "effective_date": own.effective_date.isoformat() if own.effective_date else None,
                "end_date": own.end_date.isoformat() if own.end_date else None,
                "source": own.source,
            }
        )

    return {
        "id": str(owner.id),
        "raw_name": owner.raw_name,
        "normalized_name": owner.normalized_name,
        "party_type": owner.party_type,
        "mailing_address": owner.mailing_address,
        "mailing_city": owner.mailing_city,
        "mailing_state": owner.mailing_state,
        "mailing_zip": owner.mailing_zip,
        "mailing_type": owner.mailing_type,
        "is_out_of_state": owner.is_out_of_state,
        "resolution_confidence": float(owner.resolution_confidence) if owner.resolution_confidence else None,
        "resolution_method": owner.resolution_method,
        "entity": (
            {
                "id": str(entity.id),
                "entity_name": entity.entity_name,
                "entity_type": entity.entity_type,
                "sos_file_number": entity.sos_file_number,
                "formation_date": entity.formation_date.isoformat() if entity.formation_date else None,
                "sos_status": entity.sos_status,
                "registered_agent": entity.registered_agent,
                "portfolio_size": entity.portfolio_size,
                "portfolio_value": float(entity.portfolio_value) if entity.portfolio_value else None,
            }
            if entity
            else None
        ),
        "portfolio_count": profile["portfolio_count"],
        "portfolio_value": profile["portfolio_value"],
        "ownerships": ownerships_out,
    }
