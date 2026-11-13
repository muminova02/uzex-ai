import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Cell, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useApp } from "../App";
import type { Overview, Price, Rank, StatusDist, Trend } from "../api";
import FilterBar from "../components/FilterBar";
import Icon from "../components/Icon";
import PageHeader from "../components/PageHeader";
import State from "../components/State";
import { toQuery, useApi, useFilters, type Filters } from "../filters";
import { fmtBn, fmtDate, fmtExact, fmtInt, unitLabel } from "../format";
import { t, type Lang } from "../i18n";

// sage palette for series; semantic colours for statuses (meaning is also written as text labels)
const EVERGREEN = "#446356", SAGE = "#6f9a84", SLATE = "#3f6276", INK = "#23362f", GRID = "#e4ebe6", AXIS = "#52625a";
const STATUS_COLOR: Record<string, string> = { fulfilled: "#2f6b4f", partially_fulfilled: "#2f5f7a", paid: "#5b86a0", cancelled_unpaid: "#a8382a",
  awaiting_payment: "#b07a1c", concluded: "#c99a45", unsigned: "#8a968f", new: "#6b7a72" };
const tick = { fontSize: 12, fill: AXIS };

export default function Dashboard() {
  const { lang } = useApp();
  const [filters, update, reset, qs] = useFilters();
  const nav = useNavigate();
  const q = qs ? `?${qs}` : "";
  const ov = useApi<Overview>(`/api/analytics/overview${q}`);
  const toChat = (ask?: string) => nav(`/assistant?${toQuery(filters, { ctx: 1, ask })}`);
  return (
    <div className="page">
      <PageHeader title={t("dash_title", lang)} actions={
        <button className="btn primary" onClick={() => toChat()}><Icon name="sparkles" /> {t("ask_ai", lang)}</button>} />
      <FilterBar filters={filters} update={update} reset={reset} />
      <Kpis ov={ov} lang={lang} />
      <div className="dash-grid">
        <TrendCard q={q} lang={lang} />
        <AskCard lang={lang} filters={filters} onAsk={toChat} />
      </div>
      <div className="grid2">
        <StatusCard q={q} lang={lang} />
        <RankCard q={q} lang={lang} onPick={k => update({ commodity: k })} />
        <RegionCard q={q} lang={lang} onPick={k => update({ region: k })} />
        <PriceCard q={q} lang={lang} variant={filters.variant} />
      </div>
    </div>
  );
}

/** "220 477,16 mlrd so'm" -> ["220 477,16", "mlrd so'm"] (string split only; no float arithmetic). */
function bnParts(exact: string | null | undefined, lang: Lang): [string, string] {
  const s = fmtBn(exact, lang);
  const m = s.match(/^(.*?)[\s ]((?:mlrd|млрд)[\s ].+)$/);
  return m ? [m[1], m[2]] : [s, ""];
}

