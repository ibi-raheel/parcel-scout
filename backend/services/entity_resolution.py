"""
Entity Resolution Engine — Parcel Scout v3
==========================================
Implements TAD resolution rules to cluster owner_party records into
real-world portfolio actors using PostgreSQL (recursive CTEs + adjacency
tables; no Neo4j required).

Rules
-----
1. Exact owner match      — same normalized_name across properties           (0.95)
2. Mailing address link   — different owner names at same mailing address    (0.70)
3. Deed grantee/grantor   — LLC grantee matched to person grantor via deeds  (0.85)
4. Shared registered agent — LLCs sharing a registered agent                 (0.60)
5. Fuzzy name match       — Levenshtein distance < 3 on normalized names     (0.50)

Each rule inserts edges into owner_relationship.
Union-Find over those edges produces portfolio_cluster rows.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from typing import Any

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

DB_URL = "postgresql://parcelscout:parcelscout_dev@localhost:5432/parcelscout"

# ---------------------------------------------------------------------------
# Confidence constants
# ---------------------------------------------------------------------------
CONF_EXACT_MATCH    = 0.95
CONF_DEED_CHAIN     = 0.85
CONF_MAILING_LINK   = 0.70
CONF_SHARED_AGENT   = 0.60
CONF_FUZZY_NAME     = 0.50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn():
    return psycopg2.connect(DB_URL)


def _elapsed(t0: float) -> str:
    return f"{time.time() - t0:.1f}s"


# ---------------------------------------------------------------------------
# Union-Find (disjoint sets)
# ---------------------------------------------------------------------------

class UnionFind:
    def __init__(self, nodes: list[str]):
        self.parent: dict[str, str] = {n: n for n in nodes}
        self.rank:   dict[str, int] = {n: 0  for n in nodes}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # path compression
            x = self.parent[x]
        return x

    def union(self, x: str, y: str) -> bool:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return False
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1
        return True

    def clusters(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for n in self.parent:
            groups[self.find(n)].append(n)
        return dict(groups)


# ---------------------------------------------------------------------------
# Step 1 — Clear old resolution data
# ---------------------------------------------------------------------------

def clear_previous_resolution(cur) -> None:
    log.info("Clearing previous resolution data …")
    cur.execute("DELETE FROM ownership_resolution")
    cur.execute("DELETE FROM portfolio_cluster")
    cur.execute("DELETE FROM owner_relationship")
    cur.execute("""
        UPDATE owner_party
        SET resolved_entity_id = NULL,
            resolved_person_id = NULL,
            resolution_confidence = NULL,
            resolution_method = NULL
    """)


# ---------------------------------------------------------------------------
# Rule helpers — each inserts into owner_relationship
# ---------------------------------------------------------------------------

def _bulk_insert_edges(cur, rows: list[tuple]) -> int:
    """Insert (from_id, to_id, rel_type, confidence, evidence_json) rows."""
    if not rows:
        return 0
    # Ensure UUIDs are plain strings so psycopg2 doesn't format them as {…}
    clean = [(str(r[0]), str(r[1]), r[2], r[3], r[4]) for r in rows]
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO owner_relationship
            (from_party_id, to_party_id, relationship_type, confidence, evidence)
        VALUES %s
        ON CONFLICT (from_party_id, to_party_id, relationship_type) DO UPDATE
            SET confidence = GREATEST(owner_relationship.confidence, EXCLUDED.confidence),
                evidence   = EXCLUDED.evidence
        """,
        clean,
        template="(%s::uuid, %s::uuid, %s, %s, %s::jsonb)",
    )
    return len(clean)


# ---------------------------------------------------------------------------
# Rule 1 — Exact owner match
# ---------------------------------------------------------------------------

