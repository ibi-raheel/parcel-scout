from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text
from ..database import get_db

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("")
def dashboard_stats(db: Session = Depends(get_db)):
    """Aggregate dashboard stats — counts, valuation totals, signal breakdown."""
    sql = text("""
        WITH latest_val AS (
            SELECT
                v.property_id,
                v.market_value,
                v.tax_status,
                v.tax_delinquent_amount
            FROM valuation_snapshot v
            INNER JOIN (
                SELECT property_id, MAX(tax_year) AS max_year
                FROM valuation_snapshot
                GROUP BY property_id
            ) lv ON v.property_id = lv.property_id AND v.tax_year = lv.max_year
        ),
        signal_totals AS (
            SELECT
                signal_key,
                COUNT(*) AS cnt,
                SUM(score_delta) AS total_delta
            FROM signal_result
            WHERE status = 'active'
            GROUP BY signal_key
            ORDER BY cnt DESC
            LIMIT 10
        )
        SELECT
            (SELECT COUNT(*) FROM property) AS total_properties,
            (SELECT COUNT(DISTINCT county) FROM property) AS counties,
            (SELECT COALESCE(SUM(market_value), 0) FROM latest_val) AS total_market_value,
            (SELECT COUNT(*) FROM latest_val WHERE tax_status = 'delinquent') AS delinquent_properties,
            (SELECT COALESCE(SUM(tax_delinquent_amount), 0) FROM latest_val WHERE tax_status = 'delinquent') AS total_delinquent_amount,
            (SELECT COUNT(*) FROM property WHERE in_opportunity_zone = TRUE) AS opportunity_zone_count,
            (SELECT COUNT(*) FROM property WHERE in_tirz = TRUE) AS tirz_count,
            (SELECT COUNT(*) FROM filing_node WHERE status = 'active') AS active_filings,
            (SELECT COUNT(*) FROM filing_node WHERE filing_type = 'foreclosure' AND status = 'active') AS active_foreclosures,
            (SELECT COUNT(*) FROM owner_party) AS total_owners,
            (SELECT COUNT(*) FROM owner_party WHERE is_out_of_state = TRUE) AS out_of_state_owners,
            (SELECT COUNT(*) FROM signal_result WHERE status = 'active') AS active_signals,
            (SELECT COALESCE(AVG(score_delta), 0) FROM signal_result WHERE status = 'active') AS avg_signal_score
    """)

    row = db.execute(sql).fetchone()
    if not row:
        return {}

    # County breakdown
    county_sql = text("""
        SELECT
            p.county,
            COUNT(*) AS property_count,
            COALESCE(SUM(lv.market_value), 0) AS total_value
        FROM property p
        LEFT JOIN (
            SELECT v.property_id, v.market_value
            FROM valuation_snapshot v
            INNER JOIN (
                SELECT property_id, MAX(tax_year) AS max_year
                FROM valuation_snapshot GROUP BY property_id
            ) lv ON v.property_id = lv.property_id AND v.tax_year = lv.max_year
        ) lv ON p.id = lv.property_id
        GROUP BY p.county
        ORDER BY property_count DESC
    """)
    county_rows = db.execute(county_sql).fetchall()

    # Top signals
    signals_sql = text("""
        SELECT signal_key, COUNT(*) AS cnt
        FROM signal_result
        WHERE status = 'active'
        GROUP BY signal_key
        ORDER BY cnt DESC
        LIMIT 10
    """)
    signal_rows = db.execute(signals_sql).fetchall()

    return {
        "total_properties": row.total_properties,
        "counties": row.counties,
        "total_market_value": float(row.total_market_value),
        "delinquent_properties": row.delinquent_properties,
        "total_delinquent_amount": float(row.total_delinquent_amount),
        "opportunity_zone_count": row.opportunity_zone_count,
        "tirz_count": row.tirz_count,
        "active_filings": row.active_filings,
        "active_foreclosures": row.active_foreclosures,
        "total_owners": row.total_owners,
        "out_of_state_owners": row.out_of_state_owners,
        "active_signals": row.active_signals,
        "avg_signal_score": float(row.avg_signal_score),
        "by_county": [
            {
                "county": r.county,
                "property_count": r.property_count,
                "total_value": float(r.total_value),
            }
            for r in county_rows
        ],
        "top_signals": [
            {"signal_key": r.signal_key, "count": r.cnt}
            for r in signal_rows
        ],
    }
