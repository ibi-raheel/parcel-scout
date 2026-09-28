'use client';

import { useState } from 'react';
import dynamic from 'next/dynamic';
import {
  Map,
  List,
  ChevronDown,
  Download,
  Activity,
  Shield,
  Database,
} from 'lucide-react';
import { Search } from '@/components/Search';
import { Filters } from '@/components/Filters';
import { PropertyDrawer } from '@/components/PropertyDrawer';
import { StatsBar } from '@/components/StatsBar';
import { PropertyList } from '@/components/PropertyList';
import type { FilterState, ViewMode } from '@/types';

// Dynamically import the map (no SSR — deck.gl requires browser)
const ParcelMap = dynamic(
  () => import('@/components/ParcelMap').then((m) => m.ParcelMap),
  { ssr: false, loading: () => <MapPlaceholder /> }
);

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

function MapPlaceholder() {
  return (
    <div className="flex-1 flex items-center justify-center bg-slate-950 text-slate-600 text-sm">
      Loading map…
    </div>
  );
}

export default function HomePage() {
  const [view, setView] = useState<ViewMode>('map');
  const [filters, setFilters] = useState<FilterState>(DEFAULT_FILTERS);
  const [filtersOpen, setFiltersOpen] = useState(true);
  const [selectedPropertyId, setSelectedPropertyId] = useState<string | null>(
    null
  );
  const [toolsOpen, setToolsOpen] = useState(false);

  function handleSelectProperty(id: string) {
    setSelectedPropertyId(id);
  }

  function handleCloseDrawer() {
    setSelectedPropertyId(null);
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* ── Top navigation ─────────────────────────────────────────────── */}
      <header className="flex items-center gap-3 px-4 py-2 bg-slate-900 border-b border-slate-700/60 shrink-0 z-20">
        {/* Logo */}
        <div className="flex items-center gap-2 mr-2">
          <div className="w-7 h-7 bg-blue-600 rounded-lg flex items-center justify-center shrink-0">
            <Shield size={14} className="text-white" />
          </div>
          <span className="text-sm font-bold text-slate-100 tracking-tight">
            Parcel Scout
          </span>
          <span className="text-[10px] text-slate-500 font-mono bg-slate-800 rounded px-1.5 py-0.5 border border-slate-700">
            v3
          </span>
        </div>

        {/* Search */}
        <div className="flex-1 max-w-md">
          <Search
            onSelectProperty={handleSelectProperty}
          />
        </div>

        {/* View toggle */}
        <div className="flex bg-slate-800 border border-slate-700 rounded-lg p-0.5 gap-0.5">
          <button
            onClick={() => setView('map')}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
              view === 'map'
                ? 'bg-blue-600 text-white shadow'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <Map size={13} />
            Map
          </button>
          <button
            onClick={() => setView('list')}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
              view === 'list'
                ? 'bg-blue-600 text-white shadow'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <List size={13} />
            List
          </button>
        </div>

        {/* Tools dropdown */}
        <div className="relative">
          <button
            onClick={() => setToolsOpen((o) => !o)}
            className="flex items-center gap-1.5 bg-slate-800 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-400 hover:text-slate-200 hover:border-slate-500 transition-colors"
          >
            <Activity size={13} />
            Tools
            <ChevronDown size={11} />
          </button>
          {toolsOpen && (
            <>
              <div
                className="fixed inset-0 z-20"
                onClick={() => setToolsOpen(false)}
              />
              <div className="absolute right-0 top-full mt-1 z-30 w-48 bg-slate-800 border border-slate-600 rounded-xl shadow-2xl py-1 overflow-hidden">
                <ToolItem
                  href="/api/export/properties.csv"
                  icon={<Download size={13} />}
                  label="Export CSV"
                  sub="All filtered parcels"
                />
                <ToolItem
                  href="/api/admin/sources"
                  icon={<Database size={13} />}
                  label="Source Health"
                  sub="Data freshness"
                />
                <ToolItem
                  href="/api/risk/high"
                  icon={<Shield size={13} />}
                  label="High Risk Report"
                  sub="Critical properties"
                />
              </div>
            </>
          )}
        </div>
      </header>

      {/* ── Body (sidebar + main) ───────────────────────────────────────── */}
      <div className="flex flex-1 min-h-0 relative overflow-hidden">
        {/* Filter sidebar */}
        <Filters
          filters={filters}
          onChange={setFilters}
          isOpen={filtersOpen}
          onToggle={() => setFiltersOpen((o) => !o)}
        />

        {/* Main content */}
        <main className="flex-1 flex flex-col min-w-0 overflow-hidden relative">
          {view === 'map' ? (
            <ParcelMap
              filters={filters}
              onSelectProperty={handleSelectProperty}
            />
          ) : (
            <PropertyList
              filters={filters}
              onSelectProperty={handleSelectProperty}
              selectedId={selectedPropertyId}
            />
          )}
        </main>

        {/* Property detail drawer */}
        <PropertyDrawer
          propertyId={selectedPropertyId}
          onClose={handleCloseDrawer}
        />
      </div>

      {/* ── Stats bar ───────────────────────────────────────────────────── */}
      <StatsBar />
    </div>
  );
}

function ToolItem({
  href,
  icon,
  label,
  sub,
}: {
  href: string;
  icon: React.ReactNode;
  label: string;
  sub: string;
}) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="flex items-center gap-3 px-3 py-2.5 hover:bg-slate-700 transition-colors"
    >
      <span className="text-slate-400">{icon}</span>
      <div>
        <div className="text-xs text-slate-200 font-medium">{label}</div>
        <div className="text-[10px] text-slate-500">{sub}</div>
      </div>
    </a>
  );
}