function Kpis({ ov, lang }: { ov: ReturnType<typeof useApi<Overview>>; lang: Lang }) {
  const d = ov.data;
  if (!d) return <section className="card"><State loading={ov.loading} error={ov.error} reload={ov.reload} lang={lang} rows={2} /></section>;
  const pol = d.status_policy;
  const [num, unit] = bnParts(d.value_uzs?.exact, lang);
  const share = d.cancelled.share_pct;
  return (
    <>
      <section className="kpis" aria-label="KPI">
        <div className="card kpi">
          <div className="kpi-label">{t("kpi_deals", lang)}</div>
          <div className="kpi-main"><span className="kpi-ic"><Icon name="handshake" /></span><span className="kpi-value">{fmtInt(d.deals)}</span></div>
          <div className="kpi-note">{lang === "uz" ? pol.label_uz : pol.label_ru}</div>
        </div>
        <div className="card kpi">
          <div className="kpi-label">{t("kpi_contract_value", lang)}</div>
          <div className="kpi-main"><span className="kpi-ic"><Icon name="coins" /></span>
            <span className="kpi-value">{num}<span className="kpi-unit">{unit}</span></span></div>
          <div className="kpi-note">
            <b>{t("not_revenue", lang)}</b>{d.usd_converted_deals ? <> · {t("usd_note", lang)}: {fmtInt(d.usd_converted_deals)}</> : null}
            {d.value_uzs && <details><summary>{t("exact_value", lang)}</summary><span className="tnum">{fmtExact(d.value_uzs.exact, 2)} UZS</span></details>}
          </div>
        </div>
        <div className="card kpi">
          <div className="kpi-label">{t("kpi_sellers", lang)}</div>
          <div className="kpi-main"><span className="kpi-ic"><Icon name="building" /></span><span className="kpi-value">{fmtInt(d.distinct_sellers)}</span></div>
          <div className="kpi-note">{t("distinct_note", lang)}</div>
        </div>
        <div className="card kpi">
          <div className="kpi-label">{t("kpi_buyers", lang)}</div>
          <div className="kpi-main"><span className="kpi-ic"><Icon name="users" /></span><span className="kpi-value">{fmtInt(d.distinct_buyers)}</span></div>
          <div className="kpi-note">{t("distinct_note", lang)}</div>
        </div>
      </section>
      {pol.includes_cancelled ? (
        <section className="strip" aria-label={t("cancelled_share", lang)}>
          <Icon name="info" />
          <b>{t("cancelled_share", lang)}: <span className="tnum">{share ? `${fmtExact(share.exact)}%` : "—"}</span></b>
          <span className="sep" aria-hidden="true" />
          <span className="tnum">{fmtInt(d.cancelled.count)} / {fmtInt(d.cancelled.denominator)}</span>
          <details><summary><Icon name="doc" size="sm" /> {t("how_calc", lang)}</summary><p>{t("cancel_method", lang)}</p></details>
        </section>
      ) : (
        <section className="strip neutral" aria-label={t("cancelled_share", lang)}><Icon name="info" /> {t("cancelled_share", lang)}: {t("cancelled_excluded", lang)}</section>
      )}
    </>
  );
}

function AskCard({ lang, filters, onAsk }: { lang: Lang; filters: Filters; onAsk: (q?: string) => void }) {
  const [text, setText] = useState("");
  const has = Object.keys(filters).length > 0;
  return (
    <section className="card ask-card" aria-labelledby="askc-h">
      <h2 id="askc-h"><Icon name="sparkles" /> {t("ask_card_title", lang)}</h2>
      <form onSubmit={e => { e.preventDefault(); if (text.trim()) onAsk(text.trim()); }} style={{ display: "contents" }}>
        <label className="sr-only" htmlFor="askc">{t("ask_card_label", lang)}</label>
        <textarea id="askc" rows={2} maxLength={1000} value={text} onChange={e => setText(e.target.value)}
          placeholder={lang === "uz" ? "2025-yilda nechta bitim tuzilgan?" : "Сколько сделок было заключено в 2025 году?"}
          onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); if (text.trim()) onAsk(text.trim()); } }} />
        <button type="submit" className="btn primary" disabled={!text.trim()}><Icon name="send" /> {t("ask_card_btn", lang)}</button>
      </form>
      <p className="ctx-line">{has ? t("ask_card_ctx", lang) : t("ai_explain", lang)}</p>
    </section>
  );
}

