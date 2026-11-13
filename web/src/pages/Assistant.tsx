import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useApp } from "../App";
import { api, post, type ChatReply, type Evidence } from "../api";
import AnswerView from "../components/AnswerView";
import Icon from "../components/Icon";
import PageHeader from "../components/PageHeader";
import { FILTER_KEYS, type Filters } from "../filters";
import { fmtDate, fmtInt } from "../format";
import { SUGGESTIONS, t, type Lang } from "../i18n";

interface Msg { role: "user" | "bot"; text: string; reply?: ChatReply; pending?: boolean; failed?: boolean }

const QUOTA = new Set(["llm_rate_limited", "service_busy", "deadline_exhausted"]);
const NO_DATA = new Set(["no_matching_rows", "missing_field", "out_of_period"]);

function waitText(s: number, lang: Lang) {
  const m = Math.floor(s / 60), sec = Math.round(s % 60);
  return lang === "ru" ? `${m ? `${m} мин ` : ""}${sec} с` : `${m ? `${m} daqiqa ` : ""}${sec} soniya`;
}

/** What kind of reply this is, from the API's status/reason (never inferred from the wording). */
function kindOf(m: Msg): "quota" | "nodata" | "clar" | "fail" | null {
  if (m.failed) return "fail";
  const r = m.reply;
  if (!r) return null;
  if (r.reason && QUOTA.has(r.reason)) return "quota";
  if (r.status === "execution_error" || r.kind === "error") return "fail";
  if (r.status === "ambiguous" || (r.options?.length ?? 0) > 0) return "clar";
  if (r.status && NO_DATA.has(r.status)) return "nodata";
  return null;
}

