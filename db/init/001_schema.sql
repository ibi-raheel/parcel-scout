-- Parcel Scout v3 — Canonical Schema
-- PostgreSQL 16 + PostGIS
-- Follows TAD Section 3.2 entity model

-- Extensions
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ═══════════════════════════════════════════════════════════════
-- CORE ENTITIES
-- ═══════════════════════════════════════════════════════════════

-- The stable record representing a physical parcel/improvement
CREATE TABLE property (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    county          VARCHAR(20) NOT NULL,  -- Dallas, Tarrant, Collin, Denton

    -- Address
    property_address    VARCHAR(255),
    city                VARCHAR(100),
    state               VARCHAR(2) DEFAULT 'TX',
    zip                 VARCHAR(10),

    -- Legal
    legal_description   TEXT,

    -- Physical
    acreage             NUMERIC(10,4),
    lot_sqft            NUMERIC(12,2),
    building_sqft       NUMERIC(12,2),
    year_built          INTEGER,
    num_stories         INTEGER,
    building_class      VARCHAR(20),
    condition           VARCHAR(50),

    -- Use
    state_land_use_code VARCHAR(10),
    land_use_description VARCHAR(100),
    zoning_code         VARCHAR(20),
    zoning_description  VARCHAR(100),

    -- Geometry
    latitude            NUMERIC(11,8),
    longitude           NUMERIC(12,8),
    geom                GEOMETRY(Point, 4326),
    parcel_boundary     GEOMETRY(MultiPolygon, 4326),

    -- Infrastructure
    has_frontage        BOOLEAN DEFAULT FALSE,
    nearest_highway     VARCHAR(100),
    highway_aadt        INTEGER,
    distance_to_highway_ft NUMERIC(10,2),

    -- Location intelligence
    flood_zone          VARCHAR(10),
    flood_risk          VARCHAR(20),
    in_floodplain       BOOLEAN DEFAULT FALSE,
    in_opportunity_zone BOOLEAN DEFAULT FALSE,
    in_tirz             BOOLEAN DEFAULT FALSE,
    tirz_name           VARCHAR(100),
    census_tract        VARCHAR(20),
    nearest_transit_stop VARCHAR(100),
    distance_to_transit_ft NUMERIC(10,2),

    -- Timestamps
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_property_county ON property(county);
CREATE INDEX idx_property_address ON property USING gin(property_address gin_trgm_ops);
CREATE INDEX idx_property_city ON property(city);
CREATE INDEX idx_property_geom ON property USING gist(geom);
CREATE INDEX idx_property_zoning ON property(zoning_code);

-- Source-specific identifiers (county account #, parcel ID, etc.)
CREATE TABLE source_property_ref (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id     UUID NOT NULL REFERENCES property(id),
    source          VARCHAR(50) NOT NULL,   -- dcad_bulk, tad_bulk, ccad_bulk, denton_cad
    source_id       VARCHAR(100) NOT NULL,  -- county parcel_id or account number
    source_url      TEXT,
    fetched_at      TIMESTAMPTZ,
    raw_payload     JSONB,                  -- original source record
    UNIQUE(source, source_id)
);

CREATE INDEX idx_source_ref_property ON source_property_ref(property_id);
CREATE INDEX idx_source_ref_source_id ON source_property_ref(source, source_id);

-- ═══════════════════════════════════════════════════════════════
-- OWNERSHIP & ENTITIES
-- ═══════════════════════════════════════════════════════════════

-- Person, entity, or unresolved owner candidate
CREATE TABLE owner_party (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    raw_name        VARCHAR(255) NOT NULL,      -- exact name from source
    normalized_name VARCHAR(255),                -- cleaned/normalized
    party_type      VARCHAR(20),                 -- individual, llc, corporation, trust, government, unknown

    -- Mailing
    mailing_address VARCHAR(255),
    mailing_city    VARCHAR(100),
    mailing_state   VARCHAR(20),
    mailing_zip     VARCHAR(10),
    mailing_type    VARCHAR(20),                 -- residential, po_box, corporate, agent

    -- Flags
    is_out_of_state BOOLEAN DEFAULT FALSE,

    -- Resolution
    resolved_entity_id  UUID,                    -- links to entity_record if resolved
    resolved_person_id  UUID,                    -- links to person_node if resolved
    resolution_confidence NUMERIC(3,2),          -- 0.00 to 1.00
    resolution_method   VARCHAR(50),             -- exact_match, fuzzy, mailing_link, sos_match

    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_owner_party_name ON owner_party USING gin(normalized_name gin_trgm_ops);
CREATE INDEX idx_owner_party_raw ON owner_party USING gin(raw_name gin_trgm_ops);
CREATE INDEX idx_owner_party_mailing ON owner_party(mailing_state);
CREATE INDEX idx_owner_party_entity ON owner_party(resolved_entity_id);

-- Property ↔ Owner link (many-to-many with metadata)
CREATE TABLE property_ownership (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id     UUID NOT NULL REFERENCES property(id),
    owner_party_id  UUID NOT NULL REFERENCES owner_party(id),
    ownership_pct   NUMERIC(5,2) DEFAULT 100.00,
    is_current      BOOLEAN DEFAULT TRUE,
    source          VARCHAR(50),                 -- cad_roll, deed, tax_roll
    effective_date  DATE,
    end_date        DATE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_ownership_property ON property_ownership(property_id);
CREATE INDEX idx_ownership_owner ON property_ownership(owner_party_id);
CREATE INDEX idx_ownership_current ON property_ownership(is_current);

-- Normalized organization identity
CREATE TABLE entity_record (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_name     VARCHAR(255) NOT NULL,
    entity_type     VARCHAR(20),                 -- llc, lp, inc, trust, corp

    -- TX SOS data
    sos_file_number VARCHAR(50),
    formation_date  DATE,
    sos_status      VARCHAR(50),                 -- active, forfeited, dissolved, etc.
    registered_agent VARCHAR(255),
    registered_address TEXT,

    -- Derived
    portfolio_size  INTEGER DEFAULT 0,
    portfolio_value NUMERIC(15,2) DEFAULT 0,

    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_entity_name ON entity_record USING gin(entity_name gin_trgm_ops);
CREATE INDEX idx_entity_sos ON entity_record(sos_file_number);
CREATE INDEX idx_entity_agent ON entity_record USING gin(registered_agent gin_trgm_ops);

-- Individual person (officer, member, signatory)
CREATE TABLE person_node (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    full_name       VARCHAR(255) NOT NULL,
    normalized_name VARCHAR(255),

    -- Known roles
    roles           JSONB,                       -- [{entity_id, position, source}]

    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_person_name ON person_node USING gin(normalized_name gin_trgm_ops);

-- Entity ↔ Person link
CREATE TABLE entity_person (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_id       UUID NOT NULL REFERENCES entity_record(id),
    person_id       UUID NOT NULL REFERENCES person_node(id),
    position        VARCHAR(100),                -- manager, member, officer, director, registered_agent
    source          VARCHAR(50),                 -- tx_sos, deed_signer, opencorporates
    confidence      NUMERIC(3,2) DEFAULT 1.00,
    effective_date  DATE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_entity_person_entity ON entity_person(entity_id);
CREATE INDEX idx_entity_person_person ON entity_person(person_id);

-- Entity ↔ Entity link (parent/subsidiary, shared agent, etc.)
CREATE TABLE entity_relationship (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_a_id     UUID NOT NULL REFERENCES entity_record(id),
    entity_b_id     UUID NOT NULL REFERENCES entity_record(id),
    relationship    VARCHAR(50),                 -- parent_of, subsidiary_of, shared_agent, shared_address, shared_officer
    confidence      NUMERIC(3,2) DEFAULT 0.50,
    evidence        JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_entity_rel_a ON entity_relationship(entity_a_id);
CREATE INDEX idx_entity_rel_b ON entity_relationship(entity_b_id);

-- ═══════════════════════════════════════════════════════════════
-- VALUATION
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE valuation_snapshot (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id     UUID NOT NULL REFERENCES property(id),
    tax_year        INTEGER NOT NULL,

    market_value        NUMERIC(15,2),
    total_appraised     NUMERIC(15,2),
    land_value          NUMERIC(15,2),
    improvement_value   NUMERIC(15,2),
    assessed_value      NUMERIC(15,2),

    -- Tax
    tax_status          VARCHAR(20),             -- current, delinquent, exempt
    tax_delinquent_amount NUMERIC(12,2),
    tax_delinquent_years INTEGER,
    tax_data_source     VARCHAR(30),             -- county_verified, proxy

    source              VARCHAR(50),
    fetched_at          TIMESTAMPTZ,

    UNIQUE(property_id, tax_year, source)
);

CREATE INDEX idx_valuation_property ON valuation_snapshot(property_id);
CREATE INDEX idx_valuation_year ON valuation_snapshot(tax_year);

-- ═══════════════════════════════════════════════════════════════
-- DOCUMENTS & INSTRUMENTS
-- ═══════════════════════════════════════════════════════════════

-- Recorded deed, DOT, lien, assignment, release, notice
CREATE TABLE document_event (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id     UUID REFERENCES property(id),

    -- Document identifiers
    instrument_number   VARCHAR(50),
    deed_book           VARCHAR(20),
    deed_page           VARCHAR(20),

    -- Classification
    doc_type            VARCHAR(30) NOT NULL,     -- warranty_deed, special_warranty, quit_claim, deed_of_trust, assignment, release, lien, notice, judgment
    doc_category        VARCHAR(20) NOT NULL,     -- transfer, financing, lien, release, notice, enforcement

    -- Dates
    recording_date      DATE,
    effective_date      DATE,

    -- Parties
    grantor_raw         VARCHAR(500),
    grantee_raw         VARCHAR(500),
    grantor_party_id    UUID REFERENCES owner_party(id),
    grantee_party_id    UUID REFERENCES owner_party(id),

    -- Financial (from actual clerk records)
    consideration       NUMERIC(15,2),           -- actual sale price
    loan_amount         NUMERIC(15,2),
    interest_rate       NUMERIC(5,3),
    maturity_date       DATE,
    lender_name         VARCHAR(255),

    -- Source
    source              VARCHAR(50),
    source_url          TEXT,
    raw_payload         JSONB,
    fetched_at          TIMESTAMPTZ,

    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_doc_event_property ON document_event(property_id);
CREATE INDEX idx_doc_event_type ON document_event(doc_type);
CREATE INDEX idx_doc_event_category ON document_event(doc_category);
CREATE INDEX idx_doc_event_date ON document_event(recording_date);
CREATE INDEX idx_doc_event_instrument ON document_event(instrument_number);
CREATE INDEX idx_doc_event_grantor ON document_event USING gin(grantor_raw gin_trgm_ops);
CREATE INDEX idx_doc_event_grantee ON document_event USING gin(grantee_raw gin_trgm_ops);

-- Mortgage / Deed of Trust with tracking
CREATE TABLE debt_instrument (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id     UUID NOT NULL REFERENCES property(id),
    document_event_id UUID REFERENCES document_event(id),

    -- Loan details
    original_amount     NUMERIC(15,2),
    current_balance_est NUMERIC(15,2),           -- estimated if available
    interest_rate       NUMERIC(5,3),
    loan_term_months    INTEGER,
    maturity_date       DATE,
    lender_name         VARCHAR(255),
    servicer_name       VARCHAR(255),
    loan_type           VARCHAR(30),             -- conventional, fha, va, cmbs, construction

    -- Status
    status              VARCHAR(20) DEFAULT 'active',  -- active, released, assigned, defaulted
    is_released         BOOLEAN DEFAULT FALSE,
    release_date        DATE,

    -- Refinance detection
    has_subsequent_financing BOOLEAN DEFAULT FALSE,

    -- Source
    data_quality        VARCHAR(20) DEFAULT 'unknown',  -- clerk_verified, cad_estimated, derived
    source              VARCHAR(50),
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_debt_property ON debt_instrument(property_id);
CREATE INDEX idx_debt_maturity ON debt_instrument(maturity_date);
CREATE INDEX idx_debt_status ON debt_instrument(status);
CREATE INDEX idx_debt_lender ON debt_instrument USING gin(lender_name gin_trgm_ops);

-- ═══════════════════════════════════════════════════════════════
-- EVENTS (permits, violations, crimes, 311)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE permit_event (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id     UUID REFERENCES property(id),

    permit_number   VARCHAR(50),
    permit_type     VARCHAR(100),
    permit_date     DATE,
    status          VARCHAR(50),
    description     TEXT,
    estimated_cost  NUMERIC(15,2),
    contractor      VARCHAR(255),
    address         VARCHAR(255),

    latitude        NUMERIC(11,8),
    longitude       NUMERIC(12,8),
    geom            GEOMETRY(Point, 4326),

    source          VARCHAR(50),
    fetched_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_permit_property ON permit_event(property_id);
CREATE INDEX idx_permit_date ON permit_event(permit_date);
CREATE INDEX idx_permit_geom ON permit_event USING gist(geom);

CREATE TABLE code_event (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id     UUID REFERENCES property(id),

    case_number     VARCHAR(50),
    violation_type  VARCHAR(100),
    violation_date  DATE,
    status          VARCHAR(50),
    description     TEXT,
    address         VARCHAR(255),

    latitude        NUMERIC(11,8),
    longitude       NUMERIC(12,8),
    geom            GEOMETRY(Point, 4326),

    source          VARCHAR(50),
    fetched_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_code_property ON code_event(property_id);
CREATE INDEX idx_code_date ON code_event(violation_date);

CREATE TABLE crime_event (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id     UUID REFERENCES property(id),

    incident_id     VARCHAR(50),
    incident_type   VARCHAR(100),
    incident_date   DATE,
    severity        VARCHAR(20),
    address         VARCHAR(255),

    latitude        NUMERIC(11,8),
    longitude       NUMERIC(12,8),
    geom            GEOMETRY(Point, 4326),

    source          VARCHAR(50),
    fetched_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_crime_property ON crime_event(property_id);
CREATE INDEX idx_crime_date ON crime_event(incident_date);
CREATE INDEX idx_crime_geom ON crime_event USING gist(geom);

CREATE TABLE service_request (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id     UUID REFERENCES property(id),

    request_id      VARCHAR(50),
    request_type    VARCHAR(100),
    request_date    DATE,
    status          VARCHAR(50),
    description     TEXT,
    address         VARCHAR(255),

    latitude        NUMERIC(11,8),
    longitude       NUMERIC(12,8),
    geom            GEOMETRY(Point, 4326),

    source          VARCHAR(50),
    fetched_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_service_property ON service_request(property_id);
CREATE INDEX idx_service_date ON service_request(request_date);

-- ═══════════════════════════════════════════════════════════════
-- FILINGS (UCC, court, liens, foreclosures)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE filing_node (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id     UUID REFERENCES property(id),

    filing_type     VARCHAR(30) NOT NULL,        -- ucc, lis_pendens, mechanic_lien, tax_lien, judgment, foreclosure, bankruptcy
    case_number     VARCHAR(50),
    filing_date     DATE,

    -- Parties
    plaintiff       VARCHAR(500),
    defendant       VARCHAR(500),
    secured_party   VARCHAR(500),
    debtor          VARCHAR(500),

    -- Details
    amount          NUMERIC(15,2),
    status          VARCHAR(30),                 -- active, terminated, dismissed, satisfied, sold
    description     TEXT,
    sale_date       DATE,

    -- Source
    source          VARCHAR(50),
    source_url      TEXT,
    raw_payload     JSONB,
    fetched_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_filing_property ON filing_node(property_id);
CREATE INDEX idx_filing_type ON filing_node(filing_type);
CREATE INDEX idx_filing_date ON filing_node(filing_date);
CREATE INDEX idx_filing_status ON filing_node(status);

-- ═══════════════════════════════════════════════════════════════
-- SIGNALS & SCORING
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE signal_result (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id     UUID NOT NULL REFERENCES property(id),

    signal_key      VARCHAR(50) NOT NULL,        -- long_hold, absentee_owner, mortgage_maturity, etc.
    status          VARCHAR(20) NOT NULL,        -- active, resolved, expired
    score_delta     INTEGER NOT NULL,            -- points contributed to overall score
    confidence      NUMERIC(3,2),

    -- Evidence
    evidence        JSONB,                       -- pointers to supporting records
    explanation     TEXT,                         -- human-readable reason

    -- Versioning
    signal_version  INTEGER DEFAULT 1,
    computed_at     TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,

    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_signal_property ON signal_result(property_id);
CREATE INDEX idx_signal_key ON signal_result(signal_key);
CREATE INDEX idx_signal_status ON signal_result(status);

-- ═══════════════════════════════════════════════════════════════
-- WORKFLOW
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE workflow_item (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id     UUID NOT NULL REFERENCES property(id),

    status          VARCHAR(20) DEFAULT 'new',   -- new, assigned, contacted, nurture, dead, monitor
    assigned_to     VARCHAR(100),
    priority        INTEGER DEFAULT 0,

    notes           TEXT,
    tags            VARCHAR[],

    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_workflow_property ON workflow_item(property_id);
CREATE INDEX idx_workflow_status ON workflow_item(status);

-- ═══════════════════════════════════════════════════════════════
-- REFERENCE DATA
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE highway (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(100),
    highway_type    VARCHAR(30),
    aadt            INTEGER,
    geom            GEOMETRY(MultiLineString, 4326),
    source          VARCHAR(50)
);

CREATE INDEX idx_highway_geom ON highway USING gist(geom);

CREATE TABLE transit_stop (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    stop_name       VARCHAR(100),
    system_name     VARCHAR(50),
    latitude        NUMERIC(11,8),
    longitude       NUMERIC(12,8),
    geom            GEOMETRY(Point, 4326),
    source          VARCHAR(50)
);

CREATE INDEX idx_transit_geom ON transit_stop USING gist(geom);

CREATE TABLE opportunity_zone (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tract_id        VARCHAR(20),
    county          VARCHAR(20),
    geom            GEOMETRY(MultiPolygon, 4326)
);

CREATE TABLE tirz_district (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(100),
    county          VARCHAR(20),
    city            VARCHAR(50),
    geom            GEOMETRY(MultiPolygon, 4326)
);

CREATE TABLE environmental_site (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    site_name       VARCHAR(255),
    site_type       VARCHAR(50),
    latitude        NUMERIC(11,8),
    longitude       NUMERIC(12,8),
    geom            GEOMETRY(Point, 4326),
    source          VARCHAR(50),
    fetched_at      TIMESTAMPTZ
);

CREATE INDEX idx_env_geom ON environmental_site USING gist(geom);

-- ═══════════════════════════════════════════════════════════════
-- AUDIT & PROVENANCE
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE sync_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source          VARCHAR(50) NOT NULL,
    connector       VARCHAR(50),
    status          VARCHAR(20),                 -- success, partial, failed
    records_fetched INTEGER DEFAULT 0,
    records_parsed  INTEGER DEFAULT 0,
    records_written INTEGER DEFAULT 0,
    error_message   TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Updated timestamp trigger
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tr_property_updated BEFORE UPDATE ON property FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER tr_owner_party_updated BEFORE UPDATE ON owner_party FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER tr_entity_record_updated BEFORE UPDATE ON entity_record FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER tr_person_node_updated BEFORE UPDATE ON person_node FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER tr_debt_instrument_updated BEFORE UPDATE ON debt_instrument FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER tr_workflow_item_updated BEFORE UPDATE ON workflow_item FOR EACH ROW EXECUTE FUNCTION update_updated_at();
