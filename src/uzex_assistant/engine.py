"""Query engine: question → plan → entity resolution → validated SQL → bounded execution → AnswerResult.

Every reported number is a ``Fact`` that names the query (``query_index``), the result column(s)
and the row it came from. Ratios are computed in Python with Decimal from exact SQL numerator and
denominator columns. A verified COUNT of 0 is a successful answer of 0; a SUM/MIN/MAX/ratio over
zero rows is ``no_matching_rows`` (NULL is never turned into 0).
"""
from __future__ import annotations

import datetime as dt
import re
import time
import uuid
from decimal import Decimal
from typing import Any, Literal

import duckdb
from pydantic import BaseModel, Field

from . import schema as S
from .compiler import (CompileError, CompiledQuery, Compiler, SqlPart, filter_part, lookup_queries, policy_part)
from .config import Settings, get_settings
from .executor import DUCKDB_CONFIG, QueryExecutor
from .llm import LLMError, LLMProvider, MockProvider, make_provider
from .rategate import RateGate, SharedRateGate
from .intents import looks_like_injection
from .lang import Lang, detect
from .normalize import norm
from .render import render
from .numbers import exact_ratio
from .plan import CONTRADICTORY_FLAGS, DIMENSIONS, FILTERS, METRICS, EntityRef, Filter, Period, QueryPlan
from .planner import PROMPT_VERSION, Planner, build_system_prompt
from .resolver import Catalog, Resolved, Resolver
from .sqlguard import SQLGuard, SQLRejected

Status = Literal["success", "no_matching_rows", "missing_field", "out_of_period", "ambiguous", "unsupported",
                 "execution_error"]
ALLOWED_TABLES = ("deal_facts", "contracts", "participants", "brokers", "usd_rates", "regions", "statuses",
                  "status_policies", "units", "commodities", "product_catalog")
DEAL_SOURCE_TABLES = ("deals", "contracts", "participants", "brokers", "usd_rates")


GENERIC_ROLE_WORDS = {
    "продавец", "продавцы", "продавца", "продавцов", "покупатель", "покупатели", "покупателя", "покупателей",
    "компания", "компании", "организация", "участник", "брокер", "брокеры", "брокера", "брокеров",
    "sotuvchi", "sotuvchilar", "xaridor", "xaridorlar", "kompaniya", "kompaniyalar", "tashkilot", "broker", "brokerlar",
    "seller", "sellers", "buyer", "buyers", "company", "companies", "покупателям", "продавцам", "покупателями",
    "продавцами", "xaridorlari", "sotuvchilari", "xaridorlarning", "sotuvchilarning", "brokerlari",
}
# Fields the dataset does not have; a question asking for them is "not in the data", never "not a data question"
# (live 2026-11-05 H10: the model refused a contacts/bank-details question as not_a_data_question).
MISSING_FIELD_CUES = [
    ("phone", re.compile(r"(телефон|telefon|телефонлар)", re.I)),
    ("bank_details", re.compile(r"(реквизит|rekvizit|банковск\w+\s+сч|bank hisob)", re.I)),
    ("contacts", re.compile(r"(контакт|kontakt|e-?mail|почт[аы] компан)", re.I)),
    ("profit", re.compile(r"(прибыл|foyda|фойда)", re.I)),
    ("margin", re.compile(r"(маржа|маржин|marja)", re.I)),
    ("cost", re.compile(r"(себестоим|tannarx|таннарх)", re.I)),
    ("employees", re.compile(r"(сотрудник|работник|xodim|ходим)", re.I)),
]
OVERRIDE_CUES = re.compile(r"(удал|drop\b|delete|truncate|игнорир|ignore|o'?chir|правил|qoida|инструкц|prompt|промпт)", re.I)


SQL_RESERVE_S = 5.0  # part of the request budget kept for SQL + rendering after planning
MIN_LLM_TIMEOUT_S = 3.0


class Fact(BaseModel):
    name: str
    value: Any
    unit: str | None = None
    group: dict[str, Any] = Field(default_factory=dict)
    query_index: int
    row_index: int | None = None
    columns: list[str]
    rows_used: int | None = None
    method: str | None = None


class QueryRecord(BaseModel):
    query_index: int
    purpose: str
    sql: str
    params: list[Any]
    tables: list[str] = Field(default_factory=list)
    row_count: int | None = None
    truncated: bool = False
    elapsed_ms: int | None = None
    error: str | None = None


class Evidence(BaseModel):
    query_id: str
    queries: list[QueryRecord] = Field(default_factory=list)
    source_files: list[dict] = Field(default_factory=list)
    source_row_count: int | None = None
    period_requested: dict | None = None
    period_effective: dict | None = None
    compare_periods: dict | None = None
    filters: list[dict] = Field(default_factory=list)
    status_policy: dict[str, list[str]] = Field(default_factory=dict)
    currency_unit: dict[str, Any] = Field(default_factory=dict)
    normalization: list[dict] = Field(default_factory=list)
    sample_ids: list[str] = Field(default_factory=list)
    truncation: dict | None = None
    notes: list[str] = Field(default_factory=list)
    as_of: str | None = None
    prompt_version: str = PROMPT_VERSION


class Clarification(BaseModel):
    question_ru: str
    question_uz: str
    entity_text: str | None = None
    candidates: list[dict] = Field(default_factory=list)


class AnswerResult(BaseModel):
    status: Status
    language: str = "ru"
    question: str | None = None
    intent: str | None = None
    facts: list[Fact] = Field(default_factory=list)
    table: dict | None = None
    clarification: Clarification | None = None
    reason_code: str | None = None
    reason: str | None = None
    verified_zero: bool = False
    plan: QueryPlan | None = None
    evidence: Evidence
    llm: dict = Field(default_factory=dict)
    timings_ms: dict[str, int] = Field(default_factory=dict)
    script: str = "cyrl"
    rendered: dict | None = None


class Budget:
    def __init__(self, seconds: float):
        self.t0, self.total = time.monotonic(), seconds

    def left(self) -> float:
        return self.total - (time.monotonic() - self.t0)

    def ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)


