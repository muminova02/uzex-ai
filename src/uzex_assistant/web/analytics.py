"""Dashboard and explorer analytics: fixed, parameterized SQL written by application code (no LLM).

Every endpoint builds its WHERE clause from the same validated ``FilterSpec`` (``where_clause``), so
KPI cards, charts, the deal table and the CSV export always share one scope. Money and precise ratios are
returned as exact decimal strings, plus an explicitly approximate float scaled for chart geometry.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .. import schema as S
from ..numbers import exact_ratio

PolicyName = Literal["registry", "not_cancelled", "paid", "fulfilled", "fulfilled_any", "active", "awaiting_payment_any", "cancelled"]
FORMS = ("spot", "forward", "futures")
SORTS = {"deal_date": "deal_ts", "amount_uzs": "amount_uzs", "raw_amount": "raw_amount", "quantity": "quantity_native",
         "lot_count": "lot_count", "deal_id": "deal_id"}
EXPORT_MAX_ROWS = 50_000
PAGE_MAX = 100


class FilterError(ValueError):
    def __init__(self, field: str, message: str):
        self.field, self.message = field, message
        super().__init__(f"{field}: {message}")


class FilterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: dt.date | None = None
    end: dt.date | None = None
    commodity: str | None = None
    variant: str | None = None
    region: str | None = Field(None, description="warehouse region key (seller warehouse), not a company region")
    form: Literal["spot", "forward", "futures"] | None = None
    policy: PolicyName = "registry"
    currency: Literal["UZS", "USD"] | None = None


def validate(spec: FilterSpec, catalog, coverage: tuple[dt.date, dt.date]) -> FilterSpec:
    """Bounds and catalog IDs checked; returns the canonical spec (dates clipped to coverage)."""
    lo, hi = coverage
    start, end = spec.start or lo, spec.end or hi
    if start > end:
        raise FilterError("start", "start date is after end date")
    if end < lo or start > hi:
        raise FilterError("start", f"period outside data coverage {lo}..{hi}")
    start, end = max(start, lo), min(end, hi)
    if spec.commodity and spec.commodity not in catalog.commodities:
        raise FilterError("commodity", "unknown commodity")
    if spec.variant:
        v = catalog.variants.get(spec.variant)
        if v is None:
            raise FilterError("variant", "unknown product variant")
        if spec.commodity and v["commodity_key"] != spec.commodity:
            raise FilterError("variant", "variant does not belong to the selected commodity")
    if spec.region and spec.region not in catalog.regions:
        raise FilterError("region", "unknown region")
    commodity = spec.commodity or (catalog.variants[spec.variant]["commodity_key"] if spec.variant else None)
    return spec.model_copy(update={"start": start, "end": end, "commodity": commodity})


def where_clause(spec: FilterSpec) -> tuple[str, list]:
    parts, params = ["deal_date BETWEEN ? AND ?"], [spec.start, spec.end]
    if spec.commodity:
        parts.append("commodity_key = ?")
        params.append(spec.commodity)
    if spec.variant:
        parts.append("product_variant_key = ?")
        params.append(spec.variant)
    if spec.region:
        parts.append("warehouse_region_key = ?")
        params.append(spec.region)
    if spec.form:
        parts.append("contract_form_code = ?")
        params.append(spec.form)
    if spec.currency:
        parts.append("currency = ?")
        params.append(spec.currency)
    codes = list(S.STATUS_POLICIES[spec.policy])
    if spec.policy != "registry":
        parts.append(f"status_code IN ({', '.join('?' * len(codes))})")
        params += codes
    return " AND ".join(parts), params


def money(v) -> dict | None:
    """Exact decimal string + approximate value in billions of UZS for chart geometry."""
    if v is None:
        return None
    d = Decimal(str(v))
    return {"exact": format(d, "f"), "approx_bn": float(d / Decimal(10) ** 9)}


def ratio(num, den, places: int = 2) -> dict | None:
    if num is None or not den:
        return None  # undefined, never 0 or infinity
    return {"exact": str(exact_ratio(Decimal(str(num)), Decimal(str(den)), places)), "num": format(Decimal(str(num)), "f"),
            "den": format(Decimal(str(den)), "f")}


# ------------------------------------------------------------------ SQL builders (constant text + params)
def q_overview(spec):
    w, p = where_clause(spec)
    return (f"""SELECT count(*) AS deals, sum(amount_uzs) AS value_uzs, count(DISTINCT seller_inn) AS sellers,
                count(DISTINCT buyer_inn) AS buyers, count(*) FILTER (WHERE is_cancelled) AS cancelled,
                count(*) FILTER (WHERE is_converted_from_usd) AS usd_converted
                FROM deal_facts WHERE {w}""", p)


def q_trend(spec):
    w, p = where_clause(spec)
    return (f"""SELECT deal_year, deal_month, count(*) AS deals, sum(amount_uzs) AS value_uzs,
                count(*) FILTER (WHERE is_converted_from_usd) AS usd_converted
                FROM deal_facts WHERE {w} GROUP BY deal_year, deal_month ORDER BY deal_year, deal_month""", p)


def q_rank(spec, dim: str, limit: int):
    key, label = {"commodity": ("commodity_key", "commodity_name_ru"), "category": ("product_category", "product_category"),
                  "region": ("warehouse_region_key", "warehouse_region")}[dim]
    w, p = where_clause(spec)
    return (f"""SELECT {key} AS key, any_value({label}) AS label, sum(amount_uzs) AS value_uzs, count(*) AS deals,
                count(*) OVER () AS total_groups
                FROM deal_facts WHERE {w} GROUP BY {key}
                ORDER BY sum(amount_uzs) DESC NULLS LAST, {key} LIMIT {int(limit)}""", p)


def q_status(spec):
    w, p = where_clause(spec)
    return (f"SELECT status_code, any_value(status) AS status, count(*) AS deals FROM deal_facts WHERE {w} "
            f"GROUP BY status_code ORDER BY count(*) DESC, status_code", p)


def q_price(spec):
    w, p = where_clause(spec)
    return (f"""SELECT deal_year, deal_month, currency, any_value(quantity_unit) AS unit, sum(raw_amount) AS num,
                sum(quantity_native) AS den, count(*) AS deals
                FROM deal_facts WHERE {w} AND NOT is_cancelled GROUP BY deal_year, deal_month, currency
                ORDER BY deal_year, deal_month, currency""", p)


DEAL_COLS = ("deal_id", "deal_date", "deal_product_name", "commodity_name_ru", "seller_name", "seller_inn", "buyer_name",
             "buyer_inn", "lot_count", "lot_size", "quantity_native", "quantity_unit", "raw_amount", "currency",
             "amount_uzs", "status", "status_code", "warehouse_region_key")


def search_clause(q: str | None) -> tuple[str, list]:
    if not q:
        return "", []
    q = q.strip()
    if q.isdigit() and len(q) == 9:
        return " AND (seller_inn = ? OR buyer_inn = ?)", [q, q]
    if q.isdigit() and len(q) == 6:
        return " AND (deal_id = ? OR contract_id = ?)", [q, q]
    raise FilterError("q", "search accepts a 6-digit deal/contract number or a 9-digit INN/STIR")


def q_deals(spec, q, sort: str, direction: str, limit: int, offset: int):
    w, p = where_clause(spec)
    s, sp = search_clause(q)
    col = SORTS[sort]
    d = "DESC" if direction == "desc" else "ASC"
    return (f"""SELECT {', '.join(DEAL_COLS)} FROM deal_facts WHERE {w}{s}
                ORDER BY {col} {d}, deal_id {d} LIMIT {int(limit)} OFFSET {int(offset)}""", p + sp)


def q_count(spec, q):
    w, p = where_clause(spec)
    s, sp = search_clause(q)
    return f"SELECT count(*) FROM deal_facts WHERE {w}{s}", p + sp


DETAIL_COLS = (
    "deal_id", "deal_date", "deal_time", "status", "status_code", "contract_id", "contract_form", "contract_type", "trade_type",
    "deal_product_name", "commodity_name_ru", "product_name", "brand", "size", "grade", "product_type", "product_class",
    "tnved_code", "product_category", "lot_count", "lot_size", "quantity_native", "quantity_unit", "raw_amount", "currency",
    "fx_rate_applied", "amount_uzs", "unit_price", "start_price", "base_price", "delivered_lots", "delivered_quantity",
    "remaining_quantity", "payment_date", "payment_due_date", "delivery_date", "delivery_due_date", "payment_term_days",
    "delivery_term_days", "is_payment_overdue", "is_delivery_overdue", "is_delivery_late", "seller_inn", "seller_name",
    "seller_region", "buyer_inn", "buyer_name", "buyer_region", "warehouse_region", "warehouse_address", "delivery_basis",
    "packaging", "seller_broker_code", "seller_broker_name", "buyer_broker_code", "buyer_broker_name", "source_file", "source_row")


def q_detail(deal_id: str):
    return f"SELECT {', '.join(DETAIL_COLS)} FROM deal_facts WHERE deal_id = ?", [deal_id]


EXPORT_COLS = ("deal_id", "deal_date", "deal_product_name", "seller_name", "seller_inn", "buyer_name", "buyer_inn",
               "lot_count", "lot_size", "quantity_native", "quantity_unit", "raw_amount", "currency", "fx_rate_applied",
               "amount_uzs", "status", "warehouse_region", "contract_id", "source_file", "source_row")


def q_export(spec, q, sort, direction):
    w, p = where_clause(spec)
    s, sp = search_clause(q)
    d = "DESC" if direction == "desc" else "ASC"
    return (f"SELECT {', '.join(EXPORT_COLS)} FROM deal_facts WHERE {w}{s} ORDER BY {SORTS[sort]} {d}, deal_id {d}", p + sp)


def csv_cell(v) -> str:
    """Neutralise spreadsheet formulas in text cells (OWASP CSV injection)."""
    if v is None:
        return ""
    s = v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else str(v)
    if isinstance(v, str) and s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        s = "'" + s
    return s
