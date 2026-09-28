"""
Entity Resolution API Routes — Parcel Scout v3
================================================
GET /api/entities/{owner_id}/graph      — Full relationship graph for an owner
GET /api/entities/{owner_id}/portfolio  — All properties controlled by cluster
GET /api/entities/clusters              — Top portfolio clusters ranked by value
GET /api/entities/resolve/{property_id} — Who really controls this property?
POST /api/entities/run-resolution       — Trigger full resolution pipeline
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from fastapi.responses import JSONResponse

from ..services.entity_resolution import (
    get_owner_graph,
    get_owner_portfolio,
    get_top_clusters,
    resolve_property,
    run_resolution,
)

router = APIRouter(prefix="/entities", tags=["entities"])


# ---------------------------------------------------------------------------
# Note: specific paths before parameterised paths to avoid route shadowing
# ---------------------------------------------------------------------------

@router.get("/clusters")
def list_clusters(
    limit: int = Query(50, ge=1, le=500, description="Max clusters to return"),
):
    """
    Top portfolio clusters ranked by total appraised value.
    Each cluster represents a single real-world actor controlling multiple
    owner_party records (and therefore multiple properties).
    """
    try:
        clusters = get_top_clusters(limit=limit)
        return {
            "count": len(clusters),
            "clusters": [
                {
                    "id":             str(c["id"]),
                    "rank":           c["cluster_rank"],
                    "canonical_name": c["canonical_name"],
                    "party_count":    c["party_count"],
                    "property_count": c["property_count"],
                    "total_value":    float(c["total_value"]) if c["total_value"] else 0.0,
                    "avg_value":      float(c["avg_value"]) if c["avg_value"] else 0.0,
                    "methods_used":   c["methods_used"] or [],
                    "computed_at":    c["computed_at"].isoformat() if c["computed_at"] else None,
                }
                for c in clusters
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/resolve/{property_id}")
def resolve_property_owner(property_id: str):
    """
    Determine who really controls this property.
    Follows the ownership chain: direct CAD owner → beneficial person → portfolio cluster.
    Returns confidence scores and resolution evidence.
    """
    try:
        result = resolve_property(property_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.get("resolved") and not result.get("direct_owners"):
        raise HTTPException(status_code=404, detail="Property not found or has no ownership records")

    return result


@router.get("/{owner_id}/graph")
def owner_graph(owner_id: str):
    """
    Return the entity relationship graph centered on the given owner.
    Nodes = owner_party records; edges = relationships discovered by resolution rules.
    Traverses up to depth 3 using recursive CTE.
    """
    try:
        graph = get_owner_graph(owner_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not graph["nodes"]:
        raise HTTPException(status_code=404, detail=f"Owner {owner_id} not found")

    return {
        "owner_id":    graph["center"],
        "node_count":  len(graph["nodes"]),
        "edge_count":  len(graph["edges"]),
        "nodes":       graph["nodes"],
        "edges":       graph["edges"],
    }


@router.get("/{owner_id}/portfolio")
def owner_portfolio(owner_id: str):
    """
    Return all properties controlled by the portfolio cluster containing this owner.
    If the owner is not in any cluster, returns only their directly-owned properties.
    """
    try:
        portfolio = get_owner_portfolio(owner_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Serialize datetimes and decimals
    props_out = []
    for p in portfolio.get("properties", []):
        props_out.append({
            k: (float(v) if hasattr(v, "__float__") and not isinstance(v, (int, bool)) else
                v.isoformat() if hasattr(v, "isoformat") else
                str(v) if v is not None else None)
            for k, v in p.items()
        })

    cluster = portfolio.get("cluster")
    return {
        "owner_id":      owner_id,
        "cluster":       {
            "id":             str(cluster["id"]),
            "canonical_name": cluster["canonical_name"],
            "party_count":    cluster["party_count"],
            "property_count": cluster["property_count"],
            "total_value":    float(cluster["total_value"]) if cluster["total_value"] else 0.0,
        } if cluster else None,
        "property_count": portfolio.get("property_count", 0),
        "properties":    props_out,
    }


@router.post("/run-resolution")
def trigger_resolution(background_tasks: BackgroundTasks, clear: bool = True):
    """
    Trigger the full entity resolution pipeline.
    Runs synchronously (can take 2-5 min on 56K owners).
    Returns a summary of edges, clusters, and resolved chains.
    """
    try:
        summary = run_resolution(clear=clear)
        return {"status": "complete", "summary": summary}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
