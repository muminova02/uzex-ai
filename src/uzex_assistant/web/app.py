"""FastAPI web layer over the existing services (same origin as the built frontend).

* Analytics, explorer, export, help, evidence: fixed application SQL via the existing read-only,
  killable executor. No LLM call is ever made on these routes.
* Chat: the existing BotCore + QueryService (same queue, deterministic renderer, clarification sessions,
  local help and explanations). Every browser gets an anonymous random session in an HttpOnly cookie;
  answers, evidence and clarifications are stored under that session, so a guessed id from another
  session returns 404.
* Provider quota is coordinated with the Telegram bot and scripts through the shared SQLite gate.
* No secrets are ever returned; there is no arbitrary-SQL endpoint.

Run:  .venv/bin/python -m uzex_assistant.web            (serves web/dist and /api on UZEX_WEB_HOST:UZEX_WEB_PORT)
"""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import hashlib
import html
import io
import json
import re
import secrets
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import schema as S
from ..bot_core import BotCore
from ..buildinfo import build_info, freeze_file
from ..config import PROJECT_ROOT, Settings, get_settings
from ..engine import Engine
from ..qlog import QuestionLog, anon
from ..render import POLICY, STATUS
from ..service import QueryService
from . import analytics as A

DIST = PROJECT_ROOT / "web" / "dist"
COOKIE = "uzex_sid"


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    context: A.FilterSpec | None = None  # visible dashboard context the user chose to attach
    context_lang: Literal["uz", "ru"] = "uz"


class ChooseBody(BaseModel):
    sid: str = Field(max_length=40)
    index: int = Field(ge=0, le=20)


class AppState:
    def __init__(self, settings: Settings, engine: Engine):
        self.s, self.engine = settings, engine
        self.catalog = engine.catalog
        self.coverage = engine.coverage
        con = None
        import duckdb
        con = duckdb.connect(str(settings.db_path), read_only=True)
        try:
            ident = con.execute("select identity_json from import_meta").fetchone()[0]
        finally:
            con.close()
        self.fingerprint = hashlib.sha256(ident.encode()).hexdigest()[:16]
        self.db_mtime = Path(settings.db_path).stat().st_mtime
        self.cache: OrderedDict[str, dict] = OrderedDict()
        self.service = QueryService(engine, max_concurrent=settings.bot_max_concurrent, max_waiting=settings.bot_max_waiting)
        self.chat = BotCore(self.service, session_ttl_s=settings.bot_session_ttl_s, context_ttl_s=settings.bot_context_ttl_s)

    def meta(self) -> dict:
        lo, hi = self.coverage
        return {"dataset": {"coverage_start": lo.isoformat(), "coverage_end": hi.isoformat(), "as_of": str(self.engine.as_of),
                            "synthetic": True, "fingerprint": self.fingerprint}}


def _freeze_status() -> dict:
    f = freeze_file()
    if not f.is_file():
        return {"frozen": False}
    fr = json.loads(f.read_text(encoding="utf-8"))
    return {"frozen": True, "frozen_at": fr.get("frozen_at"), "build_matches": fr.get("build_id") == build_info().get("build_id")}


def _cache_key(st: AppState, name: str, spec: A.FilterSpec, **extra) -> str:
    return json.dumps([st.fingerprint, name, spec.model_dump(mode="json"), extra], sort_keys=True, default=str)


async def run_sql(st: AppState, sql: str, params: list, max_rows: int | None = None):
    out = await asyncio.to_thread(st.engine.executor.execute, sql, params, None, max_rows)
    if not out.ok:
        status = 503 if out.error_kind in ("busy", "timeout") else 500
        raise HTTPException(status, {"error": out.error_kind, "message": "database query failed" if status == 500 else
                                     "the database is busy or the query took too long; please retry"})
    return out


async def cached(st: AppState, key: str, compute):
    if key in st.cache:
        st.cache.move_to_end(key)
        return st.cache[key]
    val = await compute()
    st.cache[key] = val
    while len(st.cache) > 512:
        st.cache.popitem(last=False)
    return val