def rule_exact_match(cur) -> int:
    """
    Owners with identical normalized_name are the same real entity.
    Only worth running for names that appear > 1 time.
    Creates bidirectional edge between every pair sharing the same name.
    Capped at pairs from groups with ≤ 50 members to avoid combinatorial explosion.
    """
    t0 = time.time()
    log.info("Rule 1: exact name match …")

    cur.execute("""
        WITH dupes AS (
            SELECT normalized_name, array_agg(id::text ORDER BY id::text) AS ids
            FROM owner_party
            WHERE normalized_name IS NOT NULL AND normalized_name != ''
            GROUP BY normalized_name
            HAVING COUNT(*) > 1 AND COUNT(*) <= 50
        )
        SELECT ids FROM dupes
    """)
    rows = cur.fetchall()

    edges = []
    for (ids,) in rows:
        # Connect first node to all others (star topology — Union-Find handles transitivity)
        anchor = ids[0]
        for other in ids[1:]:
            ev = json.dumps({"rule": "exact_name", "shared_name": True})
            edges.append((anchor, other, "exact_name", CONF_EXACT_MATCH, ev))

    inserted = _bulk_insert_edges(cur, edges)
    log.info(f"  Rule 1: {inserted} edges in {_elapsed(t0)}")
    return inserted


# ---------------------------------------------------------------------------
# Rule 2 — Mailing address linkage
# ---------------------------------------------------------------------------

def rule_mailing_address(cur) -> int:
    """
    Different owner names at the same mailing address → likely same portfolio.
    Excludes: PO Box 999999 / generic agent addresses by requiring > 1 distinct name.
    Groups capped at 100 to avoid "CT Corporation" false positives.
    """
    t0 = time.time()
    log.info("Rule 2: shared mailing address …")

    cur.execute("""
        WITH addr_groups AS (
            SELECT
                mailing_address,
                mailing_city,
                mailing_zip,
                array_agg(DISTINCT id::text ORDER BY id::text) AS ids,
                COUNT(DISTINCT normalized_name) AS distinct_names
            FROM owner_party
            WHERE mailing_address IS NOT NULL
              AND mailing_address != ''
              AND mailing_zip IS NOT NULL
            GROUP BY mailing_address, mailing_city, mailing_zip
            HAVING COUNT(DISTINCT normalized_name) BETWEEN 2 AND 100
        )
        SELECT ids, mailing_address, mailing_city, mailing_zip
        FROM addr_groups
    """)
    rows = cur.fetchall()

    edges = []
    for ids, addr, city, zipcode in rows:
        anchor = ids[0]
        ev = json.dumps({"rule": "mailing_address", "address": addr, "city": city, "zip": zipcode})
        for other in ids[1:]:
            edges.append((anchor, other, "shared_mailing_address", CONF_MAILING_LINK, ev))

    inserted = _bulk_insert_edges(cur, edges)
    log.info(f"  Rule 2: {inserted} edges in {_elapsed(t0)}")
    return inserted


# ---------------------------------------------------------------------------
# Rule 3 — Deed grantee → grantor chain (LLC → Person)
# ---------------------------------------------------------------------------

