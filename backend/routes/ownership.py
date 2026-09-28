"""
Ownership chain endpoint + resolver trigger for Collin County.
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from ..services.ownership_resolver import get_ownership_chain, run_collin_county_resolution

router = APIRouter(prefix="/properties", tags=["ownership"])


@router.get("/{property_id}/ownership-chain")
def ownership_chain(property_id: str):
    """
    Returns the full multi-source ownership chain for a property:
      - CAD direct owner (LLC name)
      - Beneficial person resolved from TX Comptroller, permits, deeds, etc.
      - All contributing evidence sources
      - Related entities controlled by the same person
      - Total portfolio stats
    """
    result = get_ownership_chain(property_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Property not found")
    return result


@router.post("/ownership-resolution/run-collin")
def trigger_collin_resolution(background_tasks: BackgroundTasks, dry_run: bool = False):
    """
    Trigger the multi-source ownership resolution for all Collin County LLC properties.
    Runs in the background; returns immediately with a confirmation message.
    """
    background_tasks.add_task(run_collin_county_resolution, dry_run=dry_run)
    return {
        "status": "started",
        "message": "Collin County ownership resolution running in background",
        "dry_run": dry_run,
    }
