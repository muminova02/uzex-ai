"""Web API on the REAL prepared DB, checked against independent SQL over the raw typed tables
(deals/contracts/participants/usd_rates, not the deal_facts view). The LLM provider is a mock that must
record ZERO calls on every dashboard/explorer/export/meta/help route."""
from __future__ import annotations

import csv
import io
from decimal import Decimal

import duckdb
import pytest
from fastapi.testclient import TestClient

from uzex_assistant.config import get_settings
from uzex_assistant.engine import Engine
from uzex_assistant.importer import ensure_database
from uzex_assistant.llm import MockProvider
from uzex_assistant.web import analytics as A
from uzex_assistant.web.app import create_app

pytestmark = pytest.mark.smoke
S = get_settings()
if not (S.data_dir / "contracts.csv").is_file():
    pytest.skip("real dataset not present", allow_module_level=True)
CANCELLED = "Аннулирован(Не оплачен)"
USD = "Доллар США"


@pytest.fixture(scope="module")
def env():
    ensure_database(S.data_dir, S.db_path, S.as_of)
    eng = Engine(S, provider=MockProvider([]))
    with TestClient(create_app(S, eng)) as c:
        ref = duckdb.connect(str(S.db_path), read_only=True)
        yield c, eng, lambda sql, p=(): ref.execute(sql, list(p)).fetchall()
        ref.close()
    eng.close()


VALUE = "sum(case when c.currency_name = 'Доллар США' then d.raw_amount * r.uzs_per_usd else d.raw_amount end)"
JOIN = "from deals d join contracts c using (contract_id) join usd_rates r on r.rate_date = d.deal_date"


def test_overview_default_matches_raw(env):
    c, eng, ref = env
    j = c.get("/api/analytics/overview").json()
    n, v, sel, buy, can = ref(f"select count(*), {VALUE}, count(distinct d.seller_inn), count(distinct d.buyer_inn), "
                              f"count(*) filter (where d.status = '{CANCELLED}') {JOIN}")[0]
    assert (j["deals"], j["distinct_sellers"], j["distinct_buyers"], j["cancelled"]["count"]) == (n, sel, buy, can) == (500000, sel, buy, 33719)
    assert Decimal(j["value_uzs"]["exact"]) == v and j["cancelled"]["share_pct"]["exact"] == "6.74"
    assert j["status_policy"]["includes_cancelled"] is True and j["dataset"]["as_of"] == "2026-10-30"


# filters with an independent raw-table definition of the same scope
CASES = [
    ({"start": "2025-01-01", "end": "2025-12-31"}, "year(d.deal_date) = 2025"),
    ({"commodity": "gasoline"}, "c.tnved_code = '271012'"),
    ({"region": "tashkent_city", "form": "spot"}, "d.\"warehouse_region\" = 'Toshkent shahri' and d.contract_form = 'Спот'"),
    ({"policy": "not_cancelled", "currency": "USD"}, f"d.status <> '{CANCELLED}' and c.currency_name = '{USD}'"),
    ({"start": "2024-03-01", "end": "2024-03-31", "region": "tashkent_region", "policy": "fulfilled"},
     "d.deal_date between '2024-03-01' and '2024-03-31' and d.warehouse_region = 'Toshkent viloyati' and d.status = 'Исполнен'"),
]


@pytest.mark.parametrize("params, where", CASES)
def test_filtered_scopes_consistent_across_cards_charts_table_export(env, params, where):
    c, _, ref = env
    n, v, sel, buy = ref(f"select count(*), {VALUE}, count(distinct d.seller_inn), count(distinct d.buyer_inn) {JOIN} where {where}")[0]
    ov = c.get("/api/analytics/overview", params=params).json()
    tr = c.get("/api/analytics/trend", params=params).json()
    stt = c.get("/api/analytics/status", params=params).json()
    reg = c.get("/api/analytics/rank", params={**params, "dim": "region", "limit": 14}).json()
    deals = c.get("/api/deals", params={**params, "size": 1}).json()
    assert ov["deals"] == n == sum(m["deals"] for m in tr["months"]) == stt["denominator"] == deals["total"] == sum(i["deals"] for i in reg["items"])
    assert (ov["distinct_sellers"], ov["distinct_buyers"]) == (sel, buy)
    if n:
        assert Decimal(ov["value_uzs"]["exact"]) == v
        assert sum(Decimal(m["value_uzs"]["exact"]) for m in tr["months"] if m["value_uzs"]) == v
        assert sum(Decimal(i["value_uzs"]["exact"]) for i in reg["items"]) == v
    assert ov["applied_filters"] == deals["applied_filters"] == tr["applied_filters"]
    if n <= A.EXPORT_MAX_ROWS:
        r = c.get("/api/deals/export.csv", params=params)
        rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
        assert r.headers["x-total-rows"] == str(n) and len(rows) - 1 == n  # full filtered result, not one page