export default function Assistant() {
  const { lang, meta, refreshMeta, options } = useApp();
  const [sp, setSp] = useSearchParams();
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);  // synchronous guard: a double Enter/click cannot send twice
  const [usedSids, setUsedSids] = useState<Set<string>>(new Set());  // a clarification can be answered once
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // "Ask about this view": dashboard filters are attached only when ctx=1, visibly, and removable
  const ctx: Filters | null = useMemo(() => {
    if (sp.get("ctx") !== "1") return null;
    const f: Filters = {};
    FILTER_KEYS.forEach(k => { const v = sp.get(k); if (v) f[k] = v; });
    return f;
  }, [sp]);
  useEffect(() => {
    if (!msgs.length) return;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const end = endRef.current;
    if (!end) return;
    // the sticky composer must never cover the newest message
    end.style.scrollMarginBottom = `${(document.querySelector(".composer")?.getBoundingClientRect().height ?? 0) + 16}px`;
    end.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "end" });
  }, [msgs]);
  const a = meta?.assistant;
  const send = async (question: string) => {
    const qtext = question.trim();
    if (!qtext || busyRef.current) return;
    busyRef.current = true; setBusy(true);
    setMsgs(m => [...m, { role: "user", text: qtext }, { role: "bot", text: t("thinking", lang), pending: true }]);
    setText("");
    try {
      const body: Record<string, unknown> = { question: qtext, context_lang: lang };
      if (ctx) body.context = { ...ctx, policy: ctx.policy || "registry" };
      const r = await post<ChatReply>("/api/chat/ask", body);
      setMsgs(m => {
        const c = [...m.slice(0, -1), { role: "bot" as const, text: r.text, reply: r }];
        if (r.question_sent && r.question_sent !== qtext) c[c.length - 2] = { role: "user", text: r.question_sent };
        return c;
      });
    } catch (e) {
      setMsgs(m => [...m.slice(0, -1), { role: "bot", text: `${t("error", lang)}: ${(e as Error).message}`, failed: true }]);
    } finally { busyRef.current = false; setBusy(false); refreshMeta(); inputRef.current?.focus(); }
  };
  // dashboard "AI shortcut": ?ask=… is sent once, then removed from the URL (a reload never re-sends it)
  const asked = useRef(false);
  useEffect(() => {
    const ask = sp.get("ask");
    if (!ask || asked.current) return;
    asked.current = true;
    setSp(p => { const n = new URLSearchParams(p); n.delete("ask"); return n; }, { replace: true });
    send(ask);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const choose = async (sid: string, index: number, label: string) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true);
    setUsedSids(u => new Set(u).add(sid));
    setMsgs(m => [...m, { role: "user", text: label }, { role: "bot", text: t("thinking", lang), pending: true }]);
    try {
      const r = await post<ChatReply>("/api/chat/choose", { sid, index });
      setMsgs(m => [...m.slice(0, -1), { role: "bot", text: r.text, reply: r }]);
    } catch (e) { setMsgs(m => [...m.slice(0, -1), { role: "bot", text: `${t("error", lang)}: ${(e as Error).message}`, failed: true }]); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const cancel = async (sid: string) => {
    setUsedSids(u => new Set(u).add(sid));
    try {
      const r = await post<ChatReply>("/api/chat/cancel", { sid, index: 0 });
      setMsgs(m => [...m, { role: "bot", text: r.text, reply: r }]);
    } catch (e) { setMsgs(m => [...m, { role: "bot", text: `${t("error", lang)}: ${(e as Error).message}`, failed: true }]); }
  };
  const ctxLabel = ctx ? Object.entries(ctx).map(([k, v]) => {
    if (k === "commodity") return options?.commodities.find(c => c.key === v)?.label ?? v;
    if (k === "region") { const r = options?.regions.find(c => c.key === v); return r ? (lang === "uz" ? r.uz : r.ru) : v; }
    if (k === "start" || k === "end") return `${k === "start" ? t("from", lang) : t("to", lang)} ${fmtDate(v)}`;
    if (k === "variant") return options?.variants[ctx.commodity || ""]?.find(x => x.key === v)?.label ?? v;
    if (k === "policy") return options?.policies.find(p => p.key === v)?.[lang] ?? v;
    if (k === "form") return options?.forms.find(f => f.key === v)?.[lang] ?? v;
    return v;
  }).join(" · ") : "";
  const started = msgs.length > 0;
  const tags = { quota: t("tag_quota", lang), nodata: t("tag_nodata", lang), clar: t("tag_clar", lang), fail: t("tag_fail", lang) };
  return (
    <div className="page assistant">
      <div className="convo-head">
        <PageHeader title={t("ai_title", lang)} lead={t("ai_lead", lang)} />
        {started && <button className="btn sm" onClick={() => { setMsgs([]); setUsedSids(new Set()); }} disabled={busy}>{t("new_chat", lang)}</button>}
      </div>
      {a && !a.configured && <div className="banner warm" role="status"><Icon name="alert" /> {t("not_configured", lang)}</div>}
      {a && a.configured && a.cooldown_s > 0 && (
        <div className="banner warm" role="status"><Icon name="clock" /> <span>{t("unavailable", lang)} {lang === "uz" ? "Taxminan" : "Примерно"} {waitText(a.cooldown_s, lang)}{a.limit_kind ? ` (${a.limit_kind})` : ""}.</span></div>
      )}
      {!started && (
        <section className="welcome" aria-labelledby="welcome-h">
          <span className="hero-ic"><Icon name="sparkles" /></span>
          <h2 id="welcome-h">{t("ai_welcome", lang)}</h2>
          <p>{t("ai_explain", lang)}</p>
          <div className="suggest">
            {SUGGESTIONS[lang].map(s => (
              <button key={s.q} onClick={() => send(s.q)} disabled={busy}>
                <span className="s-ic"><Icon name={s.icon} /></span>
                <span><span className="s-kind">{s.kind}</span><span className="s-q">{s.q}</span></span>
              </button>
            ))}
          </div>
        </section>
      )}
      {started && (
        <section className="log" aria-live="polite" aria-label={t("nav_assistant", lang)}>
          {msgs.map((m, i) => {
            const k = m.role === "bot" ? kindOf(m) : null;
            return (
              <div key={i} className={`msg ${m.role}${m.pending ? " pending" : ""}${k === "quota" ? " quota" : k === "fail" ? " fail" : ""}`}>
                <span className="who-l">{m.role === "user" ? t("you", lang) : t("assistant_name", lang)}</span>
                {k && <span className={`tag-line ${k}`}>{k === "quota" ? <Icon name="clock" size="sm" /> : k === "fail" ? <Icon name="alert" size="sm" /> : <Icon name="info" size="sm" />} {tags[k]}</span>}
                {m.reply?.blocks && m.reply.blocks.tables.length > 0
                  ? <div className="bubble rich"><AnswerView blocks={m.reply.blocks} banner={m.reply.banner} /></div>
                  : <div className="bubble" aria-busy={m.pending || undefined}>{m.text}</div>}
                {m.reply?.options && m.reply.options.length > 0 && m.reply.sid && !usedSids.has(m.reply.sid) && (
                  <div className="options" role="group" aria-label={t("tag_clar", lang)}>
                    {m.reply.options.map(o => <button key={o.index} className="btn sm" disabled={busy} onClick={() => choose(m.reply!.sid!, o.index, o.label)}>{o.label}</button>)}
                    <button className="link" onClick={() => cancel(m.reply!.sid!)}>{t("cancel", lang)}</button>
                  </div>
                )}
                {m.reply?.evidence_id && <EvidencePanel id={m.reply.evidence_id} />}
              </div>
            );
          })}
          <div ref={endRef} />
        </section>
      )}
      <div className="composer">
        <form onSubmit={e => { e.preventDefault(); send(text); }} aria-label={t("composer_label", lang)}>
          {ctx && (
            <div className="ctx" role="status">
              <span className="muted">{t("context", lang)}:</span>
              <span className="chip">{ctxLabel || t("none", lang)}
                <button type="button" aria-label={`${t("remove", lang)}: ${t("context", lang)}`}
                        onClick={() => setSp(p => { const n = new URLSearchParams(p); n.delete("ctx"); return n; })}>×</button></span>
            </div>
          )}
          <label className="sr-only" htmlFor="ask">{t("composer_label", lang)}</label>
          <textarea id="ask" ref={inputRef} rows={2} maxLength={1000} placeholder={t("ask_placeholder", lang)} value={text}
            aria-describedby="ask-hint" onChange={e => setText(e.target.value)}
            onKeyDown={e => {
              // Enter sends, Shift+Enter = newline; never during IME composition (keyCode 229 for older engines)
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) { e.preventDefault(); send(text); }
            }} />
          <div className="row">
            <span className="hint" id="ask-hint">{t("enter_hint", lang)}</span>
            <button type="submit" className="btn primary" disabled={busy || !text.trim()}><Icon name="send" /> {busy ? t("thinking", lang) : t("send", lang)}</button>
          </div>
        </form>
        {meta && (
          <div className="facts-line">
            <span><Icon name="clock" size="sm" /> {t("time_limit", lang)}: {meta.budget_s} {t("seconds", lang)}</span>
            <span><Icon name="doc" size="sm" /> {t("source_each", lang)}</span>
          </div>
        )}
      </div>
    </div>
  );
}

