'use client';

import { useQuery } from '@tanstack/react-query';
import { fetchStats, formatCurrency } from '@/lib/api';
import {
  Building2,
  AlertTriangle,
  TrendingUp,
  DollarSign,
  Flame,
  Activity,
} from 'lucide-react';

export function StatsBar() {
  const { data, isLoading } = useQuery({
    queryKey: ['stats'],
    queryFn: fetchStats,
    refetchInterval: 120_000,
  });

  const items = [
    {
      icon: Building2,
      label: 'Total Parcels',
      value: isLoading
        ? '…'
        : (data?.total_properties?.toLocaleString() ?? '—'),
      color: 'text-blue-400',
    },
    {
      icon: DollarSign,
      label: 'Total Market Value',
      value: isLoading ? '…' : formatCurrency(data?.total_market_value ?? null),
      color: 'text-emerald-400',
    },
    {
      icon: AlertTriangle,
      label: 'Distressed',
      value: isLoading
        ? '…'
        : (data?.distressed_count?.toLocaleString() ?? '—'),
      color: 'text-yellow-400',
    },
    {
      icon: Flame,
      label: 'High Risk',
      value: isLoading
        ? '…'
        : (data?.high_risk_count?.toLocaleString() ?? '—'),
      color: 'text-orange-400',
    },
    {
      icon: Activity,
      label: 'Critical',
      value: isLoading
        ? '…'
        : (data?.critical_risk_count?.toLocaleString() ?? '—'),
      color: 'text-red-400',
    },
    {
      icon: TrendingUp,
      label: 'Avg Opportunity',
      value: isLoading
        ? '…'
        : data?.avg_opportunity_score != null
          ? data.avg_opportunity_score.toFixed(1)
          : '—',
      color: 'text-purple-400',
    },
  ];

  return (
    <div className="flex items-center gap-0 bg-slate-900 border-t border-slate-700/60 px-4 py-1.5 shrink-0 overflow-x-auto">
      {items.map((item, i) => {
        const Icon = item.icon;
        return (
          <div
            key={i}
            className="flex items-center gap-2 px-4 py-1 border-r border-slate-700/60 last:border-0 shrink-0"
          >
            <Icon size={13} className={item.color} />
            <span className="text-xs text-slate-500 whitespace-nowrap">
              {item.label}
            </span>
            <span className="text-xs font-semibold text-slate-200 tabular-nums">
              {item.value}
            </span>
          </div>
        );
      })}
    </div>
  );
}