def get_state(request: Request) -> AppState:
    st: AppState = request.app.state.uzex
    m = Path(st.s.db_path).stat().st_mtime
    if m != st.db_mtime:  # the prepared DB was rebuilt: never serve stale aggregates
        st.cache.clear()
        st.db_mtime = m
    return st


def filters(request: Request, st: AppState = Depends(get_state),
            start: str | None = None, end: str | None = None, commodity: str | None = None, variant: str | None = None,
            region: str | None = None, form: str | None = None, policy: str = "registry", currency: str | None = None) -> A.FilterSpec:
    try:
        spec = A.FilterSpec.model_validate({"start": start or None, "end": end or None, "commodity": commodity or None,
                                            "variant": variant or None, "region": region or None, "form": form or None,
                                            "policy": policy or "registry", "currency": currency or None})
        return A.validate(spec, st.catalog, st.coverage)
    except A.FilterError as e:
        raise HTTPException(422, {"error": "invalid_filter", "field": e.field, "message": e.message})
    except Exception as e:  # pydantic validation
        raise HTTPException(422, {"error": "invalid_filter", "message": str(e).splitlines()[0][:200]})


def envelope(st: AppState, spec: A.FilterSpec, data: dict) -> dict:
    pol = spec.policy
    return {**st.meta(), "applied_filters": spec.model_dump(mode="json"),
            "status_policy": {"name": pol, "statuses": list(S.STATUS_POLICIES[pol]),
                              "includes_cancelled": "cancelled_unpaid" in S.STATUS_POLICIES[pol],
                              "label_ru": POLICY[pol][0], "label_uz": POLICY[pol][1]}, **data}


