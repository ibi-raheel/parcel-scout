'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { Search as SearchIcon, Building2, User, X, Loader2 } from 'lucide-react';
import { search, riskBadgeClass } from '@/lib/api';
import type { SearchResult } from '@/types';

interface SearchProps {
  onSelectProperty?: (id: string) => void;
  onSelectOwner?: (id: string) => void;
}

export function Search({ onSelectProperty, onSelectOwner }: SearchProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Cmd+K to open
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setOpen(true);
      }
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  useEffect(() => {
    if (open) {
      setTimeout(() => inputRef.current?.focus(), 50);
    } else {
      setQuery('');
      setResults([]);
      setSelected(0);
    }
  }, [open]);

  const doSearch = useCallback(async (q: string) => {
    if (!q.trim()) {
      setResults([]);
      return;
    }
    setLoading(true);
    try {
      const r = await search(q);
      setResults(r);
      setSelected(0);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => doSearch(query), 300);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [query, doSearch]);

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelected((s) => Math.min(s + 1, results.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelected((s) => Math.max(s - 1, 0));
    } else if (e.key === 'Enter' && results[selected]) {
      handleSelect(results[selected]);
    }
  }

  function handleSelect(result: SearchResult) {
    if (result.type === 'property') onSelectProperty?.(result.id);
    else if (result.type === 'owner') onSelectOwner?.(result.id);
    setOpen(false);
  }

  return (
    <>
      {/* Trigger button */}
      <button
        onClick={() => setOpen(true)}
        className="flex items-center gap-2 bg-slate-800 border border-slate-600 rounded-lg px-3 py-1.5 text-sm text-slate-400 hover:border-slate-500 hover:text-slate-300 transition-colors min-w-[220px]"
      >
        <SearchIcon size={14} />
        <span className="flex-1 text-left">Search parcels, owners…</span>
        <kbd className="text-[10px] bg-slate-700 rounded px-1 py-0.5 font-mono">
          ⌘K
        </kbd>
      </button>

      {/* Modal overlay */}
      {open && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center pt-[15vh]"
          onClick={(e) => e.target === e.currentTarget && setOpen(false)}
        >
          <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => setOpen(false)} />
          <div className="relative z-10 w-full max-w-xl bg-slate-900 border border-slate-600 rounded-xl shadow-2xl overflow-hidden">
            {/* Search input */}
            <div className="flex items-center gap-3 px-4 py-3 border-b border-slate-700">
              {loading ? (
                <Loader2 size={16} className="text-slate-400 animate-spin shrink-0" />
              ) : (
                <SearchIcon size={16} className="text-slate-400 shrink-0" />
              )}
              <input
                ref={inputRef}
                type="text"
                placeholder="Search parcels, owners, entities…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={handleKeyDown}
                className="flex-1 bg-transparent text-slate-100 placeholder-slate-500 text-sm focus:outline-none"
              />
              <button
                onClick={() => setOpen(false)}
                className="p-1 rounded hover:bg-slate-700 text-slate-500 hover:text-slate-300 transition-colors"
              >
                <X size={14} />
              </button>
            </div>

            {/* Results */}
            {results.length > 0 ? (
              <ul className="py-2 max-h-[400px] overflow-y-auto">
                {results.map((r, i) => (
                  <li key={`${r.type}-${r.id}`}>
                    <button
                      className={`w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors ${
                        i === selected
                          ? 'bg-slate-700/60'
                          : 'hover:bg-slate-800'
                      }`}
                      onMouseEnter={() => setSelected(i)}
                      onClick={() => handleSelect(r)}
                    >
                      <div className="shrink-0 w-7 h-7 rounded-md bg-slate-800 flex items-center justify-center">
                        {r.type === 'property' ? (
                          <Building2 size={13} className="text-blue-400" />
                        ) : (
                          <User size={13} className="text-purple-400" />
                        )}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="text-sm text-slate-200 truncate font-medium">
                          {r.title}
                        </div>
                        <div className="text-xs text-slate-500 truncate">
                          {r.subtitle}
                        </div>
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        {r.score != null && (
                          <span
                            className={`text-xs px-1.5 py-0.5 rounded font-semibold ${
                              r.score >= 70
                                ? 'bg-red-900/60 text-red-300'
                                : r.score >= 40
                                  ? 'bg-yellow-900/60 text-yellow-300'
                                  : 'bg-slate-700 text-slate-400'
                            }`}
                          >
                            {r.score.toFixed(0)}
                          </span>
                        )}
                        {r.badge && (
                          <span className="text-[10px] bg-slate-700 text-slate-400 rounded px-1.5 py-0.5 truncate max-w-[100px]">
                            {r.badge}
                          </span>
                        )}
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            ) : query.trim() && !loading ? (
              <div className="px-4 py-8 text-center text-sm text-slate-500">
                No results for &ldquo;{query}&rdquo;
              </div>
            ) : !query.trim() ? (
              <div className="px-4 py-6 text-center text-xs text-slate-600">
                Start typing to search properties, owners, and entities
              </div>
            ) : null}

            {/* Footer hint */}
            <div className="flex items-center justify-end gap-4 px-4 py-2 border-t border-slate-700/60 text-[10px] text-slate-600">
              <span>
                <kbd className="bg-slate-800 rounded px-1">↑↓</kbd> navigate
              </span>
              <span>
                <kbd className="bg-slate-800 rounded px-1">↵</kbd> open
              </span>
              <span>
                <kbd className="bg-slate-800 rounded px-1">esc</kbd> close
              </span>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

// Re-export the badge helper for use in other components
export { riskBadgeClass };