def rule_deed_chain(cur) -> int:
    """
    For LLC-owned properties with warranty/special-warranty deed records:
    - The LLC is the grantee (current owner)
    - The grantor is likely a person (prior owner / beneficial owner)
    We match grantee_raw → owner_party.normalized_name to find the LLC party,
    then create an edge to a person_node (creating one if needed) or to the
    grantor if they also appear as an owner_party.
    """
    t0 = time.time()
    log.info("Rule 3: deed grantee→grantor chain …")

    # Find LLCs that appear as grantee in deed transfers AND whose grantor
    # is a PERSON name (no 'LLC', 'INC', 'CORP' in grantor_raw)
    cur.execute("""
        SELECT DISTINCT
            de.grantee_raw,
            de.grantor_raw,
            op_grantee.id::text AS grantee_party_id,
            op_grantor.id::text AS grantor_party_id
        FROM document_event de
        -- Match grantee to owner_party
        JOIN owner_party op_grantee
          ON op_grantee.normalized_name = UPPER(TRIM(de.grantee_raw))
         AND op_grantee.party_type = 'llc'
        -- Try to match grantor to an owner_party too
        LEFT JOIN owner_party op_grantor
          ON op_grantor.normalized_name = UPPER(TRIM(de.grantor_raw))
         AND op_grantor.party_type = 'individual'
        WHERE de.doc_category = 'transfer'
          AND de.grantee_raw IS NOT NULL
          AND de.grantor_raw IS NOT NULL
          AND de.grantor_raw NOT ILIKE '%LLC%'
          AND de.grantor_raw NOT ILIKE '%INC%'
          AND de.grantor_raw NOT ILIKE '%CORP%'
          AND de.grantor_raw NOT ILIKE '%LP%'
          AND de.grantor_raw NOT ILIKE '%LTD%'
          AND de.grantor_raw NOT ILIKE '%TRUST%'
          AND de.grantor_raw NOT ILIKE '%BANK%'
          AND de.grantor_raw NOT ILIKE '%ASSOCIATION%'
          AND de.grantee_raw != de.grantor_raw
        LIMIT 50000
    """)
    rows = cur.fetchall()

    edges = []
    person_cache: dict[str, str] = {}  # normalized_name → person_node.id

    for grantee_raw, grantor_raw, grantee_id, grantor_id in rows:
        if grantee_id is None:
            continue

        ev = json.dumps({
            "rule": "deed_chain",
            "grantee": grantee_raw,
            "grantor": grantor_raw,
        })

        if grantor_id:
            # Both are in owner_party — direct edge
            edges.append((grantor_id, grantee_id, "deed_grantor_grantee", CONF_DEED_CHAIN, ev))
        else:
            # Create / find person_node for the grantor
            norm = grantor_raw.strip().upper() if grantor_raw else None
            if not norm:
                continue

            if norm not in person_cache:
                cur.execute("""
                    INSERT INTO person_node (full_name, normalized_name, roles)
                    VALUES (%s, %s, '[]'::jsonb)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                """, (grantor_raw.strip(), norm))
                result = cur.fetchone()
                if result:
                    person_cache[norm] = str(result[0])
                else:
                    cur.execute("SELECT id FROM person_node WHERE normalized_name = %s", (norm,))
                    r = cur.fetchone()
                    if r:
                        person_cache[norm] = str(r[0])

            # Link the LLC owner_party to person_node via resolved_person_id
            if norm in person_cache:
                cur.execute("""
                    UPDATE owner_party
                    SET resolved_person_id = %s,
                        resolution_confidence = %s,
                        resolution_method = 'deed_grantor'
                    WHERE id = %s
                      AND (resolved_person_id IS NULL OR resolution_confidence < %s)
                """, (person_cache[norm], CONF_DEED_CHAIN, grantee_id, CONF_DEED_CHAIN))

    inserted = _bulk_insert_edges(cur, edges)
    log.info(f"  Rule 3: {len(person_cache)} person nodes, {inserted} direct edges in {_elapsed(t0)}")
    return inserted


# ---------------------------------------------------------------------------
# Rule 4 — Shared registered agent
# ---------------------------------------------------------------------------

def rule_shared_registered_agent(cur) -> int:
    """
    entity_record rows sharing the same registered_agent string → related.
    Maps back to owner_party via resolved_entity_id where available,
    or via normalized_name match.
    """
    t0 = time.time()
    log.info("Rule 4: shared registered agent …")

    # First, populate entity_record from owner_party for LLCs that don't have one
    # then find shared agents
    cur.execute("""
        WITH agent_groups AS (
            SELECT
                registered_agent,
                array_agg(id::text ORDER BY id::text) AS entity_ids
            FROM entity_record
            WHERE registered_agent IS NOT NULL
              AND registered_agent != ''
              AND registered_agent NOT ILIKE '%CT CORP%'
              AND registered_agent NOT ILIKE '%NATIONAL REGISTERED%'
              AND registered_agent NOT ILIKE '%COGENCY%'
              AND registered_agent NOT ILIKE '%INCORP SERVICES%'
              AND registered_agent NOT ILIKE '%REGISTERED AGENTS INC%'
            GROUP BY registered_agent
            HAVING COUNT(*) BETWEEN 2 AND 50
        ),
        -- Map entity_record back to owner_party
        entity_party_map AS (
            SELECT er.id::text AS entity_id, op.id::text AS party_id
            FROM entity_record er
            JOIN owner_party op ON op.resolved_entity_id = er.id
        )
        SELECT ag.entity_ids, ag.registered_agent,
               array_agg(DISTINCT epm.party_id) AS party_ids
        FROM agent_groups ag
        JOIN entity_party_map epm ON epm.entity_id = ANY(ag.entity_ids)
        GROUP BY ag.entity_ids, ag.registered_agent
        HAVING COUNT(DISTINCT epm.party_id) > 1
    """)
    rows = cur.fetchall()

    edges = []
    for entity_ids, agent, party_ids in rows:
        if not party_ids or len(party_ids) < 2:
            continue
        anchor = party_ids[0]
        ev = json.dumps({"rule": "shared_registered_agent", "agent": agent})
        for other in party_ids[1:]:
            if other and other != anchor:
                edges.append((anchor, other, "shared_registered_agent", CONF_SHARED_AGENT, ev))

    inserted = _bulk_insert_edges(cur, edges)
    log.info(f"  Rule 4: {inserted} edges in {_elapsed(t0)}")
    return inserted


