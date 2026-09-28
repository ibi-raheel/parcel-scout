'use client';

import { useEffect, useRef, useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { fetchProperties } from '@/lib/api';
import type { FilterState, GeoJSONFeature } from '@/types';

const INITIAL_CENTER: [number, number] = [-96.70, 33.05];
const INITIAL_ZOOM = 10;
const BASEMAP = 'https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json';
const SOURCE_ID = 'parcels';
const LAYER_ID = 'parcels-circle';

interface ParcelMapProps {
  filters: FilterState;
  onSelectProperty: (id: string) => void;
}

export function ParcelMap({ filters, onSelectProperty }: ParcelMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const popupRef = useRef<maplibregl.Popup | null>(null);
  const [ready, setReady] = useState(false);
  const onSelectRef = useRef(onSelectProperty);
  onSelectRef.current = onSelectProperty;

  const { data, isLoading, error } = useQuery({
    queryKey: ['properties', filters],
    queryFn: () => fetchProperties(filters),
    staleTime: 30_000,
  });

  const geojson = useMemo<GeoJSON.FeatureCollection>(() => {
    if (!data?.features) return { type: 'FeatureCollection', features: [] };
    const valid = data.features.filter(
      (f: GeoJSONFeature) =>
        f.geometry?.coordinates?.[0] != null &&
        f.geometry?.coordinates?.[1] != null
    );
    return { type: 'FeatureCollection', features: valid } as GeoJSON.FeatureCollection;
  }, [data]);

  // Init map once
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: BASEMAP,
      center: INITIAL_CENTER,
      zoom: INITIAL_ZOOM,
      attributionControl: false,
    });

    map.addControl(new maplibregl.NavigationControl(), 'bottom-right');

    map.on('load', () => {
      map.addSource(SOURCE_ID, {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });

      map.addLayer({
        id: LAYER_ID,
        type: 'circle',
        source: SOURCE_ID,
        paint: {
          'circle-radius': [
            'interpolate', ['linear'], ['coalesce', ['get', 'market_value'], 0],
            0, 3,
            500000, 4,
            2000000, 6,
            10000000, 9,
            50000000, 13,
          ],
          'circle-color': [
            'interpolate', ['linear'], ['coalesce', ['get', 'score'], 0],
            0, '#3b82f6',
            25, '#60a5fa',
            50, '#eab308',
            75, '#f97316',
            100, '#ef4444',
          ],
          'circle-opacity': 0.8,
          'circle-stroke-width': 0.5,
          'circle-stroke-color': 'rgba(255,255,255,0.3)',
        },
      });

      map.on('click', LAYER_ID, (e) => {
        const feature = e.features?.[0];
        if (feature?.properties?.id) {
          onSelectRef.current(feature.properties.id);
        }
      });

      map.on('mouseenter', LAYER_ID, () => {
        map.getCanvas().style.cursor = 'pointer';
      });
      map.on('mouseleave', LAYER_ID, () => {
        map.getCanvas().style.cursor = '';
        popupRef.current?.remove();
        popupRef.current = null;
      });

      map.on('mousemove', LAYER_ID, (e) => {
        const feature = e.features?.[0];
        if (!feature) return;
        const p = feature.properties!;
        const val = p.market_value ? `$${Number(p.market_value).toLocaleString()}` : '—';
        const score = p.score ?? p.opportunity_score ?? '—';
        const scoreNum = Number(score) || 0;
        const scoreColor = scoreNum > 70 ? '#ef4444' : scoreNum >= 40 ? '#eab308' : '#3b82f6';

        const html = `
          <div style="font-size:12px;line-height:1.6;max-width:260px">
            <strong style="font-size:13px">${p.property_address || 'No address'}</strong><br>
            <span style="color:#94a3b8">${p.city || ''} · ${p.county || ''}</span>
            <hr style="margin:4px 0;border-color:#334155;border-style:solid">
            <b>Owner:</b> ${p.owner_name || '—'}<br>
            <b>Value:</b> ${val}<br>
            <b>Score:</b> <span style="color:${scoreColor};font-weight:700">${score}</span>
          </div>
        `;

        popupRef.current?.remove();
        popupRef.current = new maplibregl.Popup({
          closeButton: false,
          closeOnClick: false,
          offset: 12,
          className: 'parcel-popup',
        })
          .setLngLat(e.lngLat)
          .setHTML(html)
          .addTo(map);
      });

      setReady(true);
    });

    mapRef.current = map;
    return () => { map.remove(); mapRef.current = null; };
  }, []);

  // Update data
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const src = map.getSource(SOURCE_ID) as maplibregl.GeoJSONSource | undefined;
    if (src) src.setData(geojson);
  }, [geojson, ready]);

  return (
    <div className="relative w-full h-full">
      <div ref={containerRef} className="absolute inset-0" />

      {isLoading && (
        <div className="absolute top-3 left-1/2 -translate-x-1/2 bg-slate-900/90 border border-slate-600 rounded-full px-4 py-1.5 text-xs text-slate-300 backdrop-blur-sm z-10">
          Loading parcels…
        </div>
      )}

      {error && (
        <div className="absolute top-3 left-1/2 -translate-x-1/2 bg-red-950/90 border border-red-700 rounded-full px-4 py-1.5 text-xs text-red-300 backdrop-blur-sm z-10">
          Failed to load: {(error as Error).message}
        </div>
      )}

      {data && !isLoading && (
        <div className="absolute bottom-12 left-3 bg-slate-900/90 border border-slate-600 rounded-lg px-3 py-1.5 text-xs text-slate-300 backdrop-blur-sm z-10">
          <span className="font-semibold text-slate-100">
            {geojson.features.length.toLocaleString()}
          </span>{' '}parcels
        </div>
      )}

      <div className="absolute bottom-12 right-14 bg-slate-900/90 border border-slate-600 rounded-lg px-3 py-2 z-10 backdrop-blur-sm">
        <div className="text-[10px] text-slate-500 mb-1.5 uppercase tracking-wider">
          Opportunity Score
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] text-slate-500">0</span>
          <div className="w-24 h-2 rounded-full bg-gradient-to-r from-blue-500 via-yellow-400 to-red-500" />
          <span className="text-[10px] text-slate-500">100</span>
        </div>
      </div>
    </div>
  );
}
