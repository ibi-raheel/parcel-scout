from sqlalchemy.orm import Session
from sqlalchemy import text


def global_search(db: Session, q: str, limit: int = 20):
    """
    Full-text search across properties (address), owner_party (name),
    and entity_record (name).  Uses pg_trgm similarity for ranking.
    """
    if not q or len(q.strip()) < 2:
        return {"properties": [], "owners": [], "entities": []}

    term = q.strip()

    props_sql = text("""
        SELECT
            id::text,
            property_address,
            city,
            county,
            state,
            zip,
            similarity(property_address, :term) AS score
        FROM property
        WHERE property_address % :term
           OR property_address ILIKE :ilike
        ORDER BY score DESC
        LIMIT :lim
    """)

    owners_sql = text("""
        SELECT
            id::text,
            raw_name,
            normalized_name,
            party_type,
            mailing_city,
            mailing_state,
            similarity(normalized_name, :term) AS score
        FROM owner_party
        WHERE normalized_name % :term
           OR normalized_name ILIKE :ilike
           OR raw_name ILIKE :ilike
        ORDER BY score DESC
        LIMIT :lim
    """)

    entities_sql = text("""
        SELECT
            id::text,
            entity_name,
            entity_type,
            sos_status,
            portfolio_size,
            similarity(entity_name, :term) AS score
        FROM entity_record
        WHERE entity_name % :term
           OR entity_name ILIKE :ilike
        ORDER BY score DESC
        LIMIT :lim
    """)

    params = {"term": term, "ilike": f"%{term}%", "lim": limit}

    properties = [dict(r._mapping) for r in db.execute(props_sql, params).fetchall()]
    owners = [dict(r._mapping) for r in db.execute(owners_sql, params).fetchall()]
    entities = [dict(r._mapping) for r in db.execute(entities_sql, params).fetchall()]

    return {
        "query": term,
        "properties": properties,
        "owners": owners,
        "entities": entities,
    }
