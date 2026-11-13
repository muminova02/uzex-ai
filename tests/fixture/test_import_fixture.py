"""Import pipeline tests on the synthetic fixture (see conftest.py for hand-computed values)."""
from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal

import duckdb
import pytest

from uzex_assistant import importer
from uzex_assistant import schema as S
from uzex_assistant.importer import ImportFailure, ensure_database, read_identity

from .conftest import CONTRACTS, DEALS_2025, DEALS_2026, K1, NBSP, deal

AS_OF = dt.date(2026, 10, 30)


def row(con, deal_id, *cols):
    r = con.execute(f"select {', '.join(cols)} from deal_facts where deal_id = ?", [deal_id]).fetchone()
    return r[0] if len(cols) == 1 else r


# ---------------------------------------------------------------- counts, keys, joins

def test_row_counts(built):
    _, _, rep, con = built
    assert rep.rebuilt
    assert rep.counts == {"deals": 11, "contracts": 3, "participants": 4, "brokers": 2, "usd_rates": 7, "deal_facts": 11}
    assert all(c[3] for c in rep.checks)


def test_unique_keys(built):
    con = built[3]
    for table, key in [("deals", "deal_id"), ("contracts", "contract_id"), ("participants", "inn"),
                       ("brokers", "broker_code"), ("usd_rates", "rate_date")]:
        n, d = con.execute(f"select count(*), count(distinct {key}) from {table}").fetchone()
        assert n == d


def test_joins_do_not_multiply_rows(built):
    con = built[3]
    assert con.execute("select count(*) from deals").fetchone()[0] == con.execute("select count(*) from deal_facts").fetchone()[0]


def test_identifiers_are_strings_with_leading_zeros(built):
    con = built[3]
    assert row(con, "000001", "deal_id") == "000001"
    types = dict((n, t) for n, t, *_ in con.execute("describe deal_facts").fetchall())
    for col in ("deal_id", "contract_id", "seller_inn", "buyer_inn", "seller_broker_code", "buyer_broker_code", "tnved_code"):
        assert types[col] == "VARCHAR", col


def test_no_double_columns_in_view(built):
    con = built[3]
    assert [n for n, t, *_ in con.execute("describe deal_facts").fetchall() if t in ("DOUBLE", "FLOAT")] == []


# ---------------------------------------------------------------- money, FX, units

def test_lot_size_and_quantities(built):
    con = built[3]
    assert row(con, "000001", "lot_count", "lot_size", "quantity_native", "quantity_unit") == (22, 1000, 22000, "литр")
    assert row(con, "000001", "unit_price") == Decimal("9248.000000")
    # narrow NBSP thousands separators in Количество and Сумма сделки
    assert row(con, "000003", "lot_count", "quantity_native", "raw_amount") == (1234, 123400, Decimal("1234000.10"))
    assert row(con, "000003", "quantity_base", "quantity_base_unit", "quantity_dimension") == (123400, "kg", "mass")
    assert row(con, "000002", "quantity_base", "quantity_base_unit") == (3000, "kg")  # tonne -> kg


def test_unit_price_exact_half_up(built):
    con = built[3]
    assert row(con, "000002", "unit_price") == Decimal("9000.166667")   # 27000.50 / 3
    assert row(con, "000003", "unit_price") == Decimal("10.000001")     # 1234000.10 / 123400


def test_usd_conversion(built):
    con = built[3]
    cur, amt, rate, uzs, conv = row(con, "000002", "currency", "raw_amount", "fx_rate_applied", "amount_uzs", "is_converted_from_usd")
    assert (cur, amt, rate, conv) == ("USD", Decimal("27000.50"), Decimal("12500.50"), True)
    assert uzs == Decimal("337519750.2500") == Decimal("27000.50") * Decimal("12500.50")
    assert row(con, "000002", "unit_price_uzs") == Decimal("112506583.416667")  # 337519750.25 / 3


def test_fx_matching(built):
    con = built[3]
    # UZS deals: no FX applied, amount unchanged; every deal has the rate of its own date
    assert con.execute("select count(*) from deal_facts where currency='UZS' and (fx_rate_applied is not null or amount_uzs <> raw_amount)").fetchone()[0] == 0
    bad = con.execute("""select count(*) from deal_facts f join usd_rates r on r.rate_date = f.deal_date
                         where f.usd_rate_on_deal_date <> r.uzs_per_usd""").fetchone()[0]
    assert bad == 0
    assert row(con, "000003", "usd_rate_on_deal_date") == Decimal("12600.00")  # 11.03.2025