# ---------------------------------------------------------------------------
# Rule 5 — Fuzzy name match (Levenshtein)
# ---------------------------------------------------------------------------

def rule_fuzzy_name(cur) -> int:
    """
    Levenshtein distance < 3 on normalized_name for LLC owners.
    To avoid O(n²) cost we restrict to names of similar length (±3 chars).
    Only applied to LLCs with > 1 property (more likely to be real investors).
    """
    t0 = time.time()
    log.info("Rule 5: fuzzy name match (Levenshtein < 3) …")

    # Get LLCs that own > 1 property — these are the candidates
    cur.execute("""
        SELECT op.id::text, op.normalized_name
        FROM owner_party op
        JOIN property_ownership po ON po.owner_party_id = op.id AND po.is_current = TRUE
        WHERE op.party_type = 'llc'
          AND op.normalized_name IS NOT NULL
          AND LENGTH(op.normalized_name) >= 8
        GROUP BY op.id, op.normalized_name
        HAVING COUNT(DISTINCT po.property_id) > 1
        ORDER BY op.normalized_name
    """)
    candidates = cur.fetchall()
    log.info(f"  Fuzzy candidates (LLCs with >1 property): {len(candidates)}")

    if not candidates:
        return 0

    # Use PostgreSQL levenshtein in batches to find close pairs
    # Process in windows sorted by name length to limit comparisons
    edges = []
    batch_size = 500

    for i in range(0, len(candidates), batch_size):
        batch = candidates[i : i + batch_size]
        ids   = [str(r[0]) for r in batch]
        names = [r[1]      for r in batch]

        # Build a temporary values list and compare within + forward window
        for j, (aid, aname) in enumerate(zip(ids, names)):
            alen = len(aname)
            # Only compare against next 200 in sorted order (names are sorted)
            window = list(zip(ids[j+1:j+201], names[j+1:j+201]))
            if not window:
                continue

            # Bulk levenshtein via postgres
            params = []
            val_parts = []
            for bid, bname in window:
                if abs(len(bname) - alen) > 4:
                    continue  # skip if lengths too different
                params.extend([aid, bid, aname, bname])
                val_parts.append("(%s, %s, %s, %s)")

            if not val_parts:
                continue

            cur.execute(f"""
                SELECT a_id, b_id, a_name, b_name,
                       levenshtein(a_name, b_name) AS dist
                FROM (VALUES {','.join(val_parts)}) AS t(a_id, b_id, a_name, b_name)
                WHERE levenshtein(a_name, b_name) < 3
                  AND a_name != b_name
            """, params)
            for row in cur.fetchall():
                a_id, b_id, a_name, b_name, dist = row
                ev = json.dumps({"rule": "fuzzy_name", "name_a": a_name, "name_b": b_name, "levenshtein": dist})
                edges.append((str(a_id), str(b_id), "fuzzy_name_match", CONF_FUZZY_NAME, ev))

        if edges and len(edges) % 1000 == 0:
            log.info(f"  ... {len(edges)} fuzzy edges found so far")

    inserted = _bulk_insert_edges(cur, edges)
    log.info(f"  Rule 5: {inserted} fuzzy edges in {_elapsed(t0)}")
    return inserted


# ---------------------------------------------------------------------------
# Step 3 — Portfolio clustering (Union-Find over owner_relationship)
# ---------------------------------------------------------------------------