function TrendCard({ q, lang }: { q: string; lang: Lang }) {
  const [mode, setMode] = useState<"deals" | "value">("deals");
  const tr = useApi<Trend>(`/api/analytics/trend${q}`);
  const rows = (tr.data?.months ?? []).map(m => ({ ...m, v: mode === "deals" ? m.deals : m.value_uzs ? m.value_uzs.approx_bn : null }));
  const partial = rows.filter(r => r.partial);
  return (
    <section className="card" aria-labelledby="trend-h">
      <div className="card-head">
        <h2 id="trend-h">{t("trend", lang)}</h2>
        <div className="seg" role="group" aria-label={t("trend", lang)}>
          <button aria-pressed={mode === "deals"} onClick={() => setMode("deals")}>{t("by_count", lang)}</button>
          <button aria-pressed={mode === "value"} onClick={() => setMode("value")}>{t("by_value", lang)}</button>
        </div>
      </div>
      <p className="sub">{mode === "deals" ? `${t("deals_axis", lang)} / ${lang === "uz" ? "oy" : "мес."}` : `${t("bn_axis", lang)} / ${lang === "uz" ? "oy" : "мес."} · ${t("value_note", lang)}`}</p>
      <State loading={tr.loading} error={tr.error} empty={tr.data?.empty} reload={tr.reload} lang={lang} />
      {tr.data && !tr.data.empty && (
        <>
          <div className="chart-box" role="img" aria-label={`${t("trend", lang)}: ${rows.length} ${lang === "uz" ? "oy" : "мес."}; ${t("table_view", lang)}`}>
            <ResponsiveContainer width="100%" height={260}>
              <LineChart data={rows} margin={{ top: 8, right: 12, left: 4, bottom: 0 }}>
                <CartesianGrid stroke={GRID} vertical={false} />
                <XAxis dataKey="month" tick={tick} minTickGap={28} stroke={GRID} />
                <YAxis tick={tick} width={64} stroke={GRID} tickFormatter={v => fmtInt(Math.round(v))} />
                <Tooltip contentStyle={{ borderRadius: 10, borderColor: "#d8e0da" }} formatter={(_v, _n, item) => {
                  const p = item.payload as typeof rows[number];
                  return mode === "deals" ? [fmtInt(p.deals), t("kpi_deals", lang)] : [p.value_uzs ? `${fmtExact(p.value_uzs.exact, 2)} UZS` : "—", t("kpi_value", lang)];
                }} labelFormatter={(l, p) => `${l}${p?.[0]?.payload?.partial ? ` (${t("partial_month", lang)}: ${fmtDate(p[0].payload.covered_from)}–${fmtDate(p[0].payload.covered_to)})` : ""}`} />
                <Line dataKey="v" stroke={mode === "deals" ? EVERGREEN : SLATE} strokeWidth={2.2} dot={false} connectNulls={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
          {partial.length > 0 && <p className="note warm"><Icon name="alert" size="sm" /> {t("partial_month", lang)}: {partial.map(p => `${p.month} (${fmtDate(p.covered_from)}–${fmtDate(p.covered_to)})`).join(", ")}</p>}
          <details className="tableview"><summary>{t("table_view", lang)}</summary>
            <div className="tv"><table><thead><tr><th>{lang === "uz" ? "Oy" : "Месяц"}</th><th className="num">{t("kpi_deals", lang)}</th><th className="num">{t("kpi_value", lang)}, UZS</th></tr></thead>
              <tbody>{rows.map(r => <tr key={r.month}><td>{r.month}{r.partial ? " *" : ""}</td><td className="num">{fmtInt(r.deals)}</td><td className="num">{fmtExact(r.value_uzs?.exact, 2)}</td></tr>)}</tbody></table></div>
          </details>
        </>
      )}
    </section>
  );
}

function HBar({ data, color, onPick, labelOf, lang, unit }: {
  data: { key: string; label: string; v: number; exact: string | null; deals: number }[]; color: string; onPick?: (k: string) => void;
  labelOf?: (d: { key: string; label: string }) => string; lang: Lang; unit: string;
}) {
  const rows = data.map(d => ({ ...d, name: labelOf ? labelOf(d) : d.label }));
  return (
    <>
      <div className="chart-box" role="img" aria-label={rows.map(r => `${r.name}: ${fmtExact(r.exact, 2)} UZS`).join("; ")}>
        <ResponsiveContainer width="100%" height={Math.max(170, rows.length * 30 + 34)}>
          <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 20, left: 4, bottom: 4 }}>
            <CartesianGrid stroke={GRID} horizontal={false} />
            <XAxis type="number" tick={tick} stroke={GRID} tickFormatter={v => fmtInt(Math.round(v))} label={{ value: unit, position: "insideBottomRight", offset: -2, fontSize: 12, fill: AXIS }} />
            <YAxis type="category" dataKey="name" width={150} tick={tick} interval={0} stroke={GRID} />
            <Tooltip contentStyle={{ borderRadius: 10, borderColor: "#d8e0da" }} separator="" formatter={(_v, _n, item) => [`${fmtExact((item.payload as typeof rows[number]).exact, 2)} UZS · ${fmtInt((item.payload as typeof rows[number]).deals)} ${t("deals_axis", lang)}`, ""]} />
            <Bar dataKey="v" fill={color} radius={[0, 6, 6, 0]} isAnimationActive={false} cursor={onPick ? "pointer" : undefined}
                 onClick={onPick ? (d) => onPick((d as unknown as { key: string }).key) : undefined} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      {/* keyboard / screen-reader path to the same data and the same click-to-filter action */}
      <details className="tableview"><summary>{t("table_view", lang)}</summary>
        <div className="tv"><table><tbody>{rows.map(r => (
          <tr key={r.key}><td>{onPick ? <button className="link" onClick={() => onPick(r.key)}>{r.name}</button> : r.name}</td>
            <td className="num">{fmtExact(r.exact, 2)} UZS</td><td className="num">{fmtInt(r.deals)}</td></tr>))}</tbody></table></div>
      </details>
    </>
  );
}