def test_usd_conversion_and_no_fanout(env):
    c, _, ref = env
    j = c.get("/api/analytics/overview", params={"currency": "USD"}).json()
    raw_usd = ref(f"select count(*), sum(d.raw_amount * r.uzs_per_usd) {JOIN} where c.currency_name = '{USD}'")[0]
    assert j["deals"] == raw_usd[0] == 6590 == j["usd_converted_deals"] and Decimal(j["value_uzs"]["exact"]) == raw_usd[1]


def test_top_n_and_region_labels(env):
    c, _, ref = env
    r = c.get("/api/analytics/rank", params={"dim": "commodity", "limit": 10}).json()
    assert len(r["items"]) == 10 and r["total_groups"] == 43 and r["truncated"] is True
    vals = [Decimal(i["value_uzs"]["exact"]) for i in r["items"]]
    assert vals == sorted(vals, reverse=True)
    reg = {i["key"]: i for i in c.get("/api/analytics/rank", params={"dim": "region", "limit": 14}).json()["items"]}
    assert reg["tashkent_city"]["label_ru"] == "г. Ташкент" and reg["tashkent_region"]["label_uz"] == "Toshkent viloyati"
    assert reg["tashkent_city"]["deals"] == ref("select count(*) from deals where warehouse_region = 'Toshkent shahri'")[0][0]


def test_trend_partial_months_and_no_invented_zero(env):
    c, _, _ = env
    m = c.get("/api/analytics/trend").json()["months"]
    assert m[0]["month"] == "2022-01" and m[0]["partial"] and m[0]["covered_from"] == "2022-01-03"
    assert m[-1]["month"] == "2026-10" and m[-1]["partial"] and m[-1]["covered_to"] == "2026-10-30"
    empty = c.get("/api/analytics/trend", params={"commodity": "argon", "region": "karakalpakstan"}).json()
    for x in empty["months"]:
        if x["deals"] == 0:
            assert x["value_uzs"] is None  # no deals -> value undefined, not 0


