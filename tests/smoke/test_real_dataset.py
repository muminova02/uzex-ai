"""Real-dataset smoke tests. Build from a clean state into a temporary DB and compare with an
independent reference computed here with Python's csv module and Decimal (no DuckDB, no importer code).

Run:  .venv/bin/python -m pytest tests/smoke -m smoke
Skipped automatically when the dataset directory is absent.
"""
from __future__ import annotations

import csv
import datetime as dt
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import duckdb
import pytest

from uzex_assistant.config import get_settings
from uzex_assistant.importer import ensure_database

pytestmark = pytest.mark.smoke
DATA = get_settings().data_dir
if not (DATA / "contracts.csv").is_file():
    pytest.skip(f"real dataset not found at {DATA}", allow_module_level=True)

AS_OF = dt.date(2026, 10, 30)
SEPS = str.maketrans("", "", "   ")


def d(s: str) -> dt.date:
    day, month, year = s.split(".")
    return dt.date(int(year), int(month), int(day))


@pytest.fixture(scope="module")
def ref():
    """Independent reference from raw CSVs."""
    contracts = {r["Номер контракта"]: r for r in csv.DictReader(open(DATA / "contracts.csv", encoding="utf-8", newline=""))}
    rates = {d(r["Дата"]): Decimal(r["Курс"]) for r in csv.DictReader(open(DATA / "usd_rates.csv", encoding="utf-8", newline=""))}
    deals = {}
    for f in sorted(DATA.glob("deals_*.csv")):
        for i, r in enumerate(csv.DictReader(open(f, encoding="utf-8", newline="")), start=1):
            r["_file"], r["_row"] = f.name, i
            deals[r["Номер сделки"]] = r
    return contracts, rates, deals


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    db = tmp_path_factory.mktemp("smoke") / "uzex.duckdb"
    rep = ensure_database(DATA, db, AS_OF)  # clean state: new path
    assert rep.rebuilt and all(c[3] for c in rep.checks)
    c = duckdb.connect(str(db), read_only=True)
    yield c
    c.close()


def one(con, sql, params=None):
    return con.execute(sql, params or []).fetchone()[0]


def test_expected_control_values(con):
    assert one(con, "select count(*) from deals") == 500000
    assert one(con, "select count(*) from contracts") == 6616
    assert one(con, "select count(*) from participants") == 14392
    assert one(con, "select count(*) from brokers") == 120
    assert one(con, "select count(*) from usd_rates") == 1765
    by_year = dict(con.execute("select deal_year, count(*) from deal_facts group by 1").fetchall())
    assert by_year == {2022: 84299, 2023: 93985, 2024: 106647, 2025: 115763, 2026: 99306}
    forms = dict(con.execute("select contract_form_code, count(*) from deal_facts group by 1").fetchall())
    assert forms == {"spot": 456724, "forward": 36431, "futures": 6845}
    assert one(con, "select count(*) from deal_facts where currency = 'USD'") == 6590
    assert one(con, "select count(*) from deal_facts where status_code = 'cancelled_unpaid'") == 33719


def test_join_row_counts(con):
    assert one(con, "select count(*) from deal_facts") == 500000
    assert one(con, "select count(distinct deal_id) from deal_facts") == 500000
    assert one(con, "select count(*) from deal_facts where seller_name is null or buyer_name is null "
                    "or seller_broker_name is null or buyer_broker_name is null or currency is null "
                    "or usd_rate_on_deal_date is null") == 0


def test_deal_387831(con):
    r = con.execute("""select quantity_native, quantity_unit, unit_price, currency, delivered_quantity, lot_count, lot_size,
                              delivered_lots, raw_amount from deal_facts where deal_id = '387831'""").fetchone()
    assert r == (22000, "литр", Decimal("9248.000000"), "UZS", 16000, 22, 1000, 16, Decimal("203456000.00"))


def test_status_counts_match_reference(con, ref):
    expected = Counter(r["Состояние сделки"] for r in ref[2].values())
    assert dict(con.execute("select status, count(*) from deal_facts group by 1").fetchall()) == dict(expected)


