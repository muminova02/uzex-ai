"""Deterministic, grounded answer renderer (Russian, Uzbek Latin, Uzbek Cyrillic). No LLM call.

Structure: direct result → concise table/list → period and filters → unit/currency and method → sources.
Every value comes from the typed AnswerResult (facts, table, evidence, clarification) or from the DB
catalog (original names). Data values are inserted verbatim: company names, INN/STIR, codes and IDs
are never translated. Afterwards ``grounding_errors`` checks that every number in the text occurs in the
payload. If the check fails, a minimal facts-only rendering is used instead.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .lang import Lang, latn_to_cyrl
from .plan import METRICS
from .schema import STATUS_POLICIES

NB = " "

# ---------------------------------------------------------------- vocab (ru, uz-latn)
METRIC_LABEL = {
    "deal_count": ("Количество сделок", "Bitimlar soni"),
    "trade_value_uzs": ("Сумма сделок в сумах", "Bitimlar summasi (so'mda)"),
    "trade_value_native": ("Сумма сделок в валюте контракта", "Shartnoma valyutasidagi bitimlar summasi"),
    "paid_value_uzs": ("Сумма оплаченных сделок в сумах", "To'langan bitimlar summasi (so'mda)"),
    "quantity": ("Купленное количество товара", "Sotib olingan tovar miqdori"),
    "delivered_quantity": ("Поставленное количество", "Yetkazib berilgan miqdor"),
    "remaining_quantity": ("Оплачено, но не поставлено", "To'langan, lekin yetkazilmagan miqdor"),
    "weighted_avg_price": ("Средневзвешенная цена за единицу", "O'rtacha tortilgan birlik narxi"),
    "weighted_avg_price_uzs": ("Средневзвешенная цена за единицу в сумах", "O'rtacha tortilgan birlik narxi (so'mda)"),
    "mean_unit_price": ("Среднее арифметическое цен за единицу", "Birlik narxlarining o'rtacha arifmetigi"),
    "min_unit_price": ("Минимальная цена за единицу", "Eng past birlik narxi"),
    "max_unit_price": ("Максимальная цена за единицу", "Eng yuqori birlik narxi"),
    "avg_deal_value_uzs": ("Средняя сумма сделки в сумах", "Bitimning o'rtacha summasi (so'mda)"),
    "max_deal_value_uzs": ("Крупнейшая сумма сделки в сумах", "Eng yirik bitim summasi (so'mda)"),
    "distinct_sellers": ("Число продавцов", "Sotuvchilar soni"),
    "distinct_buyers": ("Число покупателей", "Xaridorlar soni"),
    "distinct_products": ("Число вариантов товара", "Tovar variantlari soni"),
    "cancellation_share": ("Доля аннулированных сделок", "Bekor qilingan bitimlar ulushi"),
    "overdue_payment_count": ("Сделок с просроченной оплатой", "To'lov muddati o'tgan bitimlar"),
    "overdue_delivery_count": ("Сделок с просроченной поставкой", "Yetkazish muddati o'tgan bitimlar"),
    "late_delivery_count": ("Сделок, поставленных с опозданием", "Kechikib yetkazilgan bitimlar"),
    "avg_days_to_payment": ("Среднее число дней от сделки до оплаты", "Bitimdan to'lovgacha o'rtacha kunlar"),
    "avg_days_to_delivery": ("Среднее число дней от оплаты до поставки", "To'lovdan yetkazishgacha o'rtacha kunlar"),
    "distinct_seller_brokers": ("Число брокеров продавцов", "Sotuvchi brokerlari soni"),
    "distinct_buyer_brokers": ("Число брокеров покупателей", "Xaridor brokerlari soni"),
    "min_deal_value_uzs": ("Наименьшая сумма сделки в сумах", "Eng kichik bitim summasi (so'mda)"),
    "median_deal_value_uzs": ("Медианная сумма сделки в сумах", "Bitim summasining medianasi (so'mda)"),
    "catalog_companies": ("Компаний в справочнике участников (всего)", "Ishtirokchilar ma'lumotnomasidagi kompaniyalar (jami)"),
    "seller_value_stats": ("Общая сумма продаж по продавцам", "Sotuvchilar bo'yicha jami savdo summasi"),
    "buyer_value_stats": ("Общая сумма покупок по покупателям", "Xaridorlar bo'yicha jami xarid summasi"),
}
# per-company statistics: <role>_value_stats__<stat>
STAT_LABEL = {
    ("seller", "count"): ("Продавцов со сделками", "Bitimi bor sotuvchilar"),
    ("buyer", "count"): ("Покупателей со сделками", "Bitimi bor xaridorlar"),
    ("seller", "max"): ("Наибольшая сумма продаж одного продавца", "Bitta sotuvchining eng katta jami savdosi"),
    ("buyer", "max"): ("Наибольшая сумма покупок одного покупателя", "Bitta xaridorning eng katta jami xaridi"),
    ("seller", "min"): ("Наименьшая сумма продаж одного продавца", "Bitta sotuvchining eng kichik jami savdosi"),
    ("buyer", "min"): ("Наименьшая сумма покупок одного покупателя", "Bitta xaridorning eng kichik jami xaridi"),
    ("seller", "mean"): ("Средняя (арифметическая) сумма на продавца", "Sotuvchi boshiga o'rtacha (arifmetik) summa"),
    ("buyer", "mean"): ("Средняя (арифметическая) сумма на покупателя", "Xaridor boshiga o'rtacha (arifmetik) summa"),
    ("seller", "median"): ("Медианная сумма на продавца", "Sotuvchi boshiga mediana summa"),
    ("buyer", "median"): ("Медианная сумма на покупателя", "Xaridor boshiga mediana summa"),
}
# free-SQL (custom) column aliases chosen by the planner: whole-alias phrases first, then token composition.
ALIAS_PHRASE = {
    "total_companies": ("Всего компаний", "Jami kompaniyalar"), "company_count": ("Количество компаний", "Kompaniyalar soni"),
    "companies": ("Компании", "Kompaniyalar"), "total_deals": ("Всего сделок", "Jami bitimlar"), "deal_count": ("Количество сделок", "Bitimlar soni"),
    "seller_count": ("Количество продавцов", "Sotuvchilar soni"), "buyer_count": ("Количество покупателей", "Xaridorlar soni"),
    "total_value_uzs": ("Общая сумма, сум", "Jami summa, so'm"), "total_amount": ("Общая сумма", "Jami summa"),
}
ALIAS_TOKEN = {
    "total": ("всего", "jami"), "count": ("количество", "soni"), "cnt": ("количество", "soni"), "num": ("количество", "soni"),
    "n": ("количество", "soni"), "companies": ("компаний", "kompaniyalar"), "company": ("компании", "kompaniya"),
    "participants": ("участников", "ishtirokchilar"), "sellers": ("продавцов", "sotuvchilar"), "seller": ("продавца", "sotuvchi"),
    "buyers": ("покупателей", "xaridorlar"), "buyer": ("покупателя", "xaridor"), "deals": ("сделок", "bitimlar"), "deal": ("сделки", "bitim"),
    "max": ("наибольшая", "eng katta"), "maximum": ("наибольшая", "eng katta"), "min": ("наименьшая", "eng kichik"),
    "minimum": ("наименьшая", "eng kichik"), "avg": ("средняя", "o'rtacha"), "mean": ("средняя", "o'rtacha"), "average": ("средняя", "o'rtacha"),
    "median": ("медианная", "mediana"), "sum": ("сумма", "summa"), "value": ("сумма", "summa"), "amount": ("сумма", "summa"),
    "price": ("цена", "narx"), "quantity": ("количество", "miqdor"), "qty": ("количество", "miqdor"), "share": ("доля", "ulush"),
    "pct": ("%", "%"), "uzs": ("в сумах", "so'mda"), "usd": ("в долларах США", "AQSh dollarida"), "year": ("год", "yil"),
    "month": ("месяц", "oy"), "inn": ("ИНН", "STIR"), "name": ("название", "nomi"), "region": ("регион", "hudud"),
    "product": ("товар", "tovar"), "commodity": ("товар", "tovar"), "status": ("статус", "holat"), "date": ("дата", "sana"),
    "id": ("номер", "raqami"), "unit": ("единица", "birlik"), "currency": ("валюта", "valyuta"), "per": ("на", "boshiga"),
}
MONEY_TOKENS = {"sum", "value", "amount", "price", "uzs", "usd"}
COUNT_TOKENS = {"count", "cnt", "num", "n", "companies", "deals", "total"}


def alias_label(name: str, lang: str, idx: int = 0) -> str:
    """Localized label for an internal/SQL alias; never returns the alias itself."""
    key = str(name).strip().lower()
    if key in METRIC_LABEL:
        return _l(METRIC_LABEL[key], lang)
    if key in ALIAS_PHRASE:
        return _l(ALIAS_PHRASE[key], lang)
    toks = [t for t in re.split(r"[^a-z0-9]+", key) if t]
    if toks and all(t in ALIAS_TOKEN for t in toks):
        words = [_l(ALIAS_TOKEN[t], lang) for t in toks]
        out = " ".join(dict.fromkeys(words))
        return out[:1].upper() + out[1:]
    return (f"Показатель {idx + 1}" if lang == "ru" else f"Ko'rsatkich {idx + 1}")


# ---------------------------------------------------------------- free-SQL provenance (meaning from the SQL, not the alias)
_AGG_WORD = {"Max": ("Наибольшая", "Eng katta"), "Min": ("Наименьшая", "Eng kichik"), "Avg": ("Средняя", "O'rtacha"),
             "Median": ("Медианная", "Mediana"), "Sum": ("Итого", "Jami")}
_DISTINCT_NOUN = {"seller_inn": ("Число разных продавцов (по ИНН)", "Turli sotuvchilar soni (STIR bo'yicha)"),
                  "buyer_inn": ("Число разных покупателей (по ИНН)", "Turli xaridorlar soni (STIR bo'yicha)"),
                  "seller_broker_code": ("Число разных брокеров продавцов", "Turli sotuvchi brokerlari soni"),
                  "buyer_broker_code": ("Число разных брокеров покупателей", "Turli xaridor brokerlari soni"),
                  "contract_id": ("Число разных контрактов", "Turli shartnomalar soni"),
                  "commodity_key": ("Число разных товаров", "Turli tovarlar soni")}
_GROUP_COL = {"seller_name": ("Продавец", "Sotuvchi"), "buyer_name": ("Покупатель", "Xaridor"), "seller_inn": ("ИНН продавца", "Sotuvchi STIR"),
              "buyer_inn": ("ИНН покупателя", "Xaridor STIR"), "commodity_name_ru": ("Товар", "Tovar"), "deal_product_name": ("Товар", "Tovar"),
              "warehouse_region": ("Регион склада", "Ombor hududi"), "seller_region": ("Регион продавца", "Sotuvchi hududi"),
              "buyer_region": ("Регион покупателя", "Xaridor hududi"), "deal_year": ("Год", "Yil"), "deal_month": ("Месяц", "Oy"),
              "status": ("Статус", "Holat"), "currency": ("Валюта", "Valyuta"), "quantity_unit": ("Единица", "Birlik"),
              "contract_form": ("Форма контракта", "Shartnoma shakli"), "deal_id": ("Номер сделки", "Bitim raqami")}
_DATE_COL = {"deal_date": ("Дата", "Sana"), "deal_ts": ("Дата", "Sana"), "payment_date": ("Дата оплаты", "To'lov sanasi"),
             "delivery_date": ("Дата поставки", "Yetkazish sanasi"), "payment_due_date": ("Срок оплаты", "To'lov muddati"),
             "delivery_due_date": ("Срок поставки", "Yetkazish muddati")}
_DATE_PARTS = {"deal_year": ("Год", "Yil"), "deal_month": ("Месяц", "Oy")}


def _date_lineage(inner):
    """(ru, uz) label when an output expression is a calendar date derived from one date column: the column itself,
    a CAST(... AS DATE), DATE(...), or date_trunc('day', ...). Anything else -> None (not proven to be a date)."""
    from sqlglot import exp
    node = inner
    for _ in range(3):
        if isinstance(node, exp.Column):
            return _DATE_COL.get(node.name)
        if isinstance(node, (exp.Cast, exp.TryCast)) and node.to.this in (exp.DataType.Type.DATE,):
            node = node.this
            continue
        if isinstance(node, exp.TsOrDsToDate) or type(node).__name__ in ("Date", "TsOrDsToDate"):
            node = node.this
            continue
        if isinstance(node, (exp.DateTrunc, exp.TimestampTrunc)) and str(node.args.get("unit", "")).strip("'").upper() in ("DAY", "'DAY'"):
            node = node.this
            continue
        return None
    return None


def _where_scope(where) -> tuple[str, str] | None:
    """(ru, uz) description of which deals a WHERE clause keeps; None = cannot be stated with certainty."""
    from sqlglot import exp
    if where is None:
        return ("все сделки, включая аннулированные", "barcha bitimlar, bekor qilinganlar bilan")
    cond = where.this
    status_terms = [n for n in cond.find_all(exp.Column) if n.name in ("status_code", "status", "is_cancelled")]
    other = [n for n in cond.find_all(exp.Column) if n.name not in ("status_code", "status", "is_cancelled")]
    from . import schema as _S
    raw_to_code = {k: v[0] for k, v in _S.STATUSES.items()}
    codes: set[str] | None = None
    for n in list(cond.find_all(exp.EQ)) + list(cond.find_all(exp.In)):
        col = next((c.name for c in n.find_all(exp.Column) if c.name in ("status_code", "status")), None)
        if not col or n.find_ancestor(exp.Not):
            continue
        lits = [lit.this for lit in n.find_all(exp.Literal) if lit.is_string]
        got = {raw_to_code.get(v, v) if col == "status" else v for v in lits}
        if got:
            codes = got if codes is None else codes & got
    excl = False
    for n in cond.find_all(exp.NEQ):
        if any(c.name == "status_code" for c in n.find_all(exp.Column)) and "cancelled_unpaid" in n.sql():
            excl = True
    for n in cond.find_all(exp.Not):
        if any(c.name == "is_cancelled" for c in n.find_all(exp.Column)) and not list(n.find_all(exp.In)):
            excl = True
    for n in cond.find_all(exp.EQ):
        if any(c.name == "is_cancelled" for c in n.find_all(exp.Column)) and "FALSE" in n.sql().upper():
            excl = True
    known = {code for code, *_ in _S.STATUSES.values()}
    if codes is not None and codes <= known and codes:
        if codes == {"fulfilled"}:
            base = ("только полностью исполненные сделки", "faqat to'liq bajarilgan bitimlar")
        else:
            names = sorted(codes)
            base = ("сделки со статусами: " + ", ".join(_l(STATUS.get(c, (c, c)), "ru") for c in names),
                    "holatlari: " + ", ".join(_l(STATUS.get(c, (c, c)), "uz") for c in names))
    elif status_terms and not excl:
        return None  # some other status condition: do not guess its scope
    else:
        base = ("без аннулированных сделок", "bekor qilinganlarsiz") if excl else ("все сделки, включая аннулированные",
                                                                                  "barcha bitimlar, bekor qilinganlar bilan")
    date_cols = {"deal_date", "deal_ts", "deal_year", "deal_month", "deal_quarter"}
    rest = [c for c in other if c.name not in date_cols]
    if any(c.name in date_cols for c in other):
        base = (base[0] + "; условие по дате сделки задано в запросе", base[1] + "; bitim sanasi sharti so'rovda berilgan")
    if rest:
        return (base[0] + "; плюс дополнительные условия запроса (см. «Источник»)", base[1] + "; so'rovdagi qo'shimcha shartlar bilan («Manba»)")
    return base


def sql_provenance(sql: str | None, n_cols: int) -> list[dict]:
    """Meaning of each output column derived from the executed SQL. Each item: verified, label(ru, uz), kind, scope(ru, uz).
    Columns whose meaning cannot be proven from simple aggregates over known columns are marked unverified."""
    unverified = [{"verified": False, "label": None, "kind": "other", "scope": None} for _ in range(n_cols)]
    if not sql:
        return unverified
    try:
        import sqlglot
        from sqlglot import exp
        q = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        return unverified
    if not isinstance(q, exp.Select) or q.args.get("joins") or q.args.get("with") or list(q.find_all(exp.Subquery)):
        return unverified
    tables = [t.name for t in q.find_all(exp.Table)]
    if len(tables) != 1 or len(q.expressions) != n_cols:
        return unverified
    table, scope = tables[0], _where_scope(q.args.get("where"))
    out = []
    for e in q.expressions:
        inner = e.this if isinstance(e, exp.Alias) else e
        item = {"verified": False, "label": None, "kind": "other", "scope": None}
        cols = [c.name for c in inner.find_all(exp.Column)]
        if table == "deal_facts" and _date_lineage(inner):
            item.update(verified=True, label=_date_lineage(inner), kind="date")
        elif isinstance(inner, exp.Column) and inner.name in _DATE_PARTS and table == "deal_facts":
            item.update(verified=True, label=_DATE_PARTS[inner.name], kind="text")
        elif isinstance(inner, exp.Column) and inner.name in _GROUP_COL and table == "deal_facts":
            item.update(verified=True, label=_GROUP_COL[inner.name], kind="text")
        elif isinstance(inner, exp.Count) and table == "participants" and not cols:
            item.update(verified=True, label=("Компаний в справочнике участников (всего)", "Ishtirokchilar ma'lumotnomasidagi kompaniyalar (jami)"),
                        kind="count", scope=("справочник участников, без фильтров по сделкам", "ishtirokchilar ma'lumotnomasi, bitim filtrlarisiz")
                        if q.args.get("where") is None else None)
            item["verified"] = item["scope"] is not None
        elif table == "deal_facts" and scope is not None and isinstance(inner, exp.Count):
            arg = inner.this
            if isinstance(arg, exp.Star) or (isinstance(arg, exp.Column) and arg.name == "deal_id"):
                item.update(verified=True, label=("Количество сделок", "Bitimlar soni"), kind="count", scope=scope)
            elif isinstance(arg, exp.Distinct) and len(cols) == 1 and cols[0] in _DISTINCT_NOUN:
                item.update(verified=True, label=_DISTINCT_NOUN[cols[0]], kind="count", scope=scope)
        elif (table == "deal_facts" and scope is not None and type(inner).__name__ in _AGG_WORD and cols == ["amount_uzs"]
              and isinstance(inner.this, exp.Column)):
            w = _AGG_WORD[type(inner).__name__]
            item.update(verified=True, label=(f"{w[0]} сумма сделки в сумах" if w[0] != "Итого" else "Итого сумма сделок в сумах",
                                              f"{w[1]} bitim summasi (so'mda)" if w[1] != "Jami" else "Bitimlar summasi jami (so'mda)"),
                        kind="money", scope=scope)
            if type(inner).__name__ == "Avg":
                item["approx"] = True  # SQL avg() is floating point: shown rounded to 2 dp, exact mean needs the governed metric
        out.append(item)
    return out


def alias_kind(name: str) -> str:
    toks = set(t for t in re.split(r"[^a-z0-9]+", str(name).lower()) if t)
    if toks & MONEY_TOKENS:
        return "money_usd" if "usd" in toks else "money"
    if toks & COUNT_TOKENS:
        return "count"
    return "other"
METHOD = {
    "count": ("подсчёт сделок", "bitimlar soni hisoblandi"),
    "count_if": ("подсчёт сделок, удовлетворяющих условию", "shartga mos bitimlar soni"),
    "sum_money": ("сумма по сделкам", "bitimlar bo'yicha yig'indi"),
    "sum_qty": ("количество = лоты × размер лота, в единицах контракта; разные единицы и разные варианты товара не суммируются",
                "miqdor = lotlar × lot hajmi, shartnoma birligida; turli birliklar va tovar variantlari qo'shilmaydi"),
    "weighted": ("средневзвешенная: сумма сделок ÷ общее количество (Σ сумм / Σ количества)",
                 "o'rtacha tortilgan: bitimlar summasi ÷ umumiy miqdor (Σ summa / Σ miqdor)"),
    "mean": ("простое среднее арифметическое цен отдельных сделок (не взвешенное)",
             "alohida bitim narxlarining oddiy o'rtacha arifmetigi (tortilmagan)"),
    "share": ("доля = аннулированные сделки ÷ все сделки × 100", "ulush = bekor qilingan bitimlar ÷ barcha bitimlar × 100"),
    "minmax": ("минимум/максимум цены за единицу по сделкам", "bitimlar bo'yicha birlik narxining eng kichik/eng katta qiymati"),
    "distinct": ("число различных значений", "turli qiymatlar soni"),
    "mean_value": ("сумма сделок ÷ число сделок", "bitimlar summasi ÷ bitimlar soni"),
    "minmax_value": ("наибольшая/наименьшая сумма одной сделки", "bitta bitimning eng katta/eng kichik summasi"),
    "median_value": ("медиана сумм отдельных сделок (половина сделок меньше, половина больше)",
                     "alohida bitimlar summalarining medianasi (bitimlarning yarmi kichik, yarmi katta)"),
    "catalog": ("все строки справочника участников (participants.csv), без фильтров по сделкам",
                "ishtirokchilar ma'lumotnomasidagi barcha qatorlar (participants.csv), bitim filtrlarsiz"),
    "group_stats": ("сначала сумма сделок по каждой компании (без аннулированных), затем по компаниям: количество, наибольшая, "
                    "наименьшая, среднее арифметическое (Σ ÷ количество) и медиана",
                    "avval har bir kompaniya bo'yicha bitimlar summasi (bekor qilinganlarsiz), so'ng kompaniyalar bo'yicha: soni, "
                    "eng kattasi, eng kichigi, o'rtacha arifmetik (Σ ÷ soni) va mediana"),
    "days_pay": ("среднее (дата оплаты − дата сделки) по сделкам, у которых есть дата оплаты",
                 "to'lov sanasi bor bitimlar bo'yicha (to'lov sanasi − bitim sanasi) o'rtachasi"),
    "days_del": ("среднее (дата поставки − дата оплаты) по сделкам, у которых есть обе даты",
                 "ikkala sanasi bor bitimlar bo'yicha (yetkazish sanasi − to'lov sanasi) o'rtachasi"),
}
POLICY = {
    "registry": ("все зарегистрированные сделки, включая аннулированные", "barcha ro'yxatga olingan bitimlar, bekor qilinganlar bilan"),
    "not_cancelled": ("без аннулированных (неоплаченных) сделок", "bekor qilingan (to'lanmagan) bitimlarsiz"),
    "paid": ("только оплаченные сделки", "faqat to'langan bitimlar"),
    "fulfilled": ("только полностью исполненные сделки", "faqat to'liq bajarilgan bitimlar"),
    "fulfilled_any": ("исполненные полностью или частично", "to'liq yoki qisman bajarilgan bitimlar"),
    "active": ("незавершённые сделки (не аннулированные и не исполненные полностью)", "yakunlanmagan bitimlar"),
    "awaiting_payment_any": ("неоплаченные и не аннулированные сделки", "to'lanmagan va bekor qilinmagan bitimlar"),
    "cancelled": ("только аннулированные сделки", "faqat bekor qilingan bitimlar"),
}
STATUS = {
    "new": ("Новый", "Yangi"), "unsigned": ("Не подписан", "Imzolanmagan"), "concluded": ("Заключен", "Tuzilgan"),
    "awaiting_payment": ("Ждёт оплату", "To'lov kutilmoqda"), "paid": ("Оплачен", "To'langan"),
    "partially_fulfilled": ("Исполнен частично", "Qisman bajarilgan"), "fulfilled": ("Исполнен", "Bajarilgan"),
    "cancelled_unpaid": ("Аннулирован (не оплачен)", "Bekor qilingan (to'lanmagan)"),
}
CODE = {
    "spot": ("спот", "spot"), "forward": ("форвард", "forvard"), "futures": ("фьючерс", "fyuchers"),
    "domestic": ("внутренний", "ichki"), "export": ("экспортный", "eksport"),
    "english_auction": ("английский аукцион", "ingliz auksioni"), "reverse_auction": ("обратный аукцион", "teskari auksion"),
    "double_auction": ("двусторонний аукцион", "ikki tomonlama auksion"), "UZS": ("сум", "so'm"), "USD": ("долл. США", "AQSh dollari"),
}
# Uzbek (Latin) names of the dataset commodities, for Uzbek answers. Russian answers keep the original catalog names.
COMMODITY_UZ = {
    "aluminium_profile": "Alyuminiy profil", "ammonium_nitrate": "Ammiakli selitra", "argon": "Argon", "bearing": "Podshipnik",
    "bitumen": "Bitum", "brick": "G'isht", "building_lime": "Qurilish ohagi", "caustic_soda": "Kaustik soda",
    "cement": "Portlandsement", "compound_feed": "Omuxta yem", "copper_cathode": "Mis katod", "cotton_meal": "Paxta kunjarasi (shrot)",
    "cotton_yarn": "Paxta ipi (kalava)", "cottonseed_oil": "Paxta yog'i", "diesel": "Dizel yoqilg'isi", "drinking_water": "Ichimlik suvi",
    "enamel": "Emal", "feed_mix": "Yem aralashmasi", "flour": "Bug'doy uni", "fuel_oil": "Mazut", "gasoline": "Benzin",
    "glass": "List shisha", "gypsum": "Gips", "lpg": "Suyultirilgan gaz", "lumber": "Yog'och-taxta materiallari", "nitrogen": "Azot",
    "pe_pipe": "Polietilen quvurlar", "polyethylene": "Polietilen", "polypropylene": "Polipropilen", "power_cable": "Kuch kabeli",
    "pvc": "Polivinilxlorid (PVX)", "quicklime": "So'ndirilmagan ohak", "raw_cotton": "Paxta xomashyosi", "rebar": "Armatura",
    "rebar_wire": "Armatura simi", "rice": "Guruch", "salt": "Osh tuzi", "steel_sheet": "Issiq prokatlangan po'lat list",
    "sugar": "Shakar", "sulfuric_acid": "Sulfat kislota", "sunflower_oil": "Kungaboqar yog'i", "urea": "Karbamid", "wheat": "Bug'doy",
}
CONTRACT_FORM_UZ = (("спот", "spot"), ("форвард", "forvard"), ("фьючерс", "fyuchers"))
_CYR_LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k",
            "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ц": "s",
            "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya"}


def cyr_to_lat(s: str) -> str:
    return "".join(_CYR_LAT.get(ch, _CYR_LAT.get(ch.lower(), ch).upper() if ch.isupper() else ch) for ch in s)


def code_lat(code: str) -> str:
    """Product codes (brands, sizes) in Latin capitals: аи92 → AI-92, м100 → M100, 21х30мм → 21x30mm."""
    if re.fullmatch(r"[\d.,х×x]+мм", code):
        return cyr_to_lat(code)
    out = cyr_to_lat(code).upper()
    out = re.sub(r"(\d)MM$", r"\1mm", out)
    return re.sub(r"^AI(\d+)$", r"AI-\1", out)


UNIT = {"тонна": ("т", "tonna"), "килограмм": ("кг", "kg"), "литр": ("л", "litr"), "штука": ("шт.", "dona"),
        "куб.метр": ("м³", "m³"), "кв.метр": ("м²", "m²"), "метр": ("м", "metr")}
FILTER_LABEL = {
    "contract_form": ("форма контракта", "shartnoma shakli"), "contract_type": ("тип контракта", "shartnoma turi"),
    "trade_type": ("тип торгов", "savdo turi"), "status": ("статус сделки", "bitim holati"), "currency": ("валюта", "valyuta"),
    "quantity_unit": ("единица измерения", "o'lchov birligi"), "product_category": ("категория товара", "tovar toifasi"),
    "product_group": ("группа товара", "tovar guruhi"), "industry": ("отрасль", "tarmoq"), "tnved_code": ("код ТН ВЭД", "TN VED kodi"),
    "seller_activity": ("вид деятельности продавца", "sotuvchi faoliyati"), "buyer_activity": ("вид деятельности покупателя", "xaridor faoliyati"),
    "delivery_basis": ("базис поставки", "yetkazish bazisi"), "warehouse_region": ("регион склада", "ombor hududi"),
    "seller_region": ("регион продавца", "sotuvchi hududi"), "buyer_region": ("регион покупателя", "xaridor hududi"),
}
ROLE = {"seller": ("продавец", "sotuvchi"), "buyer": ("покупатель", "xaridor"), "seller_broker": ("брокер продавца", "sotuvchi brokeri"),
        "buyer_broker": ("брокер покупателя", "xaridor brokeri"), "any_broker": ("брокер любой стороны", "istalgan tomon brokeri"),
        "warehouse_region": ("регион склада продавца", "sotuvchi ombori hududi"), "seller_region": ("регион продавца", "sotuvchi hududi"),
        "buyer_region": ("регион покупателя", "xaridor hududi")}
# ru genitive plural after "Топ-N"; uz singular after "N ta"
DIM_COL = {"seller": ("Продавец", "Sotuvchi"), "buyer": ("Покупатель", "Xaridor"), "seller_broker": ("Брокер продавца", "Sotuvchi brokeri"),
           "buyer_broker": ("Брокер покупателя", "Xaridor brokeri"), "any_broker": ("Брокер", "Broker"),
           "warehouse_region": ("Регион склада", "Ombor hududi"), "seller_region": ("Регион продавца", "Sotuvchi hududi"),
           "buyer_region": ("Регион покупателя", "Xaridor hududi"), "commodity": ("Товар", "Tovar"), "product_variant": ("Вариант товара", "Tovar varianti"),
           "status": ("Статус", "Holat"), "year": ("Год", "Yil"), "month": ("Месяц", "Oy"), "quarter": ("Квартал", "Chorak"),
           "contract_form": ("Форма контракта", "Shartnoma shakli"), "currency": ("Валюта", "Valyuta"), "quantity_unit": ("Единица", "Birlik")}
DIM_LABEL = {"seller": ("продавцов", "sotuvchi"), "buyer": ("покупателей", "xaridor"), "seller_broker": ("брокеров продавцов", "sotuvchi brokeri"),
             "buyer_broker": ("брокеров покупателей", "xaridor brokeri"), "any_broker": ("брокеров", "broker")}
MISSING = {"profit": ("прибыль", "foyda"), "cost": ("себестоимость", "tannarx"), "production_cost": ("себестоимость", "tannarx"),
           "margin": ("маржа", "marja"), "bank_details": ("банковские реквизиты", "bank rekvizitlari"),
           "phone": ("телефоны", "telefonlar"), "contacts": ("контакты", "kontaktlar"), "forecast": ("прогноз", "prognoz"),
           "employees": ("сотрудники", "xodimlar")}
T = {
    "period": ("Период", "Davr"), "full_cov": ("весь период данных", "ma'lumotlarning butun davri"),
    "filters": ("Условия", "Shartlar"), "method": ("Метод расчёта", "Hisoblash usuli"), "policy": ("Статусы", "Holatlar"),
    "units": ("Единицы и валюта", "Birliklar va valyuta"), "sources": ("Источник", "Manba"),
    "rows": ("строк в выборке", "tanlovdagi qatorlar"), "query": ("запрос", "so'rov"), "samples": ("примеры сделок", "bitim misollari"),
    "asof": ("Статусы сделок — на конец 30.10.2026; более поздние изменения неизвестны.",
             "Bitim holatlari 30.10.2026 kun oxiriga ko'ra; keyingi o'zgarishlar ma'lum emas."),
    "files": ("файлы", "fayllar"), "inn": ("ИНН", "STIR"), "deals_w": ("сделок", "ta bitim"),
    "usd_conv": ("Суммы {n} сделок в долларах США пересчитаны в сумы по курсу на дату каждой сделки (usd_rates.csv из набора данных).",
                 "{n} ta AQSh dollaridagi bitim summasi har bir bitim sanasidagi kurs bo'yicha so'mga o'girildi (ma'lumotlar to'plamidagi usd_rates.csv)."),
    "no_conv": ("Пересчёта валют не было: все сделки в выборке в сумах.", "Valyuta o'girilmadi: tanlovdagi barcha bitimlar so'mda."),
    "not_profit": ("Сумма сделок — это стоимость контрактов, а не прибыль компаний.", "Bitimlar summasi — shartnomalar qiymati, kompaniyalar foydasi emas."),
    "rounding": ("Денежные значения и цены округлены до 2 знаков только при выводе.", "Pul qiymatlari va narxlar faqat chiqarishda 2 xonagacha yaxlitlandi."),
    "verified_zero": ("Проверено по базе: при этих условиях найдено 0 сделок.", "Baza bo'yicha tekshirildi: ushbu shartlarda 0 ta bitim topildi."),
    "units_sep": ("Количества в разных единицах измерения и для разных вариантов товара показаны отдельно и не суммируются.",
                  "Turli o'lchov birliklaridagi va turli tovar variantlaridagi miqdorlar alohida ko'rsatilgan va qo'shilmaydi."),
    "trunc": ("Показаны первые {shown} из {total}.", "{total} tadan birinchi {shown} tasi ko'rsatilgan."),
    "tie": ("На границе списка есть равные значения: следующая позиция имеет такое же значение.",
            "Ro'yxat chegarasida teng qiymatlar bor: keyingi o'rin ham shunday qiymatga ega."),
    "clipped": ("Запрошенный период выходит за пределы данных; использованы только даты внутри покрытия 03.01.2022–30.10.2026.",
                "So'ralgan davr ma'lumotlar chegarasidan chiqadi; faqat 03.01.2022–30.10.2026 oralig'idagi sanalar ishlatildi."),
    "aligned": ("Последний период неполный (данные до 30.10.2026), поэтому оба периода сравниваются за одинаковый срок от начала периода.",
                "Oxirgi davr to'liq emas (ma'lumotlar 30.10.2026 gacha), shuning uchun ikkala davr boshidan bir xil muddat bo'yicha solishtirildi."),
    "default_policy": ("статусы не уточнены в вопросе — для каждого показателя применено правило по умолчанию",
                       "savolda holatlar ko'rsatilmagan — har bir ko'rsatkich uchun standart qoida qo'llandi"),
}


def _l(pair, lang: str) -> str:
    return pair[0] if lang == "ru" else pair[1]


def _d(s: Any) -> Decimal | None:
    try:
        return Decimal(str(s))
    except (InvalidOperation, ValueError):
        return None


def fmt_num(v: Any, places: int | None = None) -> str:
    """Grouped with NBSP; decimal comma. ``places`` rounds half-up (render-time only)."""
    d = _d(v)
    if d is None:
        return str(v)
    if places is not None:
        d = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    s = format(abs(d), "f")
    ip, _, fp = s.partition(".")
    ip = re.sub(r"(?<=\d)(?=(\d{3})+$)", NB, ip)
    return sign + ip + ("," + fp if fp else "")


def fmt_date(s: Any) -> str:
    try:
        return dt.date.fromisoformat(str(s)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return str(s)


def V(x: Any) -> str:
    """Mark a data value: kept verbatim (no transliteration)."""
    return f"⟦{x}⟧"


@dataclass
class Rendered:
    lang: str
    script: str
    text: str
    sections: dict = field(default_factory=dict)
    grounded: bool = True
    ungrounded: list = field(default_factory=list)
    fallback_used: bool = False
    blocks: dict | None = None  # {"heading", "tables": [{caption, columns:[{label,num}], rows, note}], "notes": [...]}


class Renderer:
    def __init__(self, catalog=None):
        self.c = catalog

    # ------------------------------------------------------------ entry
    def render(self, res, lang: Lang) -> Rendered:
        self.lang = lang.lang
        self.catalog_strings: list[str] = []
        self.tables: list[dict] = []
        try:
            sections = self._sections(res)
        except Exception as e:  # never let rendering hide a result (the exception text stays internal)
            self.tables = []
            sections = {"result": self._fallback(res), "_error": f"{type(e).__name__}: {e}"}
        text = self._join(sections)
        bad = grounding_errors(_strip_marks(text), res, self.catalog_strings)
        fallback = False
        if bad:
            self.tables = []
            sections = {"result": self._fallback(res)}
            text, fallback = self._join(sections), True
            bad = grounding_errors(_strip_marks(text), res, self.catalog_strings)
        conv = (lambda x: latn_to_cyrl(x)) if (lang.lang == "uz" and lang.script == "cyrl") else _strip_marks
        out = conv(text)
        blocks = None
        if res.status == "success" and self.tables and not bad:
            blocks = {"heading": conv(sections.get("result", "")),
                      "tables": [{"caption": conv(t["caption"]) if t.get("caption") else None,
                                  "columns": [{"label": conv(c["label"]), "num": c["num"]} for c in t["columns"]],
                                  "rows": [[conv(str(x)) for x in r] for r in t["rows"]],
                                  "note": conv(t["note"]) if t.get("note") else None} for t in self.tables],
                      "notes": [conv(sections[k]) for k in ("conditions", "method", "sources") if sections.get(k)]}
        return Rendered(lang.lang, lang.script, out, {k: _strip_marks(v) if isinstance(v, str) else v for k, v in sections.items()},
                        not bad, bad, fallback, blocks)

    def _join(self, sections: dict) -> str:
        order = ["result", "table", "conditions", "method", "sources"]
        return "\n\n".join(sections[k] for k in order if sections.get(k))

    def t(self, key: str, **kw) -> str:
        s = _l(T[key], self.lang)
        return s.format(**{k: V(v) for k, v in kw.items()}) if kw else s

    # ------------------------------------------------------------ dispatch
    def _sections(self, res) -> dict:
        st = res.status
        if st == "success":
            body = {"lookup": self._lookup, "list": self._list, "comparison": self._comparison,
                    "custom": self._custom}.get(res.intent, self._aggregate)(res)
            body["conditions"] = self._conditions(res)
            body["method"] = self._method(res)
            body["sources"] = self._sources(res)
            return body
        if st == "ambiguous":
            return {"result": self._clarification(res), "conditions": self._conditions(res, short=True)}
        if st == "no_matching_rows":
            return {"result": self._no_rows(res), "conditions": self._conditions(res), "sources": self._sources(res)}
        if st == "missing_field":
            return {"result": self._missing(res)}
        if st == "out_of_period":
            return {"result": self._out_of_period(res)}
        if st == "unsupported":
            return {"result": self._unsupported(res)}
        return {"result": self._exec_error(res)}

    # ------------------------------------------------------------ values
    def unit_text(self, unit: str | None, value_is_count=False) -> str:
        if not unit or unit in ("deals",):
            return "" if self.lang == "ru" else " ta"
        if unit in ("companies", "variants", "brokers"):
            return "" if self.lang == "ru" else " ta"
        if unit == "days":
            return NB + ("дней" if self.lang == "ru" else "kun")
        if unit == "percent":
            return "%"
        if unit in CODE:
            return NB + _l(CODE[unit], self.lang)
        if unit in UNIT:
            return NB + _l(UNIT[unit], self.lang)
        if "/" in unit:
            cur, u = unit.split("/", 1)
            cur_t = _l(CODE.get(cur, (cur, cur)), self.lang)
            u_t = _l(UNIT.get(u, (u, u)), self.lang)
            return NB + (f"{cur_t} за 1 {u_t}" if self.lang == "ru" else f"{cur_t} / 1 {u_t}")
        return NB + unit

    def val(self, f) -> str:
        if f.value is None:
            return "—"
        spec = METRICS.get(f.name.split("__")[0])
        money = spec is not None and spec.unit in ("UZS", "native_currency", "price", "price_uzs") \
            and f.unit not in ("companies", "deals", "variants", "brokers")
        if f.name.endswith("__change_pct") or (spec and spec.unit == "percent"):
            return V(fmt_num(f.value, 2)) + "%"
        if money:
            return V(fmt_num(f.value, 2)) + self.unit_text(f.unit)
        return V(fmt_num(f.value)) + self.unit_text(f.unit)

    def label(self, metric: str) -> str:
        base, _, stat = metric.partition("__")
        if base in ("seller_value_stats", "buyer_value_stats") and stat:
            return _l(STAT_LABEL[(base.split("_")[0], stat)], self.lang)
        return alias_label(metric, self.lang)

    # ------------------------------------------------------------ tables (one model for web + Telegram)
    def _tbl(self, caption: str | None, cols: list[tuple[str, bool]], rows: list[list[str]], note: str | None = None,
             text: str | None = None) -> str:
        t = {"caption": caption, "columns": [{"label": c, "num": n} for c, n in cols], "rows": rows, "note": note, "text": text}
        self.tables.append(t)
        return self._tbl_text(t)

    @staticmethod
    def _tbl_text(t: dict) -> str:
        """Phone-friendly text form of a table: label/value lines or numbered entries (never space-aligned columns)."""
        cols, L = t["columns"], ([f"{t['caption']}:"] if t.get("caption") else [])
        if t.get("text") == "compare":  # period comparison: "label: p1 → p2 (change %)" — same numbers as the table
            for r in t["rows"]:
                extra = "; ".join(x for x in (r[3], r[4]) if x not in ("—", "", None))
                L.append(f"{r[0]}: {r[1]} → {r[2]}" + (f" ({extra})" if extra else ""))
        elif len(cols) == 2 and cols[1]["label"] in ("Qiymat", "Значение"):
            L += [f"{r[0]}: {r[1]}" for r in t["rows"]]
        else:
            for r in t["rows"]:
                pairs = "; ".join(f"{c['label']}: {v}" for c, v in zip(cols[1:], r[1:]) if v not in ("", None))
                L.append(f"{r[0]} — {pairs}" if pairs else str(r[0]))
        if t.get("note"):
            L.append(t["note"])
        return "\n".join(L)

    def party(self, name, inn, kind="inn") -> str:
        return f"{V(name)} ({self.t('inn')} {V(inn)})" if kind == "inn" else f"{V(name)} ({V(inn)})"

    def commodity_name(self, key: str | None, name_ru: str | None) -> str:
        if self.lang == "uz" and key in COMMODITY_UZ:
            return COMMODITY_UZ[key]
        return V(name_ru)

    def _uz_variant(self, v: dict) -> str:
        """Uzbek label built from the structured catalog fields (form, brand, grade, class, size); no Russian text."""
        ck = v.get("commodity_key")
        base = COMMODITY_UZ.get(ck) or cyr_to_lat(str(v.get("descriptor") or ck)).capitalize()
        desc = str(v.get("descriptor") or "")
        form = next((uz for ru, uz in CONTRACT_FORM_UZ if ru in desc), None)
        attrs = []
        if v.get("brand"):
            attrs.append(code_lat(str(v["brand"])))
        g = str(v.get("grade") or "")
        if g:
            attrs.append("oliy nav" if g == "высший" else f"{g}-nav" if g.isdigit() else code_lat(g))
        c = str(v.get("class") or "")
        if c:
            attrs.append(f"{c}-sinf" if c.isdigit() else code_lat(c))
        if v.get("size"):
            attrs.append(code_lat(str(v["size"])))
        return base + (f" ({form})" if form else "") + (", " + ", ".join(attrs) if attrs else "")

    def _uz_variant_labels(self) -> dict[str, str]:
        """Labels for every variant; variants that differ only in the contract wording get that wording (in Latin)."""
        cache = getattr(self.c, "_uz_variant_labels", None)
        if cache is None:
            raw = {k: self._uz_variant(v) for k, v in self.c.variants.items()}
            unit = lambda k: self.c.variants[k].get("unit")  # the unit is shown next to the label, so it disambiguates too
            seen: dict[tuple, int] = {}
            for k, lb in raw.items():
                seen[(lb, unit(k))] = seen.get((lb, unit(k)), 0) + 1
            # variants that differ only in the contract wording: numbered by deal count (most deals first)
            order = sorted(raw, key=lambda k: (-(self.c.variants[k].get("deals") or 0), k))
            nth: dict[tuple, int] = {}
            cache = {}
            for k in order:
                key = (raw[k], unit(k))
                if seen[key] == 1:
                    cache[k] = raw[k]
                else:
                    nth[key] = nth.get(key, 0) + 1
                    cache[k] = f"{raw[k]} ({nth[key]}-variant)"
            try:
                self.c._uz_variant_labels = cache
            except Exception:
                pass
        return cache

    def variant_name(self, key: str, with_unit: bool = True) -> str:
        v = (self.c.variants.get(key) if self.c else None) or {}
        if self.lang == "uz" and v:
            name = self._uz_variant_labels().get(key) or self._uz_variant(v)
        else:
            name = v.get("display") or (v.get("examples") or [key])[0]
        self.catalog_strings.append(name)
        unit = v.get("unit")
        shown = name if (self.lang == "uz" and v) else V(name)
        return shown + (f" [{_l(UNIT.get(unit, (unit, unit)), self.lang)}]" if with_unit and unit else "")

    def brand_name(self, b: str) -> str:
        orig = (self.c.brand_original.get(b) if self.c is not None and hasattr(self.c, "brand_original") else None) or b
        self.catalog_strings.append(orig)
        return V(orig)

    def region_name(self, key: str) -> str:
        r = (self.c.regions.get(key) if self.c else None) or {}
        name = r.get("ru" if self.lang == "ru" else "uz", key)
        self.catalog_strings.append(name)
        return V(name)

    def group_label(self, g: dict) -> str:
        parts = []
        if "period_label" in g:
            return ""
        if "seller_inn" in g:
            parts.append(self.party(g.get("seller_name"), g["seller_inn"]))
        if "buyer_inn" in g:
            parts.append(self.party(g.get("buyer_name"), g["buyer_inn"]))
        for code_k, name_k in (("broker_code", "broker_name"), ("seller_broker_code", "seller_broker_name"),
                               ("buyer_broker_code", "buyer_broker_name")):
            if code_k in g:
                parts.append(f"{V(g.get(name_k))} ({V(g[code_k])})")
        for k in ("warehouse_region_key", "seller_region_key", "buyer_region_key"):
            if k in g:
                parts.append(self.region_name(g[k]))
        if "product_variant_key" in g:
            parts.append(self.variant_name(g["product_variant_key"]))
        elif "commodity_key" in g:
            parts.append(self.commodity_name(g.get("commodity_key"), g.get("commodity_name_ru")))
        if "status_code" in g:
            parts.append(_l(STATUS.get(g["status_code"], (g["status_code"],) * 2), self.lang))
        for k in ("contract_form_code", "contract_type_code", "trade_type_code"):
            if k in g:
                parts.append(_l(CODE.get(g[k], (g[k],) * 2), self.lang))
        if "deal_month" in g:
            parts.append(V(f"{int(g['deal_month']):02d}.{g['deal_year']}"))
        elif "deal_quarter" in g:
            parts.append(V(f"Q{g['deal_quarter']} {g['deal_year']}"))
        elif "deal_year" in g:
            parts.append(V(g["deal_year"]))
        for k in ("product_category", "product_group", "industry", "tnved_code", "deal_product_name", "seller_activity",
                  "buyer_activity", "delivery_basis"):
            if k in g:
                parts.append(V(g[k]))
        if "currency" in g and not any(k in g for k in ("product_variant_key",)) and len(parts) == 0:
            parts.append(_l(CODE.get(g["currency"], (g["currency"],) * 2), self.lang))
        return ", ".join(p for p in parts if p) or "—"

    # ------------------------------------------------------------ success bodies
    def _rows(self, res):
        rows: dict[int, list] = {}
        for f in res.facts:
            if f.row_index is not None and "__change" not in f.name and not f.name.endswith("__unit_total"):
                rows.setdefault(f.row_index, []).append(f)
        return [rows[k] for k in sorted(rows)]

    def _aggregate(self, res) -> dict:
        rows = self._rows(res)
        ev = res.evidence
        out = {}
        if res.verified_zero:
            out["result"] = "\n".join([f"{self.label(f.name)}: {V(0)}{self.unit_text(f.unit)}." for f in res.facts]
                                      + [self.t("verified_zero")])
            return out
        has_groups = any(f.group for f in res.facts)
        if not has_groups:
            facts = rows[0] if rows else []
            if len(facts) == 1:
                out["result"] = f"{self.label(facts[0].name)}: {self.val(facts[0])}."
                return out
            out["result"] = "Результат:" if self.lang == "ru" else "Natija:"
            scopes = [self._scope_of(res, f.name) for f in facts]
            if len(set(scopes)) > 1:  # different rows cover different deals: say so on every row
                out["table"] = self._tbl(None, [(self._col_metric(), False), (self._col_value(), True), (self._col_scope(), False)],
                                         [[self.label(f.name), self.val(f), sc] for f, sc in zip(facts, scopes)])
            else:
                out["table"] = self._tbl(None, [(self._col_metric(), False), (self._col_value(), True)],
                                         [[self.label(f.name), self.val(f)] for f in facts],
                                         f"{self._col_scope()}: {scopes[0]}" if scopes and scopes[0] != "—" else None)
            return out
        plan = res.plan
        dims = plan.group_by if plan else []
        ranking = res.intent == "ranking" or (plan and plan.top_n)
        m0 = rows[0][0].name if rows else ""
        if ranking and dims:
            who = _l(DIM_LABEL.get(dims[0], (dims[0], dims[0])), self.lang)
            head = (f"Топ-{V(len(rows))} {who} по показателю «{self.label(m0)}»:" if self.lang == "ru"
                    else f"«{self.label(m0)}» bo'yicha eng yirik {V(len(rows))} ta {who}:")
        elif dims:
            head = "Разбивка результата:" if self.lang == "ru" else "Natija taqsimoti:"
        else:  # only automatic grouping (units / currencies / product variants)
            head = ("Результат по единицам измерения и вариантам товара:" if self.lang == "ru"
                    else "Natija o'lchov birliklari va tovar variantlari bo'yicha:")
        lines = [head]
        mnames = list(dict.fromkeys(f.name for fs in rows for f in fs))
        who_col = _l(DIM_COL.get(dims[0], ("Группа", "Guruh")), self.lang) if dims else ("Вариант" if self.lang == "ru" else "Variant")
        trows = []
        for i, fs in enumerate(rows, 1):
            by = {f.name: f for f in fs}
            trows.append([f"{V(i)}. {self.group_label(fs[0].group)}"] + [self.val(by[m]) if m in by else "—" for m in mnames])
        lines.append(self._tbl(None, [(f"№ · {who_col}", False)] + [(self.label(m), True) for m in mnames], trows))
        if ev.truncation:
            total = ev.truncation.get("total_groups")
            if total:
                lines.append(self.t("trunc", shown=ev.truncation.get("shown"), total=total))
            if ev.truncation.get("tie_at_cutoff"):
                lines.append(self.t("tie"))
        subs = [f for f in res.facts if f.name.endswith("__unit_total")]
        qty = [f for f in subs if METRICS[f.name[:-12]].unit == "native_unit"]
        other = [f for f in subs if f not in qty]
        if qty:
            lines.append(("Итого по единицам измерения (все варианты товара; разные единицы не складываются): " if self.lang == "ru"
                          else "O'lchov birliklari bo'yicha jami (tovarning barcha variantlari; turli birliklar qo'shilmaydi): ")
                         + "; ".join(self.val(type(f)(**{**f.model_dump(), "name": f.name[:-12]})) for f in qty))
        if other:
            lines.append(("По всем вариантам товара вместе, отдельно для каждой единицы измерения: " if self.lang == "ru"
                          else "Tovarning barcha variantlari birgalikda, har bir o'lchov birligi uchun alohida: ")
                         + "; ".join(f"{self.label(f.name[:-12])}: " + self.val(type(f)(**{**f.model_dump(), "name": f.name[:-12]})) for f in other))
        out["result"] = lines[0]
        out["table"] = "\n".join(lines[1:])
        return out

    def _comparison(self, res) -> dict:
        ev = res.evidence
        ru = self.lang == "ru"
        per = ev.compare_periods or {}
        p = {k: f"{V(fmt_date(v['effective'][0]))}–{V(fmt_date(v['effective'][1]))}" for k, v in per.items()}
        strip = lambda g: {k: v for k, v in g.items() if k != "period_label"}

        def cells(f):
            """p1 value, p2 value, change, change % for one p1 fact (same metric, same non-period group)."""
            other = strip(f.group)
            f2 = next((x for x in res.facts if x.name == f.name and x.group.get("period_label") == "p2"
                       and strip(x.group) == other), None)
            ch = next((x for x in res.facts if x.name == f"{f.name}__change" and x.group == other), None)
            pct = next((x for x in res.facts if x.name == f"{f.name}__change_pct" and x.group == other), None)
            chg_s = pct_s = "—"
            if ch is not None:
                sign = "+" if Decimal(str(ch.value)) > 0 else ""
                chg_s = f"{sign}{self.val(type(ch)(**{**ch.model_dump(), 'name': f.name}))}"
            if pct is not None:
                pct_s = f"{'+' if Decimal(str(pct.value)) > 0 else ''}{V(fmt_num(pct.value, 2))}%"
            return [self.val(f), self.val(f2) if f2 else "—", chg_s, pct_s]

        base = [f for f in res.facts if "__change" not in f.name and f.group.get("period_label") == "p1"]
        totals = [f for f in base if f.name.endswith("__unit_total")]
        regular = [f for f in base if not f.name.endswith("__unit_total")]
        metric_of = lambda f: f.name[:-len("__unit_total")] if f.name.endswith("__unit_total") else f.name
        metrics = list(dict.fromkeys(metric_of(f) for f in regular + totals))
        val_cols = [(p.get("p1", "p1"), True), (p.get("p2", "p2"), True),
                    ("Изменение" if ru else "O'zgarish", True), ("Изменение, %" if ru else "O'zgarish, %", True)]
        grouped = any(strip(f.group) for f in regular)

        def total_label(f):
            name = f.group.get("commodity_name_ru")
            what = self.commodity_name(f.group.get("commodity_key"), name) if name else ("товар" if ru else "tovar")
            return f"Итого: {what} (все варианты)" if ru else f"Jami: {what} (barcha variantlar)"

        tables = []
        if not grouped:  # one row per metric
            tables.append(self._tbl(None, [(self._col_metric(), False)] + val_cols,
                                    [[self.label(f.name)] + cells(f) for f in regular], text="compare"))
        else:  # one compact table per metric: rows are the groups (e.g. product variants), the all-variant total first
            dims = res.plan.group_by if res.plan else []
            who = _l(DIM_COL.get(dims[0], ("Группа", "Guruh")), self.lang) if dims else ("Вариант" if ru else "Variant")
            for m in metrics:
                rows = [[total_label(f)] + cells(f) for f in totals if metric_of(f) == m]
                rows += [[self.group_label(strip(f.group))] + cells(f) for f in regular if f.name == m]
                if rows:
                    cap = self.label(m)
                    suf = _common_suffix(rows)
                    if suf:
                        rows = [[r[0]] + [c[:-len(suf)] if c != "—" else c for c in r[1:4]] + r[4:] for r in rows]
                        cap = f"{cap}, {suf.strip()}"
                    tables.append(self._tbl(cap, [(who, False)] + val_cols, rows, text="compare"))

        head = "Сравнение периодов" if ru else "Davrlarni solishtirish"
        if p.get("p1") and p.get("p2"):
            head += f" ({p['p1']} → {p['p2']})"
        lines = [head + ":"]
        for f in totals:  # the direct answer for a whole commodity: all variants together, per unit
            c = cells(f)
            lines.append(f"• {total_label(f)} — {self.label(metric_of(f))}: {c[0]} → {c[1]}"
                         + (f" ({c[3]})" if c[3] != "—" else ""))
        notes = []
        if "compare_periods_aligned_to_equivalent_span" in ev.notes:
            notes.append(self.t("aligned"))
        return {"result": "\n".join(lines), "table": "\n\n".join(tables) + ("\n" + "\n".join(notes) if notes else "")}

    def _list(self, res) -> dict:
        tbl = res.table or {}
        cols = tbl.get("columns", [])
        ci = {c: i for i, c in enumerate(cols)}
        total = next((f.value for f in res.facts if f.name == "total_rows"), None)
        rows = tbl.get("rows", [])
        head = (f"Найдено сделок: {V(fmt_num(total))}; показаны {V(len(rows))}:" if self.lang == "ru"
                else f"Topilgan bitimlar: {V(fmt_num(total))}; ko'rsatilgan: {V(len(rows))}:")
        ru = self.lang == "ru"
        trows = []
        for r in rows:
            g = lambda c: r[ci[c]]  # noqa: E731
            amt = f"{V(fmt_num(g('raw_amount'), 2))}{self.unit_text(g('currency'))}" + (
                f" (= {V(fmt_num(g('amount_uzs'), 2))}{self.unit_text('UZS')})" if g("currency") != "UZS" else "")
            trows.append([f"№{V(g('deal_id'))}", V(fmt_date(g('deal_date'))), V(g('deal_product_name')),
                          f"{V(fmt_num(g('quantity_native')))}{self.unit_text(g('quantity_unit'))}", amt,
                          f"{self.party(g('seller_name'), g('seller_inn'))} → {self.party(g('buyer_name'), g('buyer_inn'))}"])
        note = self.t("trunc", shown=len(rows), total=fmt_num(total)) if tbl.get("truncated") and total else None
        cols = [("Сделка" if ru else "Bitim", False), ("Дата" if ru else "Sana", False), ("Товар" if ru else "Tovar", False),
                ("Количество" if ru else "Miqdor", True), ("Сумма" if ru else "Summa", True),
                ("Продавец → покупатель" if ru else "Sotuvchi → xaridor", False)]
        return {"result": head, "table": self._tbl(None, cols, trows, note)}

    def _lookup(self, res) -> dict:
        fs = {f.name: f.value for f in res.facts}
        purposes = [t["purpose"] for t in (res.table or {}).get("lookups", [])]
        ru = self.lang == "ru"
        if "lookup_deal" in purposes:
            unit = fs.get("quantity_unit")
            cur = fs.get("currency")
            L = []
            st_uz = _l(STATUS.get(fs.get("status_code"), ("", "")), "uz")
            L.append((f"Сделка №{V(fs['deal_id'])} от {V(fmt_date(fs['deal_date']))}, статус: {V(fs['status'])}"
                      if ru else f"№{V(fs['deal_id'])} bitim, {V(fmt_date(fs['deal_date']))}, holati: {st_uz} ({V(fs['status'])})"))
            L.append(f"{'Товар' if ru else 'Tovar'}: {V(fs['deal_product_name'])}")
            L.append((f"Куплено: {V(fs['lot_count'])} лот(ов) × {V(fmt_num(fs['lot_size']))} = {V(fmt_num(fs['quantity_native']))}{self.unit_text(unit)}"
                      if ru else f"Sotib olingan: {V(fs['lot_count'])} lot × {V(fmt_num(fs['lot_size']))} = {V(fmt_num(fs['quantity_native']))}{self.unit_text(unit)}"))
            L.append(f"{'Сумма сделки' if ru else 'Bitim summasi'}: {V(fmt_num(fs['raw_amount'], 2))}{self.unit_text(cur)}")
            if cur == "USD":
                L.append((f"В сумах: {V(fmt_num(fs['amount_uzs'], 2))}{self.unit_text('UZS')} (курс на дату сделки {V(fmt_num(fs['fx_rate_applied']))} сум за 1 USD)"
                          if ru else f"So'mda: {V(fmt_num(fs['amount_uzs'], 2))}{self.unit_text('UZS')} (bitim sanasidagi kurs: 1 USD = {V(fmt_num(fs['fx_rate_applied']))} so'm)"))
            L.append(f"{'Цена за единицу' if ru else 'Birlik narxi'}: {V(fmt_num(fs['unit_price'], 2))}{self.unit_text(f'{cur}/{unit}')}"
                     + (" (сумма ÷ количество)" if ru else " (summa ÷ miqdor)"))
            if fs.get("delivered_quantity") is not None:
                L.append((f"Поставлено: {V(fmt_num(fs['delivered_quantity']))}{self.unit_text(unit)} ({V(fs['delivered_lots'])} лот(ов))"
                          if ru else f"Yetkazilgan: {V(fmt_num(fs['delivered_quantity']))}{self.unit_text(unit)} ({V(fs['delivered_lots'])} lot)"))
                if fs.get("remaining_quantity"):
                    L.append((f"Не поставлено: {V(fmt_num(fs['remaining_quantity']))}{self.unit_text(unit)}"
                              if ru else f"Yetkazilmagan: {V(fmt_num(fs['remaining_quantity']))}{self.unit_text(unit)}"))
            pay = fs.get("payment_date")
            L.append((f"Оплата: {V(fmt_date(pay)) if pay else 'не оплачено'} (срок {V(fmt_date(fs['payment_due_date']))})"
                      if ru else f"To'lov: {V(fmt_date(pay)) if pay else 'to‘lanmagan'} (muddat {V(fmt_date(fs['payment_due_date']))})"))
            if fs.get("delivery_due_date"):
                dd = fs.get("delivery_date")
                L.append((f"Поставка: {V(fmt_date(dd)) if dd else 'нет'} (срок {V(fmt_date(fs['delivery_due_date']))})"
                          if ru else f"Yetkazish: {V(fmt_date(dd)) if dd else 'yo‘q'} (muddat {V(fmt_date(fs['delivery_due_date']))})"))
            if fs.get("is_delivery_overdue"):
                L.append("Поставка оставшегося количества просрочена на 30.10.2026." if ru
                         else "Qolgan miqdorni yetkazish muddati 30.10.2026 holatiga o'tgan.")
            if fs.get("is_payment_overdue"):
                L.append("Оплата просрочена на 30.10.2026." if ru else "To'lov muddati 30.10.2026 holatiga o'tgan.")
            L.append(f"{'Продавец' if ru else 'Sotuvchi'}: {self.party(fs['seller_name'], fs['seller_inn'])}, "
                     f"{'брокер' if ru else 'broker'} {V(fs['seller_broker_name'])} ({V(fs['seller_broker_code'])})")
            L.append(f"{'Покупатель' if ru else 'Xaridor'}: {self.party(fs['buyer_name'], fs['buyer_inn'])}, "
                     f"{'брокер' if ru else 'broker'} {V(fs['buyer_broker_name'])} ({V(fs['buyer_broker_code'])})")
            L.append(f"{'Контракт' if ru else 'Shartnoma'}: №{V(fs['contract_id'])}, {V(fs['contract_form'])}, {V(fs['contract_type'])}")
            rows = [ln.split(": ", 1) for ln in L[1:] if ": " in ln]
            notes = [ln for ln in L[1:] if ": " not in ln]
            return {"result": L[0], "table": self._tbl(None, [("Поле" if ru else "Maydon", False), (self._col_value(), False)], rows,
                                                       "\n".join(notes) or None)}
        L = []
        if "lookup_participant" in purposes:
            L.append(f"{self.party(fs['name'], fs['inn'])}: {self.region_name(fs['region_key'])}, {V(fs['activity'])}, {V(fs['role'])}")
        elif "lookup_broker" in purposes:
            L.append(f"{V(fs['name'])} ({V(fs['broker_code'])}, {self.t('inn')} {V(fs['inn'])}): {self.region_name(fs['region_key'])}")
        elif "lookup_contract" in purposes:
            L.append((f"Контракт №{V(fs['contract_id'])}: {V(fs['product_name'])}, {V(fs['currency_name'])}, лот {V(fmt_num(fs['lot_size']))} {V(fs['quantity_unit'])}, "
                      f"срок оплаты {V(fs['payment_term_days'])} дн., поставки {V(fs['delivery_term_days'])} дн."
                      if ru else f"№{V(fs['contract_id'])} shartnoma: {V(fs['product_name'])}, {V(fs['currency_name'])}, lot {V(fmt_num(fs['lot_size']))} {V(fs['quantity_unit'])}, "
                      f"to'lov muddati {V(fs['payment_term_days'])} kun, yetkazish {V(fs['delivery_term_days'])} kun"))
        if "deal_count" in fs:
            L.append((f"Сделок: {V(fmt_num(fs['deal_count']))} (без аннулированных: {V(fmt_num(fs['not_cancelled_count']))}); "
                      f"сумма без аннулированных: {V(fmt_num(fs['trade_value_uzs'], 2)) if fs.get('trade_value_uzs') else '—'}{self.unit_text('UZS')}; "
                      f"первая/последняя сделка: {V(fmt_date(fs['first_deal_date']))} / {V(fmt_date(fs['last_deal_date']))}"
                      if ru else f"Bitimlar: {V(fmt_num(fs['deal_count']))} ta (bekor qilinganlarsiz: {V(fmt_num(fs['not_cancelled_count']))}); "
                      f"bekor qilinganlarsiz summa: {V(fmt_num(fs['trade_value_uzs'], 2)) if fs.get('trade_value_uzs') else '—'}{self.unit_text('UZS')}; "
                      f"birinchi/oxirgi bitim: {V(fmt_date(fs['first_deal_date']))} / {V(fmt_date(fs['last_deal_date']))}"))
        return {"result": L[0] if L else "—", "table": "\n".join(L[1:])}

    def _cell(self, alias: str, v) -> str:
        if v is None:
            return "—"
        if isinstance(v, bool):
            return ("да" if v else "нет") if self.lang == "ru" else ("ha" if v else "yo'q")
        kind = alias_kind(alias)
        d = _d(v) if isinstance(v, (int, float, Decimal)) or (isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v)) else None
        if d is None:
            return V(v)
        if kind == "money":
            return V(fmt_num(v, 2)) + self.unit_text("UZS")
        if kind == "money_usd":
            return V(fmt_num(v, 2)) + self.unit_text("USD")
        if d == d.to_integral_value():
            return V(fmt_num(int(d)))
        return V(fmt_num(v, 2))

    def _custom_sql(self, res) -> str | None:
        ok = [q for q in res.evidence.queries if q.purpose == "custom" and not q.error]
        return ok[-1].sql if ok else (res.plan.custom_sql if res.plan else None)

    def _custom(self, res) -> dict:
        tbl = res.table or {}
        cols = tbl.get("columns", [])
        rows = tbl.get("rows", [])[:20]
        ru = self.lang == "ru"
        prov = sql_provenance(self._custom_sql(res), len(cols))
        idx_l = lambda i: (f"Непроверенный столбец {i + 1}" if ru else f"Tasdiqlanmagan ustun {i + 1}")  # noqa: E731
        labels = [_l(pv["label"], self.lang) if pv["verified"] else idx_l(i) for i, pv in enumerate(prov)]
        kinds = [pv["kind"] if pv["verified"] else "other" for pv in prov]
        notes = []
        bad = [i for i, pv in enumerate(prov) if not pv["verified"]]
        if bad:
            names = ", ".join(str(i + 1) for i in bad)
            notes.append(f"Ограничение: смысл столбцов {names} не удалось подтвердить по самому запросу, поэтому им не присвоено "
                         f"название; выражение расчёта — в разделе «Источник». Задайте вопрос точнее, если нужен этот показатель."
                         if ru else f"Cheklov: {names}-ustun(lar) ma'nosini so'rovning o'zidan tasdiqlab bo'lmadi, shuning uchun nom berilmadi; "
                         f"hisoblash ifodasi «Manba» bo'limida. Bu ko'rsatkich kerak bo'lsa, savolni aniqroq bering.")
        if any(pv.get("approx") for pv in prov):
            notes.append("Среднее посчитано функцией avg() в запросе (с плавающей точкой) и округлено до 2 знаков при выводе."
                         if ru else "O'rtacha so'rovdagi avg() funksiyasi bilan (suzuvchi nuqtali) hisoblangan va chiqarishda 2 xonagacha yaxlitlangan.")
        if len(tbl.get("rows", [])) > len(rows):
            notes.append(self.t("trunc", shown=len(rows), total=len(tbl.get("rows", []))))
        head = "Результат запроса:" if ru else "So'rov natijasi:"
        if bad:
            head = ("Результат запроса (часть столбцов не подтверждена — см. ограничение ниже):" if ru
                    else "So'rov natijasi (ba'zi ustunlar tasdiqlanmagan — quyidagi cheklovga qarang):")
        scopes = [_l(pv["scope"], self.lang) if pv.get("scope") else "—" for pv in prov]
        if len(rows) == 1:  # one row: a vertical table; scope per row when rows cover different deals
            data = [[lb, self._cell_k(k, v), sc] for lb, k, v, sc in zip(labels, kinds, rows[0], scopes)]
            if len({sc for sc in scopes if sc != "—"}) > 1:
                table = self._tbl(None, [(self._col_metric(), False), (self._col_value(), True), (self._col_scope(), False)], data,
                                  "\n".join(notes) or None)
            else:
                one = next((sc for sc in scopes if sc != "—"), None)
                if one:
                    notes.insert(0, f"{self._col_scope()}: {one}")
                table = self._tbl(None, [(self._col_metric(), False), (self._col_value(), True)], [d[:2] for d in data], "\n".join(notes) or None)
        else:
            one = sorted({sc for sc in scopes if sc != "—"})
            if one:
                notes.insert(0, f"{self._col_scope()}: {'; '.join(one)}")
            table = self._tbl(None, [(lb, k in ("money", "count", "other")) for lb, k in zip(labels, kinds)],
                              [[self._cell_k(k, v) for k, v in zip(kinds, r)] for r in rows], "\n".join(notes) or None)
        return {"result": head, "table": table}

    def _cell_k(self, kind: str, v) -> str:
        """Format by the PROVEN kind (money/count/text); unproven numbers are shown as plain numbers, never as money."""
        if v is None:
            return "—"
        if kind == "date":
            return V(fmt_date(str(v)))
        if kind == "text":
            return V(v)
        return self._cell({"money": "x_value_uzs", "count": "x_count"}.get(kind, "x"), v)

    def _scope_of(self, res, fact_name: str) -> str:
        """Which deals a row covers, from the status policy the engine actually applied (evidence notes)."""
        base = fact_name.split("__")[0]
        ru = self.lang == "ru"
        if METRICS.get(base) and METRICS[base].kind == "catalog":
            return "справочник участников, без фильтров по сделкам" if ru else "ishtirokchilar ma'lumotnomasi, bitim filtrlarisiz"
        pol = next((n.split("=", 1)[1] for n in res.evidence.notes if n.startswith(f"policy[{base}]=")), None)
        codes: set[str] = set()
        for flt in res.evidence.filters or []:  # an explicit status filter narrows every row further than the policy
            if flt.get("field") == "status" and flt.get("op") in ("eq", "in"):
                v = flt.get("value")
                codes |= set(v if isinstance(v, list) else [v])
        if codes:
            if codes == {"fulfilled"}:
                return "только полностью исполненные сделки" if ru else "faqat to'liq bajarilgan bitimlar"
            names = ", ".join(_l(STATUS.get(c, (c, c)), self.lang) for c in sorted(codes))
            return f"сделки со статусами: {names}" if ru else f"holatlari: {names}"
        if pol == "registry":
            return "все сделки, включая аннулированные" if ru else "barcha bitimlar, bekor qilinganlar bilan"
        if pol == "not_cancelled":
            return "без аннулированных сделок" if ru else "bekor qilinganlarsiz"
        if pol:
            return _l(POLICY.get(pol, (pol, pol)), self.lang)
        return "—"

    def _col_scope(self) -> str:
        return "Охват" if self.lang == "ru" else "Qamrov"

    def _col_metric(self) -> str:
        return "Показатель" if self.lang == "ru" else "Ko'rsatkich"

    def _col_value(self) -> str:
        return "Значение" if self.lang == "ru" else "Qiymat"

    # ------------------------------------------------------------ conditions / method / sources
    def _period_line(self, ev) -> str:
        if ev.compare_periods:
            return ""
        pe = ev.period_effective or {}
        if pe.get("note"):
            return f"{self.t('period')}: {self.t('full_cov')} {V(fmt_date(pe['start']))}–{V(fmt_date(pe['end']))}"
        if pe:
            return f"{self.t('period')}: {V(fmt_date(pe['start']))}–{V(fmt_date(pe['end']))}"
        pr = ev.period_requested or {}
        if pr:
            return f"{self.t('period')}: {V(fmt_date(pr.get('start')))}–{V(fmt_date(pr.get('end')))}"
        return ""

    def _conditions(self, res, short: bool = False) -> str:
        ev = res.evidence
        L = []
        pl = self._period_line(ev)
        if pl:
            L.append(pl)
        if "period_clipped_to_coverage" in ev.notes and not ev.compare_periods:
            L.append(self.t("clipped"))
        for n in ev.normalization:
            if n["status"] != "resolved":
                continue
            m = (n.get("matched") or [{}])[0]
            role = _l(ROLE[n["role"]], self.lang) if n.get("role") in ROLE else ""
            if n["kind"] == "participant":
                L.append(f"{role or ('компания' if self.lang == 'ru' else 'kompaniya')}: {self.party(m.get('name'), m.get('inn'))}")
            elif n["kind"] == "broker":
                L.append(f"{role}: {V(m.get('name'))} ({V(m.get('broker_code'))})")
            elif n["kind"].startswith("region"):
                L.append(f"{role or _l(ROLE['warehouse_region'], self.lang)}: {self.region_name(m.get('region_key'))}")
            elif n["kind"] == "product":
                if self.lang == "uz":
                    g, c = str(m.get("grade") or ""), str(m.get("class") or "")
                    attrs = ([code_lat(str(m["brand"]))] if m.get("brand") else []) \
                        + (["oliy nav" if g == "высший" else f"{g}-nav" if g.isdigit() else code_lat(g)] if g else []) \
                        + ([f"{c}-sinf" if c.isdigit() else code_lat(c)] if c else []) \
                        + ([code_lat(str(m["size"]))] if m.get("size") else [])
                    self.catalog_strings.extend(attrs)
                else:
                    attrs = ([self.brand_name(m["brand"])] if m.get("brand") else []) + [V(m[k]) for k in ("grade", "class", "size") if m.get(k)]
                nv = n.get("variants")
                L.append(f"{'товар' if self.lang == 'ru' else 'tovar'}: {self.commodity_name(m.get('commodity_key'), m.get('name_ru'))}" + (f" ({', '.join(attrs)})" if attrs else "")
                         + (f"; {'вариантов в данных' if self.lang == 'ru' else 'variantlar'}: {V(nv)}" if nv else ""))
            elif n["kind"] in ("deal", "contract"):
                kind = {"deal": ("сделка", "bitim"), "contract": ("контракт", "shartnoma")}[n["kind"]]
                L.append(f"{_l(kind, self.lang)} №{V(n['ids'][0])}")
        for f in ev.filters:
            if f["field"] in ("warehouse_region", "seller_region", "buyer_region"):
                continue  # shown via normalization
            lbl = _l(FILTER_LABEL.get(f["field"], (f["field"], f["field"])), self.lang)
            vals = f["value"] if isinstance(f["value"], list) else [f["value"]]
            shown = [_l(STATUS[v], self.lang) if v in STATUS else _l(CODE[v], self.lang) if v in CODE else V(v) for v in vals]
            op = {"eq": "=", "in": "∈", "neq": "≠", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "between": "между" if self.lang == "ru" else "oralig'ida",
                  "is_true": "", "is_false": "нет" if self.lang == "ru" else "yo'q"}.get(f["op"], f["op"])
            L.append(f"{lbl} {op} {', '.join(shown)}".replace("  ", " "))
        if short:
            return ("\n".join(L)) if L else ""
        if res.status == "success" and res.intent in ("count", "aggregate", "ranking", "comparison"):
            pols = {}
            for k, codes in ev.status_policy.items():
                name = next((p for p, cs in STATUS_POLICIES.items() if list(cs) == codes), None)
                pols.setdefault(name, []).append(k)
            for name, ms in pols.items():
                if name in POLICY:
                    L.append(f"{self.t('policy')} ({', '.join(self.label(m) for m in ms)}): {_l(POLICY[name], self.lang)}")
            if res.plan and res.plan.status_policy is None:
                L.append(f"({self.t('default_policy')})")
        if res.status == "success" and res.intent == "list" and ev.status_policy.get("list"):
            name = next((p for p, cs in STATUS_POLICIES.items() if list(cs) == ev.status_policy["list"]), "registry")
            L.append(f"{self.t('policy')}: {_l(POLICY[name], self.lang)}")
        L = list(dict.fromkeys(L))  # the same condition from a repeated mention is shown once (live H09)
        return (f"{self.t('filters')}:\n" + "\n".join(f"• {x}" for x in L)) if L else ""

    def _method(self, res) -> str:
        ev = res.evidence
        L = []
        if res.intent == "lookup":
            purposes = [t["purpose"] for t in (res.table or {}).get("lookups", [])]
            if "lookup_deal" in purposes:
                L.append("количество = лоты × размер лота; цена за единицу = сумма сделки ÷ количество; поставлено = поставленные лоты × размер лота"
                         if self.lang == "ru" else "miqdor = lotlar × lot hajmi; birlik narxi = bitim summasi ÷ miqdor; yetkazilgan = yetkazilgan lotlar × lot hajmi")
                if any(f.name == "currency" and f.value == "USD" for f in res.facts):
                    L.append("сумма в сумах = сумма в долларах × курс на дату сделки (usd_rates.csv)" if self.lang == "ru"
                             else "so'mdagi summa = dollardagi summa × bitim sanasidagi kurs (usd_rates.csv)")
            if "lookup_summary" in purposes:
                L.append("сводка: число сделок; сумма — без аннулированных сделок, USD пересчитаны по курсу на дату сделки" if self.lang == "ru"
                         else "xulosa: bitimlar soni; summa — bekor qilinganlarsiz, USD bitim sanasidagi kurs bo'yicha o'girilgan")
            L.append(self.t("rounding"))
            L.append(self.t("not_profit"))
            return f"{self.t('method')}:\n" + "\n".join(f"• {x}" for x in L)
        metrics = list(dict.fromkeys(f.name.split("__")[0] for f in res.facts if f.name.split("__")[0] in METRICS))
        money = False
        for m in metrics:
            spec = METRICS[m]
            key = {"count": "count", "count_if": "count_if", "distinct": "distinct", "min": "minmax", "max": "minmax",
                   "median": "median_value", "catalog": "catalog", "group_stats": "group_stats"}.get(spec.kind, "distinct")
            if m in ("max_deal_value_uzs", "min_deal_value_uzs"):
                key = "minmax_value"
            if spec.kind == "sum":
                key = "sum_qty" if spec.unit == "native_unit" else "sum_money"
            if spec.kind == "ratio":
                key = {"weighted_avg_price": "weighted", "weighted_avg_price_uzs": "weighted", "mean_unit_price": "mean",
                       "cancellation_share": "share"}.get(m, "weighted")
                key = {"avg_deal_value_uzs": "mean_value", "avg_days_to_payment": "days_pay",
                       "avg_days_to_delivery": "days_del"}.get(m, key)
            money |= spec.unit in ("UZS", "native_currency", "price", "price_uzs")
            L.append(f"{self.label(m)}: {_l(METHOD[key], self.lang)}")
        if res.intent == "lookup" and any(f.name == "unit_price" for f in res.facts):
            money = True
        fv = {f.name: f.value for f in res.facts}
        pairs = [("avg_deal_value_uzs", "median_deal_value_uzs")] + [(f"{r}_value_stats__mean", f"{r}_value_stats__median") for r in ("seller", "buyer")]
        for a, b in pairs:
            if fv.get(a) is not None and fv.get(b) is not None:
                ru = self.lang == "ru"
                L.append("Среднее арифметическое = сумма ÷ количество, поэтому оно чувствительно к очень крупным значениям; медиана — "
                         "серединное значение (половина значений меньше, половина больше) и на крайние значения почти не реагирует. "
                         "Если распределение несимметрично, они расходятся." if ru else
                         "O'rtacha arifmetik = summa ÷ soni, shuning uchun juda katta qiymatlarga sezgir; mediana — o'rtadagi qiymat "
                         "(qiymatlarning yarmi kichik, yarmi katta) va chetki qiymatlarga deyarli ta'sirlanmaydi. Taqsimot nosimmetrik bo'lsa, ular farq qiladi.")
                da, db_ = Decimal(str(fv[a])), Decimal(str(fv[b]))
                cmp_ = ("больше" if da > db_ else "меньше" if da < db_ else "равно") if ru else ("katta" if da > db_ else "kichik" if da < db_ else "teng")
                L.append(f"Здесь среднее {cmp_} медианы." if ru else f"Bu yerda o'rtacha medianadan {cmp_}." if cmp_ != "teng" else "Bu yerda o'rtacha medianaga teng.")
                break
        conv = (ev.currency_unit or {}).get("usd_rows_converted_to_uzs") or {}
        uzs_metrics = [m for m in metrics if METRICS[m].unit in ("UZS", "price_uzs") and m in conv]
        counts = {conv[m] for m in uzs_metrics}
        shown_only = (ev.currency_unit or {}).get("usd_count_scope") == "shown_rows"
        if uzs_metrics and any(counts):
            if shown_only:
                L.append("В показанных строках:" if self.lang == "ru" else "Ko'rsatilgan qatorlarda:")
            if len(counts) == 1:
                L.append(self.t("usd_conv", n=fmt_num(next(iter(counts)))))
            else:
                L += [f"{self.label(m)}: " + self.t("usd_conv", n=fmt_num(conv[m])) for m in uzs_metrics]
        elif uzs_metrics:
            L.append(self.t("no_conv"))
        if any(METRICS[m].group_unit for m in metrics) and len({f.unit for f in res.facts if f.unit}) >= 1 and any(f.group for f in res.facts):
            L.append(self.t("units_sep"))
        if money:
            L.append(self.t("rounding"))
        if any(m in ("trade_value_uzs", "trade_value_native", "paid_value_uzs", "avg_deal_value_uzs", "max_deal_value_uzs") for m in metrics) \
                or res.intent == "lookup":
            L.append(self.t("not_profit"))
        return (f"{self.t('method')}:\n" + "\n".join(f"• {x}" for x in L)) if L else ""

    def _sources(self, res) -> str:
        ev = res.evidence
        files = [f["file"] for f in ev.source_files]
        deal_files = sorted(f for f in files if f.startswith("deals_"))
        other = [f for f in files if not f.startswith("deals_")]
        fl = ([f"{V(deal_files[0])}…{V(deal_files[-1])}"] if len(deal_files) > 1 else [V(f) for f in deal_files]) + [V(f) for f in other]
        parts = [f"{self.t('files')}: {', '.join(fl)}"] if fl else []
        if ev.source_row_count is not None:
            parts.append(f"{self.t('rows')}: {V(fmt_num(ev.source_row_count))}")
        parts.append(f"{self.t('query')}: {V(ev.query_id)}")
        if ev.sample_ids:
            parts.append(f"{self.t('samples')}: {', '.join(V(s) for s in ev.sample_ids[:5])}")
        return f"{self.t('sources')}: " + "; ".join(parts) + ".\n" + self.t("asof")

    # ------------------------------------------------------------ non-success
    def _cand(self, c: dict) -> str:
        if "inn" in c and "name" in c:
            role = _l(ROLE.get(c.get("role"), ("", "")), self.lang)
            return f"{self.party(c['name'], c['inn'])}" + (f", {role}" if role else "") + (
                f", {self.t('deals_w') if self.lang == 'ru' else 'bitimlar'}: {V(fmt_num(c['deals']))}" if c.get("deals") is not None else "")
        if "broker_code" in c:
            return f"{V(c['name'])} ({V(c['broker_code'])})"
        if "region_key" in c:
            return V(c["ru"] if self.lang == "ru" else c["uz"])
        if "variant_key" in c:
            name = self.variant_name(c["variant_key"])
            return f"{name} — {V(fmt_num(c.get('deals', 0)))} {self.t('deals_w') if self.lang == 'ru' else 'ta bitim'}, {_l(UNIT.get(c['unit'], (c['unit'],) * 2), self.lang)}"
        if "commodity_key" in c:
            return self.commodity_name(c["commodity_key"], c["name_ru"])
        if "option" in c:
            return _l(c["label"], self.lang)
        if "value" in c:
            return V(c["value"])
        return V(json.dumps(c, ensure_ascii=False))

    def _clarification(self, res) -> str:
        cl = res.clarification
        q = cl.question_ru if self.lang == "ru" else cl.question_uz
        L = [q] if q else []
        for i, c in enumerate(cl.candidates[:6], 1):
            L.append(f"{V(i)}) {self._cand(c)}")
        if len(cl.candidates) > 6:
            L.append(f"… {'и ещё' if self.lang == 'ru' else 'yana'} {V(len(cl.candidates) - 6)}")
        return "\n".join(L)

    def _no_rows(self, res) -> str:
        code = res.reason_code or ""
        ru = self.lang == "ru"
        ent = next((n for n in res.evidence.normalization if n["status"] == "not_found"), None)
        if code.endswith("_not_found") and ent:
            kinds = {"participant": ("Компания", "Kompaniya"), "broker": ("Брокер", "Broker"), "product": ("Товар", "Tovar"),
                     "region": ("Регион", "Hudud"), "deal": ("Сделка", "Bitim"), "contract": ("Контракт", "Shartnoma")}
            gen = {"participant": ("компаний", "kompaniyalar"), "broker": ("брокеров", "brokerlar"), "product": ("товаров", "tovarlar"),
                   "region": ("регионов", "hududlar"), "deal": ("сделок", "bitimlar"), "contract": ("контрактов", "shartnomalar")}
            k = _l(gen.get(ent["kind"], (ent["kind"],) * 2), self.lang)
            return (f"«{V(ent['text'])}» нет в справочнике {k} набора данных. Другие организации или товары вместо него не подставлялись."
                    if ru else f"«{V(ent['text'])}» ma'lumotlar to'plamidagi {k} ma'lumotnomasida yo'q. Uning o'rniga boshqa tashkilot yoki tovar qo'yilmadi.")
        if code == "no_rows_under_status_policy":
            return ("Сделки в выборке есть, но ни одна не подходит под правило статусов показателя (например, все аннулированы)."
                    if ru else "Tanlovda bitimlar bor, lekin hech biri ko'rsatkichning holat qoidasiga mos emas (masalan, hammasi bekor qilingan).")
        return ("По заданным условиям сделок нет (проверено: 0 подходящих строк). Это не ошибка системы."
                if ru else "Berilgan shartlar bo'yicha bitimlar yo'q (tekshirildi: 0 ta mos qator). Bu tizim xatosi emas.")

    def _missing(self, res) -> str:
        names = [x.strip() for x in (res.reason or "").split(",") if x.strip()]
        fields = ", ".join(_l(MISSING[n], self.lang) if n in MISSING else V(n) for n in names)
        return (f"В наборе данных нет таких сведений ({fields}). Доступны только данные реестра сделок: количества, суммы и цены сделок, "
                f"статусы, даты оплаты и поставки, участники и брокеры. Сумма сделки — это стоимость контракта, а не прибыль."
                if self.lang == "ru" else
                f"Ma'lumotlar to'plamida bunday ma'lumot yo'q ({fields}). Faqat bitimlar reyestri ma'lumotlari mavjud: miqdorlar, "
                f"summalar va narxlar, holatlar, to'lov va yetkazish sanalari, ishtirokchilar va brokerlar. Bitim summasi — shartnoma qiymati, foyda emas.")

    def _out_of_period(self, res) -> str:
        pr = res.evidence.period_requested or {}
        cp = res.evidence.compare_periods or {}
        req = f"{V(fmt_date(pr['start']))}–{V(fmt_date(pr['end']))}" if pr.get("start") else (V(res.reason) if not cp else "")
        return (f"Запрошенный период ({req}) вне данных. Набор данных охватывает сделки с 03.01.2022 по 30.10.2026, статусы — на конец 30.10.2026. "
                f"О более позднем времени данных нет, и прогнозы не делаются."
                if self.lang == "ru" else
                f"So'ralgan davr ({req}) ma'lumotlardan tashqarida. Ma'lumotlar 03.01.2022 dan 30.10.2026 gacha bo'lgan bitimlarni o'z ichiga oladi, "
                f"holatlar 30.10.2026 kun oxiriga ko'ra. Keyingi davr haqida ma'lumot yo'q, prognoz qilinmaydi.")

    def _unsupported(self, res) -> str:
        code = res.reason_code or ""
        ru = self.lang == "ru"
        if code == "instruction_not_allowed":
            return ("Этот запрос не выполняется: инструкции по изменению правил, данных или доступа игнорируются. Можно спросить о сделках из набора данных."
                    if ru else "Bu so'rov bajarilmaydi: qoidalar, ma'lumotlar yoki kirishni o'zgartirish bo'yicha ko'rsatmalar e'tiborga olinmaydi. Bitimlar haqida so'rashingiz mumkin.")
        if code == "not_a_data_question":
            return ("Я отвечаю на вопросы о данных сделок биржи (количество, суммы, цены, рейтинги, сравнения, карточки сделок и компаний). "
                    "Если нужно пояснить уже полученный ответ, напишите, например, «объясни этот результат». Список возможностей — /help."
                    if ru else "Men birja bitimlari ma'lumotlari haqidagi savollarga javob beraman (soni, summalar, narxlar, reytinglar, solishtirish, "
                    "bitim va kompaniya kartochkalari). Olingan javobni izohlash uchun, masalan, «bu natijani tushuntir» deb yozing. Imkoniyatlar ro'yxati — /help.")
        if code == "plan_mismatch":
            return ("Вопрос не удалось надёжно разобрать: план запроса не сохранил одно из условий вопроса (год, роль участника, "
                    "показатель, марка или число позиций), поэтому ответ не рассчитывался. Пожалуйста, переформулируйте вопрос короче и точнее."
                    if ru else "Savolni ishonchli tahlil qilib bo'lmadi: so'rov rejasi savoldagi shartlardan birini (yil, ishtirokchi roli, "
                    "ko'rsatkich, marka yoki o'rinlar soni) saqlamadi, shuning uchun javob hisoblanmadi. Iltimos, savolni qisqaroq va aniqroq qayta yozing.")
        if code == "contradictory_plan":
            return ("Вопрос не удалось однозначно перевести в запрос: условия противоречат друг другу. Пожалуйста, переформулируйте."
                    if ru else "Savolni bir ma'noli so'rovga aylantirib bo'lmadi: shartlar bir-biriga zid. Iltimos, qayta ifodalang.")
        return ("Такой тип вопроса пока не поддерживается." if ru else "Bunday savol turi hozircha qo'llab-quvvatlanmaydi.") + (
            f" ({V(code)})" if code else "")

    def wait_text(self, seconds: float) -> str:
        s = max(1, int(round(seconds)))
        m, sec = divmod(s, 60)
        h, m = divmod(m, 60)
        for v in (h, m, sec):
            self.catalog_strings.append(str(v))  # derived from the payload's wait hint
        ru = self.lang == "ru"
        parts = []
        if h:
            parts.append(f"{V(h)} {'ч' if ru else 'soat'}")
        if m:
            parts.append(f"{V(m)} {'мин' if ru else 'daqiqa'}")
        if sec and not h:
            parts.append(f"{V(sec)} {'с' if ru else 'soniya'}")
        return " ".join(parts)

    def _exec_error(self, res) -> str:
        """User-facing text only; internal reason codes stay in evidence notes and logs."""
        code = res.reason_code or ""
        ru = self.lang == "ru"
        llm = res.llm or {}
        if code == "llm_rate_limited":
            wait = llm.get("last_wait_hint_s")
            limit = llm.get("limit") or {}
            kind = {"TPD": ("исчерпан дневной лимит токенов", "kunlik token limiti tugagan"),
                    "TPM": ("исчерпан поминутный лимит токенов", "daqiqalik token limiti tugagan"),
                    "RPD": ("исчерпан дневной лимит запросов", "kunlik so'rovlar limiti tugagan"),
                    "RPM": ("исчерпан поминутный лимит запросов", "daqiqalik so'rovlar limiti tugagan")}.get(limit.get("kind"))
            why = (f" ({_l(kind, self.lang)})" if kind else "")
            when = self.wait_text(wait) if wait else ("немного позже" if ru else "birozdan keyin")
            msg = (f"Разбор новых вопросов временно недоступен: у сервиса языковой модели{why}. Повторите вопрос примерно через {when}. "
                   f"Справка (/help), кнопки «Источник» и пояснения к уже полученным ответам продолжают работать."
                   if ru else
                   f"Yangi savollarni tahlil qilish vaqtincha imkonsiz: til modeli xizmatida{why}. Savolni taxminan {when}dan keyin qayta bering. "
                   f"Yordam (/help), «Manba» tugmalari va olingan javoblarga izohlar ishlashda davom etadi.")
        elif code == "service_busy":
            msg = ("Сейчас слишком много вопросов одновременно. Повторите вопрос через минуту."
                   if ru else "Hozir bir vaqtda juda ko'p savol bor. Bir daqiqadan keyin qayta so'rang.")
        elif code == "session_invalid":
            msg = ("Уточнение больше не действительно. Задайте вопрос заново." if ru
                   else "Aniqlashtirish endi amal qilmaydi. Savolni qaytadan bering.")
        elif code.startswith("llm_"):
            msg = ("Не удалось разобрать вопрос из-за временной ошибки сервиса разбора." if ru
                   else "Savolni tahlil qilish xizmatidagi vaqtinchalik xato tufayli savolni tahlil qilib bo'lmadi.")
        elif code == "deadline_exhausted":
            msg = ("Время на ответ истекло, пока вопрос ждал в очереди. Повторите вопрос." if ru
                   else "Savol navbatda kutayotganda javob vaqti tugadi. Savolni qayta bering.")
        elif "timeout" in code:
            msg = ("Расчёт не уложился в отведённое время." if ru else "Hisoblash ajratilgan vaqtga sig'madi.")
        else:
            msg = ("Внутренняя ошибка при расчёте." if ru else "Hisoblashda ichki xato.")
        return msg + (" Это сбой системы, а не отсутствие данных." if ru else " Bu tizim nosozligi, ma'lumot yo'qligi emas.")

    def _fallback(self, res) -> str:
        """Readable localized fallback from the validated facts (used when the full layout fails or does not ground)."""
        L = []
        for f in res.facts[:30]:
            if f.name in METRICS or f.name.split("__")[0] in METRICS:
                L.append(f"{self.label(f.name)}: {self.val(f)}")
            else:
                cols = (res.table or {}).get("columns", [])
                prov = sql_provenance(self._custom_sql(res), len(cols)) if res.intent == "custom" else []
                i = cols.index(f.name) if f.name in cols else -1
                pv = prov[i] if 0 <= i < len(prov) else {"verified": False}
                lab = _l(pv["label"], self.lang) if pv.get("verified") else (
                    f"Непроверенный столбец {i + 1}" if self.lang == "ru" else f"Tasdiqlanmagan ustun {i + 1}")
                L.append(f"{lab}: {self._cell_k(pv.get('kind', 'other') if pv.get('verified') else 'other', f.value)}")
        if not L:
            return ("Не удалось оформить ответ; данные доступны в разделе «Источник»." if self.lang == "ru"
                    else "Javobni rasmiylashtirib bo'lmadi; ma'lumotlar «Manba» bo'limida.")
        head = "Результат (упрощённый вид):" if self.lang == "ru" else "Natija (soddalashtirilgan ko'rinish):"
        return head + "\n" + "\n".join(L)


# ---------------------------------------------------------------- grounding
_NUM = re.compile(r"\d[\d  ]*(?:[.,]\d+)?")
TEMPLATE_NUMBERS = {"1", "2", "100", "3", "0", "03.01.2022", "30.10.2026"}  # constants written by templates


def _common_suffix(rows: list[list[str]]) -> str | None:
    """The unit text shared by every value/change cell (columns 1-3) of a comparison table, if there is exactly one."""
    sufs = set()
    for r in rows:
        for cell in r[1:4]:
            if cell == "—":
                continue
            i = cell.rfind("⟧")
            if i < 0:
                return None
            sufs.add(cell[i + 1:])
    suf = sufs.pop() if len(sufs) == 1 else None
    return suf if suf and suf.strip() else None


def _strip_marks(s: str) -> str:
    return s.replace("⟦", "").replace("⟧", "")


def _canon(tok: str) -> set[str]:
    t = tok.replace(" ", "").replace(" ", "").replace(",", ".")
    out = {t}
    d = _d(t)
    if d is not None:
        out.add(format(d.normalize(), "f"))
    return out


def payload_numbers(res, extra: list[str]) -> set[str]:
    """Numbers a rendered answer may contain: computed facts and tables, the evidence of THIS answer (period,
    filters, normalization, row counts, conversions, sample ids), the clarification and reason, and catalog
    strings the renderer inserted. Deliberately NOT the plan, LLM metadata (token counts, request ids),
    timings or raw SQL text/params: a number that merely occurs somewhere in the payload is not grounding."""
    ev = res.evidence.model_dump(mode="json", exclude={"queries", "prompt_version"})
    keep = {"facts": [f.model_dump(mode="json") for f in res.facts], "table": res.table, "evidence": ev,
            "clarification": res.clarification.model_dump(mode="json") if res.clarification else None,
            "reason": res.reason, "llm_wait": (res.llm or {}).get("last_wait_hint_s"),
            "row_counts": [q.row_count for q in res.evidence.queries],
            "top_n": res.plan.top_n if res.plan else None}
    blob = json.dumps(keep, ensure_ascii=False, default=str) + " " + " ".join(extra)
    nums: set[str] = set()
    for m in re.finditer(r"\d+(?:[.,]\d+)?", blob):
        tok = m.group(0)
        nums |= _canon(tok)
        d = _d(tok.replace(",", "."))
        if d is not None and "." in tok.replace(",", "."):
            for places in (0, 1, 2):
                nums |= _canon(str(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)))
    for m in re.finditer(r"(\d{4})-(\d{2})-(\d{2})", blob):  # ISO dates as rendered DD.MM.YYYY
        y, mo, da = m.groups()
        nums |= {f"{da}.{mo}.{y}", da, mo, y, f"{mo}.{y}"}
    return nums


def grounding_errors(text: str, res, extra: list[str]) -> list[str]:
    allowed = payload_numbers(res, extra) | TEMPLATE_NUMBERS
    bad = []
    # dates | grouped numbers (NBSP or plain space, so text written elsewhere is checked too) | plain numbers
    pat = r"\d{2}\.\d{2}\.\d{4}|(?<!\d)\d{1,3}(?:[ \u00a0]\d{3})+(?:,\d+)?(?!\d)|\d+(?:,\d+)?"
    for m in re.finditer(pat, text):
        tok = m.group(0)
        if not (_canon(tok) & allowed):
            bad.append(tok)
    return bad


def render(res, lang: Lang, catalog=None) -> Rendered:
    return Renderer(catalog).render(res, lang)
