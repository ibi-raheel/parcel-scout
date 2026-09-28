'use client';

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Building2,
  AlertTriangle,
  ArrowUpDown,
  ChevronUp,
  ChevronDown,
} from 'lucide-react';
import { fetchProperties, formatCurrency, riskBadgeClass } from '@/lib/api';
import type { FilterState, PropertySummary } from '@/types';

type SortKey = 'opportunity_score' | 'market_value' | 'distress_score' | 'address';
type SortDir = 'asc' | 'desc';

interface PropertyListProps {
  filters: FilterState;
  onSelectProperty: (id: string) => void;
  selectedId: string | null;
}

export function PropertyList({
  filters,
  onSelectProperty,
  selectedId,
}: PropertyListProps) {
  const [sortKey, setSortKey] = useState<SortKey>('opportunity_score');
  const [sortDir, setSortDir] = useState<SortDir>('desc');

  const { data, isLoading, error } = useQuery({
    queryKey: ['properties', filters],
    queryFn: () => fetchProperties(filters),
    staleTime: 30_000,
  });

  const rows = useMemo<PropertySummary[]>(() => {
    if (!data?.features) return [];
    const props = data.features.map((f) => f.properties);
    return [...props].sort((a, b) => {
      const av = a[sortKey] ?? (sortDir === 'desc' ? -Infinity : Infinity);
      const bv = b[sortKey] ?? (sortDir === 'desc' ? -Infinity : Infinity);
      if (typeof av === 'string' && typeof bv === 'string') {
        return sortDir === 'asc'
          ? av.localeCompare(bv)
          : bv.localeCompare(av);
      }
      return sortDir === 'asc'
        ? (av as number) - (bv as number)
        : (bv as number) - (av as number);
    });
  }, [data, sortKey, sortDir]);

  function handleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('desc');
    }
  }

  function SortIcon({ col }: { col: SortKey }) {
    if (col !== sortKey)
      return <ArrowUpDown size={11} className="text-slate-600" />;
    return sortDir === 'asc' ? (
      <ChevronUp size={11} className="text-blue-400" />
    ) : (
      <ChevronDown size={11} className="text-blue-400" />
    );
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center flex-1 text-slate-500 text-sm">
        Loading parcels…
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center flex-1 text-red-400 text-sm">
        Error: {(error as Error).message}
      </div>
    );
  }

  return (
    <div className="flex flex-col flex-1 min-h-0 overflow-hidden">
      {/* Table header */}
      <div className="grid grid-cols-[minmax(0,2fr)_1fr_1fr_1fr_1fr_100px] gap-2 px-4 py-2 bg-slate-800/80 border-b border-slate-700/60 text-[10px] font-semibold uppercase tracking-wider text-slate-500 sticky top-0 z-10 shrink-0">
        <button
          className="flex items-center gap-1 text-left hover:text-slate-300 transition-colors"
          onClick={() => handleSort('address')}
        >
          Address <SortIcon col="address" />
        </button>
        <span>Owner</span>
        <button
          className="flex items-center gap-1 hover:text-slate-300 transition-colors"
          onClick={() => handleSort('market_value')}
        >
          Value <SortIcon col="market_value" />
        </button>
        <button
          className="flex items-center gap-1 hover:text-slate-300 transition-colors"
          onClick={() => handleSort('opportunity_score')}
        >
          Score <SortIcon col="opportunity_score" />
        </button>
        <button
          className="flex items-center gap-1 hover:text-slate-300 transition-colors"
          onClick={() => handleSort('distress_score')}
        >
          Distress <SortIcon col="distress_score" />
        </button>
        <span>Flags</span>
      </div>

      {/* Table body */}
      <div className="flex-1 overflow-y-auto">
        {rows.map((p) => (
          <button
            key={p.id}
            onClick={() => onSelectProperty(p.id)}
            className={`w-full grid grid-cols-[minmax(0,2fr)_1fr_1fr_1fr_1fr_100px] gap-2 px-4 py-2.5 text-left border-b border-slate-700/30 transition-colors hover:bg-slate-800/60 ${
              selectedId === p.id ? 'bg-blue-950/30 border-l-2 border-l-blue-500' : ''
            }`}
          >
            {/* Address */}
            <div className="min-w-0">
              <div className="flex items-center gap-1.5">
                <Building2 size={12} className="text-slate-500 shrink-0" />
                <span className="text-xs text-slate-200 truncate font-medium">
                  {p.address}
                </span>
              </div>
              <div className="text-[10px] text-slate-500 truncate pl-4">
                {p.city}, {p.county}
              </div>
            </div>

            {/* Owner */}
            <div className="min-w-0 self-center">
              <span className="text-xs text-slate-400 truncate block">
                {p.owner_name ?? '—'}
              </span>
            </div>

            {/* Value */}
            <div className="self-center">
              <span className="text-xs text-emerald-400 font-semibold tabular-nums">
                {formatCurrency(p.market_value)}
              </span>
            </div>

            {/* Opportunity score */}
            <div className="self-center">
              <ScoreChip score={p.opportunity_score} />
            </div>

            {/* Distress score */}
            <div className="self-center">
              <ScoreChip score={p.distress_score} distress />
            </div>

            {/* Flags */}
            <div className="flex flex-wrap gap-1 self-center">
              {p.has_lis_pendens && (
                <span className="text-[9px] bg-red-900/50 text-red-400 border border-red-800 rounded px-1 py-0.5">
                  LP
                </span>
              )}
              {p.has_foreclosure && (
                <span className="text-[9px] bg-orange-900/50 text-orange-400 border border-orange-800 rounded px-1 py-0.5">
                  FC
                </span>
              )}
              {p.tax_delinquent && (
                <span className="text-[9px] bg-yellow-900/50 text-yellow-400 border border-yellow-800 rounded px-1 py-0.5">
                  TD
                </span>
              )}
              {p.signal_count > 0 && (
                <span className="text-[9px] bg-slate-700 text-slate-400 rounded px-1 py-0.5 flex items-center gap-0.5">
                  <AlertTriangle size={8} />
                  {p.signal_count}
                </span>
              )}
            </div>
          </button>
        ))}

        {rows.length === 0 && (
          <div className="flex items-center justify-center py-20 text-slate-600 text-sm">
            No parcels match current filters
          </div>
        )}
      </div>

      {/* Footer count */}
      <div className="px-4 py-1.5 border-t border-slate-700/60 text-xs text-slate-600 shrink-0">
        Showing {rows.length.toLocaleString()} parcels
      </div>
    </div>
  );
}

function ScoreChip({
  score,
  distress,
}: {
  score: number | null | undefined;
  distress?: boolean;
}) {
  if (score == null)
    return <span className="text-xs text-slate-600">—</span>;

  let colorClass: string;
  if (distress) {
    colorClass =
      score >= 70
        ? 'text-red-400'
        : score >= 40
          ? 'text-orange-400'
          : 'text-slate-400';
  } else {
    colorClass =
      score >= 70
        ? 'text-red-400'
        : score >= 50
          ? 'text-yellow-400'
          : score >= 30
            ? 'text-emerald-400'
            : 'text-blue-400';
  }

  return (
    <span className={`text-xs font-semibold tabular-nums ${colorClass}`}>
      {score.toFixed(0)}
    </span>
  );
}

// suppress unused warning
const _u = riskBadgeClass;
void _u;
