// ─── Core property types ───────────────────────────────────────────────────

export interface Coordinates {
  lat: number;
  lng: number;
}

export interface Signal {
  signal_type: string;
  severity: 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
  description: string;
  detected_at: string;
  source: string;
}

export interface PropertySummary {
  id: string;
  parcel_id: string;
  address: string;
  city: string;
  state: string;
  zip: string;
  county: string;
  lat: number | null;
  lng: number | null;
  land_use: string | null;
  zoning: string | null;
  acres: number | null;
  year_built: number | null;
  sqft: number | null;
  assessed_value: number | null;
  market_value: number | null;
  opportunity_score: number | null;
  distress_score: number | null;
  equity_score: number | null;
  owner_name: string | null;
  owner_id: string | null;
  signal_count: number;
  has_lis_pendens: boolean;
  has_foreclosure: boolean;
  tax_delinquent: boolean;
  updated_at: string;
  // Location intelligence (from property detail)
  flood_zone?: string | null;
  flood_risk?: string | null;
  in_floodplain?: boolean | null;
  in_opportunity_zone?: boolean | null;
  in_tirz?: boolean | null;
  tirz_name?: string | null;
  census_tract?: string | null;
  nearest_transit_stop?: string | null;
  distance_to_transit_ft?: number | null;
  nearest_highway?: string | null;
  highway_aadt?: number | null;
  distance_to_highway_ft?: number | null;
  has_frontage?: boolean | null;
}

export interface GeoJSONFeature {
  type: 'Feature';
  geometry: {
    type: 'Point';
    coordinates: [number, number];
  } | null;
  properties: PropertySummary;
}

export interface GeoJSONCollection {
  type: 'FeatureCollection';
  features: GeoJSONFeature[];
  total: number;
  returned: number;
}

// ─── Owner types ────────────────────────────────────────────────────────────

export interface Owner {
  id: string;
  name: string;
  owner_type: string | null;
  normalized_name: string | null;
  entity_type: string | null;
  state_of_formation: string | null;
  portfolio_count: number;
  total_assessed_value: number | null;
  total_market_value: number | null;
  avg_distress_score: number | null;
  high_risk_count: number;
  properties?: PropertySummary[];
}

export interface EntityNode {
  id: string;
  name: string;
  type: string;
  role: string;
  depth: number;
  children?: EntityNode[];
}

export interface EntityResolution {
  property_id: string;
  direct_owner: string;
  controlling_entity: string | null;
  chain: EntityNode[];
  confidence: number;
}

// ─── Document types ──────────────────────────────────────────────────────────

export interface Document {
  id: string;
  property_id: string;
  doc_type: string;
  recorded_date: string | null;
  instrument_number: string | null;
  grantor: string | null;
  grantee: string | null;
  amount: number | null;
  source: string | null;
  notes: string | null;
}

// ─── Filing types ────────────────────────────────────────────────────────────

export interface Filing {
  id: string;
  property_id: string;
  filing_type: string;
  case_number: string | null;
  filed_date: string | null;
  court: string | null;
  plaintiff: string | null;
  defendant: string | null;
  status: string | null;
  amount: number | null;
  notes: string | null;
}

// ─── Debt / Valuation types ──────────────────────────────────────────────────

export interface Debt {
  id: string;
  property_id: string;
  lender: string | null;
  loan_amount: number | null;
  origination_date: string | null;
  maturity_date: string | null;
  interest_rate: number | null;
  loan_type: string | null;
  lien_position: number | null;
  status: string | null;
  maturity_analysis: {
    days_to_maturity: number | null;
    maturity_risk: 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL' | null;
    is_matured: boolean;
  } | null;
}

export interface Valuation {
  id: string;
  property_id: string;
  valuation_type: string;
  value: number;
  valuation_date: string | null;
  source: string | null;
  notes: string | null;
}

// ─── Risk types ───────────────────────────────────────────────────────────────

export interface RiskAssessment {
  property_id: string;
  risk_level: 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
  overall_score: number;
  components: {
    distress_score: number | null;
    equity_score: number | null;
    debt_maturity_risk: string | null;
    lis_pendens: boolean;
    foreclosure: boolean;
    tax_delinquent: boolean;
    signal_count: number;
  };
  recommendation: string;
  signals: Signal[];
}

// ─── Property Detail (full) ──────────────────────────────────────────────────

export interface PropertyDetail extends PropertySummary {
  owner: Owner | null;
  valuations: Valuation[];
  documents: Document[];
  debts: Debt[];
  filings: Filing[];
  signals: Signal[];
  event_counts: {
    documents: number;
    filings: number;
    signals: number;
    debts: number;
  };
}

// ─── Stats types ──────────────────────────────────────────────────────────────

export interface DashboardStats {
  total_properties: number;
  total_assessed_value: number | null;
  total_market_value: number | null;
  distressed_count: number;
  high_risk_count: number;
  critical_risk_count: number;
  counties: string[];
  avg_opportunity_score: number | null;
  signal_breakdown: Record<string, number>;
}

export interface RiskSummary {
  total: number;
  by_level: Record<string, number>;
  avg_score: number | null;
  top_signals: Array<{ signal_type: string; count: number }>;
}

// ─── Search types ─────────────────────────────────────────────────────────────

export interface SearchResult {
  type: 'property' | 'owner' | 'entity';
  id: string;
  title: string;
  subtitle: string;
  score: number | null;
  badge?: string;
}

// ─── Ownership chain types ────────────────────────────────────────────────────

export interface OwnershipSource {
  source: 'tx_comptroller' | 'permit_contractor' | 'deed_grantor' | 'opencorporates' | 'mailing_crossref';
  name: string;
  role?: string | null;
  entity?: string | null;
  permits?: number | null;
  deed_date?: string | null;
  mailing_address?: string | null;
}

export interface OwnershipChain {
  property_address: string;
  city: string;
  county: string;
  cad_owner: string;
  resolution: {
    beneficial_person: string;
    confidence: number;
    sources: OwnershipSource[];
  } | null;
  related_entities: string[];
  total_portfolio: {
    properties: number;
    total_value: number;
  };
}

// ─── Cluster types ────────────────────────────────────────────────────────────

export interface EntityCluster {
  cluster_id: string;
  controlling_entity: string;
  member_count: number;
  total_properties: number;
  total_value: number | null;
  avg_distress: number | null;
  members: string[];
}

// ─── Filter state ─────────────────────────────────────────────────────────────

export interface FilterState {
  counties: string[];
  min_value: number | null;
  max_value: number | null;
  min_score: number | null;
  max_score: number | null;
  has_lis_pendens: boolean | null;
  has_foreclosure: boolean | null;
  tax_delinquent: boolean | null;
  land_use: string | null;
  limit: number;
}

export type ViewMode = 'map' | 'list';
