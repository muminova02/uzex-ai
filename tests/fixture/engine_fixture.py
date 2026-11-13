"""Extended synthetic dataset for Prompt 3 engine tests (the Prompt 2 fixture stays unchanged).

Additions to tests/fixture/conftest.py:
  contracts 900004 Масло подсолнечное (t), 900005 Масло хлопковое (t), 900006 Бензин АИ-92 in TONNES (lot 1)
  participants: "Alfa Savdo Plus" MChJ (buyer) -> "alfa savdo" matches two companies (ambiguous)
                "Gamma Logistic" MChJ (buyer, no deals)
  deals 2025: D20 sunflower oil, D21 cottonseed oil, D22 petrol in tonnes, D23 buyer Alfa Savdo Plus
"""
from __future__ import annotations

import datetime as dt

from .conftest import CONTRACTS, DEALS_2025, DEALS_2026, K1, PARTICIPANTS, deal, write_dataset

K4 = dict(contract="900004", name="Масло подсолнечное", lot="1", unit="тонна", region="Farg‘ona viloyati",
          ctype="Внутренний", form="Спот", trade="Английский аукцион", seller="111111111")
K5 = dict(K4, contract="900005", name="Масло хлопковое")
K6 = dict(K4, contract="900006", name="Бензин Марка АИ-92")

EXTRA_CONTRACTS = [
    ["900004", "Масло подсолнечное", "", "", "", "", "", "151219", "Высоколиквидные товары", "Продовольственные товары",
     "Пищевая промышленность", "тонна", "1", "Узбекский сум", "15000000", "10 %", "EXW", "Ферганская область", "a",
     "Английский аукцион", "Внутренний", "Спот", "5", "10", "навалом", "111111111", "01.01.2025", "31.12.2026"],
    ["900005", "Масло хлопковое", "", "", "", "", "", "151229", "Высоколиквидные товары", "Продовольственные товары",
     "Пищевая промышленность", "тонна", "1", "Узбекский сум", "16000000", "10 %", "EXW", "Ферганская область", "a",
     "Английский аукцион", "Внутренний", "Спот", "5", "10", "навалом", "111111111", "01.01.2025", "31.12.2026"],
    ["900006", "Бензин", "АИ-92", "", "", "", "", "271012", "Высоколиквидные товары", "Нефтепродукты", "Нефтегаз",
     "тонна", "1", "Узбекский сум", "12000000", "10 %", "EXW", "Ферганская область", "a",
     "Английский аукцион", "Внутренний", "Спот", "5", "10", "в цистернах", "111111111", "01.01.2025", "31.12.2026"],
]
EXTRA_PARTICIPANTS = [
    ["555000001", '"Alfa Savdo Plus" MChJ', "Samarqand viloyati", "Торговая компания", "Покупатель", "Да"],
    ["555000002", '"Gamma Logistic" MChJ', "Xorazm viloyati", "Транспортная компания", "Покупатель", "Нет"],
]
EXTRA_2025 = [
    deal("000020", "10.03.2025", K4, "10", "150 000 000", "Исполнен", "11.03.2025", "12.03.2025", "10"),
    deal("000021", "10.03.2025", K5, "4", "64 000 000", "Исполнен", "11.03.2025", "12.03.2025", "4"),
    deal("000022", "11.03.2025", K6, "2", "24 000 000.50", "Исполнен", "12.03.2025", "13.03.2025", "2"),
    deal("000023", "11.03.2025", K1, "1", "9 100 000", "Исполнен", "12.03.2025", "13.03.2025", "1", buyer="555000001"),
]
AS_OF = dt.date(2026, 10, 30)


def build(base_dir):
    from uzex_assistant.importer import ensure_database

    data = write_dataset(base_dir / "raw", deals_2025=DEALS_2025 + EXTRA_2025, deals_2026=DEALS_2026,
                         contracts=CONTRACTS + EXTRA_CONTRACTS, participants=PARTICIPANTS + EXTRA_PARTICIPANTS)
    db = base_dir / "db" / "engine.duckdb"
    ensure_database(data, db, AS_OF)
    return data, db