# ====================================================================== app factory
def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or get_settings()
        eng = engine or Engine(s)
        app.state.uzex = AppState(s, eng)
        app.state.qlog = QuestionLog(s)
        yield
        if engine is None:
            eng.close()

    app = FastAPI(title="UZEX Data Assistant API (hackathon prototype)", lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    # ---------------------------------------------------------------- meta / options (no LLM)
    @app.get("/api/meta")
    async def meta(st: AppState = Depends(get_state)):
        prov = st.engine.provider
        cd = prov.cooldown_remaining() if hasattr(prov, "cooldown_remaining") else 0.0
        lim = getattr(prov, "last_limit", None) if cd > 0 else None
        name = getattr(prov, "name", None)
        mode = "demo" if name == "mock" else ("production" if st.engine.planner is not None else "no_llm")
        return {**st.meta(), "mode": mode, "assistant": {"configured": st.engine.planner is not None,
                                           "provider": getattr(prov, "name", None), "model": getattr(prov, "model", None),
                                           "cooldown_s": round(cd, 1), "limit_kind": (lim or {}).get("kind")},
                "budget_s": st.s.request_budget_s, "build": build_info()}

    @app.get("/api/health")
    async def health(st: AppState = Depends(get_state)):
        """Liveness + readiness: the read-only DB answers a trivial query. Never calls the LLM."""
        out = await asyncio.to_thread(st.engine.executor.execute, "select count(*) from deals", [], None, 1)
        if not out.ok:
            raise HTTPException(503, {"ok": False, "db": out.error_kind})
        bi = build_info()
        return {"ok": True, "db": "ok", "fingerprint": st.fingerprint, "build_id": bi.get("build_id"),
                "prompt_version": bi.get("prompt_version"), "llm_configured": st.engine.planner is not None,
                "freeze": _freeze_status()}

    @app.get("/api/filters/options")
    async def options(st: AppState = Depends(get_state)):
        c = st.catalog
        variants = {}
        for v in c.variants.values():
            variants.setdefault(v["commodity_key"], []).append(
                {"key": v["variant_key"], "label": v.get("display") or v["variant_key"], "unit": v["unit"], "deals": v["deals"]})
        for vs in variants.values():
            vs.sort(key=lambda x: -x["deals"])
        return {**st.meta(),
                "commodities": sorted(({"key": k, "label": x["name_ru"], "deals": sum(v["deals"] for v in variants.get(k, []))}
                                       for k, x in c.commodities.items()), key=lambda x: -x["deals"]),
                "variants": variants,
                "regions": [{"key": k, "uz": r["uz"], "ru": r["ru"]} for k, r in c.regions.items()],
                "forms": [{"key": k, "ru": v[0], "uz": v[1]} for k, v in
                          {"spot": ("спот", "spot"), "forward": ("форвард", "forvard"), "futures": ("фьючерс", "fyuchers")}.items()],
                "policies": [{"key": k, "ru": v[0], "uz": v[1], "includes_cancelled": "cancelled_unpaid" in S.STATUS_POLICIES[k]}
                             for k, v in POLICY.items()],
                "currencies": ["UZS", "USD"],
                "statuses": [{"key": k, "ru": v[0], "uz": v[1]} for k, v in STATUS.items()]}

    # ---------------------------------------------------------------- analytics (no LLM)
    @app.get("/api/analytics/overview")
    async def overview(spec: A.FilterSpec = Depends(filters), st: AppState = Depends(get_state)):
        async def compute():
            out = await run_sql(st, *A.q_overview(spec))
            deals, value, sellers, buyers, cancelled, usd = out.rows[0]
            return envelope(st, spec, {
                "deals": deals, "value_uzs": A.money(value), "distinct_sellers": sellers, "distinct_buyers": buyers,
                "cancelled": {"count": cancelled, "denominator": deals, "share_pct": A.ratio(Decimal(cancelled) * 100, deals) if deals else None,
                              "scope_note": "share of cancelled deals among the deals matching the current filters and status policy"},
                "usd_converted_deals": usd, "empty": deals == 0})
        return await cached(st, _cache_key(st, "overview", spec), compute)

    @app.get("/api/analytics/trend")
    async def trend(spec: A.FilterSpec = Depends(filters), st: AppState = Depends(get_state)):
        async def compute():
            out = await run_sql(st, *A.q_trend(spec), max_rows=200)
            got = {(r[0], r[1]): r for r in out.rows}
            months, y, m = [], spec.start.year, spec.start.month
            while (y, m) <= (spec.end.year, spec.end.month):
                first = dt.date(y, m, 1)
                last = dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
                r = got.get((y, m))
                months.append({"month": f"{y}-{m:02d}", "deals": r[2] if r else 0,
                               "value_uzs": A.money(r[3]) if r else None,  # no deals: value undefined, not 0
                               "usd_converted": r[4] if r else 0,
                               "partial": spec.start > first or spec.end < last,
                               "covered_from": max(spec.start, first).isoformat(), "covered_to": min(spec.end, last).isoformat()})
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            return envelope(st, spec, {"months": months, "empty": not out.rows})
        return await cached(st, _cache_key(st, "trend", spec), compute)

    @app.get("/api/analytics/rank")
    async def rank(dim: Literal["commodity", "category", "region"] = "commodity", limit: int = Query(10, ge=1, le=50),
                   spec: A.FilterSpec = Depends(filters), st: AppState = Depends(get_state)):
        async def compute():
            out = await run_sql(st, *A.q_rank(spec, dim, limit))
            items = []
            for key, label, value, deals, total in out.rows:
                item = {"key": key, "label": label, "value_uzs": A.money(value), "deals": deals}
                if dim == "region":
                    r = st.catalog.regions[key]
                    item.update(label_uz=r["uz"], label_ru=r["ru"])
                items.append(item)
            total = out.rows[0][4] if out.rows else 0
            return envelope(st, spec, {"dim": dim, "items": items, "top_n": limit, "total_groups": total,
                                       "truncated": total > len(items), "empty": not items})
        return await cached(st, _cache_key(st, "rank", spec, dim=dim, limit=limit), compute)

    @app.get("/api/analytics/status")
    async def status(spec: A.FilterSpec = Depends(filters), st: AppState = Depends(get_state)):
        async def compute():
            out = await run_sql(st, *A.q_status(spec))
            total = sum(r[2] for r in out.rows)
            items = [{"status_code": c, "status": s_, "label_ru": STATUS[c][0], "label_uz": STATUS[c][1], "deals": n,
                      "share_pct": A.ratio(Decimal(n) * 100, total)} for c, s_, n in out.rows]
            return envelope(st, spec, {"items": items, "denominator": total, "empty": total == 0})
        return await cached(st, _cache_key(st, "status", spec), compute)

    @app.get("/api/analytics/price")
    async def price(spec: A.FilterSpec = Depends(filters), st: AppState = Depends(get_state)):
        if not spec.variant:
            return envelope(st, spec, {"available": False, "reason": "variant_required", "series": []})

        async def compute():
            out = await run_sql(st, *A.q_price(spec), max_rows=200)
            series = [{"month": f"{y}-{m:02d}", "currency": cur, "unit": unit, "deals": n,
                       "weighted_price": A.ratio(num, den), "approx": float(Decimal(num) / Decimal(den)) if den else None}
                      for y, m, cur, unit, num, den, n in out.rows]
            v = st.catalog.variants[spec.variant]
            return envelope(st, spec, {"available": True, "variant_label": v.get("display"), "unit": v["unit"],
                                       "currencies": sorted({s["currency"] for s in series}), "series": series,
                                       "method": "weighted: sum(raw_amount) / sum(quantity) per month and currency; cancelled deals excluded",
                                       "empty": not series})
        return await cached(st, _cache_key(st, "price", spec), compute)

    # ---------------------------------------------------------------- deal explorer (no LLM)
    @app.get("/api/deals")
    async def deals(q: str | None = None, sort: Literal[tuple(A.SORTS)] = "deal_date", dir: Literal["asc", "desc"] = "desc",  # type: ignore[valid-type]
                    page: int = Query(1, ge=1, le=100000), size: int = Query(25, ge=1, le=A.PAGE_MAX),
                    spec: A.FilterSpec = Depends(filters), st: AppState = Depends(get_state)):
        try:
            count_sql = A.q_count(spec, q)
            rows_sql = A.q_deals(spec, q, sort, dir, size, (page - 1) * size)
        except A.FilterError as e:
            raise HTTPException(422, {"error": "invalid_filter", "field": e.field, "message": e.message})
        total = (await run_sql(st, *count_sql)).rows[0][0]
        out = await run_sql(st, *rows_sql, max_rows=size)
        rows = [{c: (format(v, "f") if isinstance(v, Decimal) else v.isoformat() if hasattr(v, "isoformat") else v)
                 for c, v in zip(A.DEAL_COLS, r)} for r in out.rows]
        return envelope(st, spec, {"rows": rows, "total": total, "page": page, "size": size, "sort": sort, "dir": dir,
                                   "pages": (total + size - 1) // size, "q": q, "empty": total == 0})

    @app.get("/api/deals/export.csv")
    async def export(q: str | None = None, sort: Literal[tuple(A.SORTS)] = "deal_date", dir: Literal["asc", "desc"] = "desc",  # type: ignore[valid-type]
                     spec: A.FilterSpec = Depends(filters), st: AppState = Depends(get_state)):
        try:
            total = (await run_sql(st, *A.q_count(spec, q))).rows[0][0]
            sql = A.q_export(spec, q, sort, dir)
        except A.FilterError as e:
            raise HTTPException(422, {"error": "invalid_filter", "field": e.field, "message": e.message})
        if total > A.EXPORT_MAX_ROWS:
            raise HTTPException(413, {"error": "export_too_large", "total": total, "limit": A.EXPORT_MAX_ROWS,
                                      "message": f"{total} deals match; narrow the filters to at most {A.EXPORT_MAX_ROWS} rows"})
        out = await run_sql(st, *sql, max_rows=A.EXPORT_MAX_ROWS)

        def gen():
            buf = io.StringIO()
            w = csv.writer(buf)
            buf.write("﻿")
            w.writerow(A.EXPORT_COLS)
            for r in out.rows:
                w.writerow([A.csv_cell(v) for v in r])
                if buf.tell() > 1 << 16:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
            yield buf.getvalue()
        name = f"uzex_deals_{spec.start}_{spec.end}_{total}rows.csv"
        return StreamingResponse(gen(), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"', "X-Total-Rows": str(total)})

    @app.get("/api/deals/{deal_id}")
    async def deal_detail(deal_id: str, st: AppState = Depends(get_state)):
        if not re.fullmatch(r"\d{6}", deal_id):
            raise HTTPException(422, {"error": "invalid_deal_id", "message": "deal id must be 6 digits"})
        out = await run_sql(st, *A.q_detail(deal_id))
        if not out.rows:
            raise HTTPException(404, {"error": "not_found", "message": f"deal {deal_id} not found"})
        d = {c: (format(v, "f") if isinstance(v, Decimal) else v.isoformat() if hasattr(v, "isoformat") else v)
             for c, v in zip(A.DETAIL_COLS, out.rows[0])}
        return {**st.meta(), "deal": d, "evidence": {"source_file": d["source_file"], "source_row": d["source_row"],
                                                       "query": A.q_detail(deal_id)[0], "params": [deal_id]}}

    # ---------------------------------------------------------------- chat (LLM only for planning new questions)
    def session_key(request: Request, response: Response) -> tuple[str, str]:
        sid = request.cookies.get(COOKIE)
        if not sid or not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", sid):
            sid = secrets.token_urlsafe(32)
            response.set_cookie(COOKIE, sid, httponly=True, samesite="lax", max_age=7 * 86400, path="/")
        return ("web:" + sid, "web:" + sid)

    def chat_payload(outs, st: AppState, key) -> dict:
        text = "\n".join(html.unescape(re.sub(r"</?(b|i|pre|code)>", "", o.html)) for o in outs)
        options, sid, evidence_id = [], None, None
        for o in outs:
            for row in o.buttons:
                for label, data in row:
                    parts = data.split(":")
                    if parts[0] == "c":
                        sid = parts[1]
                        options.append({"index": int(parts[2]), "label": label})
                    elif parts[0] == "x":
                        sid = parts[1]
                    elif parts[0] == "s":
                        evidence_id = parts[1]
        kind = st.chat.last_kind.get(key, "answer")
        last = (st.chat.last.get(key) or {}) if kind == "answer" else {}
        return {"kind": kind, "text": text, "options": options, "sid": sid, "evidence_id": evidence_id,
                "status": last.get("status"), "reason": last.get("reason"), "lang": last.get("lang"),
                "timings_ms": last.get("timings_ms"), "blocks": last.get("blocks"), "banner": last.get("banner")}

    def context_text(spec: A.FilterSpec, lang: str, st: AppState) -> str:
        c = st.catalog
        bits = [f"{spec.start}–{spec.end}"]
        if spec.variant:
            bits.append(c.variants[spec.variant].get("display") or spec.variant)
        elif spec.commodity:
            bits.append(c.commodities[spec.commodity]["name_ru"])
        if spec.region:
            bits.append(("склад: " if lang == "ru" else "ombor: ") + c.regions[spec.region]["ru" if lang == "ru" else "uz"])
        if spec.form:
            bits.append(spec.form)
        if spec.currency:
            bits.append(spec.currency)
        if spec.policy != "registry":
            bits.append(POLICY[spec.policy][0 if lang == "ru" else 1])
        return ("(Контекст: " if lang == "ru" else "(Kontekst: ") + "; ".join(bits) + ")"

    def log_question(request: Request, key, q: str, payload: dict, t0: float) -> None:
        ql = getattr(request.app.state, "qlog", None)
        if ql is not None:
            st = request.app.state.uzex
            last = (st.chat.last.get(key) or {}) if payload.get("kind") == "answer" else {}
            ql.write("web", {"ip": request.client.host if request.client else None, "session": anon(key[0][4:])},
                     q, payload.get("kind"), last, (time.perf_counter() - t0) * 1000, payload.get("text"))

    @app.post("/api/chat/ask")
    async def chat_ask(body: AskBody, request: Request, response: Response, st: AppState = Depends(get_state)):
        key = session_key(request, response)
        q = body.question.strip()
        if body.context is not None:
            try:
                spec = A.validate(body.context, st.catalog, st.coverage)
            except A.FilterError as e:
                raise HTTPException(422, {"error": "invalid_filter", "field": e.field, "message": e.message})
            q = f"{q} {context_text(spec, body.context_lang, st)}"
        t0 = time.perf_counter()
        outs = await st.chat.on_text(*key, q)
        payload = chat_payload(outs, st, key)
        log_question(request, key, q, payload, t0)
        return {"question_sent": q, **payload}

    @app.post("/api/chat/choose")
    async def chat_choose(body: ChooseBody, request: Request, response: Response, st: AppState = Depends(get_state)):
        key = session_key(request, response)
        t0 = time.perf_counter()
        toast, outs = await st.chat.on_callback(*key, f"c:{body.sid}:{body.index}")
        if not toast:
            log_question(request, key, f"[choice {body.index}]", chat_payload(outs, st, key), t0)
        if toast:
            return {"kind": "notice", "text": toast, "options": [], "sid": None, "evidence_id": None, "status": None}
        return chat_payload(outs, st, key)

    @app.post("/api/chat/cancel")
    async def chat_cancel(body: ChooseBody, request: Request, response: Response, st: AppState = Depends(get_state)):
        key = session_key(request, response)
        toast, outs = await st.chat.on_callback(*key, f"x:{body.sid}")
        return {"kind": "notice", "text": toast or chat_payload(outs, st, key)["text"], "options": [], "sid": None, "evidence_id": None}

    @app.get("/api/chat/evidence/{evidence_id}")
    async def chat_evidence(evidence_id: str, request: Request, response: Response, st: AppState = Depends(get_state)):
        key = session_key(request, response)
        rec = st.chat.answers.get(evidence_id)
        if rec is None or (rec.chat_id, rec.user_id) != key or rec.expires < st.chat.clock():
            raise HTTPException(404, {"error": "not_found", "message": "evidence not found for this session"})
        res, ev = rec.result, rec.result.evidence
        return {"query_id": ev.query_id, "status": res.status, "question": res.question,
                "source_files": ev.source_files, "source_row_count": ev.source_row_count,
                "period": ev.period_effective or ev.compare_periods, "filters": ev.filters,
                "entities": [{k: n.get(k) for k in ("kind", "text", "status", "method", "ids")} for n in ev.normalization],
                "status_policy": ev.status_policy, "currency_unit": ev.currency_unit, "sample_ids": ev.sample_ids,
                "sample_note": "a few example deal IDs from the matching rows; not the full list",
                "truncation": ev.truncation, "notes": ev.notes, "as_of": ev.as_of,
                "llm": {k: (res.llm or {}).get(k) for k in ("provider", "served_by", "model", "limit", "last_wait_hint_s")},
                "queries": [{"purpose": q.purpose, "sql": q.sql, "params": q.params, "rows": q.row_count, "ms": q.elapsed_ms}
                            for q in ev.queries]}

    # ---------------------------------------------------------------- frontend (same origin)
    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, {"error": "not_found"})
        f = (DIST / path).resolve()
        if path and f.is_file() and DIST.resolve() in f.parents:
            return FileResponse(f)
        index = DIST / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"error": "frontend not built", "hint": "cd web && npm ci && npm run build"}, status_code=503)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
        return JSONResponse(detail, status_code=exc.status_code)

    return app
