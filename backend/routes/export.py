import csv
import io
from typing import Optional
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from ..database import get_db
from ..services.property_service import get_properties
from ..config import MAX_LIMIT

router = APIRouter(prefix="/export", tags=["export"])


@router.get("/properties.csv")
def export_properties_csv(
    county: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    min_value: Optional[float] = Query(None),
    max_value: Optional[float] = Query(None),
    min_score: Optional[int] = Query(None),
    zoning_code: Optional[str] = Query(None),
    land_use: Optional[str] = Query(None),
    in_opportunity_zone: Optional[bool] = Query(None),
    in_tirz: Optional[bool] = Query(None),
    tax_status: Optional[str] = Query(None),
    limit: int = Query(5000, le=MAX_LIMIT),
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
    )

    output = io.StringIO()
    fieldnames = [
        "id", "county", "property_address", "city", "state", "zip",
        "land_use_description", "zoning_code", "building_sqft", "year_built",
        "acreage", "market_value", "tax_year", "tax_status",
        "tax_delinquent_amount", "score", "latitude", "longitude",
        "in_opportunity_zone", "in_tirz", "flood_risk",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for row in rows:
        prop, market_value, tax_year, tax_stat, tax_delinquent_amount, score = row
        writer.writerow(
            {
                "id": str(prop.id),
                "county": prop.county,
                "property_address": prop.property_address,
                "city": prop.city,
                "state": prop.state,
                "zip": prop.zip,
                "land_use_description": prop.land_use_description,
                "zoning_code": prop.zoning_code,
                "building_sqft": float(prop.building_sqft) if prop.building_sqft else "",
                "year_built": prop.year_built or "",
                "acreage": float(prop.acreage) if prop.acreage else "",
                "market_value": float(market_value) if market_value else "",
                "tax_year": tax_year or "",
                "tax_status": tax_stat or "",
                "tax_delinquent_amount": float(tax_delinquent_amount) if tax_delinquent_amount else "",
                "score": int(score) if score else 0,
                "latitude": float(prop.latitude) if prop.latitude else "",
                "longitude": float(prop.longitude) if prop.longitude else "",
                "in_opportunity_zone": prop.in_opportunity_zone,
                "in_tirz": prop.in_tirz,
                "flood_risk": prop.flood_risk or "",
            }
        )

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=properties.csv"},
    )
