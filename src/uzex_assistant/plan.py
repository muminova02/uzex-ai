"""Typed query plan (what the LLM produces) and the catalog of metrics, dimensions and filters.

The LLM never writes identifiers or numbers that end up in answers: it names entity *mentions*
(resolved deterministically against the catalogs), picks metrics/dimensions/filters from fixed
enums, and gives dates. Everything else is compiled deterministically (compiler.py).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import schema as _S

Language = Literal["uz", "ru", "other"]
Intent = Literal["count", "aggregate", "ranking", "lookup", "list", "comparison", "custom", "unsupported"]
EntityKind = Literal["product", "participant", "broker", "region", "contract", "deal"]
EntityRole = Literal["any", "seller", "buyer", "seller_broker", "buyer_broker", "any_broker",
                     "warehouse_region", "seller_region", "buyer_region"]
PolicyName = Literal["registry", "not_cancelled", "paid", "fulfilled", "fulfilled_any", "active",
                     "awaiting_payment_any", "cancelled"]


@dataclass(frozen=True)
class MetricSpec:
    kind: str  # count | sum | distinct | ratio | min | max | count_if
    expr: str = ""  # SQL over deal_facts columns (constant text, never user input)
    den: str = ""  # ratio denominator
    unit: str = ""  # deals | UZS | native_currency | native_unit | price | companies | percent
    default_policy: str = "registry"
    fixed_policy: bool = False
    group_currency: bool = False
    group_unit: bool = False
    group_variant: bool = False


METRICS: dict[str, MetricSpec] = {
    "deal_count": MetricSpec("count", "*", unit="deals", default_policy="registry"),
    "trade_value_uzs": MetricSpec("sum", "amount_uzs", unit="UZS", default_policy="not_cancelled"),
    "trade_value_native": MetricSpec("sum", "raw_amount", unit="native_currency", default_policy="not_cancelled",
                                     group_currency=True),
    "paid_value_uzs": MetricSpec("sum", "amount_uzs", unit="UZS", default_policy="paid", fixed_policy=True),
    "quantity": MetricSpec("sum", "quantity_native", unit="native_unit", default_policy="not_cancelled",
                           group_unit=True, group_variant=True),
    "delivered_quantity": MetricSpec("sum", "delivered_quantity", unit="native_unit", default_policy="fulfilled_any",
                                     group_unit=True, group_variant=True),
    "remaining_quantity": MetricSpec("sum", "remaining_quantity", unit="native_unit", default_policy="paid",
                                     group_unit=True, group_variant=True),
    "weighted_avg_price": MetricSpec("ratio", "raw_amount", "quantity_native", unit="price", default_policy="not_cancelled",
                                     group_currency=True, group_unit=True, group_variant=True),
    "weighted_avg_price_uzs": MetricSpec("ratio", "amount_uzs", "quantity_native", unit="price_uzs",
                                         default_policy="not_cancelled", group_unit=True, group_variant=True),
    "mean_unit_price": MetricSpec("ratio", "unit_price", "1", unit="price", default_policy="not_cancelled",
                                  group_currency=True, group_unit=True, group_variant=True),
    "min_unit_price": MetricSpec("min", "unit_price", unit="price", default_policy="not_cancelled",
                                 group_currency=True, group_unit=True, group_variant=True),
    "max_unit_price": MetricSpec("max", "unit_price", unit="price", default_policy="not_cancelled",
                                 group_currency=True, group_unit=True, group_variant=True),
    "avg_deal_value_uzs": MetricSpec("ratio", "amount_uzs", "1", unit="UZS", default_policy="not_cancelled"),
    "max_deal_value_uzs": MetricSpec("max", "amount_uzs", unit="UZS", default_policy="not_cancelled"),
    "distinct_sellers": MetricSpec("distinct", "seller_inn", unit="companies", default_policy="registry"),
    "distinct_buyers": MetricSpec("distinct", "buyer_inn", unit="companies", default_policy="registry"),
    "distinct_products": MetricSpec("distinct", "product_variant_key", unit="variants", default_policy="registry"),
    "cancellation_share": MetricSpec("ratio", "CASE WHEN is_cancelled THEN 1 ELSE 0 END", "1", unit="percent",
                                     default_policy="registry", fixed_policy=True),
    "overdue_payment_count": MetricSpec("count_if", "is_payment_overdue", unit="deals", default_policy="registry",
                                        fixed_policy=True),
    "overdue_delivery_count": MetricSpec("count_if", "is_delivery_overdue", unit="deals", default_policy="registry",
                                         fixed_policy=True),
    "late_delivery_count": MetricSpec("count_if", "is_delivery_late", unit="deals", default_policy="fulfilled_any",
                                      fixed_policy=True),
    # timing: mean over the deals that HAVE the dates (den = count of non-null values, not all rows)
    "avg_days_to_payment": MetricSpec("ratio", "(payment_date - deal_date)", "@nonnull", unit="days",
                                      default_policy="registry"),
    "avg_days_to_delivery": MetricSpec("ratio", "(delivery_date - payment_date)", "@nonnull", unit="days",
                                       default_policy="registry"),
    "min_deal_value_uzs": MetricSpec("min", "amount_uzs", unit="UZS", default_policy="not_cancelled"),
    "median_deal_value_uzs": MetricSpec("median", "amount_uzs", unit="UZS", default_policy="not_cancelled"),
    # companies in the participants catalog (not only those in the filtered deals); answered from the catalog table
    "catalog_companies": MetricSpec("catalog", "participants", unit="companies", default_policy="registry", fixed_policy=True),
    # distribution of TOTAL deal value per company (sum per INN, then count/max/min/exact mean/median over companies)
    "seller_value_stats": MetricSpec("group_stats", "seller_inn", unit="UZS", default_policy="not_cancelled"),
    "buyer_value_stats": MetricSpec("group_stats", "buyer_inn", unit="UZS", default_policy="not_cancelled"),
    "distinct_seller_brokers": MetricSpec("distinct", "seller_broker_code", unit="brokers", default_policy="registry"),
    "distinct_buyer_brokers": MetricSpec("distinct", "buyer_broker_code", unit="brokers", default_policy="registry"),
}
MetricName = Literal[tuple(METRICS)]  # type: ignore[valid-type]

# dimension -> (key columns, label columns)
DIMENSIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "seller": (("seller_inn",), ("seller_name",)),
    "buyer": (("buyer_inn",), ("buyer_name",)),
    "seller_broker": (("seller_broker_code",), ("seller_broker_name",)),
    "buyer_broker": (("buyer_broker_code",), ("buyer_broker_name",)),
    "any_broker": (("broker_code",), ("broker_name",)),  # union of both broker roles (compiler)
    "warehouse_region": (("warehouse_region_key",), ("warehouse_region",)),
    "seller_region": (("seller_region_key",), ("seller_region",)),
    "buyer_region": (("buyer_region_key",), ("buyer_region",)),
    "commodity": (("commodity_key",), ("commodity_name_ru",)),
    "product_variant": (("product_variant_key",), ("commodity_name_ru", "variant_descriptor", "brand_norm",
                                                   "grade_norm", "class_norm", "size_norm", "quantity_unit")),
    "deal_product_name": (("deal_product_name",), ()),
    "product_category": (("product_category",), ()),
    "product_group": (("product_group",), ()),
    "industry": (("industry",), ()),
    "tnved_code": (("tnved_code",), ()),
    "contract_form": (("contract_form_code",), ("contract_form",)),
    "contract_type": (("contract_type_code",), ("contract_type",)),
    "trade_type": (("trade_type_code",), ("trade_type",)),
    "status": (("status_code",), ("status",)),
    "currency": (("currency",), ()),
    "quantity_unit": (("quantity_unit",), ()),
    "year": (("deal_year",), ()),
    "quarter": (("deal_year", "deal_quarter"), ()),
    "month": (("deal_year", "deal_month"), ()),
    "seller_activity": (("seller_activity",), ()),
    "buyer_activity": (("buyer_activity",), ()),
    "delivery_basis": (("delivery_basis",), ()),
}
DimensionName = Literal[tuple(DIMENSIONS)]  # type: ignore[valid-type]

# filter field -> (column, type, allowed values or None)
FILTERS: dict[str, tuple[str, str, tuple | None]] = {
    "contract_form": ("contract_form_code", "enum", ("spot", "forward", "futures")),
    "contract_type": ("contract_type_code", "enum", ("domestic", "export")),
    "trade_type": ("trade_type_code", "enum", ("english_auction", "reverse_auction", "double_auction")),
    "status": ("status_code", "enum", ("new", "unsigned", "concluded", "awaiting_payment", "paid",
                                       "partially_fulfilled", "fulfilled", "cancelled_unpaid")),
    "currency": ("currency", "enum", ("UZS", "USD")),
    "quantity_unit": ("quantity_unit", "enum", ("тонна", "килограмм", "литр", "штука", "куб.метр", "кв.метр", "метр")),
    "product_category": ("product_category", "catalog", None),
    "product_group": ("product_group", "catalog", None),
    "industry": ("industry", "catalog", None),
    "tnved_code": ("tnved_code", "catalog", None),
    "seller_activity": ("seller_activity", "catalog", None),
    "buyer_activity": ("buyer_activity", "catalog", None),
    "delivery_basis": ("delivery_basis", "catalog", None),
    "warehouse_region": ("warehouse_region_key", "region", None),  # resolved like region entities
    "seller_region": ("seller_region_key", "region", None),
    "buyer_region": ("buyer_region_key", "region", None),
    "deal_year": ("deal_year", "int", None),
    "deal_month": ("deal_month", "int", None),
    "lot_size": ("lot_size", "int", None),
    "lot_count": ("lot_count", "int", None),
    "quantity_native": ("quantity_native", "number", None),
    "amount_uzs": ("amount_uzs", "number", None),
    "raw_amount": ("raw_amount", "number", None),
    "unit_price": ("unit_price", "number", None),
    "payment_term_days": ("payment_term_days", "int", None),
    "delivery_term_days": ("delivery_term_days", "int", None),
    "payment_date": ("payment_date", "date", None),
    "delivery_date": ("delivery_date", "date", None),
    "payment_due_date": ("payment_due_date", "date", None),
    "delivery_due_date": ("delivery_due_date", "date", None),
    "is_fully_delivered": ("is_fully_delivered", "bool", None),
    "is_delivery_late": ("is_delivery_late", "bool", None),
    "is_delivery_overdue": ("is_delivery_overdue", "bool", None),
    "is_payment_overdue": ("is_payment_overdue", "bool", None),
    "is_paid": ("is_paid", "bool", None),
    "is_cancelled": ("is_cancelled", "bool", None),
    "is_converted_from_usd": ("is_converted_from_usd", "bool", None),
    "seller_vat_payer": ("seller_vat_payer", "bool", None),
    "buyer_vat_payer": ("buyer_vat_payer", "bool", None),
}
FilterField = Literal[tuple(FILTERS)]  # type: ignore[valid-type]

# Raw dataset spellings accepted for enum filter values (verified values from the CSVs, schema.py).
ENUM_SYNONYMS: dict[str, dict[str, str]] = {
    "contract_form": {k.casefold(): v for k, v in _S.CONTRACT_FORMS.items()} | {"спотовый": "spot", "спотовые": "spot"},
    "contract_type": {k.casefold(): v for k, v in _S.CONTRACT_TYPES.items()} | {"экспорт": "export", "eksport": "export"},
    "trade_type": {k.casefold(): v for k, v in _S.TRADE_TYPES.items()},
    "status": {k.casefold(): v[0] for k, v in _S.STATUSES.items()},
    "currency": {k.casefold(): v for k, v in _S.CURRENCIES.items()} | {"сум": "UZS", "so'm": "UZS", "usd": "USD", "uzs": "UZS",
                                                                       "доллар": "USD", "dollar": "USD"},
}

# Boolean flags that cannot be true together by definition (deal_facts flag semantics, docs/data_model.md §6).
CONTRADICTORY_FLAGS = [
    ("is_cancelled", "is_payment_overdue"),   # overdue payment = unpaid AND not cancelled
    ("is_cancelled", "is_delivery_overdue"),  # overdue delivery requires payment
    ("is_cancelled", "is_paid"),
    ("is_paid", "is_payment_overdue"),
    ("is_cancelled", "is_delivery_late"),
]

LIST_COLUMNS = ("deal_id", "deal_date", "deal_product_name", "seller_name", "seller_inn", "buyer_name", "buyer_inn",
                "lot_count", "lot_size", "quantity_native", "quantity_unit", "raw_amount", "currency", "amount_uzs",
                "unit_price", "status")
LIST_ORDER = ("deal_date", "amount_uzs", "raw_amount", "quantity_native", "unit_price", "lot_count", "deal_id")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityRef(_M):
    kind: EntityKind
    text: str = Field(description="the mention exactly as written in the question")
    catalog_hint: str | None = Field(None, description="best catalog form (e.g. Russian product name from the list, region key, INN, code)")
    role: EntityRole = "any"
    brand: str | None = None
    grade: str | None = None
    product_class: str | None = None
    size: str | None = None


class Period(_M):
    start: dt.date | None = None
    end: dt.date | None = None
    label: str | None = None

    @field_validator("end")
    @classmethod
    def _order(cls, v, info):
        s = info.data.get("start")
        if v and s and v < s:
            raise ValueError("period end before start")
        return v


class Filter(_M):
    field: FilterField  # type: ignore[valid-type]
    op: Literal["eq", "neq", "in", "gt", "gte", "lt", "lte", "between", "is_true", "is_false"]
    value: Any = None


class Order(_M):
    by: str
    direction: Literal["asc", "desc"] = "desc"


class QueryPlan(_M):
    language: Language = "ru"
    intent: Intent
    entities: list[EntityRef] = Field(default_factory=list)
    period: Period | None = None
    compare_periods: list[Period] = Field(default_factory=list)
    align_equivalent_periods: bool = True
    metrics: list[MetricName] = Field(default_factory=list)  # type: ignore[valid-type]
    filters: list[Filter] = Field(default_factory=list)
    group_by: list[DimensionName] = Field(default_factory=list)  # type: ignore[valid-type]
    order: Order | None = None
    top_n: int | None = Field(None, ge=1, le=100)
    currency: Literal["UZS", "native"] = "UZS"
    status_policy: PolicyName | None = None
    missing_fields: list[str] = Field(default_factory=list)
    unsupported_reason: str | None = None
    clarification: str | None = None
    custom_sql: str | None = None
    custom_params: list[Any] = Field(default_factory=list)
    llm_confidence: float | None = Field(None, description="recorded only; never used to decide correctness")


def llm_json_schema() -> dict:
    """Transport JSON schema for structured outputs: every property required, optional ones nullable.

    Fields with a documented QueryPlan default (language, currency, align_equivalent_periods,
    entity role) are nullable here; a null is dropped before pydantic validation so the default
    applies. Observed live on Groq: the model emitted "currency": null and the whole plan was
    rejected (json_validate_failed). Every non-null value is still validated by QueryPlan."""
    def nullable(t: dict) -> dict:
        return {"anyOf": [t, {"type": "null"}]}

    s = {"type": "string"}
    date = {"type": "string", "description": "YYYY-MM-DD"}
    period = {"type": "object", "additionalProperties": False, "required": ["start", "end", "label"],
              "properties": {"start": nullable(date), "end": nullable(date), "label": nullable(s)}}
    entity = {"type": "object", "additionalProperties": False,
              "required": ["kind", "text", "catalog_hint", "role", "brand", "grade", "product_class", "size"],
              "properties": {
                  "kind": {"type": "string", "enum": list(EntityKind.__args__)},
                  "text": s, "catalog_hint": nullable(s),
                  "role": nullable({"type": "string", "enum": list(EntityRole.__args__)}),
                  "brand": nullable(s), "grade": nullable(s), "product_class": nullable(s), "size": nullable(s)}}
    filt = {"type": "object", "additionalProperties": False, "required": ["field", "op", "value"],
            "properties": {
                "field": {"type": "string", "enum": list(FILTERS)},
                "op": {"type": "string", "enum": ["eq", "neq", "in", "gt", "gte", "lt", "lte", "between", "is_true", "is_false"]},
                "value": {"anyOf": [s, {"type": "number"}, {"type": "boolean"}, {"type": "null"},
                                    {"type": "array", "items": {"anyOf": [s, {"type": "number"}]}}]}}}
    props = {
        "language": nullable({"type": "string", "enum": ["uz", "ru", "other"]}),
        "intent": {"type": "string", "enum": list(Intent.__args__)},
        "entities": {"type": "array", "items": entity},
        "period": nullable(period),
        "compare_periods": {"type": "array", "items": period},
        "align_equivalent_periods": nullable({"type": "boolean"}),
        "metrics": {"type": "array", "items": {"type": "string", "enum": list(METRICS)}},
        "filters": {"type": "array", "items": filt},
        "group_by": {"type": "array", "items": {"type": "string", "enum": list(DIMENSIONS)}},
        "order": nullable({"type": "object", "additionalProperties": False, "required": ["by", "direction"],
                           "properties": {"by": s, "direction": {"type": "string", "enum": ["asc", "desc"]}}}),
        "top_n": nullable({"type": "integer"}),
        "currency": nullable({"type": "string", "enum": ["UZS", "native"]}),
        "status_policy": nullable({"type": "string", "enum": list(PolicyName.__args__)}),
        "missing_fields": {"type": "array", "items": s},
        "unsupported_reason": nullable(s),
        "clarification": nullable(s),
        "custom_sql": nullable(s),
        "custom_params": {"type": "array", "items": {"anyOf": [s, {"type": "number"}]}},
        "llm_confidence": nullable({"type": "number"}),
    }
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}