class Engine:
    def __init__(self, settings: Settings | None = None, provider: LLMProvider | None = None,
                 executor: QueryExecutor | None = None):
        self.s = settings or get_settings()
        con = duckdb.connect(str(self.s.db_path), read_only=True, config=dict(DUCKDB_CONFIG))
        try:
            self.catalog = Catalog(con)
            cov = con.execute("select min(deal_date), max(deal_date) from deals").fetchone()
            self.coverage: tuple[dt.date, dt.date] = (cov[0], cov[1])
            self.as_of = con.execute("select as_of from import_meta").fetchone()[0]
            files = con.execute("select file_name, target_table, sha256, data_rows from source_files order by 1").fetchall()
            self.source_files = [{"file": f, "table": t, "sha256": h[:16], "rows": n} for f, t, h, n in files]
            schema: dict[str, set[str]] = {}
            for t, c in con.execute("select table_name, column_name from information_schema.columns where table_schema='main'").fetchall():
                if t in ALLOWED_TABLES:
                    schema.setdefault(t, set()).add(c)
        finally:
            con.close()
        self.guard = SQLGuard(schema)
        self.resolver = Resolver(self.catalog)
        self.executor = executor or QueryExecutor(self.s.db_path, workers=self.s.sql_workers, timeout_s=self.s.sql_timeout_s,
                                                  max_rows=self.s.max_result_rows, queue_timeout_s=self.s.sql_queue_timeout_s,
                                                  memory_limit=self.s.sql_memory_limit, threads=self.s.sql_threads)
        self.provider_error: str | None = None
        if provider is not None:
            self.provider = provider
        else:
            try:
                self.provider = make_provider(self.s)
            except LLMError as e:  # e.g. provider=anthropic but no key: typed plans keep working
                self.provider, self.provider_error = None, str(e)
        if self.provider is not None and not isinstance(self.provider, MockProvider):
            # one provider-wide gate for all users and all processes that use the same credential
            self.provider = (SharedRateGate(self.provider, self.s.llm_gate_path) if self.s.llm_gate_path
                             else RateGate(self.provider))
            self.provider.call_timeout_s = self.s.llm_timeout_s
        self.system_prompt = build_system_prompt(self.catalog, self.coverage, self.as_of)
        self.planner = Planner(self.provider, self.system_prompt) if self.provider else None

    def close(self) -> None:
        self.executor.close()

    # ================================================================= entry points
    def answer(self, question: str, budget_s: float | None = None) -> AnswerResult:
        """``budget_s``: remaining total budget (callers subtract queue time)."""
        budget = Budget(self.s.request_budget_s if budget_s is None else budget_s)
        ev = self._evidence()
        if not self.planner:
            return self._finish(AnswerResult(status="execution_error", question=question, reason_code="llm_not_configured",
                                reason=getattr(self, "provider_error", None)
                                or "no LLM provider configured (UZEX_LLM_PROVIDER / UZEX_LLM_API_KEY)", evidence=ev), question)
        if looks_like_injection(question):  # refused locally: no LLM call, no SQL
            ev.notes.append("local_injection_filter")
            return self._finish(AnswerResult(status="unsupported", question=question, reason_code="instruction_not_allowed",
                                             evidence=ev, llm={"local": True}), question)
        # A rate gate may spend the remaining budget waiting for a TPM slot (the call itself stays capped at
        # llm_timeout_s inside the gate); an ungated provider gets the call cap directly.
        gated = isinstance(self.provider, RateGate)
        llm_timeout = budget.left() - SQL_RESERVE_S if gated else min(self.s.llm_timeout_s, budget.left() - SQL_RESERVE_S)
        if llm_timeout < MIN_LLM_TIMEOUT_S:  # queue/retries used the budget: say so instead of a doomed call
            return self._finish(AnswerResult(status="execution_error", question=question, reason_code="deadline_exhausted",
                                             reason=f"{budget.left():.1f}s left of the request budget", evidence=ev,
                                             timings_ms={"total": budget.ms()}), question)
        try:
            plan, resp = self.planner.plan(question, timeout_s=llm_timeout)
        except LLMError as e:
            return self._finish(AnswerResult(status="execution_error", question=question, reason_code=f"llm_{e.kind}", reason=str(e),
                                evidence=ev, timings_ms={"total": budget.ms()},
                                llm={"provider": self.provider.name, "model": self.provider.model, **e.meta}), question)
        except Exception as e:  # never let a provider/SDK bug crash a handler or look like "no data"
            return self._finish(AnswerResult(status="execution_error", question=question, reason_code="llm_unexpected_error",
                                reason=type(e).__name__, evidence=ev, timings_ms={"total": budget.ms()},
                                llm={"provider": self.provider.name, "model": self.provider.model}), question)
        llm = {"provider": resp.provider, "model": resp.model, "served_by": resp.served_by, "elapsed_ms": resp.elapsed_ms,
               "usage": resp.usage, "request_id": resp.request_id, "reported_confidence_not_used": plan.llm_confidence,
               "repair_used": False, "attempts": resp.attempts, "rate_limit_waits_s": resp.rate_limit_waits_s,
               "rate_limit_headers": resp.rate_limit_headers}
        llm["defaults_applied"] = resp.defaults_applied
        if plan.intent == "unsupported" and plan.unsupported_reason == "instruction_not_allowed" and not plan.missing_fields:
            # the model refused an ordinary message (live false positive: "bu statistikalar dan xulosa nima bo'ladi");
            # nothing is executed either way, but the user is told it is not a data question, not "rules refused"
            plan = plan.model_copy(update={"unsupported_reason": "not_a_data_question"})
            ev.notes.append("model_refusal_without_injection_cues_reclassified")
        t_plan = budget.ms()
        res = self.run_plan(plan, question, budget, ev, defaults=set(resp.defaults_applied))
        res.llm = {**llm, **res.llm}
        res.timings_ms = {"plan": t_plan, **res.timings_ms, "total": budget.ms()}
        return res

    # ================================================================= clarification follow-up (no LLM call)
    VALUE_METRICS = ("trade_value_uzs", "trade_value_native", "paid_value_uzs")
    QTY_METRICS = ("quantity", "delivered_quantity", "remaining_quantity")

    def apply_choice(self, plan: QueryPlan, reason_code: str, candidate: dict, entity_text: str | None) -> tuple[QueryPlan, set]:
        """Return the stored plan with the chosen candidate applied, and the question checks to skip.
        Only the ambiguous element changes; every other entity, filter and period is kept."""
        skip: set = set()
        if reason_code == "ambiguous_volume":
            want = candidate.get("option")
            if want == "value":
                ms = [m for m in plan.metrics if m not in self.QTY_METRICS] or []
                ms = ms if any(m in self.VALUE_METRICS for m in ms) else ms + ["trade_value_uzs"]
            elif want == "quantity":
                ms = [m for m in plan.metrics if m not in self.VALUE_METRICS]
                ms = ms if any(m in self.QTY_METRICS for m in ms) else ms + ["quantity"]
            else:
                raise ValueError("unknown volume option")
            return plan.model_copy(update={"metrics": list(dict.fromkeys(ms))}), {"volume"}
        if reason_code == "ambiguous_summary_scope":
            opt = candidate.get("option")
            keep = {"period": plan.period, "filters": plan.filters if plan.intent != "custom" else [], "entities": plan.entities
                    if plan.intent != "custom" else [], "language": plan.language}
            if opt == "deals":
                new = QueryPlan(intent="aggregate", metrics=self.SUMMARY_DEAL_METRICS, **keep)
            elif opt in ("sellers", "buyers"):
                new = QueryPlan(intent="aggregate", metrics=[f"{opt[:-1]}_value_stats", "catalog_companies"], **keep)
            else:
                raise ValueError("unknown summary option")
            return new, {"summary_scope"}
        if reason_code == "ambiguous_region_role":
            role = candidate.get("option")
            if role not in ("warehouse_region", "seller_region", "buyer_region"):
                raise ValueError("unknown region role option")
            ents = [e.model_copy(update={"role": role}) if e.kind == "region" and e.role in ("any", "warehouse_region") else e
                    for e in plan.entities]
            return plan.model_copy(update={"entities": ents}), {"region_role"}
        if reason_code == "unknown_filter_value":
            field = (entity_text or "").split("=", 1)[0]
            filters = [f.model_copy(update={"value": candidate["value"]}) if f.field == field else f for f in plan.filters]
            return plan.model_copy(update={"filters": filters}), skip
        kind = reason_code.removeprefix("ambiguous_")
        def pick(e):
            if kind == "region" and "region_key" in candidate:
                return e.model_copy(update={"text": candidate["region_key"], "catalog_hint": candidate["region_key"]})
            if kind == "participant" and "inn" in candidate:
                return e.model_copy(update={"text": candidate["inn"], "catalog_hint": candidate["inn"]})
            if kind == "broker" and "broker_code" in candidate:
                return e.model_copy(update={"text": candidate["broker_code"], "catalog_hint": candidate["broker_code"]})
            if kind == "product" and "variant_key" in candidate:
                return e.model_copy(update={"catalog_hint": "variant:" + candidate["variant_key"]})
            if kind == "product" and "commodity_key" in candidate:
                name = self.catalog.commodities[candidate["commodity_key"]]["name_ru"]
                return e.model_copy(update={"text": f"{name} {e.text}", "catalog_hint": name})
            raise ValueError(f"candidate does not fit {reason_code}")
        changed, ents, filters = False, [], list(plan.filters)
        for e in plan.entities:
            # a region answer says what the WORD means, so it applies to every occurrence of that word
            if e.kind == kind and e.text == entity_text and (not changed or kind == "region"):
                ents.append(pick(e))
                changed = True
            else:
                ents.append(e)
        if kind == "region":  # region filters too (the planner may repeat the same region in several filters)
            key = candidate["region_key"]
            for i, f in enumerate(filters):
                if f.field not in ("warehouse_region", "seller_region", "buyer_region"):
                    continue
                if isinstance(f.value, list) and entity_text in [str(v) for v in f.value]:
                    filters[i] = f.model_copy(update={"value": [key if str(v) == entity_text else v for v in f.value]})
                    changed = True
                elif str(f.value) == entity_text:
                    filters[i] = f.model_copy(update={"value": key})
                    changed = True
        if not changed:
            raise ValueError("the ambiguous element is no longer in the plan")
        return plan.model_copy(update={"entities": ents, "filters": filters}), skip

    def run_plan(self, plan: QueryPlan, question: str | None = None, budget: Budget | None = None,
                 ev: Evidence | None = None, defaults: set | None = None, skip_checks: set | None = None) -> AnswerResult:
        budget = budget or Budget(self.s.request_budget_s)
        ev = ev or self._evidence()
        if defaults:
            ev.notes.append("defaults_applied=" + ",".join(sorted(defaults)))
        base = dict(question=question, language=plan.language, intent=plan.intent, plan=plan, evidence=ev)
        t0 = budget.ms()
        res = self._run_guarded(plan, question, budget, ev, base, defaults or set(), skip_checks or set())
        res.timings_ms = {**res.timings_ms, "engine": budget.ms() - t0}
        return self._finish(res, question)

    def _finish(self, res: AnswerResult, question: str | None) -> AnswerResult:
        """Answer language from the question (deterministic), then grounded deterministic rendering."""
        plan_lang = res.plan.language if res.plan else None
        lang = detect(question, plan_lang) if question else Lang(plan_lang if plan_lang in ("uz", "ru") else "ru",
                                                                 "latn" if plan_lang == "uz" else "cyrl", False, "plan")
        res.language, res.script = lang.lang, lang.script
        r = render(res, lang, getattr(self, "catalog", None))
        res.rendered = {"text": r.text, "blocks": r.blocks, "lang": r.lang, "script": r.script, "grounded": r.grounded,
                        "ungrounded": r.ungrounded, "fallback_used": r.fallback_used, "detection": lang.reason}
        return res

    def _run_guarded(self, plan, question, budget, ev, base, defaults, skip_checks) -> AnswerResult:
        try:
            if question:
                early = self._question_checks(plan, question, defaults, ev, base, skip_checks)
                if isinstance(early, AnswerResult):
                    return early
                plan = early
                base["plan"] = plan
            return self._run(plan, question, budget, ev, base)
        except CompileError as e:
            status = "ambiguous" if e.code in ("unknown_filter_value", "ambiguous_filter_value") else "unsupported"
            clar = None
            if status == "ambiguous":
                clar = Clarification(question_ru=f"Уточните значение фильтра: {e.detail}. Возможные значения указаны ниже.",
                                     question_uz=f"Filtr qiymatini aniqlashtiring: {e.detail}. Mumkin bo'lgan qiymatlar quyida.",
                                     entity_text=e.detail, candidates=[{"value": o} for o in e.options[:20]])
            return AnswerResult(status=status, reason_code=e.code, reason=e.detail, clarification=clar, **base)
        except SQLRejected as e:
            return AnswerResult(status="execution_error", reason_code="sql_rejected", reason=str(e), **base)

    # ================================================================= main pipeline
    def _run(self, plan, question, budget, ev, base) -> AnswerResult:
        ev.notes.append(f"intent={plan.intent}")
        if (question and plan.intent == "unsupported" and not plan.missing_fields
                and plan.unsupported_reason != "instruction_not_allowed" and not OVERRIDE_CUES.search(question)):
            cues = [name for name, rx in MISSING_FIELD_CUES if rx.search(question)]
            if cues:
                plan = plan.model_copy(update={"missing_fields": cues})
                base["plan"] = plan
                ev.notes.append("unsupported_reclassified_missing_field=" + ",".join(cues))
        if plan.missing_fields:  # checked first: "not in the data" is more precise than "unsupported"
            return AnswerResult(status="missing_field", reason_code="field_not_in_dataset",
                                reason=", ".join(plan.missing_fields), **base)
        if plan.intent == "unsupported":
            return AnswerResult(status="unsupported", reason_code=plan.unsupported_reason or "unsupported", **base)
        # generic role words ("продавец", "sotuvchi", "broker") are not entity mentions
        generic = [e for e in plan.entities if e.kind in ("participant", "broker", "region")
                   and norm(e.text) in GENERIC_ROLE_WORDS and not e.catalog_hint]
        if generic:
            plan = plan.model_copy(update={"entities": [e for e in plan.entities if e not in generic]})
            base["plan"] = plan
            ev.notes.append("generic_role_words_ignored=" + ",".join(e.text for e in generic))

        # ---- semantic validation (after pydantic): a plan whose filters cannot hold together must not
        # produce a "verified zero"
        bad = self._contradictions(plan) + [f"{e.kind} mention without a number: {e.text!r}" for e in plan.entities
                                             if e.kind in ("deal", "contract") and not any(ch.isdigit() for ch in (e.catalog_hint or "") + e.text)]
        if bad:
            return AnswerResult(status="unsupported", reason_code="contradictory_plan", reason="; ".join(bad), **base)

        # ---- period
        plan = self._period_from_year_filter(plan)
        period, compare, out = self._periods(plan, ev)
        if out:
            return AnswerResult(status="out_of_period", reason_code="outside_coverage", reason=out, **base)

        # ---- entities
        parts: list[SqlPart] = []
        resolved: list[Resolved] = []
        # a deal has exactly one product and one warehouse region: several of them in one question mean "any of them"
        # (OR within the same kind and role); different kinds/roles, and participants, still combine with AND
        either: dict[tuple[str, str], list[SqlPart]] = {}
        for e in plan.entities:
            r = self.resolver.resolve(e)
            resolved.append(r)
            ev.normalization.append({"kind": r.kind, "text": r.text, "normalized": r.normalized, "status": r.status,
                                     "method": r.method, "role": r.role, "ids": r.ids, "note": r.note,
                                     "matched": r.display, "variants": len(r.candidates) if r.status == "resolved" and r.kind == "product" else None})
            if r.status == "ambiguous":
                return AnswerResult(status="ambiguous", reason_code=f"ambiguous_{r.kind}", clarification=self._clarify(r), **base)
            if r.status == "not_found":
                return AnswerResult(status="no_matching_rows", reason_code=f"{r.kind}_not_found",
                                    reason=f"{r.kind} '{r.text}' not found in the dataset catalog", **base)
            if r.condition:
                if r.kind in ("product", "region"):
                    either.setdefault((r.kind, r.role or ""), []).append(SqlPart(r.condition, r.params))
                else:
                    parts.append(SqlPart(r.condition, r.params))
        for group in either.values():
            if len(group) == 1:
                parts.append(group[0])
            else:
                parts.append(SqlPart("(" + " OR ".join(f"({g.text})" for g in group) + ")", [p for g in group for p in g.params]))
                ev.notes.append("several_entities_of_one_kind_combined_with_or")

        # ---- filters
        fparts = []
        for f in plan.filters:
            col, ftype, _ = FILTERS[f.field]
            values = None
            if ftype == "region":
                values = []
                for v in (f.value if isinstance(f.value, list) else [f.value]):
                    rr = self.resolver.resolve(EntityRef(kind="region", text=str(v), role=f.field))
                    ev.normalization.append({"kind": "region(filter)", "text": str(v), "normalized": rr.normalized,
                                             "status": rr.status, "method": rr.method, "role": rr.role, "ids": rr.ids,
                                             "note": "", "matched": rr.display, "variants": None})
                    if rr.status == "ambiguous":
                        return AnswerResult(status="ambiguous", reason_code="ambiguous_region", clarification=self._clarify(rr), **base)
                    if rr.status == "not_found":
                        return AnswerResult(status="no_matching_rows", reason_code="region_not_found",
                                            reason=f"region '{v}' not found", **base)
                    values += rr.ids
            elif ftype == "catalog":
                st, values = self.resolver.filter_value(f.field, f.value)
                if st != "resolved":
                    raise CompileError("unknown_filter_value", f"{f.field}={f.value!r}", values)
            fp = filter_part(f, values)
            fparts.append(fp)
            ev.filters.append({"field": f.field, "op": f.op, "value": values if values is not None else f.value, "sql": fp.text})

        products = [r for r in resolved if r.kind == "product"]
        single_variant = bool(products) and all(len(r.candidates) == 1 for r in products)
        comp = Compiler(plan, parts, fparts, period, compare, single_variant)

        # ---- dispatch
        if plan.intent == "lookup":
            return self._lookup(plan, resolved, period, ev, base, budget)
        if plan.intent == "custom":
            return self._custom(plan, question, ev, base, budget)
        if plan.intent == "list":
            q = comp.list_deals()
            ev.status_policy = {"list": list(S.STATUS_POLICIES[q.policies["list"]])}
            rec, out = self._exec(q, ev, budget)
            if out is None:
                return self._exec_error(rec, base)
            if not out.rows:
                return AnswerResult(status="no_matching_rows", reason_code="zero_rows_verified", verified_zero=True,
                                    table=self._table(out, q.top_n), **base)
            ev.source_row_count = out.rows[0][-1]
            ev.sample_ids = [r[0] for r in out.rows[:5]]
            res = AnswerResult(status="success", table=self._table(out, q.top_n), **base)
            res.facts.append(Fact(name="total_rows", value=out.rows[0][-1], unit="deals", query_index=rec.query_index,
                                  row_index=0, columns=["total_rows"]))
            return res
        if any(METRICS[m].kind == "group_stats" for m in plan.metrics):
            return self._group_stats(comp, plan, ev, base, budget)
        if plan.metrics and all(METRICS[m].kind == "catalog" for m in plan.metrics):
            res = AnswerResult(status="success", **base)
            self._catalog_fact(res, ev, budget)
            return res if res.facts else self._exec_error(ev.queries[-1], base)
        if plan.intent in ("count", "aggregate", "ranking", "comparison"):
            if plan.intent == "comparison" and len(compare or []) != 2:
                return AnswerResult(status="unsupported", reason_code="comparison_needs_two_periods", **base)
            return self._aggregate(comp, plan, products, ev, base, budget)
        return AnswerResult(status="unsupported", reason_code="unknown_intent", **base)

    # ================================================================= question-level semantic checks
    VOLUME = re.compile(r"(объ[её]м|обьем|\bhajm|ҳажм|\bvolume)", re.I)
    MONEY_CUES = re.compile(r"(сумм|стоимост|\bсум(ах|ы|ов)?\b|so'?m|сўм|\buzs\b|\busd\b|доллар|dollar|\$|pul\b|qiymat|"
                            r"оборот|денеж|деньг|в валюте|valyuta)", re.I)
    QTY_CUES = re.compile(r"(тонн|литр|\bкг\b|килограмм|штук|куб|квадрат|натурал|физическ|miqdor|tonna|\blitr|dona\b|"
                          r"\bkg\b|natural|в единицах|birlik)", re.I)
    USD_CUES = re.compile(r"(доллар|dollar|\busd\b|\$|в валюте контракта|в исходной валюте|asl valyuta)", re.I)
    UZS_CUES = re.compile(r"(\bсум(ах|ы|ов)?\b|so'?mda|so'?m\b|сўм|\buzs\b|в сумах)", re.I)
    UZS_VALUE_METRICS = {"trade_value_uzs", "paid_value_uzs", "avg_deal_value_uzs", "max_deal_value_uzs", "weighted_avg_price_uzs"}

    def _question_checks(self, plan: QueryPlan, question: str, defaults: set, ev: Evidence, base: dict,
                         skip_checks: set | None = None):
        q = question.replace("’", "'").replace("ʻ", "'").replace("‘", "'")
        # 0) "how many companies + largest/smallest/middle amounts": deal values vs company totals vs mean/median
        if "summary_scope" not in (skip_checks or set()) and self._summary_scope_ambiguous(q):
            return AnswerResult(status="ambiguous", reason_code="ambiguous_summary_scope", clarification=Clarification(
                question_ru=("Уточните, какие суммы показать. Компании: всего в справочнике и участвовавшие в сделках — будут показаны обе цифры. "
                             "Средняя сумма — среднее арифметическое и медиана."),
                question_uz=("Qaysi summalarni ko'rsatay, aniqlashtiring. Kompaniyalar: ma'lumotnomadagi jami va bitimlarda qatnashganlar — ikkalasi "
                             "ko'rsatiladi. O'rtacha summa — o'rtacha arifmetik va mediana."),
                # labels <= 55 chars: Telegram buttons are cut at 60 (the "N) " prefix included)
                candidates=[{"option": "deals", "label": ["суммы сделок: макс., мин., среднее, медиана",
                                                          "bitim summalari: eng katta/kichik, o'rtacha, mediana"]},
                            {"option": "sellers", "label": ["общая сумма продаж каждого продавца", "har bir sotuvchining jami savdo summasi"]},
                            {"option": "buyers", "label": ["общая сумма покупок каждого покупателя", "har bir xaridorning jami xarid summasi"]}]),
                **base)
        # 1) "объём/hajm" is ambiguous unless the question fixes money vs physical quantity
        if "volume" not in (skip_checks or set()) and self.VOLUME.search(q) and not self.MONEY_CUES.search(q) and not self.QTY_CUES.search(q):
            return AnswerResult(status="ambiguous", reason_code="ambiguous_volume", clarification=Clarification(
                question_ru="Что имеется в виду под «объёмом»: сумма сделок в деньгах или количество товара в натуральных единицах (тонны, литры и т. п.)?",
                question_uz="«Hajm» deganda nima nazarda tutilgan: bitimlar summasi (pulda) yoki tovarning natural birlikdagi miqdori (tonna, litr va h.k.)?",
                candidates=[{"option": "value", "label": ["сумма сделок (сум)", "bitimlar summasi (so'm)"]},
                            {"option": "quantity", "label": ["количество товара (т, л, кг…)", "tovar miqdori (t, l, kg…)"]}]), **base)
        # 2) never silently convert dollars when the question asks for dollars / the scope is USD-only
        if set(plan.metrics) & self.UZS_VALUE_METRICS and "trade_value_native" not in plan.metrics:
            usd_scope = any((f.field == "currency" and str(f.value).upper() == "USD") or
                            (f.field == "contract_type" and str(f.value) in ("export", "Экспортный")) for f in plan.filters)
            asks_usd = bool(self.USD_CUES.search(q)) and not self.UZS_CUES.search(q)
            if asks_usd or (usd_scope and not self.UZS_CUES.search(q)):
                plan = plan.model_copy(update={"metrics": list(plan.metrics) + ["trade_value_native"], "currency": "native"})
                ev.notes.append("native_currency_added=" + ("question_mentions_usd" if asks_usd else "usd_only_scope"))
        # 3) the plan must keep the question's explicit constraints (never a silent default/substitution)
        if plan.intent in ("unsupported",) or plan.missing_fields:
            return plan
        lost = self._lost_constraints(plan, q)
        if lost:
            ev.notes.append("plan_mismatch=" + "; ".join(lost))
            return AnswerResult(status="unsupported", reason_code="plan_mismatch", reason="; ".join(lost), **base)
        # 4) participant-role cue + a region the planner put on the warehouse: genuinely unclear, ask
        if "region_role" not in (skip_checks or set()):
            amb = self._region_role_ambiguity(plan, q)
            if amb:
                return AnswerResult(status="ambiguous", reason_code="ambiguous_region_role", clarification=amb, **base)
        return plan

    # ---- explicit-constraint checks (deterministic cues; each check only fires on an unambiguous cue)
    YEAR = re.compile(r"(?<![\d.,])(20[2-3]\d)(?![\d,])")
    BUYER_CUE = re.compile(r"(покупател|покупк|купил|xaridor|харидор|sotib ol|сотиб ол)", re.I)
    SELLER_CUE = re.compile(r"(продав|продал|sotuvchi|сотувчи|sotgan|сотган)", re.I)
    WAREHOUSE_CUE = re.compile(r"(склад|ombor|омбор)", re.I)
    WEIGHTED_CUE = re.compile(r"(взвешен|tortilgan|тортилган|weighted)", re.I)
    MEDIAN_CUE = re.compile(r"(медиан|median)", re.I)
    ARITH_CUE = re.compile(r"(arifmetik|арифмет|ариф)", re.I)
    AVG_PRICE_CUE = re.compile(r"(средн\w*\s+цен|o'rtacha\s+narx|ўртача\s+нарх|цен\w*\s+в\s+среднем)", re.I)
    HOW_MANY_PARTIES = re.compile(r"(сколько\s+(?:\w+\s+){0,2}(покупател|продавц|компани|брокер)|"
                                  r"nechta\s+(?:\w+\s+){0,2}(xaridor|sotuvchi|kompaniya|broker)|"
                                  r"(xaridor|sotuvchi|kompaniya|broker)\w*\s+soni|"
                                  r"(число|количество)\s+(покупател|продавц|компани|брокер))", re.I)
    DEAL_WORD = re.compile(r"(сделк|сделок|bitim|битим|контракт|shartnoma)", re.I)
    AI_BRAND = re.compile(r"(?<![\w])(?:АИ|AI|Аи|Ai)[\s-]?(\d{2})(?!\d)")
    TOP_WORDS = {"три": 3, "тройк": 3, "uchta": 3, "пять": 5, "пятёрк": 5, "пятерк": 5, "beshta": 5, "десять": 10,
                 "десятк": 10, "o'nta": 10, "o'n ta": 10}
    TOP_NUM = re.compile(r"(?:топ|top)[\s-]*(\d{1,2})\b|\b(\d{1,2})\s*-?\s*(?:ta|та)\b|\b(\d{1,2})\s+(?:крупнейш|ведущ|лучш|"
                         r"самых|первых|компан|продавц|покупател|брокер|товар|регион|eng\b|yirik|katta)", re.I)

    COMPANY_CUE = re.compile(r"(kompaniya|kompaniyalar|компани|ishtirokchi|участник|tashkilot|организац)", re.I)
    COUNT_CUE = re.compile(r"(nechta|jami|сколько|количество|soni\b|число)", re.I)
    STAT_CUE = re.compile(r"(eng (yirik|katta|kichik)|o'rta\b|o'rtacha|o'rta (summa|qiymat)|средн|максим|миним|крупн|мелк|медиан|mediana|"
                          r"yirik/kichik|katta/kichik)", re.I)
    MONEY_WORD = re.compile(r"(summa|сумм|qiymat|стоимост)", re.I)
    SCOPE_FIXED = re.compile(r"(har bir bitim|bitim(lar)? summa|сумм\w* (отдельных )?сдел|по каждой сделк|"
                             r"kompaniya(lar)? bo'yicha jami|har bir (sotuvchi|xaridor|kompaniya)|по каждо\w* (компани|продав|покупат))", re.I)

    def _summary_scope_ambiguous(self, q: str) -> bool:
        return bool(self.COMPANY_CUE.search(q) and self.COUNT_CUE.search(q) and self.STAT_CUE.search(q)
                    and self.MONEY_WORD.search(q) and not self.SCOPE_FIXED.search(q))

    SUMMARY_DEAL_METRICS = ["catalog_companies", "distinct_sellers", "distinct_buyers", "deal_count", "max_deal_value_uzs",
                            "min_deal_value_uzs", "avg_deal_value_uzs", "median_deal_value_uzs"]

    def _plan_years(self, plan: QueryPlan) -> set[int]:
        ys: set[int] = set()
        for p in ([plan.period] if plan.period else []) + list(plan.compare_periods):
            if p.start or p.end:
                lo = (p.start or p.end).year
                hi = (p.end or p.start).year
                ys |= set(range(lo, hi + 1))
        for f in plan.filters:
            vals = f.value if isinstance(f.value, list) else [f.value]
            for v in vals:
                m = re.match(r"^(20\d\d)", str(v))
                if f.field in ("deal_year",) or (FILTERS[f.field][1] == "date" and m):
                    try:
                        ys.add(int(str(v)[:4]))
                    except ValueError:
                        pass
        return ys

    def _lost_constraints(self, plan: QueryPlan, q: str) -> list[str]:
        out: list[str] = []
        if plan.intent in ("lookup", "custom"):
            return out
        ids = {d for e in plan.entities if e.kind in ("deal", "contract") for d in re.findall(r"\d+", e.text + " " + (e.catalog_hint or ""))}
        years = {int(y) for y in self.YEAR.findall(q) if y not in ids}
        if years:
            have = self._plan_years(plan)
            miss = sorted(y for y in years if y not in have)
            if miss:
                out.append("year(s) in the question not in the plan period: " + ",".join(map(str, miss)))
        if plan.intent == "ranking":
            ql = q.casefold()
            nums = {int(x) for m in self.TOP_NUM.finditer(q) for x in m.groups() if x}
            nums |= {n for w, n in self.TOP_WORDS.items() if re.search(r"(?<!\w)" + re.escape(w), ql)}
            nums -= years
            if len(nums) == 1 and (plan.top_n or 10) not in nums:
                out.append(f"question asks for top {next(iter(nums))}, plan has top_n={plan.top_n or 10}")
        buyer, seller = bool(self.BUYER_CUE.search(q)), bool(self.SELLER_CUE.search(q))
        if buyer != seller:
            wrong = ("seller", "seller_broker", "seller_region") if buyer else ("buyer", "buyer_broker", "buyer_region")
            for e in plan.entities:
                if e.kind in ("participant", "broker", "region") and e.role in wrong:
                    out.append(f"{e.kind} {e.text!r} has role {e.role} but the question names only the "
                               f"{'buyer' if buyer else 'seller'} side")
            wrong_dims = {"seller", "seller_broker", "seller_region"} if buyer else {"buyer", "buyer_broker", "buyer_region"}
            if plan.intent == "ranking" and set(plan.group_by) & wrong_dims and not (set(plan.group_by) - wrong_dims - {"year", "quarter", "month"}):
                out.append(f"ranking groups by {sorted(set(plan.group_by) & wrong_dims)} but the question names only the "
                           f"{'buyer' if buyer else 'seller'} side")
            wf = {"seller_region"} if buyer else {"buyer_region"}
            for f in plan.filters:
                if f.field in wf:
                    out.append(f"filter {f.field} but the question names only the {'buyer' if buyer else 'seller'} side")
        ms = set(plan.metrics)
        median_ok = bool(ms & {"median_deal_value_uzs"}) or any(METRICS[m].kind == "group_stats" for m in ms) \
            or bool(plan.custom_sql and "median" in plan.custom_sql.lower())
        mean_ok = bool(ms & {"avg_deal_value_uzs", "mean_unit_price"}) or any(METRICS[m].kind == "group_stats" for m in ms) \
            or bool(plan.custom_sql and re.search(r"\bavg\s*\(|sum\s*\(.*\)\s*/\s*count", plan.custom_sql, re.I | re.S))
        if self.MEDIAN_CUE.search(q) and not median_ok:
            out.append("median requested; the plan would compute a different statistic")
        elif self.MEDIAN_CUE.search(q) and self.ARITH_CUE.search(q) and not mean_ok:
            out.append("both arithmetic mean and median requested; the plan drops the arithmetic mean")
        elif self.WEIGHTED_CUE.search(q) and plan.intent != "list" and not ms & {"weighted_avg_price", "weighted_avg_price_uzs"}:
            out.append("weighted average price requested, plan metrics: " + ",".join(plan.metrics))
        elif self.AVG_PRICE_CUE.search(q) and plan.intent != "list" and not ms & {"weighted_avg_price", "weighted_avg_price_uzs", "mean_unit_price"}:
            out.append("average price requested, plan metrics: " + ",".join(plan.metrics))
        if (self.HOW_MANY_PARTIES.search(q) and not self.DEAL_WORD.search(q) and plan.intent in ("count", "aggregate")
                and not plan.group_by and not any(m.startswith("distinct_") or METRICS[m].kind in ("group_stats", "catalog") for m in ms)):
            out.append("number of companies/brokers requested, plan metrics: " + ",".join(plan.metrics))
        for b in self.AI_BRAND.findall(q):
            if not any(b in (x or "") for e in plan.entities if e.kind == "product"
                       for x in (e.brand, e.grade, e.text, e.catalog_hint)):
                out.append(f"brand AI-{b} from the question is not in the plan")
        return out

    def _region_role_ambiguity(self, plan: QueryPlan, q: str) -> Clarification | None:
        buyer, seller = bool(self.BUYER_CUE.search(q)), bool(self.SELLER_CUE.search(q))
        if buyer == seller or self.WAREHOUSE_CUE.search(q) or any(e.kind == "participant" for e in plan.entities):
            return None
        regs = [e for e in plan.entities if e.kind == "region" and e.role in ("any", "warehouse_region")]
        if not regs or any(e.kind == "region" and e.role in ("seller_region", "buyer_region") for e in plan.entities):
            return None
        side = "buyer_region" if buyer else "seller_region"
        side_l = (("регион покупателя", "xaridor hududi") if buyer else ("регион продавца", "sotuvchi hududi"))
        t = regs[0].text
        return Clarification(
            question_ru=f"Что означает «{t}» в вопросе: регион склада (места поставки) или {side_l[0]}?",
            question_uz=f"Savoldagi «{t}» nimani bildiradi: ombor (yetkazish joyi) hududi yoki {side_l[1]}?",
            entity_text=t, candidates=[{"option": "warehouse_region", "label": ["регион склада", "ombor hududi"]},
                                       {"option": side, "label": list(side_l)}])

    @staticmethod
    def _contradictions(plan: QueryPlan) -> list[str]:
        true_flags = {f.field for f in plan.filters if f.op == "is_true" or (f.op == "eq" and f.value in (True, "true", 1))}
        out = [f"{a} and {b} cannot both be true" for a, b in CONTRADICTORY_FLAGS if a in true_flags and b in true_flags]
        statuses = {str(v) for f in plan.filters if f.field == "status" and f.op in ("eq", "in")
                    for v in (f.value if isinstance(f.value, list) else [f.value])}
        if statuses and statuses <= {"cancelled_unpaid", "Аннулирован(Не оплачен)"} and true_flags & {"is_paid", "is_payment_overdue", "is_delivery_overdue"}:
            out.append("status cancelled_unpaid contradicts payment/delivery flags")
        eqs: dict[str, set] = {}
        for f in plan.filters:
            if f.op == "eq" and not isinstance(f.value, list) and FILTERS[f.field][1] not in ("bool",):
                eqs.setdefault(f.field, set()).add(str(f.value).casefold())
        out += [f"{k} cannot equal {sorted(v)} at the same time" for k, v in eqs.items() if len(v) > 1]
        for f in plan.filters:
            if f.op == "neq" and str(f.value).casefold() in eqs.get(f.field, set()):
                out.append(f"{f.field} both = and <> {f.value}")
        lo: dict[str, Decimal] = {}
        hi: dict[str, Decimal] = {}
        for f in plan.filters:
            if FILTERS[f.field][1] in ("int", "number") and f.op in ("gt", "gte", "lt", "lte") and not isinstance(f.value, list):
                try:
                    v = Decimal(str(f.value).replace(" ", ""))
                except Exception:
                    continue
                if f.op in ("gt", "gte"):
                    lo[f.field] = max(lo.get(f.field, v), v)
                else:
                    hi[f.field] = min(hi.get(f.field, v), v)
        out += [f"{k}: lower bound {lo[k]} above upper bound {hi[k]}" for k in lo if k in hi and lo[k] > hi[k]]
        if plan.period and (plan.period.start or plan.period.end):
            for f in plan.filters:
                if f.field == "deal_year" and f.op in ("eq", "in"):
                    vals = f.value if isinstance(f.value, list) else [f.value]
                    ys = {int(v) for v in vals if str(v).isdigit()}
                    a = (plan.period.start or dt.date.min).year
                    b = (plan.period.end or dt.date.max).year
                    if ys and not any(a <= y <= b for y in ys):
                        out.append(f"deal_year {sorted(ys)} outside the period {a}..{b}")
        if plan.intent == "ranking" and plan.metrics and all(METRICS[m].group_unit for m in plan.metrics) and not plan.entities:
            out.append("ranking by quantity across different products/units is not meaningful; use a product or a value metric")
        return out

    # ================================================================= helpers: periods
    def _period_from_year_filter(self, plan: QueryPlan) -> QueryPlan:
        if plan.period or plan.compare_periods:
            return plan
        years = [f for f in plan.filters if f.field == "deal_year" and f.op in ("eq", "in")]
        if len(years) == 1:
            vals = years[0].value if isinstance(years[0].value, list) else [years[0].value]
            try:
                ys = sorted(int(v) for v in vals)
            except (TypeError, ValueError):
                return plan
            if ys == list(range(ys[0], ys[-1] + 1)):
                plan = plan.model_copy(update={
                    "period": Period(start=dt.date(ys[0], 1, 1), end=dt.date(ys[-1], 12, 31), label="deal_year filter"),
                    "filters": [f for f in plan.filters if f is not years[0]]})
        return plan

    def _clip(self, p: Period) -> tuple[dt.date, dt.date] | None:
        lo, hi = self.coverage
        a, b = max(p.start or lo, lo), min(p.end or hi, hi)
        return (a, b) if a <= b else None

    def _periods(self, plan: QueryPlan, ev: Evidence):
        lo, hi = self.coverage
        cov = f"{lo.isoformat()}..{hi.isoformat()}"
        if plan.compare_periods:
            if plan.intent != "comparison" and len(plan.compare_periods) != 2:
                return None, None, None
            clipped = []
            for p in plan.compare_periods:
                c = self._clip(p)
                if c is None:
                    return None, None, f"period {p.start}..{p.end} is outside the data coverage {cov}"
                clipped.append(c)
            req = [(p.start, p.end) for p in plan.compare_periods]
            # Align only for truncation at the snapshot END (data after it is unknown). Spans are measured
            # from each requested start, so the data's first trading day (e.g. 2022-01-03) never shortens
            # the other period.
            if plan.align_equivalent_periods and len(clipped) == 2 and any(p.end is None or p.end > hi
                                                                           for p in plan.compare_periods):
                starts = [p.start or c[0] for p, c in zip(plan.compare_periods, clipped)]
                spans = [(c[1] - st).days for st, c in zip(starts, clipped)]
                if spans[0] != spans[1]:
                    n = min(spans)
                    clipped = [(c[0], min(c[1], st + dt.timedelta(days=n))) for st, c in zip(starts, clipped)]
                    ev.notes.append("compare_periods_aligned_to_equivalent_span")
            if [tuple(c) for c in clipped] != req:
                ev.notes.append("period_clipped_to_coverage")
            ev.compare_periods = {f"p{i + 1}": {"requested": [str(r[0]), str(r[1])], "effective": [str(c[0]), str(c[1])]}
                                  for i, (r, c) in enumerate(zip(req, clipped))}
            return None, clipped, None
        if plan.period:
            ev.period_requested = {"start": str(plan.period.start), "end": str(plan.period.end)}
            c = self._clip(plan.period)
            if c is None:
                return None, None, f"period {plan.period.start}..{plan.period.end} is outside the data coverage {cov}"
            if (plan.period.start and c[0] != plan.period.start) or (plan.period.end and c[1] != plan.period.end):
                ev.notes.append("period_clipped_to_coverage")
            ev.period_effective = {"start": str(c[0]), "end": str(c[1])}
            return c, None, None
        ev.period_effective = {"start": str(lo), "end": str(hi), "note": "full coverage (no period given)"}
        return None, None, None

    # ================================================================= helpers: execution
    def _evidence(self) -> Evidence:
        return Evidence(query_id=uuid.uuid4().hex[:12], as_of=str(getattr(self, "as_of", "")))

    def _exec(self, q: CompiledQuery, ev: Evidence, budget: Budget):
        g = self.guard.validate(q.sql, q.params)
        idx = len(ev.queries)
        rec = QueryRecord(query_index=idx, purpose=q.purpose, sql=q.sql, params=[_jsonable(p) for p in q.params],
                          tables=sorted(g.tables))
        ev.queries.append(rec)
        if any(t in g.tables for t in ("deal_facts",)):
            ev.source_files = [f for f in self.source_files if f["table"] in DEAL_SOURCE_TABLES]
        elif not ev.source_files:
            ev.source_files = [f for f in self.source_files if f["table"] in g.tables]
        left = budget.left()
        out = self.executor.execute(q.sql, q.params, timeout_s=min(self.s.sql_timeout_s, left - 1))
        rec.elapsed_ms, rec.truncated = out.elapsed_ms, out.truncated
        if not out.ok:
            rec.error = f"{out.error_kind}: {out.error}"
            return rec, None
        rec.row_count = len(out.rows)
        return rec, out

    @staticmethod
    def _exec_error(rec: QueryRecord, base: dict) -> AnswerResult:
        code = rec.error.split(":", 1)[0] if rec.error else "error"
        return AnswerResult(status="execution_error", reason_code=f"sql_{code}", reason=rec.error, **base)

    @staticmethod
    def _table(out, top_n: int | None) -> dict:
        rows = out.rows
        truncated = out.truncated
        if top_n is not None and len(rows) > top_n:
            rows, truncated = rows[:top_n], True
        return {"columns": out.columns, "rows": [[_jsonable(v) for v in r] for r in rows], "truncated": truncated}

    # ================================================================= aggregate family
    def _aggregate(self, comp: Compiler, plan, products, ev, base, budget) -> AnswerResult:
        q = comp.aggregate()
        ev.status_policy = {m: list(S.STATUS_POLICIES[p]) for m, p in q.policies.items()}
        ev.notes += [f"policy[{m}]={p}" for m, p in q.policies.items()]
        if q.auto_dims:
            ev.notes.append("auto_grouping=" + ",".join(q.auto_dims))
        rec, out = self._exec(q, ev, budget)
        if out is None:
            return self._exec_error(rec, base)
        cols = out.columns
        ci = {c: i for i, c in enumerate(cols)}
        rows = out.rows
        if not q.dims and not q.periods:  # single row
            rows = rows[:1]
        scope_rows = sum(r[ci["scope_rows"]] for r in rows) if rows else 0
        ev.source_row_count = scope_rows
        usd_rows = sum(r[ci["usd_converted_rows"]] for r in rows) if rows else 0
        top_n = q.top_n
        shown = rows[:top_n] if top_n else rows
        # converted USD deals counted over exactly the rows that are shown (and the metric's own policy)
        per_metric = {m: sum(r[ci[f"{m}__usd_rows"]] for r in shown) for m in q.metrics if f"{m}__usd_rows" in ci}
        ev.currency_unit = {"usd_rows_in_scope": usd_rows, "usd_rows_converted_to_uzs": per_metric,
                            "usd_count_scope": "shown_rows" if (top_n and len(rows) > top_n) or out.truncated else "all_rows",
                            "fx_source": "usd_rates.csv (deal-date rate)", "currency_mode": plan.currency}
        total_groups = rows[0][ci["total_groups"]] if rows and "total_groups" in ci else len(rows)
        truncated = bool(top_n and len(rows) > top_n) or out.truncated
        if truncated:
            ev.truncation = {"shown": len(shown), "total_groups": total_groups, "top_n": top_n}
            if top_n and len(rows) > top_n:
                m0 = q.metrics[0]
                key = (lambda r: (r[ci[f"{m0}__num"]], r[ci[f"{m0}__den"]])) if METRICS[m0].kind == "ratio" else (lambda r: r[ci[f"{m0}__value"]])
                if key(rows[top_n]) == key(rows[top_n - 1]):
                    ev.truncation["tie_at_cutoff"] = True

        facts = []
        for ri, r in enumerate(shown):
            group = {c: _jsonable(r[ci[c]]) for c in q.dim_columns}
            for m in q.metrics:
                facts.append(self._fact(m, r, ci, ri, rec.query_index, group))
        res = AnswerResult(status="success", facts=facts, table=self._table(out, top_n), **base)
        if res.table and truncated:
            res.table["truncated"] = True
            res.table["total_groups"] = total_groups

        # comparisons: deltas per metric (and per extra group)
        if q.periods:
            res.facts += self._deltas(q, shown, ci, rec.query_index)

        # quantities / unit prices split by product variant within ONE commodity: add exact per-unit totals over all
        # variants (units and currencies never mixed; a weighted price is Σamount/Σquantity over the same rows)
        unit_metrics = [m for m in q.metrics if METRICS[m].group_unit]
        if unit_metrics and "product_variant" in q.auto_dims and len({r.ids[0] for r in products if r.ids}) == 1 and scope_rows:
            sub = Compiler(plan.model_copy(update={"group_by": ["quantity_unit"], "metrics": unit_metrics, "top_n": None,
                                                   "intent": "aggregate"}), comp.entity_parts, comp.filter_parts,
                           comp.period, comp.compare, True).aggregate()
            srec, sout = self._exec(sub, ev, budget)
            if sout is not None:
                sci = {c: i for i, c in enumerate(sout.columns)}
                com = {"commodity_key": products[0].ids[0]}
                name_ru = next((d.get("name_ru") for d in products[0].display if d.get("name_ru")), None)
                if name_ru:
                    com["commodity_name_ru"] = name_ru
                for ri, row in enumerate(sout.rows):
                    grp = {c: _jsonable(row[sci[c]]) for c in sub.dim_columns} | com
                    for m in unit_metrics:
                        f = self._fact(m, row, sci, ri, srec.query_index, grp)
                        res.facts.append(f.model_copy(update={
                            "name": f"{m}__unit_total",
                            "method": ("sum" if METRICS[m].kind == "sum" else (f.method or METRICS[m].kind))
                            + " over all variants of the commodity in this unit"}))
                if sub.periods:  # comparisons: exact change of the all-variant totals too (same p2 − p1 rule)
                    for d in self._deltas(sub, sout.rows, sci, srec.query_index):
                        base_m, _, kind = d.name.partition("__")
                        res.facts.append(d.model_copy(update={"name": f"{base_m}__unit_total__{kind}", "group": d.group | com}))
                ev.notes.append("unit_subtotals_within_commodity")

        # zero / null semantics
        if scope_rows == 0:
            count_like = all(METRICS[m].kind in ("count", "count_if", "distinct") for m in q.metrics)
            if count_like and not q.dims:
                res.verified_zero = True
                res.facts = [Fact(name=m, value=0, unit=METRICS[m].unit, query_index=rec.query_index, row_index=0,
                                  columns=[f"{m}__value"], rows_used=0, method="verified COUNT = 0") for m in q.metrics]
                ev.notes.append("verified_zero_count")
            else:
                res.status, res.reason_code = "no_matching_rows", "zero_rows_verified"
                res.facts = []
            return res
        if all(f.value is None for f in res.facts if f.name in q.metrics):
            res.status, res.reason_code = "no_matching_rows", "no_rows_under_status_policy"
        if any(METRICS[m].kind == "catalog" for m in plan.metrics):
            self._catalog_fact(res, ev, budget)
        # sample ids for traceability
        pol = q.policies.get(q.metrics[0])
        srec, sout = self._exec(comp.samples(pol), ev, budget)
        if sout is not None:
            ev.sample_ids = [r[0] for r in sout.rows]
        return res

    def _catalog_fact(self, res: AnswerResult, ev: Evidence, budget: Budget) -> None:
        """All companies in the participants catalog (independent of the deal filters)."""
        rec, out = self._exec(CompiledQuery("catalog", "SELECT count(*) AS catalog_companies FROM participants", []), ev, budget)
        if out is not None and out.rows:
            res.facts.append(Fact(name="catalog_companies", value=_jsonable(out.rows[0][0]), unit="companies",
                                  query_index=rec.query_index, row_index=0, columns=["catalog_companies"],
                                  method="count of all rows in participants.csv (catalog), not filtered by deals"))

    def _group_stats(self, comp: Compiler, plan, ev, base, budget) -> AnswerResult:
        """Distribution of TOTAL deal value per company: sum(amount_uzs) per INN under the not_cancelled policy,
        then count / max / min / median over companies; the mean is computed exactly as Σ ÷ count (Decimal)."""
        res = AnswerResult(status="success", **base)
        stats = [x for x in plan.metrics if METRICS[x].kind == "group_stats"]  # every requested role, never just the first
        for m in stats:
            r = self._group_stats_one(m, comp, ev, base, budget, res)
            if r is not None:
                return r
        if any(METRICS[x].kind == "catalog" for x in plan.metrics):
            self._catalog_fact(res, ev, budget)
        return res

    def _group_stats_one(self, m, comp: Compiler, ev, base, budget, res: AnswerResult) -> AnswerResult | None:
        key = METRICS[m].expr  # constant column name from the catalog, never user input
        where, pol = comp.base_where(), policy_part(METRICS[m].default_policy)
        sql = (f"WITH per AS (SELECT {key} AS k, sum(amount_uzs) AS v FROM deal_facts WHERE ({where.text}) AND {pol.text} "
               f"GROUP BY {key}) SELECT count(*) AS n, sum(v) AS s, max(v) AS mx, min(v) AS mn, median(v) AS md FROM per")
        ev.status_policy = {**(ev.status_policy or {}), m: list(S.STATUS_POLICIES[METRICS[m].default_policy])}
        ev.notes.append(f"policy[{m}]={METRICS[m].default_policy}")
        rec, out = self._exec(CompiledQuery("aggregate", sql, list(where.params) + list(pol.params)), ev, budget)
        if out is None:
            return self._exec_error(rec, base)
        n, total, mx, mn, md = out.rows[0]
        ev.source_row_count = (ev.source_row_count or 0) + n
        if not n:
            return AnswerResult(status="no_matching_rows", reason_code="zero_rows_verified", **base)
        qi = rec.query_index
        res.facts.append(Fact(name=f"{m}__count", value=_jsonable(n), unit="companies", query_index=qi, row_index=0, columns=["n"]))
        for suffix, v, col in (("max", mx, "mx"), ("min", mn, "mn")):
            res.facts.append(Fact(name=f"{m}__{suffix}", value=_jsonable(v), unit="UZS", query_index=qi, row_index=0, columns=[col]))
        res.facts.append(Fact(name=f"{m}__mean", value=str(exact_ratio(Decimal(total), n, 2)), unit="UZS", query_index=qi,
                              row_index=0, columns=["s", "n"], method="Σ company totals ÷ number of companies, Decimal half-up 2 dp"))
        res.facts.append(Fact(name=f"{m}__median", value=_jsonable(md), unit="UZS", query_index=qi, row_index=0, columns=["md"],
                              method="median of company totals"))
        return None

    def _fact(self, m: str, r, ci, ri, qi, group) -> Fact:
        spec = METRICS[m]
        rows_used = r[ci[f"{m}__rows"]]
        unit = spec.unit
        if unit in ("native_unit",):
            unit = group.get("quantity_unit")
        elif unit == "native_currency":
            unit = group.get("currency")
        elif unit == "price":
            unit = f"{group.get('currency', 'UZS')}/{group.get('quantity_unit', '?')}"
        elif unit == "price_uzs":
            unit = f"UZS/{group.get('quantity_unit', '?')}"
        if spec.kind == "ratio":
            num, den = r[ci[f"{m}__num"]], r[ci[f"{m}__den"]]
            if num is None or not den:
                return Fact(name=m, value=None, unit=unit, group=group, query_index=qi, row_index=ri,
                            columns=[f"{m}__num", f"{m}__den"], rows_used=rows_used, method="no rows")
            if m == "cancellation_share":
                val, method = exact_ratio(Decimal(num) * 100, den, 2), "100 × num / den, Decimal half-up 2 dp"
            else:
                val, method = exact_ratio(Decimal(num), den, 2), "num / den, Decimal half-up 2 dp"
                if m in ("weighted_avg_price", "weighted_avg_price_uzs"):
                    method = "weighted: Σamount / Σquantity, " + method
                if m == "mean_unit_price":
                    method = "arithmetic mean of unit prices: " + method
            return Fact(name=m, value=str(val), unit=unit, group=group, query_index=qi, row_index=ri,
                        columns=[f"{m}__num", f"{m}__den"], rows_used=rows_used, method=method)
        v = r[ci[f"{m}__value"]]
        return Fact(name=m, value=_jsonable(v), unit=unit, group=group, query_index=qi, row_index=ri,
                    columns=[f"{m}__value"], rows_used=rows_used)

    def _deltas(self, q: CompiledQuery, rows, ci, qi) -> list[Fact]:
        out = []
        other = [c for c in q.dim_columns if c != "period_label"]
        by_key: dict[tuple, dict[str, Any]] = {}
        for ri, r in enumerate(rows):
            key = tuple(r[ci[c]] for c in other)
            by_key.setdefault(key, {})[r[ci["period_label"]]] = (ri, r)
        for key, per in by_key.items():
            if "p1" not in per or "p2" not in per:
                continue
            for m in q.metrics:
                grp = dict(zip(other, [_jsonable(k) for k in key]))
                # the group carries quantity_unit / currency, so the change keeps the metric's real unit
                f1 = self._fact(m, per["p1"][1], ci, per["p1"][0], qi, grp)
                f2 = self._fact(m, per["p2"][1], ci, per["p2"][0], qi, grp)
                if f1.value is None or f2.value is None:
                    continue
                a, b = Decimal(str(f1.value)), Decimal(str(f2.value))
                out.append(Fact(name=f"{m}__change", value=str(b - a), unit=f1.unit, group=grp, query_index=qi,
                                columns=f1.columns, method="p2 − p1"))
                if a != 0:
                    out.append(Fact(name=f"{m}__change_pct", value=str(exact_ratio((b - a) * 100, a, 2)), unit="percent",
                                    group=grp, query_index=qi, columns=f1.columns, method="(p2 − p1) / p1 × 100, Decimal half-up"))
        return out

    # ================================================================= lookup / custom
    def _lookup(self, plan, resolved, period, ev, base, budget) -> AnswerResult:
        target = next((r for r in resolved if r.kind in ("deal", "contract", "participant", "broker")), None)
        if target is None:
            return AnswerResult(status="unsupported", reason_code="lookup_needs_id_or_company", **base)
        cond = SqlPart(target.condition, target.params)
        qs = lookup_queries(target.kind, target.ids[0], cond, period)
        res = AnswerResult(status="success", **base)
        tables = []
        for q in qs:
            rec, out = self._exec(q, ev, budget)
            if out is None:
                return self._exec_error(rec, base)
            if q.purpose.startswith("lookup_") and q.purpose != "lookup_summary" and not out.rows:
                return AnswerResult(status="no_matching_rows", reason_code=f"{target.kind}_not_found", **base)
            tbl = self._table(out, None)
            tables.append({"purpose": q.purpose, **tbl})
            if q.purpose == "lookup_summary" and out.rows:
                ev.source_row_count = out.rows[0][0]
            if out.rows:
                for c, v in zip(out.columns, out.rows[0]):
                    res.facts.append(Fact(name=c, value=_jsonable(v), query_index=rec.query_index, row_index=0, columns=[c]))
        if target.kind == "deal":
            ev.sample_ids = target.ids
            ev.source_row_count = 1
        res.table = {"lookups": tables}
        return res

    def _custom(self, plan, question, ev, base, budget) -> AnswerResult:
        if not self.s.allow_custom_sql or not plan.custom_sql:
            return AnswerResult(status="unsupported", reason_code="custom_sql_disabled_or_empty", **base)
        ev.notes.append("custom_sql: free-form SQL from the planner, validated by the SQL guard")
        attempt_plan, repaired = plan, False
        while True:
            q = CompiledQuery("custom", attempt_plan.custom_sql, list(attempt_plan.custom_params))
            err = None
            try:
                rec, out = self._exec(q, ev, budget)
                if out is None:
                    err = rec.error
            except SQLRejected as e:
                err = str(e)
                ev.queries.append(QueryRecord(query_index=len(ev.queries), purpose="custom (rejected)", sql=q.sql,
                                              params=[_jsonable(p) for p in q.params], error=err))
                if e.reason not in ("parse_error", "unknown_column", "unknown_table_alias", "param_count"):
                    return AnswerResult(status="execution_error", reason_code="sql_rejected", reason=err, **base)
            if err is None:
                break
            repairable = any(k in err for k in ("parse_error", "unknown_column", "Parser Error", "Binder Error",
                                                "unknown_table_alias", "param_count"))
            if repaired or not repairable or not self.planner or budget.left() < 20:
                return AnswerResult(status="execution_error", reason_code="custom_sql_failed", reason=err,
                                    llm={"repair_used": repaired}, **base)
            try:
                new_plan, _ = self.planner.repair(question or "", attempt_plan, err, timeout_s=min(20, budget.left() - 10))
            except LLMError as e:
                return AnswerResult(status="execution_error", reason_code=f"llm_{e.kind}", reason=str(e),
                                    llm={"repair_used": True}, **base)
            if not self._same_meaning(attempt_plan, new_plan):
                return AnswerResult(status="execution_error", reason_code="repair_changed_meaning",
                                    llm={"repair_used": True}, **base)
            attempt_plan, repaired = new_plan, True
            ev.notes.append("custom_sql_repaired_once")
        if not out.rows:
            return AnswerResult(status="no_matching_rows", reason_code="zero_rows_verified", table=self._table(out, None),
                                llm={"repair_used": repaired}, **base)
        res = AnswerResult(status="success", table=self._table(out, None), llm={"repair_used": repaired}, **base)
        for ri, r in enumerate(out.rows[:50]):
            for c, v in zip(out.columns, r):
                if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
                    res.facts.append(Fact(name=c, value=_jsonable(v), query_index=rec.query_index, row_index=ri, columns=[c]))
        if out.truncated:
            ev.truncation = {"shown": len(out.rows), "note": "result capped at max_result_rows"}
        return res

    @staticmethod
    def _same_meaning(a: QueryPlan, b: QueryPlan) -> bool:
        """A repair may change only custom_sql text: same params, same plan otherwise, same tables."""
        keep = {"custom_sql", "llm_confidence"}
        da, db = a.model_dump(exclude=keep), b.model_dump(exclude=keep)
        return da == db and b.custom_sql is not None

    # ================================================================= clarification
    @staticmethod
    def _clarify(r: Resolved) -> Clarification:
        k = r.kind
        if k == "region":
            ru, uz = "Уточните: имеется в виду г. Ташкент или Ташкентская область?", "Aniqlashtiring: Toshkent shahri yoki Toshkent viloyatimi?"
        elif k == "product" and r.method == "no_variant_with_these_attributes":
            ru, uz = (f"Товар с такими характеристиками («{r.text}») не найден. Какой из вариантов имеется в виду?",
                      f"Bunday tavsifdagi tovar («{r.text}») topilmadi. Qaysi variant nazarda tutilgan?")
        elif k == "product":
            ru, uz = f"Уточните товар «{r.text}»: какой из вариантов?", f"«{r.text}» tovarini aniqlashtiring: qaysi biri?"
        elif r.method == "similar_names_only":
            ru, uz = (f"Компания «{r.text}» точно не найдена. Возможно, имеется в виду одна из этих (укажите ИНН)?",
                      f"«{r.text}» kompaniyasi aniq topilmadi. Balki quyidagilardan biri (STIRni ko'rsating)?")
        else:
            ru, uz = (f"Под «{r.text}» подходит несколько организаций. Укажите ИНН или полное название.",
                      f"«{r.text}» ga bir nechta tashkilot mos keladi. STIR yoki to'liq nomini ko'rsating.")
        return Clarification(question_ru=ru, question_uz=uz, entity_text=r.text, candidates=r.candidates)


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    return v