def build_portfolio_clusters(cur) -> dict[str, Any]:
    t0 = time.time()
    log.info("Building portfolio clusters …")

    # Load all edges
    cur.execute("SELECT from_party_id::text, to_party_id::text FROM owner_relationship")
    edges = cur.fetchall()

    # Load all owner_party ids
    cur.execute("SELECT id::text FROM owner_party")
    all_ids = [r[0] for r in cur.fetchall()]

    uf = UnionFind(all_ids)
    for from_id, to_id in edges:
        uf.union(from_id, to_id)

    clusters = uf.clusters()

    # Only keep clusters with > 1 member (actual groups)
    multi = {root: members for root, members in clusters.items() if len(members) > 1}
    log.info(f"  Total owners: {len(all_ids)}, Clusters (multi-party): {len(multi)}")

    # For each cluster, compute stats from DB
    # Load property counts and values per owner
    cur.execute("""
        SELECT po.owner_party_id::text,
               COUNT(DISTINCT po.property_id) AS prop_count,
               COALESCE(SUM(vs.market_value), 0) AS total_val
        FROM property_ownership po
        JOIN valuation_snapshot vs ON vs.property_id = po.property_id
        WHERE po.is_current = TRUE
        GROUP BY po.owner_party_id
    """)
    owner_stats: dict[str, tuple[int, float]] = {}
    for owner_id, prop_count, total_val in cur.fetchall():
        owner_stats[owner_id] = (int(prop_count), float(total_val))

    # Load owner names
    cur.execute("SELECT id::text, raw_name, normalized_name FROM owner_party")
    owner_names: dict[str, tuple[str, str]] = {}
    for oid, raw, norm in cur.fetchall():
        owner_names[oid] = (raw or "", norm or "")

    # Load relationship methods used per pair
    cur.execute("""
        SELECT from_party_id::text, relationship_type
        FROM owner_relationship
    """)
    party_methods: dict[str, set[str]] = defaultdict(set)
    for pid, rtype in cur.fetchall():
        party_methods[pid].add(rtype)

    cluster_rows = []
    for rank, (root, members) in enumerate(
        sorted(multi.items(), key=lambda kv: -sum(owner_stats.get(m, (0, 0))[1] for m in kv[1])),
        start=1,
    ):
        prop_count = sum(owner_stats.get(m, (0, 0))[0] for m in members)
        total_val  = sum(owner_stats.get(m, (0, 0))[1] for m in members)
        avg_val    = total_val / prop_count if prop_count else 0.0

        # Pick canonical name: member with most properties
        best = max(members, key=lambda m: owner_stats.get(m, (0, 0))[0])
        canonical = owner_names.get(best, ("UNKNOWN", "UNKNOWN"))[0]

        methods = sorted({m for pid in members for m in party_methods.get(pid, set())})

        cluster_rows.append((
            rank,
            canonical,
            len(members),
            prop_count,
            total_val,
            avg_val,
            [m for m in members],
            methods,
        ))

        if rank > 10000:
            break

    # Insert clusters — party_ids must be cast from text[] to uuid[]
    for row in cluster_rows:
        rank, canonical, party_count, prop_count, total_val, avg_val, pids, methods = row
        cur.execute("""
            INSERT INTO portfolio_cluster
                (cluster_rank, canonical_name, party_count, property_count,
                 total_value, avg_value, party_ids, methods_used)
            VALUES (%s, %s, %s, %s, %s, %s,
                    ARRAY(SELECT unnest(%s::text[])::uuid),
                    %s)
        """, (rank, canonical, party_count, prop_count, total_val, avg_val,
              pids, methods))

    log.info(f"  Inserted {len(cluster_rows)} clusters in {_elapsed(t0)}")
    return {
        "total_owners": len(all_ids),
        "clusters_formed": len(multi),
        "singleton_owners": len(all_ids) - sum(len(v) for v in multi.values()),
        "top_clusters": cluster_rows[:5],
    }


# ---------------------------------------------------------------------------
# Step 4 — Ownership chain resolution
# ---------------------------------------------------------------------------