def test_currency_from_contract(built):
    con = built[3]
    assert dict(con.execute("select contract_id, currency from contracts").fetchall()) == {
        "900001": "UZS", "900002": "USD", "900003": "UZS"}


# ---------------------------------------------------------------- delivery, statuses, deadlines

def test_partial_delivery(built):
    con = built[3]
    assert row(con, "000001", "status_code", "delivered_lots", "delivered_quantity", "remaining_lots", "remaining_quantity") == (
        "partially_fulfilled", 16, 16000, 6, 6000)
    assert row(con, "000001", "delivery_due_date", "is_delivery_overdue") == (dt.date(2025, 3, 22), True)


def test_empty_delivered_is_null_not_zero(built):
    con = built[3]
    assert row(con, "000007", "delivered_lots", "delivered_quantity", "remaining_lots") == (None, None, 2)
    assert row(con, "000003", "delivered_lots", "remaining_lots") == (None, None)  # cancelled: nothing owed


def test_status_policies(built):
    con = built[3]
    counts = dict(con.execute("""select p.policy, count(f.deal_id) from status_policies p
                                 left join deal_facts f on f.status_code = p.status_code group by 1""").fetchall())
    assert counts == {"registry": 11, "not_cancelled": 10, "paid": 5, "fulfilled": 2, "fulfilled_any": 3,
                      "active": 8, "awaiting_payment_any": 5, "cancelled": 1}


def test_status_codes(built):
    con = built[3]
    got = dict(con.execute("select deal_id, status_code from deal_facts").fetchall())
    assert got["000006"] == "new" and got["000010"] == "concluded" and got["000011"] == "unsigned"
    assert got["000003"] == "cancelled_unpaid"


@pytest.mark.parametrize("deal_id, due, overdue", [
    ("000004", dt.date(2026, 10, 30), False),  # due == as_of -> not overdue
    ("000005", dt.date(2026, 10, 29), True),   # due <  as_of -> overdue
])
def test_payment_deadline_boundary(built, deal_id, due, overdue):
    assert row(built[3], deal_id, "payment_due_date", "is_payment_overdue") == (due, overdue)


@pytest.mark.parametrize("deal_id, due, overdue", [
    ("000007", dt.date(2026, 10, 30), False),
    ("000008", dt.date(2026, 10, 29), True),
])
def test_delivery_deadline_boundary(built, deal_id, due, overdue):
    assert row(built[3], deal_id, "delivery_due_date", "is_delivery_overdue") == (due, overdue)


def test_cancelled_is_not_payment_overdue(built):
    assert row(built[3], "000003", "is_payment_overdue") is False


def test_late_delivery_history(built):
    con = built[3]
    assert row(con, "000009", "delivery_due_date", "is_delivery_late", "is_delivery_overdue") == (dt.date(2025, 3, 22), True, False)
    assert row(con, "000002", "is_delivery_late") is False  # 15.03 <= 11.03 + 7


# ---------------------------------------------------------------- regions, parties, brokers

def test_regions_kept_distinct(built):
    con = built[3]
    assert row(con, "000001", "warehouse_region_key", "seller_region_key", "buyer_region_key") == ("fergana", "fergana", "namangan")
    assert row(con, "000002", "warehouse_region_key", "buyer_region_key") == ("tashkent_city", "andijan")
    assert con.execute("select warehouse_region, warehouse_region_key from contracts where contract_id='900002'").fetchone() == (
        "г. Ташкент", "tashkent_city")


def test_brokers_joined_separately(built):
    assert row(built[3], "000001", "seller_broker_code", "seller_broker_name", "buyer_broker_code", "buyer_broker_name") == (
        "BK001", "BROKER ONE", "BK002", "BROKER TWO")


def test_quoted_name_with_comma(built):
    assert row(built[3], "000001", "buyer_name") == '"АЛЬФА, САВДО" МЧЖ'


def test_source_identifiers(built):
    con = built[3]
    assert row(con, "000003", "source_file", "source_row") == ("deals_2025.csv", 3)
    assert row(con, "000004", "source_file", "source_row") == ("deals_2026.csv", 1)


