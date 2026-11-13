"""Deterministic plan → SQL compiler.

Column names come only from the constant catalogs in plan.py; every value (dates, ids, codes,
status lists, filter values) is a bound ``?`` parameter. Parameters are collected in the same
order as their placeholders appear in the SQL text.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from . import schema as S
from .plan import DIMENSIONS, ENUM_SYNONYMS, FILTERS, LIST_COLUMNS, LIST_ORDER, METRICS, Filter, QueryPlan


class CompileError(ValueError):
    def __init__(self, code: str, detail: str = "", options: list | None = None):
        self.code, self.detail, self.options = code, detail, options or []
        super().__init__(f"{code}: {detail}")


@dataclass
class SqlPart:
    text: str
    params: list[Any] = field(default_factory=list)


@dataclass
class CompiledQuery:
    purpose: str
    sql: str
    params: list[Any]
    dims: list[str] = field(default_factory=list)  # dimension names (in result order)
    dim_columns: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    policies: dict[str, str] = field(default_factory=dict)  # metric -> policy name
    top_n: int | None = None
    auto_dims: list[str] = field(default_factory=list)
    periods: dict[str, tuple[dt.date, dt.date]] = field(default_factory=dict)


def _join(parts: list[SqlPart], sep: str) -> SqlPart:
    return SqlPart(sep.join(p.text for p in parts), [x for p in parts for x in p.params])


def policy_part(policy: str) -> SqlPart:
    codes = list(S.STATUS_POLICIES[policy])
    return SqlPart(f"status_code IN ({', '.join('?' * len(codes))})", codes)


def _num(v: Any) -> Decimal:
    try:
        return Decimal(str(v).replace(" ", "").replace(" ", ""))
    except InvalidOperation as e:
        raise CompileError("bad_filter_value", f"not a number: {v!r}") from e


def _coerce(ftype: str, v: Any):
    if ftype == "int":
        d = _num(v)
        if d != d.to_integral_value():
            raise CompileError("bad_filter_value", f"not an integer: {v!r}")
        return int(d)
    if ftype == "number":
        return _num(v)
    if ftype == "date":
        try:
            return dt.date.fromisoformat(str(v))
        except ValueError as e:
            raise CompileError("bad_filter_value", f"not a date: {v!r}") from e
    return str(v)


def filter_part(f: Filter, resolved_values: list | None = None) -> SqlPart:
    col, ftype, allowed = FILTERS[f.field]
    if f.op in ("is_true", "is_false") or ftype == "bool":
        truth = f.op == "is_true" or (f.op == "eq" and f.value in (True, "true", 1))
        return SqlPart(col if truth else f"NOT {col}")
    vals = resolved_values if resolved_values is not None else (f.value if isinstance(f.value, list) else [f.value])
    if ftype == "enum":
        syn = ENUM_SYNONYMS.get(f.field, {})
        vals = [v if v in allowed else syn.get(str(v).casefold(), next((a for a in allowed if str(a).casefold() == str(v).casefold()), v))
                for v in vals]
        bad = [v for v in vals if v not in allowed]
        if bad:
            raise CompileError("unknown_filter_value", f"{f.field}={bad}", list(allowed))
    if ftype == "region" and resolved_values is None:
        raise CompileError("unresolved_region_filter", f.field)
    vals = [_coerce(ftype, v) for v in vals]
    if f.op == "in" or (f.op == "eq" and len(vals) > 1):
        return SqlPart(f"{col} IN ({', '.join('?' * len(vals))})", vals)
    if f.op == "between":
        if len(vals) != 2:
            raise CompileError("bad_filter_value", "between needs two values")
        return SqlPart(f"{col} BETWEEN ? AND ?", vals)
    op = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[f.op]
    if len(vals) != 1:
        raise CompileError("bad_filter_value", f"{f.op} needs one value")
    return SqlPart(f"{col} {op} ?", vals)


def metric_parts(name: str, policy: str) -> tuple[list[SqlPart], list[str]]:
    m = METRICS[name]
    pol = policy_part(policy)
    F = lambda extra="": SqlPart(f"FILTER (WHERE {pol.text}{extra})", list(pol.params))  # noqa: E731
    out: list[SqlPart] = []
    if m.kind == "count":
        out.append(SqlPart(f"count(*) {F().text} AS {name}__value", F().params))
    elif m.kind == "count_if":
        f = F(f" AND {m.expr}")
        out.append(SqlPart(f"count(*) {f.text} AS {name}__value", f.params))
    elif m.kind == "sum":
        out.append(SqlPart(f"sum({m.expr}) {F().text} AS {name}__value", F().params))
    elif m.kind == "median":  # exact on DECIMAL in DuckDB (no binary float)
        out.append(SqlPart(f"median({m.expr}) {F().text} AS {name}__value", F().params))
    elif m.kind in ("min", "max"):
        out.append(SqlPart(f"{m.kind}({m.expr}) {F().text} AS {name}__value", F().params))
    elif m.kind == "distinct":
        out.append(SqlPart(f"count(DISTINCT {m.expr}) {F().text} AS {name}__value", F().params))
    elif m.kind == "ratio":
        out.append(SqlPart(f"sum({m.expr}) {F().text} AS {name}__num", F().params))
        den = "count(*)" if m.den == "1" else (f"count({m.expr})" if m.den == "@nonnull" else f"sum({m.den})")
        out.append(SqlPart(f"{den} {F().text} AS {name}__den", F().params))
    out.append(SqlPart(f"count(*) {F().text} AS {name}__rows", F().params))
    if m.unit in ("UZS", "price_uzs"):  # USD deals actually converted for THIS metric (same status policy)
        fu = F(" AND is_converted_from_usd")
        out.append(SqlPart(f"count(*) {fu.text} AS {name}__usd_rows", fu.params))
    cols = [p.text.rsplit(" AS ", 1)[1] for p in out]
    return out, cols


def metric_order_expr(name: str) -> str:
    return f"{name}__num / nullif({name}__den, 0)" if METRICS[name].kind == "ratio" else f"{name}__value"


class Compiler:
    def __init__(self, plan: QueryPlan, entity_parts: list[SqlPart], filter_parts: list[SqlPart],
                 period: tuple[dt.date, dt.date] | None, compare: list[tuple[dt.date, dt.date]] | None,
                 single_variant: bool):
        self.p, self.entity_parts, self.filter_parts = plan, entity_parts, filter_parts
        self.period, self.compare, self.single_variant = period, compare, single_variant

    # --------------------------------------------------------------- shared pieces
    def base_where(self) -> SqlPart:
        parts = list(self.entity_parts) + list(self.filter_parts)
        if self.compare:
            parts.append(_join([SqlPart("(deal_date BETWEEN ? AND ?)", [a, b]) for a, b in self.compare], " OR "))
            parts[-1] = SqlPart(f"({parts[-1].text})", parts[-1].params)
        elif self.period:
            parts.append(SqlPart("deal_date BETWEEN ? AND ?", list(self.period)))
        return _join(parts, " AND ") if parts else SqlPart("TRUE")

    def policy_of(self, metric: str) -> str:
        m = METRICS[metric]
        return m.default_policy if (m.fixed_policy or not self.p.status_policy) else self.p.status_policy

    # --------------------------------------------------------------- aggregate / ranking / comparison
    def aggregate(self) -> CompiledQuery:
        metrics = list(dict.fromkeys(m for m in (self.p.metrics or []) if METRICS[m].kind not in ("catalog", "group_stats"))) or ["deal_count"]
        dims = list(dict.fromkeys(self.p.group_by))
        auto: list[str] = []
        need_cur = any(METRICS[m].group_currency for m in metrics) and (
            self.p.currency == "native" or any(METRICS[m].unit in ("native_currency", "price") for m in metrics))
        if need_cur and "currency" not in dims:
            auto.append("currency")
        if any(METRICS[m].group_unit for m in metrics) and "quantity_unit" not in dims:
            auto.append("quantity_unit")
        if any(METRICS[m].group_variant for m in metrics) and not self.single_variant and "product_variant" not in dims:
            auto.append("product_variant")
        dims_all = dims + auto
        if "any_broker" in dims_all and len(dims_all) > 1:
            raise CompileError("unsupported_grouping", "any_broker cannot be combined with other groupings")

        sel: list[SqlPart] = []
        group_exprs: list[str] = []
        dim_cols: list[str] = []
        if self.compare:
            cases = _join([SqlPart(f"WHEN deal_date BETWEEN ? AND ? THEN 'p{i + 1}'", [a, b])
                           for i, (a, b) in enumerate(self.compare)], " ")
            sel.append(SqlPart(f"CASE {cases.text} END AS period_label", cases.params))
            group_exprs.append("period_label")
            dim_cols.append("period_label")
        for d in dims_all:
            keys, labels = DIMENSIONS[d]
            for c in keys:
                sel.append(SqlPart(c))
                group_exprs.append(c)
                dim_cols.append(c)
            for c in labels:
                sel.append(SqlPart(c))
                group_exprs.append(c)
                dim_cols.append(c)
        mcols: list[str] = []
        policies = {}
        for m in metrics:
            pol = self.policy_of(m)
            policies[m] = pol
            parts, cols = metric_parts(m, pol)
            sel += parts
            mcols += cols
        sel.append(SqlPart("count(*) AS scope_rows"))
        sel.append(SqlPart("count(*) FILTER (WHERE is_converted_from_usd) AS usd_converted_rows"))
        ranking = self.p.intent == "ranking" or self.p.top_n is not None
        if group_exprs and not self.compare:
            sel.append(SqlPart("count(*) OVER () AS total_groups"))
        where = self.base_where()
        source = "deal_facts"
        pre = SqlPart("")
        if "any_broker" in dims_all:
            # a deal counts once per broker even when the same broker served both sides
            used = sorted({"deal_id", "status_code", "is_converted_from_usd", "deal_date"} |
                          {c for m in metrics for c in self._metric_columns(m)})
            cols = ", ".join(used)
            pre = SqlPart(
                f"WITH src AS (SELECT seller_broker_code AS broker_code, seller_broker_name AS broker_name, {cols} "
                f"FROM deal_facts WHERE {where.text} UNION SELECT buyer_broker_code AS broker_code, "
                f"buyer_broker_name AS broker_name, {cols} FROM deal_facts WHERE {where.text}) ",
                where.params + where.params)
            source, where = "src", SqlPart("TRUE")
        sql = SqlPart(pre.text + "SELECT " + ", ".join(p.text for p in sel) + f" FROM {source} WHERE {where.text}",
                      pre.params + [x for p in sel for x in p.params] + where.params)
        if group_exprs:
            sql.text += " GROUP BY " + ", ".join(group_exprs)
            order = []
            if self.compare:
                order.append("period_label")
            if ranking or not self.compare:
                by = self.p.order.by if self.p.order else metrics[0]
                direction = (self.p.order.direction if self.p.order else "desc").upper()
                if by in METRICS and by in metrics:
                    order.append(f"{metric_order_expr(by)} {direction} NULLS LAST")
                elif by in DIMENSIONS and by in dims_all:
                    order += [f"{c} {direction}" for c in DIMENSIONS[by][0]]
                else:
                    order.append(f"{metric_order_expr(metrics[0])} DESC NULLS LAST")
            order += [c for c in group_exprs if c not in ("period_label",)]
            sql.text += " ORDER BY " + ", ".join(order)
            limit = (self.p.top_n or 10) + 1 if ranking else 201
            sql.text += f" LIMIT {int(limit)}"
        return CompiledQuery("aggregate", sql.text, sql.params, dims_all, dim_cols, metrics, policies,
                             (self.p.top_n or 10) if ranking else None, auto,
                             {f"p{i + 1}": c for i, c in enumerate(self.compare or [])})

    @staticmethod
    def _metric_columns(m: str) -> set[str]:
        spec = METRICS[m]
        cols = set()
        for e in (spec.expr, spec.den):
            for c in ("amount_uzs", "raw_amount", "quantity_native", "delivered_quantity", "remaining_quantity",
                      "unit_price", "seller_inn", "buyer_inn", "product_variant_key", "is_cancelled",
                      "is_payment_overdue", "is_delivery_overdue", "is_delivery_late", "payment_date", "delivery_date",
                      "deal_date", "seller_broker_code", "buyer_broker_code"):
                if c in e:
                    cols.add(c)
        return cols

    # --------------------------------------------------------------- list
    def list_deals(self) -> CompiledQuery:
        where = self.base_where()
        parts = [where]
        if self.p.status_policy:
            parts.append(policy_part(self.p.status_policy))
        w = _join(parts, " AND ")
        by = self.p.order.by if self.p.order and self.p.order.by in LIST_ORDER else "deal_date"
        direction = (self.p.order.direction if self.p.order else "desc").upper()
        n = min(self.p.top_n or 10, 50)
        sql = (f"SELECT {', '.join(LIST_COLUMNS)}, count(*) OVER () AS total_rows FROM deal_facts WHERE {w.text} "
               f"ORDER BY {by} {direction}, deal_id LIMIT {n + 1}")
        return CompiledQuery("list", sql, w.params, top_n=n,
                             policies={"list": self.p.status_policy or "registry"})

    # --------------------------------------------------------------- samples
    def samples(self, policy: str | None) -> CompiledQuery:
        parts = [self.base_where()]
        if policy:
            parts.append(policy_part(policy))
        w = _join(parts, " AND ")
        return CompiledQuery("sample_ids", f"SELECT deal_id FROM deal_facts WHERE {w.text} ORDER BY deal_id LIMIT 5", w.params)


DEAL_LOOKUP_COLUMNS = (
    "deal_id", "deal_date", "deal_time", "status", "status_code", "contract_id", "contract_form", "contract_type",
    "trade_type", "deal_product_name", "commodity_name_ru", "lot_count", "lot_size", "quantity_native", "quantity_unit",
    "raw_amount", "currency", "fx_rate_applied", "amount_uzs", "unit_price", "start_price", "delivered_lots",
    "delivered_quantity", "remaining_quantity", "payment_date", "payment_due_date", "delivery_date", "delivery_due_date",
    "is_payment_overdue", "is_delivery_overdue", "is_delivery_late", "seller_inn", "seller_name", "seller_region",
    "buyer_inn", "buyer_name", "buyer_region", "warehouse_region", "seller_broker_code", "seller_broker_name",
    "buyer_broker_code", "buyer_broker_name", "source_file", "source_row")


def lookup_queries(kind: str, ident: str, condition: SqlPart, period: tuple[dt.date, dt.date] | None) -> list[CompiledQuery]:
    per = SqlPart(" AND deal_date BETWEEN ? AND ?", list(period)) if period else SqlPart("")
    if kind == "deal":
        return [CompiledQuery("lookup_deal", f"SELECT {', '.join(DEAL_LOOKUP_COLUMNS)} FROM deal_facts WHERE deal_id = ?", [ident])]
    summary = ("SELECT count(*) AS deal_count, count(*) FILTER (WHERE NOT is_cancelled) AS not_cancelled_count, "
               "sum(amount_uzs) FILTER (WHERE NOT is_cancelled) AS trade_value_uzs, min(deal_date) AS first_deal_date, "
               "max(deal_date) AS last_deal_date FROM deal_facts WHERE ")
    if kind == "contract":
        return [CompiledQuery("lookup_contract", "SELECT * FROM contracts WHERE contract_id = ?", [ident]),
                CompiledQuery("lookup_summary", summary + "contract_id = ?" + per.text, [ident] + per.params)]
    if kind == "participant":
        return [CompiledQuery("lookup_participant", "SELECT inn, name, region, region_key, activity, role, vat_payer "
                                                    "FROM participants WHERE inn = ?", [ident]),
                CompiledQuery("lookup_summary", summary + condition.text + per.text, condition.params + per.params)]
    if kind == "broker":
        return [CompiledQuery("lookup_broker", "SELECT broker_code, name, inn, region, region_key FROM brokers WHERE broker_code = ?",
                              [ident]),
                CompiledQuery("lookup_summary", summary + condition.text + per.text, condition.params + per.params)]
    raise CompileError("unsupported_lookup", kind)
