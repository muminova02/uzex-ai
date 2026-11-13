import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "./api";

// Shareable, non-sensitive filter state lives in the URL (?start=…&commodity=…). The dashboard and the
// deal explorer read the same parameters, so the selection is explicit and visible when switching pages.
export const FILTER_KEYS = ["start", "end", "commodity", "variant", "region", "form", "policy", "currency"] as const;
export type FilterKey = (typeof FILTER_KEYS)[number];
export type Filters = Partial<Record<FilterKey, string>>;

export function useFilters(): [Filters, (patch: Filters) => void, () => void, string] {
  const [sp, setSp] = useSearchParams();
  const filters = useMemo(() => {
    const f: Filters = {};
    for (const k of FILTER_KEYS) {
      const v = sp.get(k);
      if (v) f[k] = v;
    }
    return f;
  }, [sp]);
  const update = useCallback((patch: Filters) => {
    setSp(prev => {
      const n = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(patch)) {
        if (v) n.set(k, v); else n.delete(k);
      }
      if ("commodity" in patch && !("variant" in patch)) n.delete("variant");
      return n;
    }, { replace: false });
  }, [setSp]);
  const reset = useCallback(() => setSp(prev => {
    const n = new URLSearchParams(prev);
    FILTER_KEYS.forEach(k => n.delete(k));
    return n;
  }), [setSp]);
  return [filters, update, reset, toQuery(filters)];
}

export function toQuery(f: Filters, extra: Record<string, string | number | undefined> = {}): string {
  const p = new URLSearchParams();
  for (const k of FILTER_KEYS) if (f[k]) p.set(k, f[k] as string);
  for (const [k, v] of Object.entries(extra)) if (v !== undefined && v !== "") p.set(k, String(v));
  return p.toString();
}

/** Fetch with cancellation: a newer request aborts the older one and stale results are discarded. */
export function useApi<T>(url: string | null): { data: T | null; error: ApiError | Error | null; loading: boolean; reload: () => void } {
  const [state, setState] = useState<{ data: T | null; error: ApiError | Error | null; loading: boolean; url: string | null }>(
    { data: null, error: null, loading: !!url, url: null });
  const [tick, setTick] = useState(0);
  const latest = useRef<string | null>(null);
  useEffect(() => {
    if (!url) { setState({ data: null, error: null, loading: false, url }); return; }
    const ctrl = new AbortController();
    latest.current = url;
    setState(s => ({ ...s, loading: true, error: null }));
    api<T>(url, { signal: ctrl.signal })
      .then(d => { if (latest.current === url) setState({ data: d, error: null, loading: false, url }); })
      .catch(e => { if (e.name !== "AbortError" && latest.current === url) setState({ data: null, error: e, loading: false, url }); });
    return () => ctrl.abort();
  }, [url, tick]);
  // never show data that belongs to a previous URL
  const fresh = state.url === url;
  return { data: fresh ? state.data : null, error: fresh ? state.error : null, loading: state.loading || !fresh, reload: () => setTick(x => x + 1) };
}
