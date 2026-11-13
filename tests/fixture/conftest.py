"""Synthetic, hand-checkable mini dataset (independent of the real data).

Hand-computed expectations (as_of = 2026-10-30):
  D1 000001 K1 petrol 22 lots x 1000 l, 203 456 000 UZS, partial 16 lots
      -> 22 000 l, 9 248 UZS/l, delivered 16 000 l, remaining 6 000 l,
         delivery due 12.03.2025 + 10 = 22.03.2025 < as_of -> overdue (remaining > 0)
  D2 000002 K2 copper USD 3 t, 27 000.50 USD (NBSP separator), rate 10.03.2025 = 12 500.50
      -> amount_uzs = 337 519 750.2500, unit price 9 000.166667 USD/t (half-up from 9000.1666...)
  D3 000003 K3 flour 1 234 lots (narrow NBSP) x 100 kg, 1 234 000.10 UZS, cancelled
      -> 123 400 kg, unit price 10.000001 UZS/kg (from 10.00000081...)
  D4 Ждёт оплату 25.10.2026 + 5 = 30.10.2026 = as_of -> NOT overdue (boundary)
  D5 Ждёт оплату 24.10.2026 + 5 = 29.10.2026 < as_of -> payment overdue
  D7 Оплачен paid 20.10.2026 + 10 = 30.10.2026 -> delivery NOT overdue (boundary)
  D8 Оплачен paid 19.10.2026 + 10 = 29.10.2026 -> delivery overdue
  D9 Исполнен paid 12.03.2025, delivered 23.03.2025 > due 22.03.2025 -> late delivery
"""
from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest

from uzex_assistant import schema as S

NBSP, NNBSP = " ", " "

CONTRACTS = [
    # id, product, brand, size, grade, type, class, tnved, group, category, industry, unit, lot, currency,
    # base price, deposit, basis, region, address, trade, ctype, form, pay, deliv, pack, seller, reg, valid
    ["900001", "Бензин", "АИ-92", "", "", "", "", "271012", "Высоколиквидные товары", "Нефтепродукты", "Нефтегаз",
     "литр", "1000", "Узбекский сум", "9000", "10 %", "EXW", "Ферганская область", "addr, 1", "Английский аукцион",
     "Внутренний", "Спот", "5", "10", "в цистернах", "111111111", "01.01.2025", "31.12.2026"],
    ["900002", "Катод медный", "М0к", "", "", "", "", "740311", "Высоколиквидные товары", "Металлопродукция", "Металлургия",
     "тонна", "1", "Доллар США", "8 900.00", "15 %", "FCA", "г. Ташкент", "addr 2", "Английский аукцион",
     "Экспортный", "Спот", "3", "7", "навалом", "222222222", "01.01.2025", "31.12.2026"],
    ["900003", "Мука", "", "", "1 сорт", "", "", "110100", "Высоколиквидные товары", "Продовольственные товары",
     "Пищевая промышленность", "килограмм", "100", "Узбекский сум", "9", "20 %", "EXW", "Ферганская область", "addr 3",
     "Двусторонний аукцион", "Внутренний", "Форвард", "10", "5", "в мешках", "111111111", "01.01.2025", "31.12.2026"],
]
PARTICIPANTS = [
    ["111111111", "Fargona Neft MChJ", "Farg‘ona viloyati", "Производитель", "Продавец", "Да"],
    ["222222222", "Tashkent Copper AJ", "Toshkent shahri", "Производитель", "Продавец", "Нет"],
    ["333333333", '"АЛЬФА, САВДО" МЧЖ', "Namangan viloyati", "Торговая компания", "Покупатель", "Да"],
    ["444444444", "Andijon Trade", "Andijon viloyati", "Торговая компания", "Покупатель", "Нет"],
]
BROKERS = [["BK001", "BROKER ONE", "555555555", "Toshkent shahri"], ["BK002", "BROKER TWO", "666666666", "Namangan viloyati"]]
RATES = [["10.03.2025", "12500.50"], ["11.03.2025", "12600.00"], ["19.10.2026", "13000.00"], ["20.10.2026", "13010.00"],
         ["24.10.2026", "13050.00"], ["25.10.2026", "13060.00"], ["30.10.2026", "13100.00"]]