def resolve_ownership_chains(cur) -> int:
    """
    For every current property_ownership record, create an ownership_resolution
    that points at:
    - direct_owner_id   = the direct owner_party
    - beneficial_owner_id = resolved person (if deed chain found) or same as direct
    - cluster_id        = portfolio_cluster this owner belongs to
    - confidence        = from resolution_confidence or default 0.95
    """
    t0 = time.time()
    log.info("Resolving ownership chains …")

    # Build party → cluster lookup from portfolio_cluster
    cur.execute("SELECT id::text, ARRAY(SELECT unnest(party_ids)::text) FROM portfolio_cluster")
    party_cluster: dict[str, str] = {}
    for cid, pids in cur.fetchall():
        for pid in (pids or []):
            party_cluster[str(pid)] = str(cid)

    # Load all current ownerships
    cur.execute("""
        SELECT po.property_id::text, po.owner_party_id::text,
               op.resolved_person_id::text,
               op.resolution_confidence
        FROM property_ownership po
        JOIN owner_party op ON op.id = po.owner_party_id
        WHERE po.is_current = TRUE
    """)
    ownerships = cur.fetchall()

    rows = []
    for prop_id, owner_id, person_id, conf in ownerships:
        cluster_id = party_cluster.get(owner_id)
        final_conf = float(conf) if conf else 0.95

        chain = {"direct_owner": owner_id}
        if person_id:
            chain["beneficial_person"] = person_id
            chain["method"] = "deed_chain"
        if cluster_id:
            chain["cluster_id"] = cluster_id

        rows.append((
            prop_id,
            owner_id,
            # beneficial_owner_id: only if different owner_party (not used here)
            None,
            # beneficial_person_id: the resolved person_node
            person_id if person_id else None,
            cluster_id,
            final_conf,
            json.dumps(chain),
        ))

    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO ownership_resolution
            (property_id, direct_owner_id, beneficial_owner_id,
             beneficial_person_id, cluster_id, confidence, resolution_chain)
        VALUES %s
        ON CONFLICT (property_id) DO UPDATE
            SET direct_owner_id      = EXCLUDED.direct_owner_id,
                beneficial_owner_id  = EXCLUDED.beneficial_owner_id,
                beneficial_person_id = EXCLUDED.beneficial_person_id,
                cluster_id           = EXCLUDED.cluster_id,
                confidence           = EXCLUDED.confidence,
                resolution_chain     = EXCLUDED.resolution_chain,
                computed_at          = NOW()
        """,
        rows,
        template="(%s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s::jsonb)",
    )

    log.info(f"  Resolved {len(rows)} property chains in {_elapsed(t0)}")
    return len(rows)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_resolution(clear: bool = True) -> dict[str, Any]:
    """
    Execute the full entity resolution pipeline.
    Returns a summary dict with counts and top portfolio clusters.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info("=== Entity Resolution Engine — START ===")
    t_total = time.time()

    conn = _conn()
    conn.autocommit = False
    cur = conn.cursor()

    try:
        if clear:
            clear_previous_resolution(cur)
            conn.commit()

        # --- Rules ---
        r1 = rule_exact_match(cur)
        conn.commit()

        r2 = rule_mailing_address(cur)
        conn.commit()

        r3 = rule_deed_chain(cur)
        conn.commit()

        r4 = rule_shared_registered_agent(cur)
        conn.commit()

        r5 = rule_fuzzy_name(cur)
        conn.commit()

        total_edges = r1 + r2 + r3 + r4 + r5

        # --- Clustering ---
        cluster_stats = build_portfolio_clusters(cur)
        conn.commit()

        # --- Ownership chains ---
        resolved = resolve_ownership_chains(cur)
        conn.commit()

        # --- Final summary query ---
        cur.execute("""
            SELECT cluster_rank, canonical_name, party_count, property_count,
                   ROUND(total_value/1e6, 1) AS value_m
            FROM portfolio_cluster
            ORDER BY cluster_rank
            LIMIT 20
        """)
        top_clusters = cur.fetchall()

        cur.execute("SELECT COUNT(*) FROM owner_relationship")
        edge_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM portfolio_cluster")
        cluster_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM person_node")
        person_count = cur.fetchone()[0]

        # Example LLC → person chain
        cur.execute("""
            SELECT op_llc.raw_name AS llc_name,
                   pn.full_name    AS person_name,
                   COUNT(DISTINCT po.property_id) AS properties
            FROM owner_party op_llc
            JOIN person_node pn ON pn.id = op_llc.resolved_person_id
            JOIN property_ownership po ON po.owner_party_id = op_llc.id AND po.is_current
            GROUP BY op_llc.raw_name, pn.full_name
            ORDER BY properties DESC
            LIMIT 5
        """)
        llc_chains = cur.fetchall()

        summary = {
            "edges_inserted": {
                "rule_1_exact_name":    r1,
                "rule_2_mailing_addr":  r2,
                "rule_3_deed_chain":    r3,
                "rule_4_shared_agent":  r4,
                "rule_5_fuzzy_name":    r5,
                "total":                total_edges,
                "post_dedup_in_db":     edge_count,
            },
            "clusters": {
                "formed":               cluster_count,
                "total_owners":         cluster_stats["total_owners"],
                "singleton_owners":     cluster_stats["singleton_owners"],
            },
            "person_nodes_created":     person_count,
            "properties_resolved":      resolved,
            "top_20_clusters":          [
                {
                    "rank":         rank,
                    "name":         name,
                    "party_count":  parties,
                    "property_count": props,
                    "total_value_M": float(val_m),
                }
                for rank, name, parties, props, val_m in top_clusters
            ],
            "llc_person_chains":        [
                {"llc": llc, "person": person, "properties": cnt}
                for llc, person, cnt in llc_chains
            ],
            "elapsed_seconds": round(time.time() - t_total, 1),
        }

        log.info(f"=== Entity Resolution Engine — DONE in {_elapsed(t_total)} ===")
        return summary

    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# Query helpers used by API routes
