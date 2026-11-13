"""Prompt 4 on the real DB: rendered ru/uz answers checked against independent SQL over the raw tables."""
from __future__ import annotations

import json
import re

import duckdb
import pytest

from uzex_assistant.config import PROJECT_ROOT, get_settings
from uzex_assistant.engine import Engine
from uzex_assistant.importer import ensure_database
from uzex_assistant.plan import QueryPlan
from uzex_assistant.render import NB, fmt_num

pytestmark = pytest.mark.smoke
S = get_settings()
if not (S.data_dir / "contracts.csv").is_file():
    pytest.skip("real dataset not present", allow_module_level=True)
PLANS = PROJECT_ROOT / "examples" / "plans"
CANCELLED = "Аннулирован(Не оплачен)"


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


def run(eng, name, question=None):
    data = json.loads((PLANS / f"{name}.json").read_text(encoding="utf-8"))
    q = data.pop("_question", None)
    return eng.run_plan(QueryPlan.model_validate(data), question or q)


def t(res):
    return res.rendered["text"].replace(NB, " ")


def n(x, places=None):
    return fmt_num(x, places).replace(NB, " ")


def nums(s):
    return sorted(m.replace(" ", "") for m in re.findall(r"\d[\d ]*(?:,\d+)?", s.split("\n\n")[0] + s.split("\n\n")[1]))


def test_count_ru_uz(eng, ref):
    exp = ref("select count(*) from deals where year(deal_date) = 2025")[0][0]
    ru = run(eng, "01_count_2025", "Сколько сделок было заключено в 2025 году?")
    uz = run(eng, "01_count_2025", "2025-yilda nechta bitim tuzilgan?")
    cy = run(eng, "01_count_2025", "2025 йилда нечта битим тузилган?")
    assert f"Количество сделок: {n(exp)}." in t(ru)
    assert f"Bitimlar soni: {n(exp)} ta." in t(uz)
    assert f"Битимлар сони: {n(exp)} та." in t(cy)
    assert ru.facts[0].value == uz.facts[0].value == cy.facts[0].value == exp


def test_partial_delivery_deal(eng, ref):
    lots, lot, amt, dl = ref("select lot_count, lot_size, raw_amount, delivered_lots from deals where deal_id = '387831'")[0]
    ru = t(run(eng, "05_lookup_deal", "Покажи сделку 387831"))
    uz = t(run(eng, "05_lookup_deal", "387831-raqamli bitim haqida ma'lumot"))
    q, d = lots * lot, dl * lot
    assert f"= {n(q)} л" in ru and f"Поставлено: {n(d)} л" in ru and f"Не поставлено: {n(q - d)} л" in ru
    assert f"{n(amt / q, 2)} сум за 1 л" in ru
    assert f"Yetkazilgan: {n(d)} litr" in uz and f"Yetkazilmagan: {n(q - d)} litr" in uz


def test_usd_conversion_count_for_shown_ranking_rows(eng, ref):
    res = run(eng, "07_ranking_top_sellers_uz")
    top = [r[0] for r in ref(f"""select d.seller_inn from deals d join contracts c using (contract_id) join usd_rates r on r.rate_date = d.deal_date
            where year(d.deal_date) = 2025 and d.status <> '{CANCELLED}' group by 1
            order by sum(case when c.currency_name = 'Доллар США' then d.raw_amount * r.uzs_per_usd else d.raw_amount end) desc limit 5""")]
    usd = ref(f"""select count(*) from deals d join contracts c using (contract_id) where year(d.deal_date) = 2025
                 and c.currency_name = 'Доллар США' and d.status <> '{CANCELLED}' and d.seller_inn in (select unnest(?))""", [top])[0][0]
    total = ref("select count(distinct seller_inn) from deals where year(deal_date) = 2025")[0][0]
    txt = t(res)
    assert f"{n(usd)} ta AQSh dollaridagi bitim summasi" in txt and "Ko'rsatilgan qatorlarda" in txt
    assert f"{total} tadan birinchi 5 tasi ko'rsatilgan." in txt
    for inn in top:
        assert f"(STIR {inn})" in txt


def test_mixed_units_petrol(eng, ref):
    res = run(eng, "06_aggregate_petrol_price")
    rows = dict(ref(f"""select c.quantity_unit, sum(d.lot_count * d.lot_size) from deals d join contracts c using (contract_id)
                       join product_catalog pc using (contract_id)
                       where year(d.deal_date) = 2025 and pc.commodity_key = 'gasoline' and pc.brand_norm = 'аи92'
                         and d.status <> '{CANCELLED}' group by 1"""))
    txt = t(res)
    liters = [int(x.replace(" ", "")) for x in re.findall(r"Купленное количество товара: ([\d ]+) л", txt)]
    tonnes = [int(x.replace(" ", "")) for x in re.findall(r"Купленное количество товара: ([\d ]+) т", txt)]
    assert sum(liters) == rows["литр"] and sum(tonnes) == rows["тонна"]
    assert n(rows["литр"] + rows["тонна"]) not in txt  # never added across units
    assert "не суммируются" in txt and "Средневзвешенная" in txt and "Σ сумм / Σ количества" in txt


def test_partial_year_comparison(eng, ref):
    res = run(eng, "10_comparison_2025_2026")
    a = ref("select count(*) from deals where deal_date between '2025-01-01' and '2025-10-30'")[0][0]
    b = ref("select count(*) from deals where deal_date between '2026-01-01' and '2026-10-30'")[0][0]
    txt = t(res)
    # table layout (2026-11-05): "<period>: <value>" cells, change in its own column
    assert "(01.01.2025–30.10.2025 → 01.01.2026–30.10.2026)" in txt and f": {n(a)} → {n(b)} (+{n(b - a)}; " in txt
    assert "Последний период неполный" in txt


def test_list_and_truncation(eng, ref):
    total = ref("""select count(*) from deals d join product_catalog pc using (contract_id)
                   where pc.commodity_key = 'raw_cotton' and d.deal_date between '2026-03-01' and '2026-03-31'""")[0][0]
    txt = t(run(eng, "11_list_buyer_deals"))
    assert f"Найдено сделок: {n(total)}; показаны 5" in txt and f"Показаны первые 5 из {n(total)}." in txt


def test_volume_without_context_is_clarified(eng):
    res = run(eng, "07_ranking_top_sellers_uz", "2025-yilda savdo hajmi bo'yicha eng yirik 5 ta sotuvchi kim?")
    assert res.status == "ambiguous" and res.reason_code == "ambiguous_volume" and "Hajm" in t(res)


def test_ranking_ru_uz_same_numbers(eng):
    ru = run(eng, "07_ranking_top_sellers_uz", "Топ-5 продавцов по сумме сделок в 2025 году")
    uz = run(eng, "07_ranking_top_sellers_uz", "2025-yilda bitimlar summasi bo'yicha eng yirik 5 ta sotuvchi kim?")
    assert (ru.rendered["lang"], uz.rendered["lang"]) == ("ru", "uz")
    assert nums(t(ru)) == nums(t(uz))


def test_every_example_is_grounded(eng):
    for f in sorted(PLANS.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        q = data.pop("_question", None)
        res = eng.run_plan(QueryPlan.model_validate(data), q)
        assert res.rendered["grounded"] and not res.rendered["fallback_used"], (f.name, res.rendered["ungrounded"])