function EvidencePanel({ id }: { id: string }) {
  const { lang } = useApp();
  const [ev, setEv] = useState<Evidence | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = () => { if (!ev) api<Evidence>(`/api/chat/evidence/${id}`).then(setEv).catch(e => setErr(e.message)); };
  return (
    <details className="evidence" onToggle={e => (e.currentTarget as HTMLDetailsElement).open && load()}>
      <summary><Icon name="doc" size="sm" /> {t("evidence", lang)}</summary>
      {err && <div className="state error" role="alert">{err}</div>}
      {!ev && !err && <span className="sr-only">{t("loading", lang)}</span>}
      {ev && (
        <div className="ev">
          <dl>
            <div className="kv"><dt>query_id</dt><dd><code>{ev.query_id}</code></dd></div>
            <div className="kv"><dt>{lang === "uz" ? "Fayllar" : "Файлы"}</dt><dd>{ev.source_files.map(f => f.file).join(", ") || "—"}</dd></div>
            <div className="kv"><dt>{t("period", lang)}</dt><dd>{ev.period ? JSON.stringify(ev.period) : "—"}</dd></div>
            <div className="kv"><dt>{t("filters", lang)}</dt><dd>{ev.filters.map(f => `${f.field} ${f.op} ${JSON.stringify(f.value)}`).join("; ") || "—"}
              {ev.entities.length > 0 && <> · {ev.entities.map(e => `${e.kind}: ${e.text} → ${e.ids.join(",") || e.status}`).join("; ")}</>}</dd></div>
            <div className="kv"><dt>{t("policy", lang)}</dt><dd>{Object.entries(ev.status_policy).map(([k, v]) => `${k}: ${v.length} ${lang === "uz" ? "holat" : "статусов"}`).join("; ") || "—"}</dd></div>
            <div className="kv"><dt>{lang === "uz" ? "Valyuta / birlik" : "Валюта / единицы"}</dt><dd>{Object.keys(ev.currency_unit).length ? JSON.stringify(ev.currency_unit) : "—"}</dd></div>
            <div className="kv"><dt>{lang === "uz" ? "Tanlovdagi qatorlar" : "Строк в выборке"}</dt><dd className="tnum">{fmtInt(ev.source_row_count)}</dd></div>
            <div className="kv"><dt>{lang === "uz" ? "Misol bitimlar" : "Примеры сделок"}</dt><dd>{ev.sample_ids.join(", ") || "—"} <span className="muted">({t("sample_note", lang)})</span></dd></div>
            <div className="kv"><dt>as_of</dt><dd>{ev.as_of}</dd></div>
          </dl>
          {ev.queries.length > 0 && (
            <details className="sql"><summary>{t("sql", lang)}</summary>
              {ev.queries.map((q, i) => <div key={i}><p className="muted small">{q.purpose} · {q.rows} rows · {q.ms} ms</p><pre>{q.sql}</pre><code>{JSON.stringify(q.params)}</code></div>)}
            </details>
          )}
        </div>
      )}
    </details>
  );
}
