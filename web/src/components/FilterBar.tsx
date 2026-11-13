import { useId, useRef, useState } from "react";
import { useApp } from "../App";
import type { Filters } from "../filters";
import { fmtDate } from "../format";
import { t } from "../i18n";
import Icon from "./Icon";

// Compact primary row (period · commodity · warehouse region · "Filtrlar") + an inline panel with every other filter.
// All state stays in the URL (useFilters); nothing here changes what the API computes.
export default function FilterBar({ filters, update, reset }: { filters: Filters; update: (p: Filters) => void; reset: () => void }) {
  const { lang, options, meta } = useApp();
  const ds = meta?.dataset;
  const lo = ds?.coverage_start ?? "2022-01-03", hi = ds?.coverage_end ?? "2026-10-30";
  const id = useId();
  const [open, setOpen] = useState(false);
  const startRef = useRef<HTMLInputElement>(null);
  const variants = filters.commodity ? options?.variants[filters.commodity] ?? [] : [];
  const presets: [string, string, string | undefined, string | undefined][] = [
    ["all", t("preset_all", lang), undefined, undefined],
    ...["2022", "2023", "2024", "2025"].map(y => [y, y, y === "2022" ? lo : `${y}-01-01`, `${y}-12-31`] as [string, string, string, string]),
    ["2026", t("preset_ytd", lang), "2026-01-01", hi],
    ["12m", t("preset_12m", lang), "2025-10-31", hi],
  ];
  const norm = (v?: string, d?: string) => (v || d || "");
  const current = presets.find(([, , s, e]) => norm(filters.start, lo) === norm(s, lo) && norm(filters.end, hi) === norm(e, hi));
  const periodKey = current ? current[0] : "custom";
  const onPeriod = (k: string) => {
    if (k === "custom") { setOpen(true); setTimeout(() => startRef.current?.focus(), 0); return; }
    const p = presets.find(x => x[0] === k);
    if (p) update({ start: p[2], end: p[3] });
  };
  const regionLabel = (k: string): string => { const r = options?.regions.find(x => x.key === k); return (r ? (lang === "uz" ? r.uz : r.ru) : undefined) ?? k; };
  const comLabel = (k: string) => options?.commodities.find(x => x.key === k)?.label ?? k;
  const varLabel = (k: string) => variants.find(x => x.key === k)?.label ?? k;
  const pol = options?.policies.find(p => p.key === (filters.policy || "registry"));
  // "Filtrlar" badge = filters that live only in the panel (accurate: the primary-row ones are visible already)
  const advCount = [filters.variant, filters.form, filters.policy, filters.currency].filter(Boolean).length + (periodKey === "custom" ? 1 : 0);
  const chips: [string, string, keyof Filters][] = [];
  if (filters.start || filters.end) chips.push([t("period", lang), `${fmtDate(filters.start || lo)}–${fmtDate(filters.end || hi)}`, "start"]);
  if (filters.commodity) chips.push([t("commodity", lang), comLabel(filters.commodity), "commodity"]);
  if (filters.variant) chips.push([t("variant", lang), varLabel(filters.variant), "variant"]);
  if (filters.region) chips.push([t("region_short", lang), regionLabel(filters.region), "region"]);
  if (filters.form) chips.push([t("form", lang), options?.forms.find(f => f.key === filters.form)?.[lang] ?? filters.form ?? "", "form"]);
  if (filters.currency) chips.push([t("currency", lang), filters.currency, "currency"]);
  return (
    <>
      <section className="card filters" aria-label={t("filters", lang)}>
        <div className="filter-row">
          <label className="field"><span>{t("period", lang)}</span>
            <select value={periodKey} onChange={e => onPeriod(e.target.value)}>
              {presets.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
              <option value="custom">{periodKey === "custom" ? `${fmtDate(filters.start || lo)} — ${fmtDate(filters.end || hi)}` : t("period_custom", lang)}</option>
            </select>
          </label>
          <label className="field"><span>{t("commodity", lang)}</span>
            <select value={filters.commodity || ""} onChange={e => update({ commodity: e.target.value || undefined })}>
              <option value="">{t("all", lang)}</option>
              {options?.commodities.map(c => <option key={c.key} value={c.key}>{c.label}</option>)}
            </select>
          </label>
          <label className="field"><span>{t("region_short", lang)}</span>
            <select value={filters.region || ""} onChange={e => update({ region: e.target.value || undefined })}>
              <option value="">{t("all", lang)}</option>
              {options?.regions.map(r => <option key={r.key} value={r.key}>{lang === "uz" ? r.uz : r.ru}</option>)}
            </select>
          </label>
          <div className="field"><span aria-hidden="true">{t("filters", lang)}</span>
            <button className="btn filters-btn" aria-expanded={open} aria-controls={`${id}-adv`} onClick={() => setOpen(o => !o)}>
              <Icon name="filter" /> {t("filters", lang)}
              {advCount > 0 && <span className="count-badge" aria-label={`${advCount} ${t("active_count", lang)}`}>{advCount}</span>}
            </button>
          </div>
        </div>
        <div className="adv" id={`${id}-adv`} hidden={!open} role="group" aria-label={t("more_filters", lang)}>
          <fieldset className="dates" style={{ border: "none", padding: 0, margin: 0 }}>
            <legend className="sr-only">{t("period", lang)}</legend>
            <label className="field"><span>{t("period", lang)}: {t("from", lang)}</span>
              <input ref={startRef} type="date" min={lo} max={hi} value={filters.start || lo} onChange={e => update({ start: e.target.value || undefined })} /></label>
            <label className="field"><span>{t("period", lang)}: {t("to", lang)}</span>
              <input type="date" min={lo} max={hi} value={filters.end || hi} onChange={e => update({ end: e.target.value || undefined })} /></label>
          </fieldset>
          <label className="field"><span>{t("variant", lang)}</span>
            <select value={filters.variant || ""} disabled={!filters.commodity} onChange={e => update({ variant: e.target.value || undefined, commodity: filters.commodity })}>
              <option value="">{filters.commodity ? t("all", lang) : (lang === "uz" ? "Avval tovarni tanlang" : "Сначала выберите товар")}</option>
              {variants.map(v => <option key={v.key} value={v.key}>{v.label} [{v.unit}] · {v.deals}</option>)}
            </select>
          </label>
          <label className="field"><span>{t("form", lang)}</span>
            <select value={filters.form || ""} onChange={e => update({ form: e.target.value || undefined })}>
              <option value="">{t("all", lang)}</option>
              {options?.forms.map(f => <option key={f.key} value={f.key}>{f[lang]}</option>)}
            </select>
          </label>
          <label className="field"><span>{t("policy", lang)}</span>
            <select value={filters.policy || "registry"} onChange={e => update({ policy: e.target.value === "registry" ? undefined : e.target.value })}>
              {options?.policies.map(p => <option key={p.key} value={p.key}>{p[lang]}</option>)}
            </select>
          </label>
          <label className="field"><span>{t("currency", lang)}</span>
            <select value={filters.currency || ""} onChange={e => update({ currency: e.target.value || undefined })}>
              <option value="">{t("all", lang)}</option>
              {options?.currencies.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>
          <div className="adv-foot">
            <button className="link" onClick={reset}>{t("reset", lang)}</button>
            <button className="btn sm" onClick={() => setOpen(false)}>{t("done", lang)}</button>
          </div>
        </div>
      </section>
      <div className="filter-meta" aria-live="polite">
        {pol && (  /* only once the catalog is loaded: never guess whether cancelled deals are included */
          <span className={`policy-chip${pol.includes_cancelled ? "" : " excl"}`}>
            {(filters.policy || "registry") === "registry" ? t("policy_all_incl", lang)
              : `${pol[lang]} · ${pol.includes_cancelled ? t("includes_cancelled", lang) : t("excludes_cancelled", lang)}`}
          </span>
        )}
        {chips.map(([k, v, key]) => (
          <span className="chip" key={key}>{k}: <b>{v}</b>
            <button aria-label={`${t("remove", lang)}: ${k}`} onClick={() => update(key === "start" ? { start: undefined, end: undefined } : { [key]: undefined })}>×</button>
          </span>
        ))}
        {(chips.length > 0 || filters.policy) && <button className="link" onClick={reset}>{t("reset", lang)}</button>}
      </div>
    </>
  );
}
