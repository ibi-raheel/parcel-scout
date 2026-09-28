'use client';

import { useState } from 'react';
import {
  SlidersHorizontal,
  ChevronDown,
  ChevronUp,
  X,
  RotateCcw,
} from 'lucide-react';
import type { FilterState } from '@/types';

const COUNTIES = [
  'Harris',
  'Dallas',
  'Tarrant',
  'Bexar',
  'Travis',
  'Collin',
  'Denton',
  'Fort Bend',
  'Williamson',
  'Montgomery',
];

interface FiltersProps {
  filters: FilterState;
  onChange: (f: FilterState) => void;
  isOpen: boolean;
  onToggle: () => void;
}

function activeFilterCount(f: FilterState): number {
  let n = 0;
  if (f.counties.length) n++;
  if (f.min_value != null || f.max_value != null) n++;
  if (f.min_score != null || f.max_score != null) n++;
  if (f.has_lis_pendens) n++;
  if (f.has_foreclosure) n++;
  if (f.tax_delinquent) n++;
  if (f.land_use) n++;
  return n;
}

const DEFAULT_FILTERS: FilterState = {
  counties: ['Collin'],
  min_value: null,
  max_value: null,
  min_score: null,
  max_score: null,
  has_lis_pendens: null,
  has_foreclosure: null,
  tax_delinquent: null,
  land_use: null,
  limit: 5000,
};