# ---------------------------------------------------------------- idempotency and reproducibility

def _hashes(d):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir())}


def test_reuse_without_rebuild(built):
    data, db, rep, _ = built
    mtime = db.stat().st_mtime_ns
    again = ensure_database(data, db, AS_OF)
    assert again.rebuilt is False and db.stat().st_mtime_ns == mtime
    assert again.identity == rep.identity


def test_clean_state_build_is_reproducible(fresh_copy, tmp_path):
    data = fresh_copy()
    before = _hashes(data)
    a = ensure_database(data, tmp_path / "a.duckdb", AS_OF)
    b = ensure_database(data, tmp_path / "b.duckdb", AS_OF)
    assert a.rebuilt and b.rebuilt and a.identity == b.identity and a.counts == b.counts
    q = "select * exclude (deal_ts) from deal_facts order by deal_id"
    with duckdb.connect(str(tmp_path / "a.duckdb"), read_only=True) as ca, duckdb.connect(str(tmp_path / "b.duckdb"), read_only=True) as cb:
        assert ca.execute(q).fetchall() == cb.execute(q).fetchall()
    assert _hashes(data) == before  # raw CSVs unchanged


def test_rebuild_when_source_changes(fresh_copy, tmp_path):
    data = fresh_copy()
    db = tmp_path / "x.duckdb"
    ensure_database(data, db, AS_OF)
    with open(data / "brokers.csv", "a", encoding="utf-8") as fh:
        fh.write("BK003,BROKER THREE,777777777,Xorazm viloyati\n")
    rep = ensure_database(data, db, AS_OF)
    assert rep.rebuilt and rep.counts["brokers"] == 3


def test_rebuild_when_schema_version_or_as_of_changes(fresh_copy, tmp_path, monkeypatch):
    data = fresh_copy()
    db = tmp_path / "x.duckdb"
    ensure_database(data, db, AS_OF)
    assert ensure_database(data, db, dt.date(2026, 10, 31)).rebuilt
    assert not ensure_database(data, db, dt.date(2026, 10, 31)).rebuilt
    monkeypatch.setattr(S, "SCHEMA_VERSION", "test-bump")
    assert ensure_database(data, db, dt.date(2026, 10, 31)).rebuilt


def test_readonly_connection(built):
    con = importer.connect_readonly(built[1])
    with pytest.raises(duckdb.Error):
        con.execute("create table x (a int)")
    con.close()


# ---------------------------------------------------------------- failures are reported, never coerced

def _bad_deal(**kw):
    base = dict(did="000099", date="10.03.2025", k=K1, qty="1", amount="9 000 000", status="Исполнен",
                pay="12.03.2025", deliv="13.03.2025", delivered="1")
    base.update(kw)
    return deal(**base)


@pytest.mark.parametrize("bad, needle", [
    (_bad_deal(amount="9,000"), "Сумма сделки"),                 # comma is not a decimal/thousands mark here
    (_bad_deal(amount="9 000.123"), "Сумма сделки"),             # 3 decimals would be silently rounded by CAST
    (_bad_deal(amount="90 00"), "Сумма сделки"),                 # wrong thousands grouping
    (_bad_deal(amount="abc"), "Сумма сделки"),
    (_bad_deal(amount=""), "Сумма сделки"),                      # empty is not zero
    (_bad_deal(qty="1.5"), "Количество"),
    (_bad_deal(date="31.02.2025"), "Дата сделки"),               # impossible date
    (_bad_deal(date="1.03.2025"), "Дата сделки"),                # not DD.MM.YYYY (strptime would accept it)
    (_bad_deal(date="2025-03-10"), "Дата сделки"),
    (_bad_deal(delivered="x"), "Поставлено"),
    (_bad_deal(status="Удален"), "Состояние сделки"),
    (_bad_deal(k={**K1, "region": "Unknown viloyati"}), "Регион"),
    (_bad_deal(k={**K1, "contract": "999999"}), "deals without contract"),
    (_bad_deal(k={**K1, "lot": "100"}), "lot_size"),
    (_bad_deal(did="000001"), "duplicate key"),
    (_bad_deal(date="12.03.2025"), "without USD rate"),
    (_bad_deal(delivered="2"), "delivered_lots > lot_count"),
    (_bad_deal(status="Ждёт оплату"), "unpaid-status deals with payment_date"),
])
def test_malformed_deal_rows_fail_loudly(fresh_copy, tmp_path, bad, needle):
    data = fresh_copy(deals_2025=DEALS_2025 + [bad])
    db = tmp_path / "bad.duckdb"
    with pytest.raises(ImportFailure) as e:
        ensure_database(data, db, AS_OF)
    assert needle in str(e.value)
    assert not db.exists() and not (tmp_path / "bad.duckdb.building").exists()