K1 = dict(contract="900001", name="Бензин Марка АИ-92", lot="1000", unit="литр", region="Farg‘ona viloyati",
          ctype="Внутренний", form="Спот", trade="Английский аукцион", seller="111111111")
K2 = dict(contract="900002", name="Катод медный Марка М0к", lot="1", unit="тонна", region="Toshkent shahri",
          ctype="Экспортный", form="Спот", trade="Английский аукцион", seller="222222222")
K3 = dict(contract="900003", name="Мука Сорт 1 сорт", lot="100", unit="килограмм", region="Farg‘ona viloyati",
          ctype="Внутренний", form="Форвард", trade="Двусторонний аукцион", seller="111111111")


def deal(did, date, k, qty, amount, status, pay="", deliv="", delivered="", buyer="333333333",
         sb="BK001", bb="BK002", start="1"):
    return [did, date, "10:00:00", k["trade"], qty, amount, start, k["contract"], k["name"], k["lot"], k["unit"],
            k["region"], k["ctype"], k["form"], status, k["seller"], buyer, sb, bb, pay, deliv, delivered]


DEALS_2025 = [
    deal("000001", "10.03.2025", K1, "22", "203 456 000", "Исполнен(Частично)", "12.03.2025", "20.03.2025", "16"),
    deal("000002", "10.03.2025", K2, "3", f"27{NBSP}000.50", "Исполнен", "11.03.2025", "15.03.2025", "3", buyer="444444444"),
    deal("000003", "11.03.2025", K3, f"1{NNBSP}234", f"1{NNBSP}234{NNBSP}000.10", "Аннулирован(Не оплачен)"),
    deal("000009", "10.03.2025", K1, "5", "46 000 000", "Исполнен", "12.03.2025", "23.03.2025", "5"),
    deal("000010", "11.03.2025", K3, "3", "2 700", "Заключен"),
]
DEALS_2026 = [
    deal("000004", "25.10.2026", K1, "1", "9 500 000", "Ждёт оплату"),
    deal("000005", "24.10.2026", K1, "1", "9 400 000", "Ждёт оплату"),
    deal("000006", "30.10.2026", K3, "2", "1 800", "Новый"),
    deal("000007", "20.10.2026", K1, "2", "18 000 000", "Оплачен", "20.10.2026"),
    deal("000008", "19.10.2026", K1, "2", "18 100 000", "Оплачен", "19.10.2026"),
    deal("000011", "19.10.2026", K1, "1", "9 000 000", "Не подписан", buyer="444444444"),
]


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def write_dataset(d: Path, deals_2025=None, deals_2026=None, contracts=None, participants=None, brokers=None, rates=None):
    d.mkdir(parents=True, exist_ok=True)
    write_csv(d / "deals_2025.csv", S.DEAL_COLUMNS, DEALS_2025 if deals_2025 is None else deals_2025)
    write_csv(d / "deals_2026.csv", S.DEAL_COLUMNS, DEALS_2026 if deals_2026 is None else deals_2026)
    write_csv(d / "contracts.csv", S.CONTRACT_COLUMNS, contracts or CONTRACTS)
    write_csv(d / "participants.csv", S.PARTICIPANT_COLUMNS, participants or PARTICIPANTS)
    write_csv(d / "brokers.csv", S.BROKER_COLUMNS, brokers or BROKERS)
    write_csv(d / "usd_rates.csv", S.RATE_COLUMNS, rates or RATES)
    return d


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """(data_dir, db_path, report, connection) for the default fixture dataset."""
    import datetime as dt

    import duckdb

    from uzex_assistant.importer import ensure_database

    base = tmp_path_factory.mktemp("fx")
    data = write_dataset(base / "raw")
    db = base / "db" / "fixture.duckdb"
    rep = ensure_database(data, db, dt.date(2026, 10, 30))
    con = duckdb.connect(str(db), read_only=True)
    yield data, db, rep, con
    con.close()


@pytest.fixture
def fresh_copy(tmp_path):
    def make(**overrides):
        return write_dataset(tmp_path / "raw", **overrides)
    return make


__all__ = ["shutil"]