export function Filters({ filters, onChange, isOpen, onToggle }: FiltersProps) {
  const [countyExpanded, setCountyExpanded] = useState(true);
  const [valueExpanded, setValueExpanded] = useState(true);
  const [distressExpanded, setDistressExpanded] = useState(true);
  const [scoreExpanded, setScoreExpanded] = useState(false);

  const count = activeFilterCount(filters);

  function toggleCounty(county: string) {
    const next = filters.counties.includes(county)
      ? filters.counties.filter((c) => c !== county)
      : [...filters.counties, county];
    onChange({ ...filters, counties: next });
  }

  function setNum(
    field: 'min_value' | 'max_value' | 'min_score' | 'max_score',
    raw: string
  ) {
    const v = raw === '' ? null : Number(raw);
    onChange({ ...filters, [field]: v });
  }

  function toggleBool(
    field: 'has_lis_pendens' | 'has_foreclosure' | 'tax_delinquent'
  ) {
    const cur = filters[field];
    onChange({ ...filters, [field]: cur ? null : true });
  }

  if (!isOpen) {
    return (
      <button
        onClick={onToggle}
        className="absolute left-0 top-1/2 -translate-y-1/2 z-10 bg-slate-800 border border-slate-600 rounded-r-lg p-2 hover:bg-slate-700 transition-colors"
        title="Open filters"
      >
        <SlidersHorizontal size={16} className="text-slate-300" />
        {count > 0 && (
          <span className="absolute -top-1.5 -right-1.5 bg-blue-500 text-white text-[10px] rounded-full w-4 h-4 flex items-center justify-center font-bold">
            {count}
          </span>
        )}
      </button>
    );
  }

  return (
    <aside className="w-64 bg-slate-900 border-r border-slate-700/60 flex flex-col shrink-0 overflow-y-auto">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-700/60 sticky top-0 bg-slate-900 z-10">
        <div className="flex items-center gap-2">
          <SlidersHorizontal size={15} className="text-blue-400" />
          <span className="text-sm font-semibold text-slate-200">Filters</span>
          {count > 0 && (
            <span className="bg-blue-500 text-white text-[10px] rounded-full px-1.5 py-0.5 font-bold">
              {count}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {count > 0 && (
            <button
              onClick={() => onChange(DEFAULT_FILTERS)}
              className="p-1 rounded hover:bg-slate-700 text-slate-400 hover:text-slate-200 transition-colors"
              title="Reset all filters"
            >
              <RotateCcw size={13} />
            </button>
          )}
          <button
            onClick={onToggle}
            className="p-1 rounded hover:bg-slate-700 text-slate-400 hover:text-slate-200 transition-colors"
            title="Close filters"
          >
            <X size={13} />
          </button>
        </div>
      </div>

      {/* County filter */}
      <div className="border-b border-slate-700/60">
        <button
          className="flex w-full items-center justify-between px-4 py-2.5 text-xs font-semibold text-slate-400 uppercase tracking-wider hover:text-slate-200 transition-colors"
          onClick={() => setCountyExpanded((v) => !v)}
        >
          County
          {countyExpanded ? (
            <ChevronUp size={13} />
          ) : (
            <ChevronDown size={13} />
          )}
        </button>
        {countyExpanded && (
          <div className="px-4 pb-3 space-y-1">
            {COUNTIES.map((county) => (
              <label
                key={county}
                className="flex items-center gap-2 cursor-pointer group"
              >
                <input
                  type="checkbox"
                  checked={filters.counties.includes(county)}
                  onChange={() => toggleCounty(county)}
                  className="w-3.5 h-3.5 accent-blue-500 rounded"
                />
                <span className="text-sm text-slate-400 group-hover:text-slate-200 transition-colors">
                  {county}
                </span>
              </label>
            ))}
          </div>
        )}
      </div>

      {/* Value range */}
      <div className="border-b border-slate-700/60">
        <button
          className="flex w-full items-center justify-between px-4 py-2.5 text-xs font-semibold text-slate-400 uppercase tracking-wider hover:text-slate-200 transition-colors"
          onClick={() => setValueExpanded((v) => !v)}
        >
          Market Value
          {valueExpanded ? (
            <ChevronUp size={13} />
          ) : (
            <ChevronDown size={13} />
          )}
        </button>
        {valueExpanded && (
          <div className="px-4 pb-3 space-y-2">
            <div>
              <label className="text-xs text-slate-500 block mb-1">
                Min ($)
              </label>
              <input
                type="number"
                placeholder="0"
                value={filters.min_value ?? ''}
                onChange={(e) => setNum('min_value', e.target.value)}
                className="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:border-blue-500"
              />
            </div>
            <div>
              <label className="text-xs text-slate-500 block mb-1">
                Max ($)
              </label>
              <input
                type="number"
                placeholder="No limit"
                value={filters.max_value ?? ''}
                onChange={(e) => setNum('max_value', e.target.value)}
                className="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:border-blue-500"
              />
            </div>
          </div>
        )}
      </div>

      {/* Score range */}
      <div className="border-b border-slate-700/60">
        <button
          className="flex w-full items-center justify-between px-4 py-2.5 text-xs font-semibold text-slate-400 uppercase tracking-wider hover:text-slate-200 transition-colors"
          onClick={() => setScoreExpanded((v) => !v)}
        >
          Opportunity Score
          {scoreExpanded ? (
            <ChevronUp size={13} />
          ) : (
            <ChevronDown size={13} />
          )}
        </button>
        {scoreExpanded && (
          <div className="px-4 pb-3 space-y-2">
            <div>
              <label className="text-xs text-slate-500 block mb-1">
                Min Score (0–100)
              </label>
              <input
                type="number"
                min={0}
                max={100}
                placeholder="0"
                value={filters.min_score ?? ''}
                onChange={(e) => setNum('min_score', e.target.value)}
                className="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:border-blue-500"
              />
            </div>
            <div>
              <label className="text-xs text-slate-500 block mb-1">
                Max Score (0–100)
              </label>
              <input
                type="number"
                min={0}
                max={100}
                placeholder="100"
                value={filters.max_score ?? ''}
                onChange={(e) => setNum('max_score', e.target.value)}
                className="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:border-blue-500"
              />
            </div>
          </div>
        )}
      </div>

      {/* Distress signals */}
      <div className="border-b border-slate-700/60">
        <button
          className="flex w-full items-center justify-between px-4 py-2.5 text-xs font-semibold text-slate-400 uppercase tracking-wider hover:text-slate-200 transition-colors"
          onClick={() => setDistressExpanded((v) => !v)}
        >
          Distress Signals
          {distressExpanded ? (
            <ChevronUp size={13} />
          ) : (
            <ChevronDown size={13} />
          )}
        </button>
        {distressExpanded && (
          <div className="px-4 pb-3 space-y-2">
            {(
              [
                {
                  field: 'has_lis_pendens' as const,
                  label: 'Lis Pendens',
                  color: 'text-red-400',
                },
                {
                  field: 'has_foreclosure' as const,
                  label: 'Foreclosure',
                  color: 'text-orange-400',
                },
                {
                  field: 'tax_delinquent' as const,
                  label: 'Tax Delinquent',
                  color: 'text-yellow-400',
                },
              ] as const
            ).map(({ field, label, color }) => (
              <label
                key={field}
                className="flex items-center gap-2 cursor-pointer group"
              >
                <input
                  type="checkbox"
                  checked={filters[field] === true}
                  onChange={() => toggleBool(field)}
                  className="w-3.5 h-3.5 accent-blue-500 rounded"
                />
                <span
                  className={`text-sm ${color} group-hover:brightness-125 transition-all`}
                >
                  {label}
                </span>
              </label>
            ))}
          </div>
        )}
      </div>

      {/* Limit */}
      <div className="px-4 py-3">
        <label className="text-xs text-slate-500 block mb-1">
          Result Limit
        </label>
        <select
          value={filters.limit}
          onChange={(e) => onChange({ ...filters, limit: Number(e.target.value) })}
          className="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm text-slate-200 focus:outline-none focus:border-blue-500"
        >
          <option value={500}>500</option>
          <option value={1000}>1,000</option>
          <option value={2500}>2,500</option>
          <option value={5000}>5,000</option>
        </select>
      </div>
    </aside>
  );
}