def test_money_totals_match_reference(con, ref):
    contracts, rates, deals = ref
    native, uzs = defaultdict(Decimal), Decimal(0)
    for r in deals.values():
        amt = Decimal(r["Сумма сделки"].translate(SEPS))
        cur = contracts[r["Номер контракта"]]["Валюта"]
        native[cur] += amt
        uzs += amt * rates[d(r["Дата сделки"])] if cur == "Доллар США" else amt
    got = dict(con.execute("select currency_name, sum(raw_amount) from deal_facts group by 1").fetchall())
    assert got == dict(native)
    assert one(con, "select sum(amount_uzs) from deal_facts") == uzs


def test_quantity_totals_match_reference(con, ref):
    q, dq = defaultdict(int), defaultdict(int)
    for r in ref[2].values():
        q[r["Единица измерения"]] += int(r["Количество"]) * int(r["Лот"])
        if r["Поставлено"]:
            dq[r["Единица измерения"]] += int(r["Поставлено"]) * int(r["Лот"])
    assert dict(con.execute("select quantity_unit, sum(quantity_native) from deal_facts group by 1").fetchall()) == dict(q)
    got = dict(con.execute("select quantity_unit, sum(delivered_quantity) from deal_facts group by 1").fetchall())
    assert got == dict(dq)


def test_unit_price_and_fx_every_row(con, ref):
    contracts, rates, deals = ref
    got = {r[0]: r[1:] for r in con.execute(
        "select deal_id, unit_price, fx_rate_applied, amount_uzs, source_file, source_row from deal_facts").fetchall()}
    mismatches = []
    for did, r in deals.items():
        amt = Decimal(r["Сумма сделки"].translate(SEPS))
        qty = int(r["Количество"]) * int(r["Лот"])
        price = (amt / qty).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        usd = contracts[r["Номер контракта"]]["Валюта"] == "Доллар США"
        rate = rates[d(r["Дата сделки"])] if usd else None
        exp = (price, rate, amt * rate if usd else amt, r["_file"], r["_row"])
        if got[did] != exp:
            mismatches.append((did, got[did], exp))
    assert mismatches == []


def test_deadline_flags_match_reference(con, ref):
    contracts, _, deals = ref
    pay_over = deliv_over = late = 0
    for r in deals.values():
        c = contracts[r["Номер контракта"]]
        st = r["Состояние сделки"]
        due_pay = d(r["Дата сделки"]) + dt.timedelta(days=int(c["Срок оплаты"]))
        if not r["Дата оплаты"] and st != "Аннулирован(Не оплачен)" and due_pay < AS_OF:
            pay_over += 1
        if r["Дата оплаты"]:
            due_del = d(r["Дата оплаты"]) + dt.timedelta(days=int(c["Срок поставки"]))
            remaining = int(r["Количество"]) - int(r["Поставлено"] or 0)
            if st != "Исполнен" and remaining > 0 and due_del < AS_OF:
                deliv_over += 1
            if r["Дата поставки"] and d(r["Дата поставки"]) > due_del:
                late += 1
    assert one(con, "select count(*) filter (where is_payment_overdue) from deal_facts") == pay_over
    assert one(con, "select count(*) filter (where is_delivery_overdue) from deal_facts") == deliv_over
    assert one(con, "select count(*) filter (where is_delivery_late) from deal_facts") == late


def test_regions_and_brokers_separate(con):
    # warehouse = seller region (audit A10); buyer region is an independent column
    assert one(con, "select count(*) from deal_facts where warehouse_region_key <> seller_region_key") == 0
    assert one(con, "select count(*) from deal_facts where buyer_region_key <> seller_region_key") > 0
    assert one(con, "select count(*) from deal_facts where seller_broker_code <> buyer_broker_code") > 0
    assert one(con, "select count(distinct warehouse_region_key) from deal_facts") == 14


def test_incompatible_units_for_same_product_are_visible(con):
    # petrol sold in litres and tonnes (audit A10): dimension column separates them
    dims = {r[0] for r in con.execute(
        "select distinct quantity_dimension from deal_facts where lower(product_name) like 'бензин%'").fetchall()}
    assert dims == {"mass", "volume"}


def test_prepared_db_reused_on_second_ensure(tmp_path):
    db = tmp_path / "r.duckdb"
    assert ensure_database(DATA, db, AS_OF).rebuilt
    assert not ensure_database(DATA, db, AS_OF).rebuilt
