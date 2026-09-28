from typing import Optional, List, Any, Dict
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from ..database import get_db
from ..services.property_service import get_properties, get_property_detail
from ..config import DEFAULT_LIMIT, MAX_LIMIT

router = APIRouter(prefix="/properties", tags=["properties"])


def _prop_to_summary(row) -> Dict[str, Any]:
    """Convert a property query row to a GeoJSON Feature."""
    prop, market_value, tax_year, tax_status, tax_delinquent_amount, score = row
    p = prop

    return {
        "type": "Feature",
        "id": str(p.id),
        "geometry": (
            {
                "type": "Point",
                "coordinates": [float(p.longitude), float(p.latitude)],
            }
            if p.latitude and p.longitude
            else None
        ),
        "properties": {
            "id": str(p.id),
            "county": p.county,
            "property_address": p.property_address,
            "city": p.city,
            "state": p.state,
            "zip": p.zip,
            "land_use_description": p.land_use_description,
            "zoning_code": p.zoning_code,
            "building_sqft": float(p.building_sqft) if p.building_sqft else None,
            "year_built": p.year_built,
            "acreage": float(p.acreage) if p.acreage else None,
            "market_value": float(market_value) if market_value else None,
            "tax_year": tax_year,
            "tax_status": tax_status,
            "tax_delinquent_amount": float(tax_delinquent_amount) if tax_delinquent_amount else None,
            "score": int(score) if score else 0,
            "in_opportunity_zone": p.in_opportunity_zone,
            "in_tirz": p.in_tirz,
            "flood_risk": p.flood_risk,
        },
    }