function RankCard({ q, lang, onPick }: { q: string; lang: Lang; onPick: (k: string) => void }) {
  const r = useApi<Rank>(`/api/analytics/rank${q}${q ? "&" : "?"}dim=commodity&limit=10`);
  const d = r.data;
  return (
    <section className="card" aria-labelledby="rank-h">
      <h2 id="rank-h">{t("top_commodities", lang)}</h2>
      {d && <p className="sub">{d.truncated ? `top-${d.items.length} / ${d.total_groups} ${t("groups", lang)}` : `${d.items.length} ${t("groups", lang)}`} · {t("bn_axis", lang)} · {t("click_filters", lang)}</p>}
      <State loading={r.loading} error={r.error} empty={d?.empty} reload={r.reload} lang={lang} />
      {d && !d.empty && <HBar lang={lang} unit={t("bn_axis", lang)} color={EVERGREEN} onPick={onPick}
        data={d.items.map(i => ({ key: i.key, label: i.label, v: i.value_uzs?.approx_bn ?? 0, exact: i.value_uzs?.exact ?? null, deals: i.deals }))} />}
    </section>
  );
}

function RegionCard({ q, lang, onPick }: { q: string; lang: Lang; onPick: (k: string) => void }) {
  const r = useApi<Rank>(`/api/analytics/rank${q}${q ? "&" : "?"}dim=region&limit=14`);
  const d = r.data;
  return (
    <section className="card" aria-labelledby="reg-h">
      <h2 id="reg-h">{t("region_rank", lang)}</h2>
      <p className="sub">{t("region_note", lang)} · {t("bn_axis", lang)}</p>
      <State loading={r.loading} error={r.error} empty={d?.empty} reload={r.reload} lang={lang} />
      {d && !d.empty && <HBar lang={lang} unit={t("bn_axis", lang)} color={SAGE} onPick={onPick}
        labelOf={x => { const i = d.items.find(y => y.key === x.key); return (lang === "uz" ? i?.label_uz : i?.label_ru) ?? x.label; }}
        data={d.items.map(i => ({ key: i.key, label: i.label, v: i.value_uzs?.approx_bn ?? 0, exact: i.value_uzs?.exact ?? null, deals: i.deals }))} />}
    </section>
  );
}