def test_failure_reports_file_and_row(fresh_copy, tmp_path):
    data = fresh_copy(deals_2025=DEALS_2025 + [_bad_deal(amount="12,5")])
    with pytest.raises(ImportFailure) as e:
        ensure_database(data, tmp_path / "bad.duckdb", AS_OF)
    assert "('deals_2025.csv', 6, '12,5')" in str(e.value)


def test_header_mismatch_fails(fresh_copy, tmp_path):
    data = fresh_copy()
    text = (data / "usd_rates.csv").read_text(encoding="utf-8").replace("Курс", "Rate", 1)
    (data / "usd_rates.csv").write_text(text, encoding="utf-8")
    with pytest.raises(ImportFailure, match="header mismatch"):
        ensure_database(data, tmp_path / "bad.duckdb", AS_OF)


def test_wrong_field_count_fails(fresh_copy, tmp_path):
    data = fresh_copy()
    with open(data / "brokers.csv", "a", encoding="utf-8") as fh:
        fh.write("BK009,ONLY THREE,123456789\n")
    with pytest.raises(ImportFailure, match="fields"):
        ensure_database(data, tmp_path / "bad.duckdb", AS_OF)


def test_missing_file_fails(fresh_copy, tmp_path):
    data = fresh_copy()
    (data / "brokers.csv").unlink()
    with pytest.raises(ImportFailure, match="missing file"):
        ensure_database(data, tmp_path / "bad.duckdb", AS_OF)


def test_contract_bad_deposit_and_currency(fresh_copy, tmp_path):
    bad = [r[:] for r in CONTRACTS]
    bad[0][13] = "Евро"
    bad[1][15] = "15%%"
    data = fresh_copy(contracts=bad)
    with pytest.raises(ImportFailure) as e:
        ensure_database(data, tmp_path / "bad.duckdb", AS_OF)
    assert "Валюта" in str(e.value) and "Сумма залога" in str(e.value)


def test_failed_rebuild_keeps_existing_database(fresh_copy, tmp_path):
    data = fresh_copy()
    db = tmp_path / "keep.duckdb"
    good = ensure_database(data, db, AS_OF)
    rows = DEALS_2026 + [deal("000098", "19.10.2026", K1, "1", f"1{NBSP}0", "Новый")]
    from .conftest import write_csv
    write_csv(data / "deals_2026.csv", S.DEAL_COLUMNS, rows)
    with pytest.raises(ImportFailure):
        ensure_database(data, db, AS_OF)
    assert read_identity(db) == good.identity  # old prepared DB untouched and still readable


def test_cli_reports_failure(fresh_copy, tmp_path, capsys, monkeypatch):
    # about error reporting, not the freeze: pin a future freeze date so the test does not depend on the clock
    monkeypatch.setenv("UZEX_FREEZE_AT", "2099-01-01T00:00:00+05:00")
    from uzex_assistant.config import get_settings
    get_settings.cache_clear()
    data = fresh_copy(deals_2025=DEALS_2025 + [_bad_deal(amount="oops")])
    try:
        code = importer.main(["build", "--data-dir", str(data), "--db-path", str(tmp_path / "c.duckdb")])
    finally:
        monkeypatch.delenv("UZEX_FREEZE_AT")
        get_settings.cache_clear()  # never leak the pinned date into later tests
    assert code == 2 and "Сумма сделки" in capsys.readouterr().err


def test_metadata_readable_without_optional_modules(built):
    con = built[3]
    sv, as_of, built_at, ver = con.execute("select schema_version, as_of, built_at_utc, duckdb_version from import_meta").fetchone()
    assert sv == S.SCHEMA_VERSION and as_of == AS_OF and built_at.endswith("+00:00") and ver == duckdb.__version__
    assert con.execute("select count(*) from import_checks where not ok").fetchone()[0] == 0
    assert con.execute("select count(*) from source_files").fetchone()[0] == 6
