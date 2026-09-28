from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from ..models.owner import OwnerParty, PropertyOwnership, EntityRecord
from ..models.property import Property
from ..models.signal import ValuationSnapshot


def get_owner_profile(db: Session, owner_id: str):
    """Return owner details plus their full property portfolio."""
    owner = db.query(OwnerParty).filter(OwnerParty.id == owner_id).first()
    if not owner:
        return None

    # Resolve entity if linked
    entity = None
    if owner.resolved_entity_id:
        entity = db.query(EntityRecord).filter(EntityRecord.id == owner.resolved_entity_id).first()

    # All properties (current + historical)
    ownerships = (
        db.query(PropertyOwnership, Property)
        .join(Property, PropertyOwnership.property_id == Property.id)
        .filter(PropertyOwnership.owner_party_id == owner_id)
        .order_by(PropertyOwnership.is_current.desc(), PropertyOwnership.effective_date.desc())
        .all()
    )

    # Latest valuations for portfolio summary
    portfolio_value: float = 0.0
    property_ids = [str(o.PropertyOwnership.property_id) for o in ownerships if o.PropertyOwnership.is_current]

    if property_ids:
        val_sql = text("""
            SELECT SUM(v.market_value)
            FROM valuation_snapshot v
            INNER JOIN (
                SELECT property_id, MAX(tax_year) AS max_year
                FROM valuation_snapshot
                WHERE property_id = ANY(:ids)
                GROUP BY property_id
            ) latest ON v.property_id = latest.property_id AND v.tax_year = latest.max_year
        """)
        result = db.execute(val_sql, {"ids": property_ids}).scalar()
        portfolio_value = float(result or 0)

    return {
        "owner": owner,
        "entity": entity,
        "ownerships": ownerships,
        "portfolio_count": sum(1 for o in ownerships if o.PropertyOwnership.is_current),
        "portfolio_value": portfolio_value,
    }


def list_owners(
    db: Session,
    party_type: Optional[str] = None,
    is_out_of_state: Optional[bool] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    q = db.query(OwnerParty)
    if party_type:
        q = q.filter(OwnerParty.party_type == party_type)
    if is_out_of_state is not None:
        q = q.filter(OwnerParty.is_out_of_state == is_out_of_state)
    if search:
        q = q.filter(OwnerParty.normalized_name.ilike(f"%{search}%"))
    return q.order_by(OwnerParty.normalized_name).limit(limit).offset(offset).all()
