"""Independent reference answers for the accuracy-audit set v2 (eval/cases_v2.py).

Same rules as reference.py: pure Python over the RAW CSVs, imports nothing from uzex_assistant, no DuckDB,
no prepared DB. Product attributes (class, size, brand) are normalised HERE with simple rules written for the
raw spellings (e.g. "4 класс" = "IV КЛАСС" = "4-класс"), independently of the engine's catalog code.

    .venv/bin/python -I scripts/eval/reference_v2.py uzex_spot_data eval/golden_v2.json
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reference import AS_OF, CANCELLED, PAID, compact, load, q2  # noqa: E402  (raw-CSV loader, no engine code)

ROMAN = {"I": "1", "II": "2", "III": "3", "IV": "4", "V": "5"}


def wheat_class(s: str) -> str | None:
    if not s:
        return None
    t = s.upper().replace("-", " ").replace("КЛАСС", "").strip()
    return ROMAN.get(t, t) if t else None


def build(data: Path) -> dict:
    contracts, parts, rates, D = load(data)
    y = lambda r, year: r["date"].year == year  # noqa: E731
    nc = lambda r: r["status"] != CANCELLED  # noqa: E731
    G: dict = {}
    G["A01_english_auction_2023"] = sum(y(r, 2023) and contracts[r["contract"]]["Тип торгов"] == "Английский аукцион" for r in D)
    G["A02_distinct_buyers_2025"] = len({r["buyer"] for r in D if y(r, 2025)})
    G["A03_distinct_seller_brokers_2024"] = len({r["sb"] for r in D if y(r, 2024)})
    v = defaultdict(Decimal)
    for r in D:
        if y(r, 2025) and nc(r):
            v[r["sb"]] += r["uzs"]
    G["A04_top3_seller_brokers_value_2025"] = [[k, q2(x)] for k, x in sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    G["A05_bk111_any_role_2026"] = sum(y(r, 2026) and (r["sb"] == "BK111" or r["bb"] == "BK111") for r in D)
    G["A06_andijan_buyers_2025"] = sum(y(r, 2025) and parts[r["buyer"]]["Регион"] == "Andijon viloyati" for r in D)
    G["A07_namangan_sellers_value_2024"] = q2(sum((r["uzs"] for r in D if y(r, 2024) and nc(r)
                                                   and parts[r["seller"]]["Регион"] == "Namangan viloyati"), Decimal(0)))
    wc = Counter(r["region"] for r in D if y(r, 2026))
    G["A08_top_warehouse_region_2026"] = [[k, n] for k, n in sorted(wc.items(), key=lambda kv: (-kv[1], kv[0]))[:1]]
    agg = defaultdict(lambda: [Decimal(0), 0])
    for r in D:
        if y(r, 2025) and nc(r) and r["tnved"] == "271012" and "АИ92" in compact(r["brand"]):
            assert not r["usd"]
            agg[r["unit"]][0] += r["amt"]
            agg[r["unit"]][1] += r["lots"] * r["lot"]
    G["A09_ai92_wavg_price_2025_by_unit"] = {u: q2(a / q) for u, (a, q) in agg.items()}
    def cls(r):  # class field, else the class written in the product name ("Пшеница 4-класс"): raw data has both
        c = wheat_class(contracts[r["contract"]]["Класс товара"])
        if c:
            return c
        m = re.search(r"(?<![\wА-Яа-я])(IV|III|II|I|\d)[\s-]*класс", r["name"], re.I)
        return ROMAN.get(m.group(1).upper(), m.group(1)) if m else None
    G["A10_wheat_class4_2024"] = sum(y(r, 2024) and r["tnved"] == "100199" and cls(r) == "4" for r in D)
    G["A11_rebar_d12_qty_2025"] = sum(r["lots"] * r["lot"] for r in D if y(r, 2025) and nc(r) and r["tnved"] == "721420"
                                      and re.sub(r"\s", "", contracts[r["contract"]]["Размер товара"]).upper() == "Ø12ММ")
    days = [(r["pay"] - r["date"]).days for r in D if y(r, 2025) and r["pay"]]
    G["A12_avg_days_to_payment_2025"] = q2(Decimal(sum(days)) / len(days))
    dd = [(r["deliv"] - r["pay"]).days for r in D if y(r, 2024) and r["pay"] and r["deliv"]]
    G["A13_avg_days_payment_to_delivery_2024"] = q2(Decimal(sum(dd)) / len(dd))
    G["A14_fulfilled_2024"] = sum(y(r, 2024) and r["status"] == "Исполнен" for r in D)
    rem = defaultdict(int)
    for r in D:
        if r["tnved"] == "252329" and r["status"] in PAID:
            rem[r["unit"]] += (r["lots"] - (r["delivered"] or 0)) * r["lot"]
    G["A15_cement_paid_undelivered_qty"] = dict(rem)
    G["A16_export_usd_value_2025"] = q2(sum((r["amt"] for r in D if y(r, 2025) and nc(r) and r["usd"]), Decimal(0)))
    G["A17_q1_2025_vs_q1_2026"] = {"p1": sum(dt.date(2025, 1, 1) <= r["date"] <= dt.date(2025, 3, 31) for r in D),
                                   "p2": sum(dt.date(2026, 1, 1) <= r["date"] <= dt.date(2026, 3, 31) for r in D)}
    G["A18_cotton_value_2024_vs_2025"] = {f"p{i}": q2(sum((r["uzs"] for r in D if y(r, yr) and nc(r) and r["tnved"] == "520100"), Decimal(0)))
                                          for i, yr in ((1, 2024), (2, 2025))}
    sv = defaultdict(Decimal)
    for r in D:
        if y(r, 2025) and nc(r) and r["tnved"] == "170199":
            sv[r["buyer"]] += r["uzs"]
    G["A19_top5_sugar_buyers_value_2025"] = [[k, q2(x)] for k, x in sorted(sv.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
    n25 = [r for r in D if y(r, 2025)]
    G["A20_cancellation_share_2025"] = q2(Decimal(sum(r["status"] == CANCELLED for r in n25)) * 100 / len(n25))
    # ---------------------------------------------------------------- holdout
    G["H01_overdue_payment_2026"] = sum(y(r, 2026) and r["pay"] is None and nc(r) and r["date"] + dt.timedelta(days=r["pay_term"]) < AS_OF
                                        for r in D)
    G["H02_distinct_buyer_brokers"] = len({r["bb"] for r in D})
    br = defaultdict(Decimal)
    for r in D:
        if y(r, 2025) and nc(r):
            br[parts[r["buyer"]]["Регион"]] += r["uzs"]
    G["H03_top3_buyer_regions_value_2025"] = [[k, q2(x)] for k, x in sorted(br.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    ur = defaultdict(lambda: [Decimal(0), 0])
    for r in D:
        if y(r, 2024) and nc(r) and r["tnved"] == "310210":
            assert not r["usd"]
            ur[r["unit"]][0] += r["amt"]
            ur[r["unit"]][1] += r["lots"] * r["lot"]
    G["H04_urea_wavg_price_2024_by_unit"] = {u: q2(a / q) for u, (a, q) in ur.items()}
    def cem(r):  # brand field, else the brand written in the deal's product name (raw data has both forms)
        b = compact(r["brand"]).replace(",", ".")
        return b == "ЦЕМI42.5Н" if b else "ЦЕМI42.5Н" in compact(r["name"]).replace(",", ".")
    G["H05_cem_i_425_2025"] = sum(y(r, 2025) and r["tnved"] == "252329" and cem(r) for r in D)
    G["H06_tashkent_region_warehouse_2025"] = sum(y(r, 2025) and r["region"] == "Toshkent viloyati" for r in D)
    pe = [r for r in D if y(r, 2025) and nc(r) and r["tnved"] == "390120"]
    G["H07_polyethylene_avg_deal_value_2025"] = q2(sum((r["uzs"] for r in pe), Decimal(0)) / len(pe))
    G["H08_bk085_seller_broker_2024_vs_2025"] = {f"p{i}": sum(y(r, yr) and r["sb"] == "BK085" for r in D) for i, yr in ((1, 2024), (2, 2025))}
    G["H09_tashkent_city_2025"] = sum(y(r, 2025) and r["region"] == "Toshkent shahri" for r in D)
    G["H10_missing_field"] = "missing_field"
    G["R01_tashkent_city_2024"] = sum(y(r, 2024) and r["region"] == "Toshkent shahri" for r in D)
    return G


def main() -> int:
    data, out = Path(sys.argv[1]), Path(sys.argv[2])
    fp = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(data.glob("*.csv"))}
    g = build(data)
    out.write_text(json.dumps({"method": "independent pure-Python computation over raw CSVs (scripts/eval/reference_v2.py); "
                                         "no engine code, no DuckDB, no prepared DB",
                               "computed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                               "as_of": AS_OF.isoformat(), "source_sha256": fp, "answers": g}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(json.dumps(g, ensure_ascii=False, indent=0)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
