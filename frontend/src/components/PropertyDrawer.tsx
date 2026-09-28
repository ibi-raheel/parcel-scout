'use client';

import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  X,
  MapPin,
  User,
  DollarSign,
  AlertTriangle,
  Building2,
  FileText,
  TrendingUp,
  ChevronRight,
  ExternalLink,
  Loader2,
  ShieldAlert,
  Calendar,
  Layers,
  CheckCircle2,
  CircleDot,
  Link2,
} from 'lucide-react';
import {
  fetchPropertyDetail,
  fetchPropertyRisk,
  resolveEntity,
  getOwnershipChain,
  formatCurrency,
  riskBadgeClass,
} from '@/lib/api';
import type {
  PropertyDetail,
  RiskAssessment,
  EntityResolution,
  OwnershipChain,
  OwnershipSource,
} from '@/types';

type Tab = 'summary' | 'distress' | 'owner' | 'financials' | 'location';

interface PropertyDrawerProps {
  propertyId: string | null;
  onClose: () => void;
}

export function PropertyDrawer({ propertyId, onClose }: PropertyDrawerProps) {
  const [tab, setTab] = useState<Tab>('summary');

  useEffect(() => {
    if (propertyId) setTab('summary');
  }, [propertyId]);

  const { data: property, isLoading: propLoading } = useQuery({
    queryKey: ['property', propertyId],
    queryFn: () => fetchPropertyDetail(propertyId!),
    enabled: !!propertyId,
  });

  const { data: risk } = useQuery({
    queryKey: ['risk', propertyId],
    queryFn: () => fetchPropertyRisk(propertyId!),
    enabled: !!propertyId,
  });

  const { data: entity } = useQuery({
    queryKey: ['entity', propertyId],
    queryFn: () => resolveEntity(propertyId!),
    enabled: !!propertyId,
  });

  const { data: ownershipChain } = useQuery({
    queryKey: ['ownership-chain', propertyId],
    queryFn: () => getOwnershipChain(propertyId!),
    enabled: !!propertyId,
    retry: false,
  });

  if (!propertyId) return null;

  const tabs: { id: Tab; label: string }[] = [
    { id: 'summary', label: 'Summary' },
    { id: 'owner', label: 'Owner' },
    { id: 'distress', label: 'Distress' },
    { id: 'financials', label: 'Financials' },
    { id: 'location', label: 'Location' },
  ];

  return (
    <>
      {/* Drawer - no backdrop to avoid WebGL resize */}
      <aside className="fixed right-0 top-0 bottom-0 z-40 w-[480px] bg-slate-900 border-l border-slate-700/60 flex flex-col shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="flex items-start justify-between px-5 pt-4 pb-3 border-b border-slate-700/60 shrink-0">
          {propLoading ? (
            <div className="flex items-center gap-2 text-slate-400">
              <Loader2 size={16} className="animate-spin" />
              <span className="text-sm">Loading…</span>
            </div>
          ) : property ? (
            <div className="flex-1 min-w-0 pr-3">
              <h2 className="text-base font-semibold text-slate-100 truncate">
                {property.address}
              </h2>
              <div className="flex items-center gap-1.5 mt-0.5 text-xs text-slate-400">
                <MapPin size={11} />
                <span>
                  {property.city}, {property.state} {property.zip}
                </span>
                <span className="text-slate-600">·</span>
                <span>{property.county} County</span>
              </div>
            </div>
          ) : (
            <div className="text-sm text-slate-400">Property not found</div>
          )}
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg hover:bg-slate-700 text-slate-400 hover:text-slate-200 transition-colors shrink-0"
          >
            <X size={16} />
          </button>
        </div>

        {/* Quick-answer bar */}
        {property && (
          <div className="grid grid-cols-4 divide-x divide-slate-700/60 border-b border-slate-700/60 shrink-0">
            <QuickStat
              label="Opp. Score"
              value={
                property.opportunity_score != null
                  ? property.opportunity_score.toFixed(0)
                  : '—'
              }
              sub="/ 100"
              color={scoreToColor(property.opportunity_score)}
            />
            <QuickStat
              label="Market Value"
              value={formatCurrency(property.market_value)}
              color="text-emerald-400"
            />
            <QuickStat
              label="Signals"
              value={String(property.signal_count)}
              sub="active"
              color={
                property.signal_count > 3
                  ? 'text-red-400'
                  : property.signal_count > 0
                    ? 'text-yellow-400'
                    : 'text-slate-400'
              }
            />
            <QuickStat
              label="Risk"
              value={risk?.risk_level ?? '—'}
              color={
                risk?.risk_level === 'CRITICAL'
                  ? 'text-red-400'
                  : risk?.risk_level === 'HIGH'
                    ? 'text-orange-400'
                    : risk?.risk_level === 'MEDIUM'
                      ? 'text-yellow-400'
                      : 'text-green-400'
              }
            />
          </div>
        )}

        {/* Tabs */}
        <div className="flex border-b border-slate-700/60 shrink-0 overflow-x-auto">
          {tabs.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-4 py-2.5 text-xs font-medium whitespace-nowrap transition-colors border-b-2 ${
                tab === t.id
                  ? 'border-blue-500 text-blue-400'
                  : 'border-transparent text-slate-500 hover:text-slate-300'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Tab content */}
        <div className="flex-1 overflow-y-auto">
          {propLoading ? (
            <div className="flex items-center justify-center h-40">
              <Loader2 size={24} className="animate-spin text-slate-500" />
            </div>
          ) : property ? (
            <>
              {tab === 'summary' && (
                <SummaryTab
                  property={property}
                  risk={risk}
                  ownershipChain={ownershipChain}
                />
              )}
              {tab === 'owner' && (
                <OwnerTab
                  property={property}
                  entity={entity}
                  ownershipChain={ownershipChain}
                />
              )}
              {tab === 'distress' && (
                <DistressTab property={property} risk={risk} />
              )}
              {tab === 'financials' && <FinancialsTab property={property} />}
              {tab === 'location' && <LocationTab property={property} />}
            </>
          ) : null}
        </div>
      </aside>
    </>
  );
}

// ─── Quick stat cell ──────────────────────────────────────────────────────────

function QuickStat({
  label,
  value,
  sub,
  color,
}: {
  label: string;
  value: string;
  sub?: string;
  color?: string;
}) {
  return (
    <div className="flex flex-col items-center py-2.5 px-2">
      <div className={`text-sm font-bold tabular-nums ${color ?? 'text-slate-200'}`}>
        {value}
        {sub && <span className="text-xs font-normal text-slate-500 ml-0.5">{sub}</span>}
      </div>
      <div className="text-[10px] text-slate-500 mt-0.5">{label}</div>
    </div>
  );
}

// ─── Summary Tab ──────────────────────────────────────────────────────────────

function SummaryTab({
  property,
  risk,
  ownershipChain,
}: {
  property: PropertyDetail;
  risk?: RiskAssessment;
  ownershipChain?: OwnershipChain;
}) {
  const mapsLink =
    property.lat && property.lng
      ? `https://www.google.com/maps/search/?api=1&query=${property.lat},${property.lng}`
      : `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(property.address + ', ' + property.city + ', ' + property.state)}`;

  return (
    <div className="p-4 space-y-4">
      {/* Street View placeholder */}
      <div className="relative bg-slate-800 rounded-lg overflow-hidden h-36 flex items-center justify-center">
        <div className="flex flex-col items-center gap-2 text-slate-600">
          <Building2 size={28} />
          <span className="text-xs">No street view available</span>
        </div>
        <a
          href={mapsLink}
          target="_blank"
          rel="noopener noreferrer"
          className="absolute bottom-2 right-2 bg-slate-900/80 border border-slate-600 rounded-md px-2 py-1 text-[10px] text-slate-400 hover:text-slate-200 flex items-center gap-1 backdrop-blur-sm transition-colors"
        >
          <ExternalLink size={10} />
          Maps
        </a>
      </div>

      {/* Ownership chain summary */}
      {ownershipChain && (
        <Section title="Ownership Chain">
          <OwnershipChainSummary chain={ownershipChain} compact />
        </Section>
      )}

      {/* Key metrics grid */}
      <Section title="Property Details">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
          <DL label="Parcel ID" value={property.parcel_id} />
          <DL label="Land Use" value={property.land_use} />
          <DL label="Year Built" value={property.year_built} />
          <DL
            label="Sq Ft"
            value={
              property.sqft != null ? property.sqft.toLocaleString() : null
            }
          />
          <DL
            label="Acres"
            value={property.acres != null ? property.acres.toFixed(2) : null}
          />
          <DL label="Zoning" value={property.zoning} />
        </dl>
      </Section>

      {/* Scores */}
      <Section title="Scores">
        <div className="space-y-2.5">
          <ScoreBar
            label="Opportunity"
            score={property.opportunity_score}
            color="bg-blue-500"
          />
          <ScoreBar
            label="Distress"
            score={property.distress_score}
            color="bg-orange-500"
          />
          <ScoreBar
            label="Equity"
            score={property.equity_score}
            color="bg-emerald-500"
          />
        </div>
      </Section>

      {/* Recommendation */}
      {risk?.recommendation && (
        <Section title="Recommendation">
          <div className="bg-blue-950/40 border border-blue-700/40 rounded-lg p-3">
            <p className="text-sm text-blue-200 leading-relaxed">
              {risk.recommendation}
            </p>
          </div>
        </Section>
      )}

      {/* Distress flags */}
      {(property.has_lis_pendens || property.has_foreclosure || property.tax_delinquent) && (
        <Section title="Active Flags">
          <div className="flex flex-wrap gap-2">
            {property.has_lis_pendens && (
              <Flag label="Lis Pendens" color="bg-red-900/60 text-red-300 border-red-700" />
            )}
            {property.has_foreclosure && (
              <Flag label="Foreclosure" color="bg-orange-900/60 text-orange-300 border-orange-700" />
            )}
            {property.tax_delinquent && (
              <Flag label="Tax Delinquent" color="bg-yellow-900/60 text-yellow-300 border-yellow-700" />
            )}
          </div>
        </Section>
      )}
    </div>
  );
}

// ─── Ownership chain compact/full component ──────────────────────────────────

function confidenceColor(c: number): string {
  if (c >= 0.8) return 'text-green-400';
  if (c >= 0.5) return 'text-yellow-400';
  return 'text-red-400';
}

function confidenceBg(c: number): string {
  if (c >= 0.8) return 'bg-green-900/40 border-green-700/50';
  if (c >= 0.5) return 'bg-yellow-900/40 border-yellow-700/50';
  return 'bg-red-900/40 border-red-700/50';
}

function sourceLabel(source: OwnershipSource['source']): string {
  switch (source) {
    case 'tx_comptroller':  return 'TX Comptroller';
    case 'permit_contractor': return 'Permit Contractor';
    case 'deed_grantor':    return 'Deed Record';
    case 'opencorporates':  return 'Registered Agent';
    case 'mailing_crossref': return 'Mailing Address';
  }
}

function sourceIcon(source: OwnershipSource['source']) {
  switch (source) {
    case 'tx_comptroller':  return <CheckCircle2 size={11} className="text-green-400 shrink-0" />;
    case 'permit_contractor': return <TrendingUp size={11} className="text-blue-400 shrink-0" />;
    case 'deed_grantor':    return <FileText size={11} className="text-purple-400 shrink-0" />;
    case 'opencorporates':  return <CircleDot size={11} className="text-cyan-400 shrink-0" />;
    case 'mailing_crossref': return <Link2 size={11} className="text-orange-400 shrink-0" />;
  }
}

function OwnershipChainSummary({
  chain,
  compact,
}: {
  chain: OwnershipChain;
  compact?: boolean;
}) {
  const res = chain.resolution;

  return (
    <div className="space-y-2">
      {/* Chain visualization */}
      <div className="flex items-center gap-1.5 flex-wrap">
        <div className="flex items-center gap-1.5 bg-slate-800 rounded-md px-2.5 py-1.5 text-xs text-slate-300 font-medium">
          <Building2 size={11} className="text-slate-500" />
          {chain.cad_owner}
        </div>
        {res && (
          <>
            <ChevronRight size={14} className="text-slate-600 shrink-0" />
            <div className={`flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-semibold border ${confidenceBg(res.confidence)}`}>
              <User size={11} className={confidenceColor(res.confidence)} />
              <span className={confidenceColor(res.confidence)}>
                {res.beneficial_person}
              </span>
            </div>
          </>
        )}
      </div>

      {/* Confidence badge */}
      {res && (
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-slate-500 uppercase tracking-wide">Confidence</span>
          <span className={`text-xs font-bold ${confidenceColor(res.confidence)}`}>
            {(res.confidence * 100).toFixed(0)}%
          </span>
          <span className="text-[10px] text-slate-600">
            ({res?.sources?.length} source{res?.sources?.length !== 1 ? 's' : ''})
          </span>
        </div>
      )}

      {/* Sources list (full mode) */}
      {!compact && res && res?.sources?.length > 0 && (
        <div className="space-y-1.5 mt-2">
          {res?.sources?.map((s, i) => (
            <div key={i} className="flex items-start gap-2 bg-slate-800/50 rounded px-2.5 py-2">
              {sourceIcon(s.source)}
              <div className="flex-1 min-w-0">
                <div className="text-xs font-medium text-slate-200">{s.name}</div>
                <div className="text-[10px] text-slate-500 flex items-center gap-1.5 mt-0.5 flex-wrap">
                  <span className="text-slate-600">{sourceLabel(s.source)}</span>
                  {s.role && <span>· {s.role}</span>}
                  {s.permits != null && <span>· {s.permits} permit{s.permits !== 1 ? 's' : ''}</span>}
                  {s.deed_date && <span>· {new Date(s.deed_date).toLocaleDateString()}</span>}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Owner Tab ────────────────────────────────────────────────────────────────

function OwnerTab({
  property,
  entity,
  ownershipChain,
}: {
  property: PropertyDetail;
  entity?: EntityResolution;
  ownershipChain?: OwnershipChain;
}) {
  const owner = (property as any).current_owner || (property as any).owner;

  return (
    <div className="p-4 space-y-4">
      {/* CAD Direct Owner */}
      <Section title="CAD Owner (Direct)">
        {owner ? (
          <div className="bg-slate-800/60 rounded-lg p-3 space-y-2">
            <div className="flex items-center gap-2">
              <Building2 size={14} className="text-slate-400" />
              <span className="text-sm font-semibold text-slate-200">
                {owner.raw_name || owner.name || owner.display_name || 'Unknown'}
              </span>
              {(owner.party_type || owner.owner_type) && (
                <span className="ml-auto text-[10px] bg-slate-700 text-slate-400 rounded px-1.5 py-0.5">
                  {owner.party_type || owner.owner_type}
                </span>
              )}
            </div>
            <dl className="grid grid-cols-2 gap-2 mt-2">
              <DL label="Type" value={owner.party_type || owner.owner_type} />
              <DL label="Mailing" value={owner.mailing_address} />
              <DL label="State" value={owner.mailing_state || owner.state_of_formation} />
              <DL label="Out-of-State" value={owner.out_of_state ? 'Yes' : 'No'} />
            </dl>
            {owner.total_market_value != null && (
              <div className="mt-2 pt-2 border-t border-slate-700/60 grid grid-cols-2 gap-2">
                <DL
                  label="Portfolio Value"
                  value={formatCurrency(owner.total_market_value)}
                />
                <DL
                  label="Avg Distress"
                  value={owner.avg_distress_score?.toFixed(1)}
                />
              </div>
            )}
          </div>
        ) : (
          <div className="text-sm text-slate-500 bg-slate-800/60 rounded-lg p-3">
            {property.owner_name ?? 'Unknown owner'}
          </div>
        )}
      </Section>

      {/* Beneficial Ownership Resolution */}
      {ownershipChain ? (
        <Section title="Beneficial Owner Resolution">
          {ownershipChain.resolution ? (
            <div className="space-y-3">
              {/* Resolved person card */}
              <div className={`rounded-lg p-3 border ${confidenceBg(ownershipChain.resolution.confidence)}`}>
                <div className="flex items-center gap-2 mb-2">
                  <User size={14} className={confidenceColor(ownershipChain.resolution.confidence)} />
                  <span className={`text-sm font-bold ${confidenceColor(ownershipChain.resolution.confidence)}`}>
                    {ownershipChain.resolution.beneficial_person}
                  </span>
                  <span className={`ml-auto text-xs font-semibold ${confidenceColor(ownershipChain.resolution.confidence)}`}>
                    {(ownershipChain.resolution.confidence * 100).toFixed(0)}% confidence
                  </span>
                </div>
                <div className="text-[10px] text-slate-400">
                  Resolved from {ownershipChain.resolution.sources.length} independent source
                  {ownershipChain.resolution.sources.length !== 1 ? 's' : ''}
                </div>
              </div>

              {/* Evidence sources */}
              <div className="space-y-1.5">
                <div className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Evidence Sources</div>
                {ownershipChain.resolution.sources.map((s, i) => (
                  <div key={i} className="flex items-start gap-2 bg-slate-800/50 rounded-lg px-3 py-2.5">
                    {sourceIcon(s.source)}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-semibold text-slate-200">{s.name}</span>
                        <span className="text-[10px] text-slate-500 bg-slate-700/60 rounded px-1.5 py-0.5">
                          {sourceLabel(s.source)}
                        </span>
                      </div>
                      <div className="text-[10px] text-slate-500 mt-0.5 flex flex-wrap gap-1.5">
                        {s.role && <span>Role: {s.role}</span>}
                        {s.entity && <span>Entity: {s.entity}</span>}
                        {s.permits != null && <span>{s.permits} permit{s.permits !== 1 ? 's' : ''} filed</span>}
                        {s.deed_date && <span>Deed: {new Date(s.deed_date).toLocaleDateString()}</span>}
                        {s.mailing_address && <span>Address match</span>}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="text-sm text-slate-500 bg-slate-800/40 rounded-lg p-3 text-center">
              No beneficial owner resolved — insufficient evidence
            </div>
          )}
        </Section>
      ) : null}

      {/* Entity control chain (legacy resolver) */}
      {entity && entity.chain?.length > 0 && (
        <Section title="Control Chain">
          <div className="flex items-center gap-2 mb-3">
            <span className="text-xs text-slate-500">Confidence:</span>
            <span className="text-xs font-semibold text-slate-200">
              {(entity.confidence * 100).toFixed(0)}%
            </span>
          </div>
          {entity.controlling_entity && (
            <div className="bg-blue-950/40 border border-blue-700/40 rounded-lg p-3 mb-3">
              <div className="text-[10px] text-blue-400 uppercase tracking-wider mb-1">
                Controlling Entity
              </div>
              <div className="text-sm font-semibold text-blue-200">
                {entity.controlling_entity}
              </div>
            </div>
          )}
          <div className="space-y-1">
            {entity.chain?.map((node, i) => (
              <div
                key={node.id}
                className="flex items-center gap-2"
                style={{ paddingLeft: `${node.depth * 16}px` }}
              >
                {i > 0 && (
                  <ChevronRight size={12} className="text-slate-600 shrink-0" />
                )}
                <div className="flex-1 flex items-center gap-2 bg-slate-800/50 rounded px-2.5 py-1.5">
                  <Building2 size={11} className="text-slate-500 shrink-0" />
                  <span className="text-xs text-slate-300">{node.name}</span>
                  <span className="ml-auto text-[10px] text-slate-600">
                    {node.role}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Related LLCs */}
      {ownershipChain && ownershipChain.related_entities?.length > 0 && (
        <Section title={`Related Entities (${ownershipChain.related_entities?.length})`}>
          <div className="space-y-1">
            {ownershipChain.related_entities?.map((name, i) => (
              <div key={i} className="flex items-center gap-2 bg-slate-800/50 rounded px-2.5 py-1.5">
                <Building2 size={11} className="text-slate-500 shrink-0" />
                <span className="text-xs text-slate-300">{name}</span>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Portfolio stats */}
      {ownershipChain && (
        <Section title="Portfolio (This Owner)">
          <div className="grid grid-cols-2 gap-3">
            <div className="bg-slate-800/60 rounded-lg p-3 text-center">
              <div className="text-xl font-bold text-slate-100">
                {ownershipChain.total_portfolio.properties}
              </div>
              <div className="text-[10px] text-slate-500 mt-0.5 uppercase tracking-wide">Properties</div>
            </div>
            <div className="bg-slate-800/60 rounded-lg p-3 text-center">
              <div className="text-xl font-bold text-emerald-400">
                {formatCurrency(ownershipChain.total_portfolio.total_value)}
              </div>
              <div className="text-[10px] text-slate-500 mt-0.5 uppercase tracking-wide">Total Value</div>
            </div>
          </div>
        </Section>
      )}

      {/* Portfolio sample (legacy) */}
      {owner?.properties && owner.properties.length > 0 && (
        <Section title={`Portfolio Sample (${owner.portfolio_count} total)`}>
          <div className="space-y-1.5">
            {owner.properties.slice(0, 5).map((p) => (
              <div
                key={p.id}
                className="flex items-center gap-2 bg-slate-800/50 rounded px-2.5 py-2"
              >
                <Building2 size={11} className="text-slate-500 shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-slate-300 truncate">
                    {p.address}
                  </div>
                  <div className="text-[10px] text-slate-500">{p.city}</div>
                </div>
                <span className="text-xs text-slate-400 tabular-nums">
                  {formatCurrency(p.market_value)}
                </span>
              </div>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}

// ─── Distress Tab ─────────────────────────────────────────────────────────────

function DistressTab({
  property,
  risk,
}: {
  property: PropertyDetail;
  risk?: RiskAssessment;
}) {
  return (
    <div className="p-4 space-y-4">
      {/* Risk assessment */}
      {risk && (
        <Section title="Pre-Foreclosure Risk Assessment">
          <div className="flex items-center gap-3 mb-3">
            <span
              className={`text-sm font-bold px-3 py-1.5 rounded-lg border ${riskBadgeClass(risk.risk_level)}`}
            >
              {risk.risk_level}
            </span>
            <span className="text-sm text-slate-400">
              Score: <span className="text-slate-200 font-semibold">{risk.overall_score.toFixed(1)}</span>
            </span>
          </div>
          {/* Tier breakdown */}
          <div className="grid grid-cols-2 gap-2 mb-3">
            {[
              { label: 'Distress Score', val: risk.components.distress_score?.toFixed(1) },
              { label: 'Equity Score', val: risk.components.equity_score?.toFixed(1) },
              { label: 'Debt Maturity', val: risk.components.debt_maturity_risk },
              { label: 'Active Signals', val: String(risk.components.signal_count) },
            ].map(({ label, val }) => (
              <div key={label} className="bg-slate-800/60 rounded-lg p-2.5">
                <div className="text-[10px] text-slate-500 uppercase tracking-wide mb-1">{label}</div>
                <div className="text-sm font-semibold text-slate-200">{val ?? '—'}</div>
              </div>
            ))}
          </div>
          {/* Flag pills */}
          <div className="flex flex-wrap gap-2">
            {risk.components.lis_pendens && (
              <Flag label="Lis Pendens" color="bg-red-900/60 text-red-300 border-red-700" />
            )}
            {risk.components.foreclosure && (
              <Flag label="Foreclosure" color="bg-orange-900/60 text-orange-300 border-orange-700" />
            )}
            {risk.components.tax_delinquent && (
              <Flag label="Tax Delinquent" color="bg-yellow-900/60 text-yellow-300 border-yellow-700" />
            )}
          </div>
        </Section>
      )}

      {/* Signals */}
      {property.signals?.length > 0 && (
        <Section title={`Distress Signals (${property.signals?.length})`}>
          <div className="space-y-2">
            {property.signals?.map((s, i) => (
              <div
                key={i}
                className="flex gap-3 bg-slate-800/60 rounded-lg p-3"
              >
                <ShieldAlert
                  size={14}
                  className={
                    s.severity === 'CRITICAL'
                      ? 'text-red-400 mt-0.5 shrink-0'
                      : s.severity === 'HIGH'
                        ? 'text-orange-400 mt-0.5 shrink-0'
                        : 'text-yellow-400 mt-0.5 shrink-0'
                  }
                />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-0.5">
                    <span className="text-xs font-semibold text-slate-200">
                      {s.signal_type}
                    </span>
                    <span
                      className={`text-[10px] px-1.5 py-0.5 rounded border ${riskBadgeClass(s.severity)}`}
                    >
                      {s.severity}
                    </span>
                  </div>
                  <p className="text-xs text-slate-400">{s.description}</p>
                  <div className="text-[10px] text-slate-600 mt-1">
                    {new Date(s.detected_at).toLocaleDateString()} · {s.source}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Court filings / liens / UCC */}
      {property.filings?.length > 0 && (
        <Section title={`Court Records, Liens & Filings (${property.filings?.length})`}>
          <div className="space-y-2">
            {property.filings?.map((f) => (
              <div
                key={f.id}
                className="bg-slate-800/60 rounded-lg p-3 space-y-1"
              >
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold text-slate-200">
                    {f.filing_type}
                  </span>
                  {f.status && (
                    <span className="text-[10px] bg-slate-700 text-slate-400 rounded px-1.5 py-0.5">
                      {f.status}
                    </span>
                  )}
                </div>
                {f.case_number && (
                  <div className="text-xs text-slate-500">
                    Case #{f.case_number}
                  </div>
                )}
                {(f.plaintiff || f.defendant) && (
                  <div className="text-xs text-slate-400">
                    {f.plaintiff && <span>Plaintiff: {f.plaintiff}</span>}
                    {f.plaintiff && f.defendant && ' · '}
                    {f.defendant && <span>Defendant: {f.defendant}</span>}
                  </div>
                )}
                <div className="flex items-center gap-3 text-[10px] text-slate-600">
                  {f.filed_date && (
                    <span>{new Date(f.filed_date).toLocaleDateString()}</span>
                  )}
                  {f.amount != null && (
                    <span>{formatCurrency(f.amount)}</span>
                  )}
                  {f.court && <span>{f.court}</span>}
                </div>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Tax status */}
      <Section title="Tax Status">
        <div className="bg-slate-800/60 rounded-lg p-3 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">Delinquent</span>
            <span className={`text-xs font-semibold ${property.tax_delinquent ? 'text-yellow-400' : 'text-green-400'}`}>
              {property.tax_delinquent ? 'YES' : 'No'}
            </span>
          </div>
          {property.valuations && property.valuations?.length > 0 && (
            <div className="pt-2 border-t border-slate-700/60 space-y-1">
              {property.valuations.slice(0, 3).map((v) => (
                <div key={v.id} className="flex items-center justify-between text-xs">
                  <span className="text-slate-500">{v.valuation_type} {v.valuation_date ? `(${new Date(v.valuation_date).getFullYear()})` : ''}</span>
                  <span className="text-slate-300 font-medium">{formatCurrency(v.value)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="text-[10px] text-slate-600 mt-1">Source: County CAD / Appraisal District</div>
      </Section>

      {property.signals?.length === 0 &&
        property.filings?.length === 0 &&
        !risk && (
          <div className="text-center py-12 text-slate-600 text-sm">
            No distress signals recorded
          </div>
        )}
    </div>
  );
}

// ─── Financials Tab ───────────────────────────────────────────────────────────

function FinancialsTab({ property }: { property: PropertyDetail }) {
  return (
    <div className="p-4 space-y-4">
      {/* Valuation */}
      <Section title="Current Valuations">
        <dl className="grid grid-cols-2 gap-3">
          <ValuationCard
            label="Assessed Value"
            value={property.assessed_value}
          />
          <ValuationCard label="Market Value" value={property.market_value} />
        </dl>
      </Section>

      {/* Valuation history */}
      {property.valuations?.length > 0 && (
        <Section title={`Valuation History (${property.valuations?.length} records)`}>
          <div className="space-y-2">
            {property.valuations
              .slice()
              .sort((a, b) => {
                const da = a.valuation_date ? new Date(a.valuation_date).getTime() : 0;
                const db = b.valuation_date ? new Date(b.valuation_date).getTime() : 0;
                return db - da;
              })
              .map((v) => (
                <div
                  key={v.id}
                  className="flex items-center justify-between bg-slate-800/50 rounded px-3 py-2"
                >
                  <div>
                    <div className="text-xs font-medium text-slate-300">
                      {v.valuation_type}
                    </div>
                    {v.valuation_date && (
                      <div className="text-[10px] text-slate-500">
                        {new Date(v.valuation_date).toLocaleDateString()}
                      </div>
                    )}
                    {v.source && (
                      <div className="text-[10px] text-slate-600">{v.source}</div>
                    )}
                  </div>
                  <span className="text-sm font-semibold text-emerald-400 tabular-nums">
                    {formatCurrency(v.value)}
                  </span>
                </div>
              ))}
          </div>
        </Section>
      )}

      {/* Debt instruments */}
      {property.debts?.length > 0 && (
        <Section title={`Debt Instruments (${property.debts?.length})`}>
          <div className="space-y-3">
            {property.debts
              .slice()
              .sort((a, b) => {
                const da = a.origination_date
                  ? new Date(a.origination_date).getTime()
                  : 0;
                const db = b.origination_date
                  ? new Date(b.origination_date).getTime()
                  : 0;
                return db - da;
              })
              .map((debt) => (
                <div
                  key={debt.id}
                  className="bg-slate-800/60 rounded-lg p-3 space-y-2"
                >
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-semibold text-slate-200">
                      {debt.lender ?? 'Unknown Lender'}
                    </span>
                    <span
                      className={`text-xs px-2 py-0.5 rounded border ${riskBadgeClass(
                        debt.maturity_analysis?.maturity_risk ?? null
                      )}`}
                    >
                      {debt.status ?? 'Active'}
                    </span>
                  </div>
                  <dl className="grid grid-cols-2 gap-1.5">
                    <DL label="Amount" value={formatCurrency(debt.loan_amount)} />
                    <DL label="Type" value={debt.loan_type} />
                    <DL
                      label="Originated"
                      value={
                        debt.origination_date
                          ? new Date(debt.origination_date).toLocaleDateString()
                          : null
                      }
                    />
                    <DL
                      label="Maturity"
                      value={
                        debt.maturity_date
                          ? new Date(debt.maturity_date).toLocaleDateString()
                          : null
                      }
                    />
                    {debt.interest_rate != null && (
                      <DL
                        label="Rate"
                        value={`${(debt.interest_rate * 100).toFixed(2)}%`}
                      />
                    )}
                    <DL label="Lien Position" value={debt.lien_position} />
                  </dl>
                  {debt.maturity_analysis && (
                    <div className="pt-2 border-t border-slate-700/60">
                      <div className="flex items-center gap-2">
                        <Calendar size={11} className="text-slate-500" />
                        <span className="text-xs text-slate-400">
                          {debt.maturity_analysis.is_matured
                            ? 'MATURED'
                            : debt.maturity_analysis.days_to_maturity != null
                              ? `${debt.maturity_analysis.days_to_maturity} days to maturity`
                              : 'Maturity unknown'}
                        </span>
                        {debt.maturity_analysis.maturity_risk && (
                          <span
                            className={`ml-auto text-[10px] px-1.5 py-0.5 rounded border ${riskBadgeClass(
                              debt.maturity_analysis.maturity_risk
                            )}`}
                          >
                            {debt.maturity_analysis.maturity_risk}
                          </span>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              ))}
          </div>
        </Section>
      )}

      {/* Deed history timeline */}
      {property.documents?.length > 0 && (
        <Section title={`Deed / Instrument History (${property.documents?.length})`}>
          <div className="relative pl-4">
            <div className="absolute left-1.5 top-0 bottom-0 w-px bg-slate-700" />
            {property.documents
              .slice()
              .sort((a, b) => {
                const da = a.recorded_date
                  ? new Date(a.recorded_date).getTime()
                  : 0;
                const db = b.recorded_date
                  ? new Date(b.recorded_date).getTime()
                  : 0;
                return db - da;
              })
              .map((doc) => (
                <div key={doc.id} className="relative mb-3 last:mb-0">
                  <div className="absolute -left-[11px] top-2 w-2 h-2 rounded-full bg-blue-500 border-2 border-slate-900" />
                  <div className="bg-slate-800/50 rounded-lg p-2.5 ml-2">
                    <div className="flex items-center gap-2 mb-1">
                      <FileText size={11} className="text-slate-500" />
                      <span className="text-xs font-medium text-slate-200">
                        {doc.doc_type}
                      </span>
                      {doc.recorded_date && (
                        <span className="ml-auto text-[10px] text-slate-500">
                          {new Date(doc.recorded_date).toLocaleDateString()}
                        </span>
                      )}
                    </div>
                    {(doc.grantor || doc.grantee) && (
                      <div className="text-[10px] text-slate-500">
                        {doc.grantor && <span>From: {doc.grantor}</span>}
                        {doc.grantor && doc.grantee && ' → '}
                        {doc.grantee && <span>{doc.grantee}</span>}
                      </div>
                    )}
                    {doc.amount != null && (
                      <div className="text-[10px] text-emerald-500 mt-0.5">
                        {formatCurrency(doc.amount)}
                      </div>
                    )}
                    {doc.instrument_number && (
                      <div className="text-[10px] text-slate-600 mt-0.5">
                        Instrument #{doc.instrument_number}
                      </div>
                    )}
                  </div>
                </div>
              ))}
          </div>
        </Section>
      )}
    </div>
  );
}

// ─── Location Tab ─────────────────────────────────────────────────────────────

function LocationTab({ property }: { property: PropertyDetail }) {
  const mapEmbedUrl =
    property.lat && property.lng
      ? `https://maps.google.com/maps?q=${property.lat},${property.lng}&z=16&output=embed`
      : null;

  return (
    <div className="p-4 space-y-4">
      {/* Embedded map */}
      <div className="bg-slate-800 rounded-lg overflow-hidden h-48">
        {mapEmbedUrl ? (
          <iframe
            src={mapEmbedUrl}
            className="w-full h-full border-0"
            loading="lazy"
            title="Property Map"
          />
        ) : (
          <div className="flex items-center justify-center h-full text-slate-600">
            <div className="text-center">
              <MapPin size={28} className="mx-auto mb-2" />
              <div className="text-xs">No coordinates available</div>
            </div>
          </div>
        )}
      </div>

      {/* Location data */}
      <Section title="Location Details">
        <dl className="grid grid-cols-2 gap-2">
          <DL label="Address" value={property.address} />
          <DL label="City" value={property.city} />
          <DL label="State" value={property.state} />
          <DL label="ZIP" value={property.zip} />
          <DL label="County" value={property.county} />
          <DL label="Zoning" value={property.zoning} />
          <DL label="Land Use" value={property.land_use} />
          <DL label="Latitude" value={property.lat?.toFixed(6)} />
          <DL label="Longitude" value={property.lng?.toFixed(6)} />
          <DL label="Acres" value={property.acres != null ? property.acres.toFixed(2) : null} />
        </dl>
      </Section>

      {/* Zoning, Flood, Transit, TIRZ, Opportunity Zone */}
      <Section title="Overlays & Zones">
        <div className="grid grid-cols-2 gap-2">
          {property.zoning && (
            <div className="flex items-center gap-1.5 bg-slate-800 rounded-lg px-3 py-2">
              <Layers size={12} className="text-blue-400 shrink-0" />
              <div>
                <div className="text-[10px] text-slate-500">Zoning</div>
                <div className="text-xs text-slate-300">{property.zoning}</div>
              </div>
            </div>
          )}
          {property.flood_risk && (
            <div className="flex items-center gap-1.5 bg-slate-800 rounded-lg px-3 py-2">
              <AlertTriangle size={12} className={property.flood_risk === 'HIGH' ? 'text-red-400 shrink-0' : 'text-slate-400 shrink-0'} />
              <div>
                <div className="text-[10px] text-slate-500">Flood Risk</div>
                <div className="text-xs text-slate-300">{property.flood_risk}</div>
              </div>
            </div>
          )}
          {property.in_opportunity_zone && (
            <div className="flex items-center gap-1.5 bg-emerald-900/40 border border-emerald-700/50 rounded-lg px-3 py-2">
              <CheckCircle2 size={12} className="text-emerald-400 shrink-0" />
              <div className="text-xs text-emerald-300 font-medium">Opportunity Zone</div>
            </div>
          )}
          {property.in_tirz && (
            <div className="flex items-center gap-1.5 bg-purple-900/40 border border-purple-700/50 rounded-lg px-3 py-2">
              <CheckCircle2 size={12} className="text-purple-400 shrink-0" />
              <div>
                <div className="text-[10px] text-purple-400">TIRZ District</div>
                {property.tirz_name && <div className="text-xs text-purple-300">{property.tirz_name}</div>}
              </div>
            </div>
          )}
          {property.nearest_transit_stop && (
            <div className="col-span-2 flex items-center gap-1.5 bg-slate-800 rounded-lg px-3 py-2">
              <MapPin size={12} className="text-blue-400 shrink-0" />
              <div>
                <div className="text-[10px] text-slate-500">Nearest Transit</div>
                <div className="text-xs text-slate-300">
                  {property.nearest_transit_stop}
                  {property.distance_to_transit_ft != null && (
                    <span className="text-slate-500 ml-1.5">
                      {(property.distance_to_transit_ft / 5280).toFixed(2)} mi
                    </span>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      </Section>

      {/* Census tract */}
      {property.census_tract && (
        <Section title="Census / Infrastructure">
          <dl className="grid grid-cols-2 gap-2">
            <DL label="Census Tract" value={property.census_tract} />
            {property.nearest_highway && (
              <DL
                label="Nearest Highway"
                value={`${property.nearest_highway}${property.distance_to_highway_ft != null ? ` (${(property.distance_to_highway_ft / 5280).toFixed(2)} mi)` : ''}`}
              />
            )}
            {property.highway_aadt != null && (
              <DL label="AADT" value={property.highway_aadt.toLocaleString()} />
            )}
            {property.flood_zone && (
              <DL label="Flood Zone" value={property.flood_zone} />
            )}
          </dl>
        </Section>
      )}

      {/* External Links */}
      <Section title="External Links">
        <div className="flex flex-wrap gap-2">
          {property.lat && property.lng && (
            <ExternalLinkBtn
              href={`https://www.google.com/maps/search/?api=1&query=${property.lat},${property.lng}`}
              label="Google Maps"
            />
          )}
          <ExternalLinkBtn
            href={`https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(
              `${property.address}, ${property.city}, ${property.state} ${property.zip}`
            )}`}
            label="Street View"
          />
          {property.lat && property.lng && (
            <ExternalLinkBtn
              href={`https://flood.beta.syr.edu/?lat=${property.lat}&lng=${property.lng}`}
              label="Flood Zone"
            />
          )}
          <ExternalLinkBtn
            href={`https://efts.sec.gov/LATEST/search-index?q="${encodeURIComponent(property.owner_name ?? '')}"&dateRange=custom&startdt=2015-01-01`}
            label="SEC Filings"
          />
        </div>
      </Section>
    </div>
  );
}

// ─── Small reusable components ────────────────────────────────────────────────

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <h3 className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 mb-2">
        {title}
      </h3>
      {children}
    </div>
  );
}

function DL({
  label,
  value,
}: {
  label: string;
  value: string | number | null | undefined;
}) {
  return (
    <div>
      <dt className="text-[10px] text-slate-600 uppercase tracking-wide">
        {label}
      </dt>
      <dd className="text-xs text-slate-300 font-medium truncate">
        {value != null && value !== '' ? String(value) : '—'}
      </dd>
    </div>
  );
}

function Flag({ label, color }: { label: string; color: string }) {
  return (
    <span
      className={`flex items-center gap-1 text-xs px-2 py-1 rounded-md border font-medium ${color}`}
    >
      <AlertTriangle size={11} />
      {label}
    </span>
  );
}

function ScoreBar({
  label,
  score,
  color,
}: {
  label: string;
  score: number | null | undefined;
  color: string;
}) {
  const pct = score != null ? Math.min(100, Math.max(0, score)) : 0;
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs text-slate-400">{label}</span>
        <span className="text-xs font-semibold text-slate-200 tabular-nums">
          {score != null ? score.toFixed(1) : '—'}
        </span>
      </div>
      <div className="h-1.5 bg-slate-700 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function ValuationCard({
  label,
  value,
}: {
  label: string;
  value: number | null | undefined;
}) {
  return (
    <div className="bg-slate-800/60 rounded-lg p-3 flex flex-col gap-1">
      <div className="flex items-center gap-1.5">
        <DollarSign size={12} className="text-emerald-400" />
        <span className="text-[10px] text-slate-500 uppercase tracking-wide">
          {label}
        </span>
      </div>
      <span className="text-lg font-bold text-emerald-400 tabular-nums">
        {formatCurrency(value ?? null)}
      </span>
    </div>
  );
}

function ExternalLinkBtn({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="flex items-center gap-1.5 bg-slate-800 border border-slate-600 rounded-lg px-2.5 py-1.5 text-xs text-slate-400 hover:text-slate-200 hover:border-slate-500 transition-colors"
    >
      <ExternalLink size={11} />
      {label}
    </a>
  );
}

function scoreToColor(score: number | null | undefined): string {
  if (score == null) return 'text-slate-400';
  if (score >= 70) return 'text-red-400';
  if (score >= 50) return 'text-orange-400';
  if (score >= 30) return 'text-yellow-400';
  return 'text-green-400';
}