def _prop_detail_to_dict(detail: Dict) -> Dict[str, Any]:
    p = detail["property"]
    ownership_row = detail["current_ownership"]

    owner_data = None
    if ownership_row:
        own, op = ownership_row
        owner_data = {
            "id": str(op.id),
            "raw_name": op.raw_name,
            "normalized_name": op.normalized_name,
            "party_type": op.party_type,
            "mailing_address": op.mailing_address,
            "mailing_city": op.mailing_city,
            "mailing_state": op.mailing_state,
            "mailing_zip": op.mailing_zip,
            "is_out_of_state": op.is_out_of_state,
            "ownership_pct": float(own.ownership_pct) if own.ownership_pct else None,
            "effective_date": own.effective_date.isoformat() if own.effective_date else None,
        }

    valuations = [
        {
            "tax_year": v.tax_year,
            "market_value": float(v.market_value) if v.market_value else None,
            "land_value": float(v.land_value) if v.land_value else None,
            "improvement_value": float(v.improvement_value) if v.improvement_value else None,
            "assessed_value": float(v.assessed_value) if v.assessed_value else None,
            "tax_status": v.tax_status,
            "tax_delinquent_amount": float(v.tax_delinquent_amount) if v.tax_delinquent_amount else None,
        }
        for v in detail["valuations"]
    ]

    documents = [
        {
            "id": str(d.id),
            "doc_type": d.doc_type,
            "doc_category": d.doc_category,
            "recording_date": d.recording_date.isoformat() if d.recording_date else None,
            "grantor_raw": d.grantor_raw,
            "grantee_raw": d.grantee_raw,
            "consideration": float(d.consideration) if d.consideration else None,
            "loan_amount": float(d.loan_amount) if d.loan_amount else None,
            "lender_name": d.lender_name,
            "instrument_number": d.instrument_number,
        }
        for d in detail["documents"]
    ]

    debts = [
        {
            "id": str(d.id),
            "original_amount": float(d.original_amount) if d.original_amount else None,
            "current_balance_est": float(d.current_balance_est) if d.current_balance_est else None,
            "interest_rate": float(d.interest_rate) if d.interest_rate else None,
            "maturity_date": d.maturity_date.isoformat() if d.maturity_date else None,
            "lender_name": d.lender_name,
            "loan_type": d.loan_type,
            "status": d.status,
        }
        for d in detail["debts"]
    ]

    filings = [
        {
            "id": str(f.id),
            "filing_type": f.filing_type,
            "case_number": f.case_number,
            "filing_date": f.filing_date.isoformat() if f.filing_date else None,
            "plaintiff": f.plaintiff,
            "defendant": f.defendant,
            "amount": float(f.amount) if f.amount else None,
            "status": f.status,
            "description": f.description,
            "sale_date": f.sale_date.isoformat() if f.sale_date else None,
        }
        for f in detail["filings"]
    ]

    signals = [
        {
            "signal_key": s.signal_key,
            "score_delta": s.score_delta,
            "confidence": float(s.confidence) if s.confidence else None,
            "explanation": s.explanation,
            "computed_at": s.computed_at.isoformat() if s.computed_at else None,
        }
        for s in detail["signals"]
    ]

    total_score = sum(s["score_delta"] for s in signals)

    return {
        "id": str(p.id),
        "county": p.county,
        "property_address": p.property_address,
        "city": p.city,
        "state": p.state,
        "zip": p.zip,
        "legal_description": p.legal_description,
        "acreage": float(p.acreage) if p.acreage else None,
        "lot_sqft": float(p.lot_sqft) if p.lot_sqft else None,
        "building_sqft": float(p.building_sqft) if p.building_sqft else None,
        "year_built": p.year_built,
        "num_stories": p.num_stories,
        "building_class": p.building_class,
        "condition": p.condition,
        "state_land_use_code": p.state_land_use_code,
        "land_use_description": p.land_use_description,
        "zoning_code": p.zoning_code,
        "zoning_description": p.zoning_description,
        "latitude": float(p.latitude) if p.latitude else None,
        "longitude": float(p.longitude) if p.longitude else None,
        "has_frontage": p.has_frontage,
        "nearest_highway": p.nearest_highway,
        "highway_aadt": p.highway_aadt,
        "distance_to_highway_ft": float(p.distance_to_highway_ft) if p.distance_to_highway_ft else None,
        "flood_zone": p.flood_zone,
        "flood_risk": p.flood_risk,
        "in_floodplain": p.in_floodplain,
        "in_opportunity_zone": p.in_opportunity_zone,
        "in_tirz": p.in_tirz,
        "tirz_name": p.tirz_name,
        "census_tract": p.census_tract,
        "nearest_transit_stop": p.nearest_transit_stop,
        "distance_to_transit_ft": float(p.distance_to_transit_ft) if p.distance_to_transit_ft else None,
        "score": total_score,
        "current_owner": owner_data,
        "valuations": valuations,
        "documents": documents,
        "debts": debts,
        "filings": filings,
        "signals": signals,
        "event_counts": detail["event_counts"],
    }


@router.get("")
def list_properties(
    county: Optional[str] = Query(None, description="Filter by county name"),
    city: Optional[str] = Query(None),
    min_value: Optional[float] = Query(None),
    max_value: Optional[float] = Query(None),
    min_score: Optional[int] = Query(None),
    zoning_code: Optional[str] = Query(None),
    land_use: Optional[str] = Query(None),
    in_opportunity_zone: Optional[bool] = Query(None),
    in_tirz: Optional[bool] = Query(None),
    tax_status: Optional[str] = Query(None),
    limit: int = Query(DEFAULT_LIMIT, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    rows = get_properties(
        db,
        county=county,
        city=city,
        min_value=min_value,
        max_value=max_value,
        min_score=min_score,
        zoning_code=zoning_code,
        land_use=land_use,
        in_opportunity_zone=in_opportunity_zone,
        in_tirz=in_tirz,
        tax_status=tax_status,
        limit=limit,
        offset=offset,
    )

    features = [_prop_to_summary(row) for row in rows]
    return {
        "type": "FeatureCollection",
        "count": len(features),
        "features": features,
    }


@router.get("/{property_id}")
def property_detail(property_id: str, db: Session = Depends(get_db)):
    detail = get_property_detail(db, property_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Property not found")
    return _prop_detail_to_dict(detail)
