"""Independent reference answers for the Prompt 6 golden dataset.

Pure Python over the RAW CSV files (csv + Decimal + datetime). It imports nothing from uzex_assistant,
uses no DuckDB, and does not read the prepared database or deal_facts. Products, regions, statuses and
deadlines are defined from raw fields (TN VED code, raw names/brand, raw region strings, raw statuses),
written here independently of the engine's catalog logic.

    .venv/bin/python -I scripts/eval/reference.py uzex_spot_data eval/golden_v1.json
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

AS_OF = dt.date(2026, 10, 30)
CANCELLED = "Аннулирован(Не оплачен)"
PAID = {"Оплачен", "Исполнен(Частично)", "Исполнен"}


def d(s: str) -> dt.date:
    day, month, year = s.split(".")
    return dt.date(int(year), int(month), int(day))


def num(s: str) -> Decimal:
    return Decimal(s.replace(" ", "").replace(" ", "").replace(" ", ""))


def q2(x: Decimal) -> str:
    return str(x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def load(data: Path):
    contracts = {r["Номер контракта"]: r for r in csv.DictReader(open(data / "contracts.csv", encoding="utf-8", newline=""))}
    parts = {r["ИНН"]: r for r in csv.DictReader(open(data / "participants.csv", encoding="utf-8", newline=""))}
    rates = {d(r["Дата"]): Decimal(r["Курс"]) for r in csv.DictReader(open(data / "usd_rates.csv", encoding="utf-8", newline=""))}
    deals = []
    for f in sorted(data.glob("deals_*.csv")):
        for r in csv.DictReader(open(f, encoding="utf-8", newline="")):
            c = contracts[r["Номер контракта"]]
            amt = num(r["Сумма сделки"])
            usd = c["Валюта"] == "Доллар США"
            date = d(r["Дата сделки"])
            deals.append({
                "id": r["Номер сделки"], "date": date, "status": r["Состояние сделки"], "form": r["Форма контракта"],
                "lots": int(r["Количество"]), "lot": int(r["Лот"]), "unit": r["Единица измерения"], "amt": amt, "usd": usd,
                "uzs": amt * rates[date] if usd else amt, "seller": r["ИНН продавца"], "buyer": r["ИНН покупателя"],
                "sb": r["Брокер продавца"], "bb": r["Брокер покупателя"], "region": r["Регион"], "contract": r["Номер контракта"],
                "name": r["Наименование товара"], "tnved": c["Код ТН ВЭД"], "brand": c["Марка товара"],
                "pay": d(r["Дата оплаты"]) if r["Дата оплаты"] else None, "deliv": d(r["Дата поставки"]) if r["Дата поставки"] else None,
                "delivered": int(r["Поставлено"]) if r["Поставлено"] else None,
                "pay_term": int(c["Срок оплаты"]), "del_term": int(c["Срок поставки"]), "product": c["Наименование продукта"],
            })
    return contracts, parts, rates, deals


def compact(s: str) -> str:
    return re.sub(r"[\s\-]", "", s or "").upper()


def build(data: Path) -> dict:
    contracts, parts, rates, D = load(data)
    y = lambda r, year: r["date"].year == year  # noqa: E731
    G: dict = {}
    G["M01_total"] = len(D)
    G["M02_2025"] = sum(y(r, 2025) for r in D)
    forms = Counter(r["form"] for r in D)
    G["M03_forms"] = {"spot": forms["Спот"], "forward": forms["Форвард"], "futures": forms["Фьючерс"]}
    G["M04_usd_deals"] = sum(r["usd"] for r in D)
    G["M05_cancelled"] = sum(r["status"] == CANCELLED for r in D)
    G["M06_status_2026"] = dict(Counter(r["status"] for r in D if y(r, 2026)))
    r7 = next(r for r in D if r["id"] == "387831")
    qty = r7["lots"] * r7["lot"]
    G["M07_deal_387831"] = {"lots": r7["lots"], "lot": r7["lot"], "quantity": qty, "unit": r7["unit"], "amount": str(r7["amt"]),
                            "unit_price": q2(r7["amt"] / qty), "delivered": r7["delivered"] * r7["lot"],
                            "remaining": qty - r7["delivered"] * r7["lot"], "status": r7["status"], "seller": r7["seller"],
                            "buyer": r7["buyer"]}
    c8 = contracts["102557"]
    G["M08_contract_102557"] = {"product": c8["Наименование продукта"], "brand": c8["Марка товара"], "currency": c8["Валюта"],
                                "lot": int(c8["Лот"]), "unit": c8["Единица измерения"], "pay_term": int(c8["Срок оплаты"]),
                                "del_term": int(c8["Срок поставки"]), "seller": c8["ИНН продавца"],
                                "deals": sum(r["contract"] == "102557" for r in D)}
    sv = defaultdict(Decimal)
    for r in D:
        if y(r, 2025) and r["status"] != CANCELLED:
            sv[r["seller"]] += r["uzs"]
    G["M09_top5_sellers_value_2025"] = [[i, q2(v)] for i, v in sorted(sv.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
    bc = Counter(r["buyer"] for r in D if y(r, 2024))
    G["M10_top5_buyers_count_2024"] = [[i, n] for i, n in sorted(bc.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
    bb = Counter(r["bb"] for r in D)
    G["M11_top3_buyer_brokers"] = [[i, n] for i, n in sorted(bb.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    G["M12_bk073_as_seller_broker_2025"] = sum(r["sb"] == "BK073" and y(r, 2025) for r in D)
    G["M13_bk073_as_buyer_broker_2025"] = sum(r["bb"] == "BK073" and y(r, 2025) for r in D)
    fer = [r for r in D if y(r, 2024) and r["region"] == "Farg‘ona viloyati"]
    G["M14_fergana_warehouse_2024"] = {"deals": len(fer), "value_uzs_not_cancelled": q2(sum((r["uzs"] for r in fer if r["status"] != CANCELLED), Decimal(0)))}
    G["M15_samarkand_buyers_2023"] = sum(y(r, 2023) and parts[r["buyer"]]["Регион"] == "Samarqand viloyati" for r in D)
    G["M16_ai95_2025"] = sum(y(r, 2025) and r["tnved"] == "271012" and ("АИ95" in compact(r["brand"]) or "АИ95" in compact(r["name"]))
                             for r in D)
    an = [r for r in D if y(r, 2025) and r["tnved"] == "310230" and r["status"] != CANCELLED]
    assert {r["unit"] for r in an} == {"тонна"} and not any(r["usd"] for r in an)
    G["M17_wavg_ammonium_nitrate_2025"] = {"price": q2(sum((r["amt"] for r in an), Decimal(0)) / sum(r["lots"] * r["lot"] for r in an)),
                                           "unit": "UZS/тонна", "deals_not_cancelled": len(an)}
    ex = [r for r in D if y(r, 2024) and r["usd"] and r["status"] != CANCELLED]
    G["M18_export_2024"] = {"value_uzs": q2(sum((r["uzs"] for r in ex), Decimal(0))), "value_usd": q2(sum((r["amt"] for r in ex), Decimal(0))),
                            "deals_not_cancelled": len(ex)}
    G["M19_lot_1000"] = sum(r["lot"] == 1000 for r in D)
    G["M20_partial_2025"] = sum(y(r, 2025) and r["status"] == "Исполнен(Частично)" for r in D)
    over = 0
    for r in D:
        if r["status"] in PAID and r["status"] != "Исполнен" and r["pay"]:
            remaining = r["lots"] - (r["delivered"] or 0)
            if remaining > 0 and r["pay"] + dt.timedelta(days=r["del_term"]) < AS_OF:
                over += 1
    G["M21_overdue_delivery"] = over
    G["M22_overdue_payment"] = sum(r["pay"] is None and r["status"] != CANCELLED and r["date"] + dt.timedelta(days=r["pay_term"]) < AS_OF
                                   for r in D)
    G["M22b_payment_due_on_asof"] = sum(r["pay"] is None and r["status"] != CANCELLED and r["date"] + dt.timedelta(days=r["pay_term"]) == AS_OF
                                        for r in D)
    G["M23_compare_2025_2026_equiv"] = {
        "p1": sum(dt.date(2025, 1, 1) <= r["date"] <= dt.date(2025, 10, 30) for r in D),
        "p2": sum(dt.date(2026, 1, 1) <= r["date"] <= dt.date(2026, 10, 30) for r in D)}
    pq = defaultdict(int)
    for r in D:
        if y(r, 2025) and r["tnved"] == "271012" and r["status"] != CANCELLED:
            pq[r["unit"]] += r["lots"] * r["lot"]
    G["M24_petrol_qty_2025_by_unit"] = dict(pq)
    cot = [r for r in D if r["tnved"] == "520100" and dt.date(2026, 3, 1) <= r["date"] <= dt.date(2026, 3, 31)]
    G["M25_top5_cotton_mar2026"] = {"total": len(cot), "ids": [r["id"] for r in sorted(cot, key=lambda r: (-r["uzs"], r["id"]))[:5]]}
    G["M31b_tashkent_city_2024"] = sum(y(r, 2024) and r["region"] == "Toshkent shahri" for r in D)
    return G


def main() -> int:
    data, out = Path(sys.argv[1]), Path(sys.argv[2])
    fp = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(data.glob("*.csv"))}
    g = build(data)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"method": "independent pure-Python computation over raw CSVs (scripts/eval/reference.py); "
                                         "no engine code, no DuckDB, no prepared DB",
                               "computed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                               "as_of": AS_OF.isoformat(), "source_sha256": fp, "answers": g}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(json.dumps(g, ensure_ascii=False)[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
