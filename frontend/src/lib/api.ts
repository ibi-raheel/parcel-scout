import type {
  GeoJSONCollection,
  PropertyDetail,
  Owner,
  Document,
  Filing,
  Debt,
  RiskAssessment,
  DashboardStats,
  RiskSummary,
  EntityCluster,
  EntityResolution,
  FilterState,
  SearchResult,
  OwnershipChain,
} from '@/types';

// ─── Base fetch helper ────────────────────────────────────────────────────────

const BASE = '/api';

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`API ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

// ─── Properties ───────────────────────────────────────────────────────────────

export function buildPropertyParams(filters: Partial<FilterState>): string {
  const params = new URLSearchParams();
  if (filters.counties?.length)
    params.set('county', filters.counties.join(','));
  if (filters.min_value != null)
    params.set('min_value', String(filters.min_value));
  if (filters.max_value != null)
    params.set('max_value', String(filters.max_value));
  if (filters.min_score != null)
    params.set('min_score', String(filters.min_score));
  if (filters.max_score != null)
    params.set('max_score', String(filters.max_score));
  if (filters.has_lis_pendens != null)
    params.set('has_lis_pendens', String(filters.has_lis_pendens));
  if (filters.has_foreclosure != null)
    params.set('has_foreclosure', String(filters.has_foreclosure));
  if (filters.tax_delinquent != null)
    params.set('tax_delinquent', String(filters.tax_delinquent));
  if (filters.land_use) params.set('land_use', filters.land_use);
  params.set('limit', String(filters.limit ?? 5000));
  return params.toString();
}

export async function fetchProperties(
  filters: Partial<FilterState> = {}
): Promise<GeoJSONCollection> {
  const qs = buildPropertyParams(filters);
  return apiFetch<GeoJSONCollection>(`/properties?${qs}`);
}

export async function fetchPropertyDetail(id: string): Promise<PropertyDetail> {
  return apiFetch<PropertyDetail>(`/properties/${id}`);
}

export async function fetchPropertyDebt(id: string): Promise<Debt[]> {
  return apiFetch<Debt[]>(`/properties/${id}/debt`);
}

export async function fetchPropertyRisk(id: string): Promise<RiskAssessment> {
  return apiFetch<RiskAssessment>(`/properties/${id}/risk`);
}

export async function getOwnershipChain(propertyId: string): Promise<OwnershipChain> {
  return apiFetch<OwnershipChain>(`/properties/${propertyId}/ownership-chain`);
}

// ─── Owners ───────────────────────────────────────────────────────────────────

export async function fetchOwners(search?: string): Promise<Owner[]> {
  const qs = search ? `?search=${encodeURIComponent(search)}` : '';
  return apiFetch<Owner[]>(`/owners${qs}`);
}

export async function fetchOwner(id: string): Promise<Owner> {
  return apiFetch<Owner>(`/owners/${id}`);
}

// ─── Entities ─────────────────────────────────────────────────────────────────

export async function fetchEntityClusters(): Promise<EntityCluster[]> {
  return apiFetch<EntityCluster[]>('/entities/clusters');
}

export async function resolveEntity(
  propertyId: string
): Promise<EntityResolution> {
  return apiFetch<EntityResolution>(`/entities/resolve/${propertyId}`);
}

// ─── Documents ────────────────────────────────────────────────────────────────

export async function fetchDocuments(propertyId?: string): Promise<Document[]> {
  const qs = propertyId
    ? `?property_id=${encodeURIComponent(propertyId)}`
    : '';
  return apiFetch<Document[]>(`/documents${qs}`);
}

// ─── Filings ──────────────────────────────────────────────────────────────────

export async function fetchFilings(propertyId?: string): Promise<Filing[]> {
  const qs = propertyId
    ? `?property_id=${encodeURIComponent(propertyId)}`
    : '';
  return apiFetch<Filing[]>(`/filings${qs}`);
}

// ─── Risk ─────────────────────────────────────────────────────────────────────

export async function fetchHighRisk(): Promise<PropertyDetail[]> {
  return apiFetch<PropertyDetail[]>('/risk/high');
}

export async function fetchRiskSummary(): Promise<RiskSummary> {
  return apiFetch<RiskSummary>('/risk/summary');
}

// ─── Stats ────────────────────────────────────────────────────────────────────

export async function fetchStats(): Promise<DashboardStats> {
  return apiFetch<DashboardStats>('/stats');
}

// ─── Search ───────────────────────────────────────────────────────────────────

export interface RawSearchResponse {
  properties?: Array<{
    id: string;
    address: string;
    city: string;
    county: string;
    opportunity_score: number | null;
    owner_name: string | null;
  }>;
  owners?: Array<{
    id: string;
    name: string;
    portfolio_count: number;
    owner_type: string | null;
  }>;
}

export async function search(q: string): Promise<SearchResult[]> {
  if (!q.trim()) return [];
  const raw = await apiFetch<RawSearchResponse>(
    `/search?q=${encodeURIComponent(q)}`
  );
  const results: SearchResult[] = [];

  (raw.properties ?? []).forEach((p) => {
    results.push({
      type: 'property',
      id: p.id,
      title: p.address,
      subtitle: `${p.city} · ${p.county}`,
      score: p.opportunity_score,
      badge: p.owner_name ?? undefined,
    });
  });

  (raw.owners ?? []).forEach((o) => {
    results.push({
      type: 'owner',
      id: o.id,
      title: o.name,
      subtitle: `${o.portfolio_count} properties · ${o.owner_type ?? 'Unknown'}`,
      score: null,
      badge: o.owner_type ?? undefined,
    });
  });

  return results;
}

// ─── Admin ────────────────────────────────────────────────────────────────────

export async function fetchSourceHealth(): Promise<unknown> {
  return apiFetch<unknown>('/admin/sources');
}

export async function fetchFreshness(): Promise<unknown> {
  return apiFetch<unknown>('/admin/freshness');
}

// ─── Utilities ────────────────────────────────────────────────────────────────

export function formatCurrency(v: number | null | undefined): string {
  if (v == null) return '—';
  if (v >= 1_000_000_000)
    return `$${(v / 1_000_000_000).toFixed(1)}B`;
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(0)}K`;
  return `$${v.toFixed(0)}`;
}

export function formatScore(v: number | null | undefined): string {
  if (v == null) return '—';
  return v.toFixed(1);
}

export function scoreColor(score: number | null): string {
  if (score == null) return '#64748b';
  if (score >= 80) return '#ef4444';
  if (score >= 60) return '#f97316';
  if (score >= 40) return '#eab308';
  if (score >= 20) return '#22c55e';
  return '#3b82f6';
}

export function riskBadgeClass(
  level: string | null
): string {
  switch (level) {
    case 'CRITICAL':
      return 'bg-red-900/60 text-red-300 border border-red-700';
    case 'HIGH':
      return 'bg-orange-900/60 text-orange-300 border border-orange-700';
    case 'MEDIUM':
      return 'bg-yellow-900/60 text-yellow-300 border border-yellow-700';
    case 'LOW':
      return 'bg-green-900/60 text-green-300 border border-green-700';
    default:
      return 'bg-slate-700 text-slate-300 border border-slate-600';
  }
}