function StatusCard({ q, lang }: { q: string; lang: Lang }) {
  const r = useApi<StatusDist>(`/api/analytics/status${q}`);
  const d = r.data;
  const rows = (d?.items ?? []).map(i => ({ ...i, name: lang === "uz" ? i.label_uz : i.label_ru, v: i.deals }));
  return (
    <section className="card" aria-labelledby="st-h">
      <h2 id="st-h">{t("status_dist", lang)}</h2>
      {d && <p className="sub">{t("denominator", lang)}: {fmtInt(d.denominator)} {t("deals_axis", lang)} ({lang === "uz" ? d.status_policy.label_uz : d.status_policy.label_ru})</p>}
      <State loading={r.loading} error={r.error} empty={d?.empty} reload={r.reload} lang={lang} />
      {d && !d.empty && (
        <>
          <div className="chart-box" role="img" aria-label={rows.map(x => `${x.name}: ${fmtInt(x.deals)} (${fmtExact(x.share_pct?.exact)}%)`).join("; ")}>
            <ResponsiveContainer width="100%" height={Math.max(160, rows.length * 32 + 20)}>
              <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 64, left: 4, bottom: 4 }}>
                <XAxis type="number" hide />
                <YAxis type="category" dataKey="name" width={170} tick={tick} interval={0} stroke={GRID} />
                <Tooltip contentStyle={{ borderRadius: 10, borderColor: "#d8e0da" }} separator="" formatter={(_v, _n, item) => { const p = item.payload as typeof rows[number]; return [`${fmtInt(p.deals)} (${fmtExact(p.share_pct?.exact)}%)`, ""]; }} />
                <Bar dataKey="v" radius={[0, 6, 6, 0]} isAnimationActive={false} label={{ position: "right", fontSize: 12, fill: INK, formatter: (v: unknown) => fmtInt(Number(v)) }}>
                  {rows.map(x => <Cell key={x.status_code} fill={STATUS_COLOR[x.status_code] ?? INK} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
          <details className="tableview"><summary>{t("table_view", lang)}</summary>
            <div className="tv"><table><tbody>{rows.map(x => <tr key={x.status_code}><td>{x.name}</td><td className="num">{fmtInt(x.deals)}</td><td className="num">{fmtExact(x.share_pct?.exact)}%</td></tr>)}</tbody></table></div>
          </details>
        </>
      )}
    </section>
  );
}

function PriceCard({ q, lang, variant }: { q: string; lang: Lang; variant?: string }) {
  const r = useApi<Price>(variant ? `/api/analytics/price${q}` : null);
  const d = r.data;
  const cur = d?.currencies?.[0];
  const rows = (d?.series ?? []).filter(s => s.currency === cur);
  return (
    <section className="card" aria-labelledby="pr-h">
      <h2 id="pr-h">{t("price_trend", lang)}</h2>
      {!variant && <p className="note">{t("price_need_variant", lang)}</p>}
      {variant && <State loading={r.loading} error={r.error} empty={d?.empty} reload={r.reload} lang={lang} />}
      {d && !d.available && d.reason && <p className="note">{d.reason}</p>}
      {d && d.available && !d.empty && (
        <>
          <p className="sub">{d.variant_label} · {cur} / 1 {unitLabel(d.unit, lang)} · {t("price_method", lang)}
            {(d.currencies?.length ?? 0) > 1 ? ` · ${lang === "uz" ? "boshqa valyutalar alohida" : "другие валюты отдельно"}: ${d.currencies?.slice(1).join(", ")}` : ""}</p>
          <div className="chart-box" role="img" aria-label={t("price_trend", lang)}>
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={rows} margin={{ top: 8, right: 12, left: 4, bottom: 0 }}>
                <CartesianGrid stroke={GRID} vertical={false} />
                <XAxis dataKey="month" tick={tick} minTickGap={28} stroke={GRID} />
                <YAxis tick={tick} width={84} stroke={GRID} tickFormatter={v => fmtInt(Math.round(v))} />
                <Tooltip contentStyle={{ borderRadius: 10, borderColor: "#d8e0da" }} separator="" formatter={(_v, _n, item) => { const p = item.payload as typeof rows[number]; return [`${fmtExact(p.weighted_price?.exact)} ${p.currency} · ${fmtInt(p.deals)} ${t("deals_axis", lang)}`, ""]; }} />
                <Line dataKey="approx" stroke={INK} strokeWidth={2} dot={{ r: 2 }} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <details className="tableview"><summary>{t("table_view", lang)}</summary>
            <div className="tv"><table><tbody>{rows.map(p => <tr key={p.month}><td>{p.month}</td><td className="num">{fmtExact(p.weighted_price?.exact)} {p.currency}</td><td className="num">{fmtInt(p.deals)}</td></tr>)}</tbody></table></div>
          </details>
        </>
      )}
    </section>
  );
}