def test_price_requires_variant_and_uses_weighted_ratio(env):
    c, _, ref = env
    assert c.get("/api/analytics/price", params={"commodity": "gasoline"}).json()["reason"] == "variant_required"
    key = "gasoline|бензин|аи92||||литр"
    p = c.get("/api/analytics/price", params={"variant": key, "start": "2025-01-01", "end": "2025-12-31"}).json()
    assert p["available"] and p["unit"] == "литр" and p["currencies"] == ["UZS"]
    jan = next(s for s in p["series"] if s["month"] == "2025-01")
    num, den = ref(f"""select sum(d.raw_amount), sum(d.lot_count * d.lot_size) from deals d join product_catalog pc using (contract_id)
                       where pc.variant_key = ? and d.deal_date between '2025-01-01' and '2025-01-31' and d.status <> '{CANCELLED}'""", [key])[0]
    assert Decimal(jan["weighted_price"]["num"]) == num and Decimal(jan["weighted_price"]["den"]) == den
    assert jan["weighted_price"]["exact"] == str((num / den).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP"))


def test_pagination_stable_and_complete(env):
    c, _, ref = env
    p1 = c.get("/api/deals", params={"sort": "amount_uzs", "dir": "desc", "size": 50, "page": 1, "commodity": "raw_cotton"}).json()
    p2 = c.get("/api/deals", params={"sort": "amount_uzs", "dir": "desc", "size": 50, "page": 2, "commodity": "raw_cotton"}).json()
    ids = [r["deal_id"] for r in p1["rows"] + p2["rows"]]
    assert len(ids) == len(set(ids)) == 100
    assert c.get("/api/deals", params={"sort": "amount_uzs", "dir": "desc", "size": 50, "page": 1, "commodity": "raw_cotton"}).json()["rows"] == p1["rows"]
    exp = [r[0] for r in ref("""select deal_id from deal_facts where commodity_key = 'raw_cotton'
                               order by amount_uzs desc, deal_id desc limit 100""")]
    assert ids == exp


def test_search_and_detail(env):
    c, _, ref = env
    j = c.get("/api/deals", params={"q": "979389563", "size": 5}).json()
    assert j["total"] == ref("select count(*) from deals where seller_inn = '979389563' or buyer_inn = '979389563'")[0][0]
    d = c.get("/api/deals/387831").json()["deal"]
    assert (d["quantity_native"], d["delivered_quantity"], d["remaining_quantity"], d["unit_price"]) == (22000, 16000, 6000, "9248.000000")
    assert d["seller_broker_code"] == "BK073" and d["buyer_broker_code"] == "BK062" and d["source_file"] == "deals_2025.csv"
    assert c.get("/api/deals/999999").status_code == 404 and c.get("/api/deals/abc").status_code == 422


@pytest.mark.parametrize("params, field", [({"start": "2027-01-01"}, "start"), ({"start": "2025-05-01", "end": "2025-01-01"}, "start"),
                                           ({"commodity": "x"}, "commodity"), ({"variant": "x"}, "variant"), ({"region": "moon"}, "region"),
                                           ({"commodity": "rice", "variant": "gasoline|бензин|аи92||||литр"}, "variant")])
def test_invalid_filters_are_422_not_empty(env, params, field):
    c, _, _ = env
    r = c.get("/api/analytics/overview", params=params)
    assert r.status_code == 422 and r.json()["field"] == field
    assert c.get("/api/analytics/overview", params={"policy": "drop table"}).status_code == 422
    assert c.get("/api/deals", params={"sort": "seller_inn; drop"}).status_code == 422
    assert c.get("/api/deals", params={"size": 1000}).status_code == 422


def test_empty_result_is_200_with_empty_flag(env):
    c, _, _ = env
    j = c.get("/api/analytics/overview", params={"commodity": "argon", "currency": "USD"}).json()
    assert j["deals"] == 0 and j["empty"] is True and j["value_uzs"] is None


def test_export_limit_explicit(env):
    c, _, _ = env
    r = c.get("/api/deals/export.csv")
    assert r.status_code == 413 and r.json()["total"] == 500000 and r.json()["limit"] == A.EXPORT_MAX_ROWS


def test_zero_llm_calls_on_local_routes(env):
    c, eng, _ = env
    for url in ("/api/meta", "/api/filters/options", "/api/analytics/overview", "/api/analytics/trend", "/api/analytics/status",
                "/api/analytics/rank?dim=category", "/api/deals?size=5", "/api/deals/387831"):
        assert c.get(url).status_code == 200
    assert c.post("/api/chat/ask", json={"question": "nimalarni so'ray olaman"}).json()["kind"] == "help"
    assert c.post("/api/chat/ask", json={"question": "Что ты умеешь?"}).json()["kind"] == "help"
    assert c.post("/api/chat/ask", json={"question": "bu natijani tushuntir"}).json()["kind"] == "no_context"
    assert eng.provider.calls == []


def test_no_secrets_in_meta_or_options(env):
    c, _, _ = env
    blob = c.get("/api/meta").text + c.get("/api/filters/options").text
    for s in (S.llm_api_key, S.bot_token):
        if s is not None and s.get_secret_value():
            assert s.get_secret_value() not in blob
    assert "gsk_" not in blob


def test_money_precision_beyond_js_safe_integer():
    big = Decimal("123456789012345678901.2345")  # > 2**53
    m = A.money(big)
    assert m["exact"] == "123456789012345678901.2345" and isinstance(m["approx_bn"], float)
    r = A.ratio(Decimal("9007199254740993"), 3)
    assert r["num"] == "9007199254740993" and r["exact"] == "3002399751580331.00"
    assert A.ratio(5, 0) is None and A.ratio(None, 3) is None  # undefined, never inf/0


def test_csv_formula_neutralised():
    assert A.csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)" and A.csv_cell("+1") == "'+1" and A.csv_cell("@x") == "'@x"
    assert A.csv_cell("-5") == "'-5" and A.csv_cell(-5) == "-5" and A.csv_cell("OK") == "OK" and A.csv_cell(None) == ""
