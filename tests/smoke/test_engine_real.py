"""Engine on the real prepared DB (typed plans, no LLM). References are hand-written SQL over the
raw typed tables (deals/contracts/participants), not over the deal_facts view the engine uses."""
from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import duckdb
import pytest

from uzex_assistant.config import PROJECT_ROOT, get_settings
from uzex_assistant.engine import Engine
from uzex_assistant.importer import ensure_database
from uzex_assistant.plan import QueryPlan

pytestmark = pytest.mark.smoke
S = get_settings()
if not (S.data_dir / "contracts.csv").is_file():
    pytest.skip("real dataset not present", allow_module_level=True)
PLANS = PROJECT_ROOT / "examples" / "plans"


@pytest.fixture(scope="module")
def eng():
    ensure_database(S.data_dir, S.db_path, S.as_of)
    e = Engine(S, provider=None)
    yield e
    e.close()


@pytest.fixture(scope="module")
def ref():
    c = duckdb.connect(str(S.db_path), read_only=True)
    yield lambda sql, p=(): c.execute(sql, list(p)).fetchall()
    c.close()


def run(eng, name):
    data = json.loads((PLANS / f"{name}.json").read_text(encoding="utf-8"))
    data.pop("_question", None)
    return eng.run_plan(QueryPlan.model_validate(data))


def val(res, name, **group):
    hits = [f for f in res.facts if f.name == name and all(f.group.get(k) == v for k, v in group.items())]
    assert len(hits) == 1, [(f.name, f.group) for f in res.facts]
    return hits[0].value


def test_control_counts(eng):
    assert val(run(eng, "01_count_2025"), "deal_count") == 115763
    assert val(run(eng, "02_count_spot_uz"), "deal_count") == 456724
    assert val(run(eng, "03_count_usd"), "deal_count") == 6590
    assert val(run(eng, "04_count_cancelled"), "deal_count") == 33719


def test_deal_387831(eng):
    res = run(eng, "05_lookup_deal")
    assert (val(res, "quantity_native"), val(res, "quantity_unit"), val(res, "unit_price"), val(res, "delivered_quantity")) == (
        22000, "литр", "9248.000000", 16000)


def test_petrol_ai92_price_per_unit(eng, ref):
    res = run(eng, "06_aggregate_petrol_price")
    rows = ref("""select c.quantity_unit, sum(d.raw_amount), sum(d.lot_count * d.lot_size)
                  from deals d join contracts c using (contract_id)
                  where year(d.deal_date) = 2025 and c.tnved_code = '271012' and d.status <> 'Аннулирован(Не оплачен)'
                    and (upper(replace(replace(coalesce(c.brand, ''), '-', ''), ' ', '')) = 'АИ92'
                         or upper(replace(c.product_name, '-', '')) like '%АИ92%')
                  group by 1""")
    for unit, num, den in rows:
        got = [Decimal(f.value) for f in res.facts if f.name == "weighted_avg_price" and f.group["quantity_unit"] == unit]
        qty = sum(f.value for f in res.facts if f.name == "quantity" and f.group["quantity_unit"] == unit)
        assert qty == den
        # per-variant prices must bracket the per-unit reference (variants are not merged)
        overall = (num / den).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert min(got) <= overall <= max(got)


def test_top_sellers_match_reference(eng, ref):
    res = run(eng, "07_ranking_top_sellers_uz")
    exp = ref("""select d.seller_inn, sum(case when c.currency_name = 'Доллар США' then d.raw_amount * r.uzs_per_usd else d.raw_amount end) v
                 from deals d join contracts c using (contract_id) join usd_rates r on r.rate_date = d.deal_date
                 where year(d.deal_date) = 2025 and d.status <> 'Аннулирован(Не оплачен)' group by 1 order by v desc limit 5""")
    got = [(f.group["seller_inn"], Decimal(f.value)) for f in res.facts if f.name == "trade_value_uzs"]
    assert got == [(i, Decimal(v)) for i, v in exp]
    assert res.evidence.truncation["top_n"] == 5


def test_any_broker_counts_each_deal_once_per_broker(eng, ref):
    res = run(eng, "08_ranking_brokers")
    top = [f for f in res.facts if f.name == "deal_count"][0]
    code = top.group["broker_code"]
    n = ref("select count(*) from deals where seller_broker_code = ? or buyer_broker_code = ?", [code, code])[0][0]
    assert top.value == n


def test_cross_table(eng, ref):
    res = run(eng, "09_cross_table")
    n = ref("""select count(*) from deals d join contracts c using (contract_id) join participants b on b.inn = d.buyer_inn
               where year(d.deal_date) = 2024 and c.product_category = 'Нефтепродукты' and b.region_key = 'samarkand'""")[0][0]
    assert val(res, "deal_count") == n


def test_comparison_equivalent_periods(eng, ref):
    res = run(eng, "10_comparison_2025_2026")
    n25 = ref("select count(*) from deals where deal_date between '2025-01-01' and '2025-10-30'")[0][0]
    assert val(res, "deal_count", period_label="p1") == n25
    assert val(res, "deal_count", period_label="p2") == 99306
    assert Decimal(val(res, "deal_count__change")) == 99306 - n25


def test_no_data_and_ambiguity(eng):
    assert run(eng, "20_nodata_out_of_period").status == "out_of_period"
    assert run(eng, "21_nodata_missing_field").status == "missing_field"
    assert run(eng, "22_nodata_unknown_company").reason_code == "participant_not_found"
    z = run(eng, "23_verified_zero")
    assert z.status == "success" and z.verified_zero and val(z, "deal_count") == 0
    for name in ("24_ambiguous_region", "25_ambiguous_product", "26_ambiguous_company"):
        assert run(eng, name).status == "ambiguous", name


def test_every_example_runs_within_budget(eng):
    for f in sorted(PLANS.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        data.pop("_question", None)
        res = eng.run_plan(QueryPlan.model_validate(data))
        assert res.status != "execution_error", (f.name, res.reason)
        assert sum(q.elapsed_ms or 0 for q in res.evidence.queries) < 5000, f.name