# ---------------------------------------------------------------------------

def get_owner_graph(owner_id: str) -> dict[str, Any]:
    """
    Return the full relationship graph centred on owner_id.
    Uses a recursive CTE to traverse owner_relationship edges up to depth 4.
    """
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            WITH RECURSIVE graph AS (
                -- Seed: the owner itself
                SELECT
                    op.id::text          AS node_id,
                    op.raw_name          AS node_name,
                    op.party_type        AS node_type,
                    NULL::text           AS edge_from,
                    NULL::text           AS edge_rel,
                    NULL::numeric        AS edge_conf,
                    0                    AS depth
                FROM owner_party op
                WHERE op.id = %s::uuid

                UNION ALL

                -- Traverse outgoing edges
                SELECT
                    op2.id::text,
                    op2.raw_name,
                    op2.party_type,
                    g.node_id,
                    r.relationship_type,
                    r.confidence,
                    g.depth + 1
                FROM graph g
                JOIN owner_relationship r
                  ON r.from_party_id::text = g.node_id
                  OR r.to_party_id::text   = g.node_id
                JOIN owner_party op2
                  ON op2.id = CASE
                        WHEN r.from_party_id::text = g.node_id THEN r.to_party_id
                        ELSE r.from_party_id
                     END
                WHERE g.depth < 3
            )
            SELECT DISTINCT node_id, node_name, node_type, edge_from, edge_rel, edge_conf, depth
            FROM graph
            ORDER BY depth, node_name
        """, (owner_id,))
        graph_rows = cur.fetchall()

        nodes, edges = [], []
        seen_nodes, seen_edges = set(), set()
        for row in graph_rows:
            nid = row["node_id"]
            if nid not in seen_nodes:
                nodes.append({"id": nid, "name": row["node_name"], "type": row["node_type"]})
                seen_nodes.add(nid)
            if row["edge_from"]:
                ekey = (row["edge_from"], nid, row["edge_rel"])
                if ekey not in seen_edges:
                    edges.append({
                        "from": row["edge_from"],
                        "to":   nid,
                        "rel":  row["edge_rel"],
                        "conf": float(row["edge_conf"]) if row["edge_conf"] else None,
                    })
                    seen_edges.add(ekey)

        return {"nodes": nodes, "edges": edges, "center": owner_id}
    finally:
        cur.close()
        conn.close()


def get_owner_portfolio(owner_id: str) -> dict[str, Any]:
    """
    Return all properties controlled by the cluster containing owner_id.
    """
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # Find the cluster
        cur.execute("""
            SELECT id, canonical_name, party_count, property_count, total_value
            FROM portfolio_cluster
            WHERE %s::uuid = ANY(party_ids)
            LIMIT 1
        """, (owner_id,))
        cluster = cur.fetchone()

        if not cluster:
            # Fallback: just this owner's properties
            cur.execute("""
                SELECT p.id, p.property_address, p.city, p.county,
                       vs.market_value, vs.tax_year
                FROM property_ownership po
                JOIN property p ON p.id = po.property_id
                LEFT JOIN valuation_snapshot vs
                  ON vs.property_id = p.id
                  AND vs.tax_year = (SELECT MAX(tax_year) FROM valuation_snapshot WHERE property_id = p.id)
                WHERE po.owner_party_id = %s::uuid AND po.is_current = TRUE
                ORDER BY vs.market_value DESC NULLS LAST
                LIMIT 500
            """, (owner_id,))
            props = cur.fetchall()
            return {
                "cluster": None,
                "owner_id": owner_id,
                "property_count": len(props),
                "properties": [dict(r) for r in props],
            }

        # Get all owners in cluster, then all their properties
        cur.execute("""
            SELECT DISTINCT p.id, p.property_address, p.city, p.county,
                   p.state_land_use_code, vs.market_value, vs.tax_year,
                   op.raw_name AS owner_name, op.id::text AS owner_id
            FROM portfolio_cluster pc
            JOIN property_ownership po
              ON po.owner_party_id = ANY(pc.party_ids)
             AND po.is_current = TRUE
            JOIN property p ON p.id = po.property_id
            JOIN owner_party op ON op.id = po.owner_party_id
            LEFT JOIN valuation_snapshot vs
              ON vs.property_id = p.id
              AND vs.tax_year = (SELECT MAX(tax_year) FROM valuation_snapshot WHERE property_id = p.id)
            WHERE pc.id = %s::uuid
            ORDER BY vs.market_value DESC NULLS LAST
            LIMIT 1000
        """, (cluster["id"],))
        props = cur.fetchall()

        return {
            "cluster": dict(cluster),
            "property_count": len(props),
            "properties": [dict(r) for r in props],
        }
    finally:
        cur.close()
        conn.close()


def get_top_clusters(limit: int = 50) -> list[dict[str, Any]]:
    """Return top portfolio clusters ranked by total value."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT id, cluster_rank, canonical_name, party_count,
                   property_count, total_value, avg_value, methods_used, computed_at
            FROM portfolio_cluster
            ORDER BY cluster_rank
            LIMIT %s
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def resolve_property(property_id: str) -> dict[str, Any]:
    """
    Resolve who really controls a property:
    direct owner → beneficial owner → portfolio cluster.
    """
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # Get ownership resolution
        cur.execute("""
            SELECT
                or2.confidence,
                or2.resolution_chain,
                or2.computed_at,
                op_direct.id::text     AS direct_id,
                op_direct.raw_name     AS direct_name,
                op_direct.party_type   AS direct_type,
                op_benef.id::text      AS beneficial_id,
                op_benef.raw_name      AS beneficial_name,
                op_benef.party_type    AS beneficial_type,
                pn.id::text            AS person_node_id,
                pn.full_name           AS person_node_name,
                pc.id::text            AS cluster_id,
                pc.canonical_name      AS cluster_name,
                pc.property_count      AS cluster_props,
                pc.total_value         AS cluster_value
            FROM ownership_resolution or2
            LEFT JOIN owner_party op_direct ON op_direct.id = or2.direct_owner_id
            LEFT JOIN owner_party op_benef  ON op_benef.id  = or2.beneficial_owner_id
            LEFT JOIN person_node pn        ON pn.id        = or2.beneficial_person_id
            LEFT JOIN portfolio_cluster pc  ON pc.id        = or2.cluster_id
            WHERE or2.property_id = %s::uuid
        """, (property_id,))
        row = cur.fetchone()

        if not row:
            # Fallback to raw ownership
            cur.execute("""
                SELECT op.id::text, op.raw_name, op.party_type, po.ownership_pct
                FROM property_ownership po
                JOIN owner_party op ON op.id = po.owner_party_id
                WHERE po.property_id = %s::uuid AND po.is_current = TRUE
                LIMIT 5
            """, (property_id,))
            owners = cur.fetchall()
            return {
                "property_id": property_id,
                "resolved": False,
                "direct_owners": [dict(r) for r in owners],
            }

        result = dict(row)
        # Build a clean person_node sub-object if we have one
        if row.get("person_node_id"):
            result["person_node"] = {
                "id":   row["person_node_id"],
                "name": row["person_node_name"],
            }
        else:
            result["person_node"] = None
        result["property_id"] = property_id
        result["resolved"] = True
        return result
    finally:
        cur.close()
        conn.close()
