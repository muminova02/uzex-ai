import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useApp } from "../App";
import { api, ApiError, type DealDetail, type Deals } from "../api";
import FilterBar from "../components/FilterBar";
import Icon from "../components/Icon";
import PageHeader from "../components/PageHeader";
import State from "../components/State";
import { toQuery, useApi, useFilters } from "../filters";
import { fmtDate, fmtExact, fmtInt, unitLabel } from "../format";
import { t, type Lang } from "../i18n";

const EXPORT_LIMIT = 50000;  // enforced by the backend (HTTP 413 above it); mirrored here only to explain it up front
const SORTS = { deal_date: "col_deal", quantity: "col_qty", amount_uzs: "col_amount_uzs", lot_count: "col_lots" } as const;
type SortKey = keyof typeof SORTS;
const SIZES = [25, 50, 100];

function pageList(cur: number, last: number): (number | "…")[] {
  const s = new Set([1, 2, 3, cur - 1, cur, cur + 1, last].filter(p => p >= 1 && p <= last));
  const out: (number | "…")[] = [];
  [...s].sort((a, b) => a - b).forEach((p, i, arr) => { if (i && p - arr[i - 1] > 1) out.push("…"); out.push(p); });
  return out;
}

export default function Explorer() {
  const { lang, options } = useApp();
  const [filters, update, reset] = useFilters();
  const [sp, setSp] = useSearchParams();
  const page = Number(sp.get("page") || 1), sort = (sp.get("sort") || "deal_date") as SortKey, dir = sp.get("dir") || "desc", q = sp.get("q") || "";
  const size = SIZES.includes(Number(sp.get("size"))) ? Number(sp.get("size")) : 25;
  const [search, setSearch] = useState(q);
  const [open, setOpen] = useState<{ id: string; from: HTMLElement | null } | null>(null);
  const [exportErr, setExportErr] = useState<string | null>(null);
  useEffect(() => { setSearch(q); }, [q]);
  const setParam = (patch: Record<string, string | undefined>) => setSp(prev => {
    const n = new URLSearchParams(prev);
    Object.entries(patch).forEach(([k, v]) => (v ? n.set(k, v) : n.delete(k)));
    return n;
  });
  const fq = toQuery(filters);
  useEffect(() => { if (page !== 1) setParam({ page: undefined }); /* eslint-disable-next-line */ }, [fq]);
  const r = useApi<Deals>(`/api/deals?${toQuery(filters, { q: q || undefined, sort, dir, page, size })}`);
  const d = r.data;
  const regionName = (k: string) => { const x = options?.regions.find(y => y.key === k); return x ? (lang === "uz" ? x.uz : x.ru) : k; };
  const statusName = (code: string, raw: string) => options?.statuses.find(s => s.key === code)?.[lang] ?? raw;
  const tooMany = !!d && d.total > EXPORT_LIMIT;
  const doExport = async () => {
    setExportErr(null);
    const href = `/api/deals/export.csv?${toQuery(filters, { q: q || undefined, sort, dir })}`;
    try {  // ask the backend first so a refusal is shown as text, never as a silently truncated file
      const head = await fetch(href, { method: "HEAD" });
      if (head.status === 413) { setExportErr(t("export_need_filter", lang)); return; }
    } catch { /* HEAD unsupported: fall through to the normal download, the backend still enforces the limit */ }
    window.location.href = href;
  };
  const setSort = (key: SortKey, newDir?: string) => setParam({ sort: key, dir: newDir ?? (sort === key && dir === "desc" ? "asc" : "desc"), page: undefined });
  const th = (key: SortKey, label: string, num = false) => (
    <th className={num ? "num" : undefined} aria-sort={sort === key ? (dir === "asc" ? "ascending" : "descending") : "none"} scope="col">
      <button className="sort" onClick={() => setSort(key)}>{label}<span aria-hidden="true">{sort === key ? (dir === "asc" ? "▲" : "▼") : "↕"}</span></button>
    </th>
  );
  return (
    <div className="page">
      <PageHeader title={t("deals_title", lang)} />
      <FilterBar filters={filters} update={update} reset={reset} />
      <section className="card deals-card" aria-label={t("table_label", lang)}>
        <div className="toolbar">
          <form className="search" role="search" onSubmit={e => { e.preventDefault(); setParam({ q: search.trim() || undefined, page: undefined }); }}>
            <label className="sr-only" htmlFor="q">{t("search", lang)}</label>
            <span className="inp"><Icon name="search" />
              <input id="q" inputMode="numeric" autoComplete="off" placeholder={lang === "uz" ? "Bitim / shartnoma raqami yoki STIR" : "Номер сделки / контракта или ИНН"}
                     value={search} onChange={e => setSearch(e.target.value)} /></span>
            <button type="submit" className="btn primary">{t("search_btn", lang)}</button>
          </form>
          <div className="export">
            <button type="button" className="btn" onClick={doExport} disabled={!d || d.total === 0 || tooMany}
                    aria-describedby="export-why"><Icon name="download" /> {t("export", lang)}</button>
            <span id="export-why" className={`why${tooMany || exportErr ? " warn" : ""}`} role={exportErr ? "alert" : undefined}>
              {exportErr ?? (tooMany ? `${t("export_need_filter", lang)} (${t("found", lang).toLowerCase()}: ${fmtInt(d!.total)})`
                : d && d.total > 0 ? `${t("export_ready", lang)}: ${fmtInt(d.total)} ${t("rows", lang)}` : "")}
            </span>
          </div>
        </div>
        {q && <div className="filter-meta" style={{ marginTop: 10 }}><span className="chip">{t("search_btn", lang)}: <b>{q}</b>
          <button aria-label={t("clear_search", lang)} onClick={() => setParam({ q: undefined, page: undefined })}>×</button></span></div>}
        {d && !d.empty && (
          <div className="result-line" aria-live="polite">
            <span className="count tnum">{fmtInt(d.total)} {t("deals_count", lang)}</span>
            <span className="sep" aria-hidden="true" />
            <span className="muted">{t("page", lang)} <span className="tnum">{fmtInt(d.page)} / {fmtInt(d.pages)}</span></span>
            <label className="sort-ctl muted small">{t("sort_by", lang)}
              <select value={`${sort}:${dir}`} onChange={e => { const [k, dd] = e.target.value.split(":"); setSort(k as SortKey, dd); }}>
                {(Object.keys(SORTS) as SortKey[]).flatMap(k => ["desc", "asc"].map(dd =>
                  <option key={`${k}:${dd}`} value={`${k}:${dd}`}>{t(SORTS[k], lang)} · {t(dd as "asc" | "desc", lang)}</option>))}
              </select>
            </label>
          </div>
        )}
        {r.error ? <State loading={false} error={new Error((r.error as ApiError).body?.message || r.error.message)} reload={r.reload} lang={lang} />
          : r.loading ? <State loading error={null} reload={r.reload} lang={lang} rows={6} />
          : d && d.empty ? <State loading={false} error={null} empty reload={r.reload} lang={lang} /> : null}
        {d && !d.empty && !r.loading && (
          <>
            <div className="table-wrap" tabIndex={0} role="region" aria-label={`${t("table_label", lang)} — ${t("page", lang)} ${d.page}`}>
              <table className="deals">
                <thead><tr>
                  {th("deal_date", t("col_deal", lang))}
                  <th scope="col">{t("col_product", lang)}</th>
                  <th scope="col">{t("col_parties", lang)}</th>
                  {th("quantity", t("col_qty", lang), true)}
                  {th("amount_uzs", t("col_amount_uzs", lang), true)}
                  <th scope="col">{t("col_status", lang)}</th>
                  <th scope="col"><span className="sr-only">{t("details", lang)}</span></th>
                </tr></thead>
                <tbody>
                  {d.rows.map(row => {
                    const id = String(row.deal_id);
                    const unit = unitLabel(String(row.quantity_unit), lang);
                    return (
                      <tr key={id}>
                        <td><div className="d-id">№{id}</div><div className="d-sub tnum">{fmtDate(String(row.deal_date))}</div></td>
                        <td><div className="d-prod">{String(row.commodity_name_ru ?? row.deal_product_name)}</div>
                          {row.commodity_name_ru && row.deal_product_name !== row.commodity_name_ru && <div className="d-sub">{String(row.deal_product_name)}</div>}
                          <div className="d-sub">{regionName(String(row.warehouse_region_key))}</div></td>
                        <td><div className="role">
                          <span className="tag">{t("seller_tag", lang)}</span><span className="who">{String(row.seller_name)}</span>
                          <span className="tag">{t("buyer_tag", lang)}</span><span className="who">{String(row.buyer_name)}</span>
                        </div></td>
                        <td className="num"><div>{fmtInt(Number(row.quantity_native))} {unit}</div>
                          <div className="d-sub">{fmtInt(Number(row.lot_count))} × {fmtInt(Number(row.lot_size))}</div></td>
                        <td className="num"><div><b>{fmtExact(String(row.amount_uzs))}</b></div>
                          {row.currency !== "UZS" && <div className="d-sub">{fmtExact(String(row.raw_amount))} {String(row.currency)}</div>}</td>
                        <td><span className={`badge b-${row.status_code}`}>{statusName(String(row.status_code), String(row.status))}</span></td>
                        <td><button className="row-btn" aria-label={`№${id} — ${t("open_details", lang)}`}
                                    onClick={e => setOpen({ id, from: e.currentTarget })}><Icon name="chevron" /></button></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <nav className="pager" aria-label={t("pages_nav", lang)}>
              <div className="pages">
                <button className="btn sm" disabled={d.page <= 1} onClick={() => setParam({ page: String(d.page - 1) })}>‹ {t("prev", lang)}</button>
                {pageList(d.page, d.pages).map((p, i) => p === "…" ? <span key={`g${i}`} className="gap" aria-hidden="true">…</span> :
                  <button key={p} className="pg" aria-current={p === d.page ? "page" : undefined} aria-label={`${t("page", lang)} ${p}`}
                          onClick={() => setParam({ page: p === 1 ? undefined : String(p) })}>{fmtInt(p)}</button>)}
                <button className="btn sm" disabled={d.page >= d.pages} onClick={() => setParam({ page: String(d.page + 1) })}>{t("next", lang)} ›</button>
              </div>
              <label className="size-ctl"><span className="sr-only">{t("per_page", lang)}</span>
                <select value={size} onChange={e => setParam({ size: e.target.value === "25" ? undefined : e.target.value, page: undefined })}>
                  {SIZES.map(s => <option key={s} value={s}>{s} / {t("per_page", lang)}</option>)}
                </select>
              </label>
            </nav>
          </>
        )}
      </section>
      {open && <DealPanel id={open.id} lang={lang} onClose={() => { const f = open.from; setOpen(null); setTimeout(() => f?.focus(), 0); }} />}
    </div>
  );
}

/** Deal detail: modal side panel; focus moves in, Tab is trapped, Escape/overlay close, focus returns to the row button. */
function DealPanel({ id, onClose, lang }: { id: string; onClose: () => void; lang: Lang }) {
  const [data, setData] = useState<DealDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const ref = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const c = new AbortController();
    setData(null); setErr(null);
    api<DealDetail>(`/api/deals/${id}`, { signal: c.signal }).then(setData).catch(e => e.name !== "AbortError" && setErr(e.message));
    return () => c.abort();
  }, [id]);
  useEffect(() => {
    ref.current?.querySelector<HTMLElement>("button")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); closeRef.current(); return; }
      if (e.key !== "Tab" || !ref.current) return;
      const f = Array.from(ref.current.querySelectorAll<HTMLElement>("button, summary, a[href]"));
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
  const x = data?.deal;
  const ru = lang === "ru";
  const row = (k: string, v: React.ReactNode) => <div className="kv"><dt>{k}</dt><dd>{v ?? "—"}</dd></div>;
  return (
    <div className="panel-back" onClick={onClose}>
      <aside className="panel" ref={ref} role="dialog" aria-modal="true" aria-labelledby="deal-h" onClick={e => e.stopPropagation()}>
        <div className="panel-head"><h2 id="deal-h">{t("details", lang)} №{id}</h2>
          <button className="icon-btn" onClick={onClose} aria-label={t("close", lang)}><Icon name="close" /></button></div>
        {err && <div className="state error" role="alert">{err}</div>}
        {!x && !err && <div className="state" role="status">{t("loading", lang)}</div>}
        {x && (
          <dl>
            {row(ru ? "Дата / время" : "Sana / vaqt", `${fmtDate(String(x.deal_date))} ${x.deal_time}`)}
            {row(ru ? "Статус" : "Holat", <span className={`badge b-${x.status_code}`}>{String(x.status)}</span>)}
            {row(ru ? "Товар" : "Tovar", String(x.deal_product_name))}
            {row(ru ? "Контракт" : "Shartnoma", `№${x.contract_id} · ${x.contract_form} · ${x.contract_type} · ${x.trade_type}`)}
            {row(ru ? "Лоты × размер лота" : "Lotlar × lot hajmi", `${fmtInt(Number(x.lot_count))} × ${fmtInt(Number(x.lot_size))} = ${fmtInt(Number(x.quantity_native))} ${unitLabel(String(x.quantity_unit), lang)}`)}
            {row(ru ? "Сумма (исходная)" : "Summa (asl)", <span className="tnum">{fmtExact(String(x.raw_amount))} {String(x.currency)}</span>)}
            {x.currency === "USD" && row(ru ? "Курс на дату сделки" : "Bitim sanasidagi kurs", <span className="tnum">{fmtExact(String(x.fx_rate_applied))} UZS / 1 USD</span>)}
            {row(ru ? "Сумма в сумах" : "So'mdagi summa", <span className="tnum">{fmtExact(String(x.amount_uzs))} UZS</span>)}
            {row(ru ? "Цена за единицу" : "Birlik narxi", <span className="tnum">{fmtExact(String(x.unit_price))} {String(x.currency)} / 1 {unitLabel(String(x.quantity_unit), lang)}</span>)}
            {row(ru ? "Поставлено / не поставлено" : "Yetkazilgan / yetkazilmagan",
              x.delivered_quantity == null ? (ru ? "поставки не было" : "yetkazish bo'lmagan") : `${fmtInt(Number(x.delivered_quantity))} / ${fmtInt(Number(x.remaining_quantity))} ${unitLabel(String(x.quantity_unit), lang)}`)}
            {row(ru ? "Оплата (срок)" : "To'lov (muddat)", `${fmtDate(x.payment_date as string)} (${fmtDate(String(x.payment_due_date))})`)}
            {row(ru ? "Поставка (срок)" : "Yetkazish (muddat)", `${fmtDate(x.delivery_date as string)} (${fmtDate(x.delivery_due_date as string)})`)}
            {row(ru ? "Продавец" : "Sotuvchi", `${x.seller_name} · ${ru ? "ИНН" : "STIR"} ${x.seller_inn} · ${x.seller_region}`)}
            {row(ru ? "Брокер продавца" : "Sotuvchi brokeri", `${x.seller_broker_name} (${x.seller_broker_code})`)}
            {row(ru ? "Покупатель" : "Xaridor", `${x.buyer_name} · ${ru ? "ИНН" : "STIR"} ${x.buyer_inn} · ${x.buyer_region}`)}
            {row(ru ? "Брокер покупателя" : "Xaridor brokeri", `${x.buyer_broker_name} (${x.buyer_broker_code})`)}
            {row(ru ? "Склад" : "Ombor", `${x.warehouse_region} · ${x.warehouse_address} · ${x.delivery_basis}`)}
            {row(ru ? "Источник" : "Manba", `${x.source_file}, ${ru ? "строка данных" : "ma'lumot qatori"} ${x.source_row}`)}
          </dl>
        )}
        {data && <details className="sql"><summary>{t("sql", lang)}</summary><pre>{data.evidence.query}</pre><code>{JSON.stringify(data.evidence.params)}</code></details>}
      </aside>
    </div>
  );
}
