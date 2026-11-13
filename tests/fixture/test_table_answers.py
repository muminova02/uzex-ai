"""Table answers (web + Telegram) and the raw-English-alias defect (2026-11-05).

Root cause of the defect: for "jami nechta kompaniya ... eng yirik/kichik va o'rta summalarni chiqar" the planner chose
free SQL (intent=custom); the custom renderer printed the model's column aliases (total_companies, max_deal_value_uzs, ...)
and raw values joined by commas, and the grounding fallback printed `name = value unit`. These tests pin the fix."""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal

import duckdb
import pytest

from uzex_assistant.bot_core import BotCore, Out
from uzex_assistant.config import Settings
from uzex_assistant.engine import Engine
from uzex_assistant.llm import MockProvider
from uzex_assistant.render import NB, Renderer, alias_kind, alias_label
from uzex_assistant.service import QueryService
from uzex_assistant.tg_format import answer_html, rich_message, split_units

from .engine_fixture import build
from .test_render import PLANS, Q

Y2025 = {"start": "2025-01-01", "end": "2025-12-31"}
COMPOUND = "jami nechta kompaniya ma'lumotlari kiritilgan va eng yirik/kichik va o'rta summalarni chiqar"
ALIASES = ("total_companies", "max_deal_value_uzs", "avg_deal_value_uzs", "min_deal_value_uzs", "median_deal_value_uzs",
           "seller_value_stats", "catalog_companies", "deal_count", "__")
CUSTOM = {"intent": "custom", "custom_sql": "SELECT count(DISTINCT seller_inn) AS total_companies, max(amount_uzs) AS max_deal_value_uzs, "
          "min(amount_uzs) AS min_deal_value_uzs, avg(amount_uzs) AS avg_deal_value_uzs FROM deal_facts", "custom_params": []}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    data, db = build(tmp_path_factory.mktemp("tables"))
    e = Engine(Settings(_env_file=None, data_dir=data, db_path=db, sql_workers=1, request_budget_s=60), provider=MockProvider([]))
    yield e, db
    e.close()


def ask(e, q, plan):
    e.provider._responses = [plan]
    return e.answer(q)


def no_alias(s: str):
    for a in ALIASES:
        assert a not in s, (a, s[:300])


def sql(db, q):
    c = duckdb.connect(str(db), read_only=True)
    try:
        return c.execute(q).fetchall()
    finally:
        c.close()


def fmt_money(d) -> str:
    q = Decimal(str(d)).quantize(Decimal("0.01"))
    ip, fp = f"{q:f}".split(".")
    return re.sub(r"(?<=\d)(?=(\d{3})+$)", " ", ip) + "," + fp


# ------------------------------------------------------------------ the reported compound question
def test_compound_question_asks_instead_of_guessing(env):
    e, _ = env
    for plan in (CUSTOM, {"intent": "aggregate", "metrics": ["deal_count", "max_deal_value_uzs"]}):
        r = ask(e, COMPOUND, plan)
        assert r.status == "ambiguous" and r.reason_code == "ambiguous_summary_scope"
        assert [c["option"] for c in r.clarification.candidates] == ["deals", "sellers", "buyers"]
        assert not any(q.purpose == "custom" for q in r.evidence.queries)  # nothing ran before the user chose
        no_alias(r.rendered["text"])
    ru = ask(e, "сколько всего компаний и покажи максимальную, минимальную и среднюю сумму", CUSTOM)
    assert ru.reason_code == "ambiguous_summary_scope" and "медиана" in ru.rendered["text"]
    # an explicit scope is not asked again
    fixed = ask(e, "jami nechta kompaniya va har bir bitim summasining eng kattasi", {"intent": "aggregate", "metrics": ["max_deal_value_uzs", "distinct_sellers"]})
    assert fixed.reason_code != "ambiguous_summary_scope"


def _choose(e, r, opt):
    cand = next(c for c in r.clarification.candidates if c["option"] == opt)
    plan, skip = e.apply_choice(r.plan, r.reason_code, cand, None)
    return e.run_plan(plan, COMPOUND, skip_checks=skip)


def test_deal_level_choice_is_exact_and_labelled(env):
    e, db = env
    r = ask(e, COMPOUND, CUSTOM)
    f = _choose(e, r, "deals")
    assert f.status == "success" and f.rendered["grounded"] and not f.rendered["fallback_used"]
    t = f.rendered["blocks"]["tables"][0]
    # rows cover different deals -> an explicit scope column on every row
    assert [c["label"] for c in t["columns"]] == ["Ko'rsatkich", "Qiymat", "Qamrov"]
    rows = {r[0]: r[1].replace(NB, " ") for r in t["rows"]}
    scope = {r[0]: r[2] for r in t["rows"]}
    incl, excl = "barcha bitimlar, bekor qilinganlar bilan", "bekor qilinganlarsiz"
    assert scope["Sotuvchilar soni"] == incl and scope["Bitimlar soni"] == incl
    assert scope["Eng yirik bitim summasi (so'mda)"] == excl and scope["Bitim summasining medianasi (so'mda)"] == excl
    assert scope["Ishtirokchilar ma'lumotnomasidagi kompaniyalar (jami)"].startswith("ishtirokchilar ma'lumotnomasi")
    mx, mn, md, s, n = sql(db, "SELECT max(amount_uzs), min(amount_uzs), median(amount_uzs), sum(amount_uzs), count(*) FROM deal_facts "
                               "WHERE status_code <> 'cancelled_unpaid'")[0]
    cat = sql(db, "SELECT count(*) FROM participants")[0][0]
    assert rows["Eng yirik bitim summasi (so'mda)"] == f"{fmt_money(mx)} so'm"
    assert rows["Eng kichik bitim summasi (so'mda)"] == f"{fmt_money(mn)} so'm"
    assert rows["Bitim summasining medianasi (so'mda)"] == f"{fmt_money(md)} so'm"
    assert rows["Bitimning o'rtacha summasi (so'mda)"] == f"{fmt_money(Decimal(s) / n)} so'm"  # exact mean, not a float avg
    assert rows["Ishtirokchilar ma'lumotnomasidagi kompaniyalar (jami)"].startswith(str(cat))
    assert "Sotuvchilar soni" in rows and "Xaridorlar soni" in rows  # participating companies kept apart from the catalog
    no_alias(f.rendered["text"])


@pytest.mark.parametrize("opt,key", [("sellers", "seller_inn"), ("buyers", "buyer_inn")])
def test_company_total_choice_matches_independent_sql(env, opt, key):
    e, db = env
    f = _choose(e, ask(e, COMPOUND, CUSTOM), opt)
    assert f.status == "success" and f.rendered["grounded"]
    n, s, mx, mn, md = sql(db, f"WITH per AS (SELECT {key}, sum(amount_uzs) v FROM deal_facts WHERE status_code <> 'cancelled_unpaid' "
                               f"GROUP BY {key}) SELECT count(*), sum(v), max(v), min(v), median(v) FROM per")[0]
    t = f.rendered["blocks"]["tables"][0]
    vals = [r[1].replace(NB, " ") for r in t["rows"]]
    assert t["columns"][-1]["label"] == "Qamrov" and all(r[2] == "bekor qilinganlarsiz" for r in t["rows"][:-1])
    for want in (fmt_money(mx), fmt_money(mn), fmt_money(md), fmt_money(Decimal(s) / n)):
        assert any(v.startswith(want) for v in vals), (want, vals)
    no_alias(f.rendered["text"])


# ------------------------------------------------------------------ free SQL never shows aliases (3 scripts)
@pytest.mark.parametrize("q,hdr,label", [
    ("Bitimlar bo'yicha umumiy statistika", "So'rov natijasi:", "Eng katta bitim summasi (so'mda)"),
    ("Общая статистика по сделкам", "Результат запроса:", "Наибольшая сумма сделки в сумах"),
    ("Битимлар бўйича умумий статистика", "Сўров натижаси:", "Енг катта битим суммаси (сўмда)")])
def test_custom_result_is_localized_table(env, q, hdr, label):
    e, _ = env
    r = ask(e, q, CUSTOM)
    t = r.rendered["text"]
    assert r.status == "success" and r.rendered["grounded"] and hdr in t and label in t
    no_alias(t)
    tbl = r.rendered["blocks"]["tables"][0]
    assert len(tbl["columns"]) == 2 and all("_" not in row[0] for row in tbl["rows"])


def test_alias_registry_and_cell_kinds():
    assert alias_label("max_deal_value_uzs", "uz") == "Eng yirik bitim summasi (so'mda)"
    assert alias_label("total_companies", "ru") == "Всего компаний"
    assert alias_label("weird_xyz_col", "uz", 2) == "Ko'rsatkich 3"  # unknown token: generic label, never the alias
    assert alias_kind("avg_deal_value_uzs") == "money" and alias_kind("total_companies") == "count" and alias_kind("x_usd") == "money_usd"
    r = Renderer()
    r.lang, r.catalog_strings, r.tables = "uz", [], []
    clean = lambda s: s.replace("⟦", "").replace("⟧", "").replace(NB, " ")  # noqa: E731
    assert clean(r._cell("max_value_uzs", Decimal("123456789012.345"))) == "123 456 789 012,35 so'm"   # half-up, grouping
    assert clean(r._cell("total_count", 0)) == "0" and clean(r._cell("max_value_uzs", None)) == "—"
    assert clean(r._cell("min_value_uzs", Decimal("0.004"))) == "0,00 so'm"
    assert clean(r._cell("n", 1234567)) == "1 234 567"


# ------------------------------------------------------------------ table shapes by question type
def test_summary_ranking_comparison_deal_and_mixed_units(env):
    e, _ = env
    s = ask(e, Q["value_uzs"][1], PLANS["value_uzs"])
    ts = s.rendered["blocks"]["tables"][0]
    assert [c["label"] for c in ts["columns"]] == ["Ko'rsatkich", "Qiymat", "Qamrov"] and len(ts["rows"]) == 2
    assert {r[2] for r in ts["rows"]} == {"barcha bitimlar, bekor qilinganlar bilan", "bekor qilinganlarsiz"}
    rk = ask(e, Q["ranking"][0], PLANS["ranking"])
    tr = rk.rendered["blocks"]["tables"][0]
    assert tr["columns"][0]["label"].startswith("№ · Покупатель") and tr["columns"][1]["num"] and len(tr["rows"]) == 2
    assert '"АЛЬФА, САВДО" МЧЖ (ИНН 333333333)' in tr["rows"][0][0]  # full original name + INN
    assert "Показаны первые 2 из 3." in rk.rendered["text"]  # top-N disclosure
    cp = ask(e, Q["comparison"][0], PLANS["comparison"])
    tc = cp.rendered["blocks"]["tables"][0]
    assert [c["label"] for c in tc["columns"]][-2:] == ["Изменение", "Изменение, %"]
    assert "Последний период неполный" in cp.rendered["text"]  # partial-year alignment kept
    lk = ask(e, Q["lookup"][1], PLANS["lookup"])
    tl = lk.rendered["blocks"]["tables"][0]
    assert [c["label"] for c in tl["columns"]] == ["Maydon", "Qiymat"]
    assert any(r[0] == "Tovar" for r in tl["rows"]) and any("lot" in r[1] for r in tl["rows"])
    mu = ask(e, Q["petrol_qty"][0], PLANS["petrol_qty"])
    flat = " ".join(" ".join(r) for t in mu.rendered["blocks"]["tables"] for r in t["rows"]).replace(NB, " ")
    assert "35 000 л" in flat and "2 т" in flat and "35 002" not in flat  # units in separate rows, never summed


def test_comparison_percent_omitted_when_undefined(env):
    e, _ = env
    r = ask(e, "Сравни сделки с аннулированными 2025 и 2026", {"intent": "comparison", "metrics": ["deal_count"],
            "compare_periods": [{"start": "2022-01-01", "end": "2022-12-31"}, Y2025]})
    if r.status == "success":
        row = r.rendered["blocks"]["tables"][0]["rows"][0]
        if row[1].startswith("0"):
            assert row[-1] == "—"  # p1 = 0: percentage change is undefined, not shown


def test_uz_cyrillic_and_russian_labels(env):
    e, _ = env
    cy = ask(e, Q["value_uzs"][2], PLANS["value_uzs"])
    ru = ask(e, Q["value_uzs"][0], PLANS["value_uzs"])
    assert cy.rendered["blocks"]["tables"][0]["columns"][0]["label"] == "Кўрсаткич"
    assert ru.rendered["blocks"]["tables"][0]["columns"][0]["label"] == "Показатель"
    num = lambda cell: re.sub(r"[^\d,]", "", cell)  # noqa: E731  (digits of one cell; labels/units differ by language)
    assert [num(r[1]) for r in cy.rendered["blocks"]["tables"][0]["rows"]] == \
        [num(r[1]) for r in ru.rendered["blocks"]["tables"][0]["rows"]]


def test_fallback_is_localized_and_alias_free(env):
    e, _ = env
    r = ask(e, Q["value_uzs"][1], PLANS["value_uzs"])
    rr = Renderer(e.catalog)
    rr.lang, rr.catalog_strings, rr.tables = "uz", [], []
    fb = rr._fallback(r).replace("⟦", "").replace("⟧", "")
    assert fb.startswith("Natija (soddalashtirilgan ko'rinish):") and "Bitimlar soni" in fb
    no_alias(fb)
    assert r.evidence.query_id not in fb  # technical ids stay in the evidence view


def test_failures_and_help_are_not_tables(env):
    e, _ = env
    r = ask(e, "2027-yilda nechta bitim bo'ladi?", {"intent": "count", "metrics": ["deal_count"], "period": {"start": "2027-01-01", "end": "2027-12-31"}})
    assert r.status == "out_of_period" and r.rendered["blocks"] is None


# ------------------------------------------------------------------ Telegram
def test_telegram_text_matches_web_numbers_and_keeps_rows(env):
    e, _ = env
    r = ask(e, Q["list"][0], PLANS["list"])
    web_cells = [c for t in r.rendered["blocks"]["tables"] for row in t["rows"] for c in row]
    tg = "\n".join(answer_html(r.rendered["text"]))
    for c in web_cells:
        for n in re.findall(r"\d[\d  ]*,\d{2}", c):
            assert n in tg  # same facts and numbers in both channels
    assert "|" not in tg and "  " not in tg.replace(NB, " ").replace("  ", "")  # no ASCII column alignment
    many = ["1. " + "A" * 200 + f" — Сумма: {i} сум" for i in range(60)]
    chunks = split_units(many, 3900)
    assert sum(c.count("\n") + 1 for c in chunks) == 60 and all(len(c) <= 3900 for c in chunks)  # nothing dropped


def test_telegram_escaping_of_names():
    out = "\n".join(answer_html('Natija:\n1. "A<B> & Co" MChJ — Summa: 1 000,00 so\'m'))
    assert "&lt;B&gt; &amp; Co" in out and "<B>" not in out


def test_rich_message_builder_uses_real_table_blocks(env):
    e, _ = env
    r = ask(e, Q["ranking"][0], PLANS["ranking"])
    msg = rich_message(r.rendered["blocks"], banner="TEST")
    tbl = next(b for b in msg.blocks if b.type == "table")
    assert tbl.cells[0][0].is_header and tbl.cells[0][1].align == "right" and len(tbl.cells) == 1 + len(r.rendered["blocks"]["tables"][0]["rows"])
    assert msg.model_dump(exclude_none=True)["blocks"][0]["type"] == "paragraph"


def test_rich_send_falls_back_to_html_with_buttons(env):
    from uzex_assistant import bot as botmod

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_rich_message(self, **kw):
            raise RuntimeError("rich messages unsupported here")

        async def send_message(self, **kw):
            self.sent.append(kw)

    b = FakeBot()
    outs = [Out("part 1", rich={"heading": "h", "tables": [], "notes": []}, rich_covers=2), Out("part 2", [[("📎", "s:abc")]])]
    asyncio.run(botmod.send_outs(b, 1, outs))
    assert [m["text"] for m in b.sent] == ["part 1", "part 2"] and b.sent[-1]["reply_markup"] is not None  # evidence button kept


def test_bot_keeps_evidence_button_and_blocks_reach_web_payload(env):
    e, _ = env
    e.provider = MockProvider(lambda s, u: PLANS["value_uzs"])
    e.planner.provider = e.provider
    core = BotCore(QueryService(e))
    outs = asyncio.run(core.on_text(77, 1, Q["value_uzs"][1]))
    assert outs[-1].buttons and outs[-1].buttons[0][0][1].startswith("s:")
    assert core.last[(77, 1)]["blocks"]["tables"][0]["columns"][0]["label"] == "Ko'rsatkich"
    assert outs[0].rich is None  # rich tables are off by default (enabled only after a check in real Telegram clients)


def test_clarification_labels_fit_telegram_buttons(env):
    e, _ = env
    r = ask(e, COMPOUND, CUSTOM)
    for c in r.clarification.candidates:
        assert all(len(f"3) {lab}") <= 60 for lab in c["label"])  # never cut mid-word in a Telegram button


def test_test_mode_banner_reaches_the_table_view(env):
    e, _ = env
    e.provider = MockProvider(lambda s, u: PLANS["value_uzs"])
    e.planner.provider = e.provider
    core = BotCore(QueryService(e))  # MockProvider => test mode
    asyncio.run(core.on_text(78, 1, Q["value_uzs"][1]))
    assert core.last[(78, 1)]["blocks"] and core.last[(78, 1)]["banner"]  # web shows it above the table


# ------------------------------------------------------------------ free SQL: meaning from the SQL itself, never from the alias
def _custom(sql):
    return {"intent": "custom", "custom_sql": sql, "custom_params": []}


def test_misleading_alias_is_replaced_by_sql_provenance(env):
    e, _ = env
    r = ask(e, "Bitimlar bo'yicha umumiy statistika", CUSTOM)  # alias says "total_companies" but SQL counts distinct sellers
    t = r.rendered["blocks"]["tables"][0]
    labels = [row[0] for row in t["rows"]]
    assert labels[0] == "Turli sotuvchilar soni (STIR bo'yicha)" and "Jami kompaniyalar" not in r.rendered["text"]
    assert labels[1:] == ["Eng katta bitim summasi (so'mda)", "Eng kichik bitim summasi (so'mda)", "O'rtacha bitim summasi (so'mda)"]
    assert "Qamrov: barcha bitimlar, bekor qilinganlar bilan" in t["note"]  # no WHERE -> cancelled deals included, said explicitly
    assert "avg() funksiyasi" in t["note"]  # floating-point SQL mean disclosed
    no_alias(r.rendered["text"])


def test_where_clause_scope_is_detected(env):
    e, _ = env
    r = ask(e, "statistika", _custom("SELECT count(*) AS n, median(amount_uzs) AS med FROM deal_facts WHERE status_code <> 'cancelled_unpaid'"))
    t = r.rendered["blocks"]["tables"][0]
    assert [row[0] for row in t["rows"]] == ["Bitimlar soni", "Mediana bitim summasi (so'mda)"]
    assert t["note"].startswith("Qamrov: bekor qilinganlarsiz")
    r2 = ask(e, "статистика", _custom("SELECT count(*) AS n FROM deal_facts WHERE NOT is_cancelled AND deal_year = 2025"))
    assert "без аннулированных сделок; условие по дате сделки задано в запросе" in r2.rendered["blocks"]["tables"][0]["note"]
    r3 = ask(e, "статистика", _custom("SELECT count(*) AS n FROM deal_facts WHERE NOT is_cancelled AND seller_inn = '111111111'"))
    assert "плюс дополнительные условия" in r3.rendered["blocks"]["tables"][0]["note"]


@pytest.mark.parametrize("sql", [
    "SELECT sum(amount_uzs) / count(*) AS avg_deal_value_uzs FROM deal_facts",            # arithmetic: not proven
    "SELECT count(*) AS total_companies FROM deal_facts WHERE status_code LIKE 'p%'",    # status pattern: scope cannot be stated
    "SELECT max(quantity_native) AS max_qty FROM deal_facts",                             # mixed units: no safe meaning
])
def test_unprovable_columns_get_a_limitation_not_a_label(env, sql):
    e, _ = env
    r = ask(e, "statistika", _custom(sql))
    txt = r.rendered["text"]
    assert r.status == "success" and "Tasdiqlanmagan ustun 1" in txt and "Cheklov:" in txt and "«Manba»" in txt
    assert "tasdiqlanmagan" in r.rendered["blocks"]["heading"]
    no_alias(txt)
    for word in ("kompaniya", "o'rtacha", "miqdor"):
        assert word not in r.rendered["blocks"]["tables"][0]["rows"][0][0].lower()


def test_catalog_count_from_participants_table(env):
    e, _ = env
    r = ask(e, "katalog", _custom("SELECT count(*) AS c FROM participants"))
    t = r.rendered["blocks"]["tables"][0]
    assert t["rows"][0][0] == "Ishtirokchilar ma'lumotnomasidagi kompaniyalar (jami)"
    assert "ishtirokchilar ma'lumotnomasi, bitim filtrlarisiz" in t["note"]


def test_both_company_roles_are_computed_not_just_the_first(env):
    e, db = env
    r = ask(e, "sotuvchi va xaridorlar bo'yicha jami summalar statistikasi",
            {"intent": "aggregate", "metrics": ["catalog_companies", "seller_value_stats", "buyer_value_stats"]})
    labels = [row[0] for row in r.rendered["blocks"]["tables"][0]["rows"]]
    assert "Bitimi bor sotuvchilar" in labels and "Bitimi bor xaridorlar" in labels  # live plan shape 2026-11-05
    assert labels.count("Ishtirokchilar ma'lumotnomasidagi kompaniyalar (jami)") == 1


# ------------------------------------------------------------------ 2026-11-05 evening: date lineage, status scope, governed median
QRU_DAY = ("В какой день 2025 года была зафиксирована максимальная общая стоимость полностью исполненных сделок? Укажите дату, "
           "количество сделок и общую сумму в узбекских сумах.")
QUZ_MM = ("2025-yilda to‘liq bajarilgan barcha bitimlarning so‘mdagi qiymati bo‘yicha o‘rtacha arifmetik va median summani alohida "
          "hisoblang. Dollar bitimlarini har bir bitim sanasidagi kurs bilan aylantiring. Nima uchun bu ikki ko‘rsatkich farq qiladi?")
DAY_SQL = [
    "SELECT deal_date, count(*) AS deal_count, sum(amount_uzs) AS total_uzs FROM deal_facts WHERE status_code = 'fulfilled' "
    "AND deal_year = 2025 GROUP BY deal_date ORDER BY total_uzs DESC LIMIT 1",
    "SELECT CAST(deal_date AS DATE) AS day, COUNT(*) AS n, SUM(amount_uzs) AS total FROM deal_facts WHERE status = 'Исполнен' "
    "AND deal_year = 2025 GROUP BY 1 ORDER BY 3 DESC LIMIT 1",
    "SELECT date_trunc('day', deal_ts) AS d, count(*) AS c, sum(amount_uzs) AS s FROM deal_facts WHERE status_code IN ('fulfilled') "
    "AND deal_year = 2025 GROUP BY 1 ORDER BY 3 DESC LIMIT 1"]


@pytest.mark.parametrize("sql", DAY_SQL)
def test_date_column_is_verified_by_lineage_and_formatted(env, sql):
    e, db = env
    r = ask(e, QRU_DAY, {"intent": "custom", "metrics": ["deal_count", "trade_value_uzs"], "custom_sql": sql, "custom_params": []})
    t = r.rendered["blocks"]["tables"][0]
    labels = [row[0] for row in t["rows"]]
    assert labels == ["Дата", "Количество сделок", "Итого сумма сделок в сумах"], labels
    assert "Непроверенный" not in r.rendered["text"] and "не подтверждена" not in r.rendered["blocks"]["heading"]
    want = sql_day = sql_cmp(db)
    assert t["rows"][0][1] == want[0] and t["rows"][1][1].replace(NB, " ") == want[1]
    assert t["rows"][2][1].replace(NB, " ") == f"{want[2]} сум"
    assert t["note"].startswith("Охват: только полностью исполненные сделки")
    tg = "\n".join(answer_html(r.rendered["text"])).replace(NB, " ")
    assert f"Дата: {want[0]}" in tg and want[2] in tg  # same verified label/value in the Telegram text


def sql_cmp(db):
    d, n, s = sql(db, "SELECT deal_date, count(*), sum(amount_uzs) FROM deal_facts WHERE status_code = 'fulfilled' AND deal_year = 2025 "
                      "GROUP BY deal_date ORDER BY 3 DESC LIMIT 1")[0]
    return d.strftime("%d.%m.%Y"), f"{n:,}".replace(",", " "), fmt_money(s)


def test_status_scope_for_explicit_status_lists(env):
    e, _ = env
    r = ask(e, "статусы", _custom("SELECT count(*) AS n FROM deal_facts WHERE status_code IN ('paid', 'partially_fulfilled')"))
    assert "Охват: сделки со статусами:" in r.rendered["blocks"]["tables"][0]["note"] and "Непроверенный" not in r.rendered["text"]


@pytest.mark.parametrize("plan", [
    {"intent": "aggregate", "metrics": ["avg_deal_value_uzs", "median_deal_value_uzs"], "period": Y2025, "status_policy": "fulfilled"},
    {"intent": "aggregate", "metrics": ["avg_deal_value_uzs", "median_deal_value_uzs"], "period": Y2025,
     "filters": [{"field": "status", "op": "eq", "value": "fulfilled"}]}])
def test_governed_median_plan_is_not_rejected_and_is_exact(env, plan):
    e, db = env
    r = ask(e, QUZ_MM, plan)
    assert r.status == "success" and r.reason_code != "plan_mismatch", r.reason
    rows = {row[0]: row[1].replace(NB, " ") for row in r.rendered["blocks"]["tables"][0]["rows"]}
    s, n, md = sql(db, "SELECT sum(amount_uzs), count(*), median(amount_uzs) FROM deal_facts WHERE status_code = 'fulfilled' "
                       "AND deal_year = 2025")[0]
    assert rows["Bitimning o'rtacha summasi (so'mda)"] == f"{fmt_money(Decimal(s) / n)} so'm"   # same population as the median
    assert rows["Bitim summasining medianasi (so'mda)"] == f"{fmt_money(md)} so'm"
    assert "faqat to'liq bajarilgan bitimlar" in r.rendered["text"]
    txt = r.rendered["text"]
    assert "O'rtacha arifmetik = summa ÷ soni" in txt and "mediana — o'rtadagi qiymat" in txt  # generic explanation, no invented causes


def test_dropping_one_of_two_requested_statistics_is_still_caught(env):
    e, _ = env
    r = ask(e, QUZ_MM, {"intent": "aggregate", "metrics": ["median_deal_value_uzs"], "period": Y2025, "status_policy": "fulfilled"})
    assert r.status == "unsupported" and r.reason_code == "plan_mismatch" and "arithmetic mean" in r.reason
    r2 = ask(e, QUZ_MM, {"intent": "aggregate", "metrics": ["avg_deal_value_uzs"], "period": Y2025, "status_policy": "fulfilled"})
    assert r2.reason_code == "plan_mismatch" and "median" in r2.reason


def test_bot_log_records_executed_custom_sql_for_diagnosis(env):
    e, _ = env
    e.provider = MockProvider(lambda s, u: {"intent": "custom", "custom_sql": DAY_SQL[0], "custom_params": []})
    e.planner.provider = e.provider
    core = BotCore(QueryService(e))
    asyncio.run(core.on_text(79, 1, QRU_DAY))
    assert core.last[(79, 1)]["custom_sql"] and "GROUP BY deal_date" in core.last[(79, 1)]["custom_sql"]


# ---------------------------------------------------------------- commodity comparison over periods (variants + totals)
CMP_COMMODITY = {"intent": "comparison", "metrics": ["weighted_avg_price_uzs", "trade_value_uzs", "quantity"],
                 "entities": [{"kind": "product", "text": "бензин"}], "align_equivalent_periods": False,
                 "compare_periods": [{"start": "2025-01-01", "end": "2025-12-31"}, {"start": "2026-01-01", "end": "2026-12-31"}]}


@pytest.mark.parametrize("q", ["Бензин за весь период на сколько процентов подорожал?",
                               "benzin butun davr bo'yicha necha foizga oshdi?"])
def test_commodity_period_comparison_is_compact_and_keeps_units(env, q):
    e, db = env
    r = ask(e, q, CMP_COMMODITY)
    assert r.status == "success"
    b = r.rendered["blocks"]
    flat = (b["heading"] + " " + " ".join(" ".join(row) for t in b["tables"] for row in t["rows"])).replace(NB, " ")
    # no unknown units, no placeholder labels, counts never used for kilograms/litres
    for bad in ("/ 1 ?", "за 1 ?", "None", "Ko'rsatkich 1", "Показатель 1"):
        assert bad not in flat and bad not in r.rendered["text"], bad
    # the heading is a short summary, the per-variant rows live only in tables (one table per metric)
    assert len(b["heading"].splitlines()) <= 1 + len([f for f in r.facts if f.name.endswith("__unit_total")
                                                      and f.group.get("period_label") == "p1"])
    assert all(t["caption"] for t in b["tables"]) and len(b["tables"]) == 3
    qty = b["tables"][2]  # tables follow the plan's metric order: price, value, quantity
    for row in qty["rows"]:
        if row[3] != "—":
            unit = row[3].replace(NB, " ").split(" ")[-1]
            assert unit in ("л", "т", "litr", "tonna", "l", "t"), row
    # change facts carry the metric unit, and totals over all variants get an exact p2 − p1 change
    for f in r.facts:
        if f.name.endswith("__change") and not f.name.startswith(("trade_value", "deal_count")):
            assert f.unit and "?" not in f.unit and f.unit != "None", (f.name, f.unit)
    totals = [f for f in r.facts if f.name.endswith("__unit_total") ]
    for t in totals:
        other = {k: v for k, v in t.group.items() if k != "period_label"}
        if t.group["period_label"] != "p1":
            continue
        t2 = next((x for x in totals if x.name == t.name and x.group == {**other, "period_label": "p2"}), None)
        ch = next((x for x in r.facts if x.name == f"{t.name}__change" and x.group == other), None)
        assert (ch is None) == (t2 is None or t2.value is None or t.value is None)  # a change exists exactly when both sides do
        if ch is None:
            continue
        assert Decimal(str(ch.value)) == Decimal(str(t2.value)) - Decimal(str(t.value))
    # Telegram text: same numbers, compact "p1 → p2 (%)" lines instead of repeating the period headers per row
    assert "→" in r.rendered["text"]


def test_commodity_comparison_total_matches_independent_sql(env):
    e, db = env
    r = ask(e, "benzin narxi qancha o'zgardi?", CMP_COMMODITY)
    con = duckdb.connect(str(db), read_only=True)
    try:
        for f in r.facts:
            if f.name != "weighted_avg_price_uzs__unit_total":
                continue
            p = r.evidence.compare_periods[f.group["period_label"]]["effective"]
            num, den = con.execute(
                "SELECT sum(amount_uzs), sum(quantity_native) FROM deal_facts WHERE commodity_key = ? AND quantity_unit = ? "
                "AND deal_date BETWEEN ? AND ? AND status_code <> 'cancelled_unpaid'",
                [f.group["commodity_key"], f.group["quantity_unit"], p[0], p[1]]).fetchone()
            assert Decimal(str(f.value)) == (Decimal(num) / Decimal(den)).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
    finally:
        con.close()


def test_several_products_in_one_question_mean_any_of_them(env):
    """'benzin va paxta' / 'бензин и масло': a deal has one product, so the products are OR-ed (never AND -> 0 rows)."""
    e, db = env
    plan = {"intent": "count", "metrics": ["deal_count"],
            "entities": [{"kind": "product", "text": "бензин"}, {"kind": "product", "text": "масло подсолнечное"}]}
    r = ask(e, "benzin va kungaboqar yog'i bo'yicha bitimlar soni", plan)
    con = duckdb.connect(str(db), read_only=True)
    try:
        each = [con.execute("SELECT count(*) FROM deal_facts WHERE commodity_key = ? AND status_code <> 'cancelled_unpaid'",
                            [n["ids"][0]]).fetchone()[0] for n in r.evidence.normalization if n["kind"] == "product"]
    finally:
        con.close()
    assert len(each) == 2 and all(c > 0 for c in each)
    assert r.status == "success" and "several_entities_of_one_kind_combined_with_or" in r.evidence.notes
    total = next(f for f in r.facts if f.name == "deal_count")
    assert total.value == sum(each)  # union of two disjoint products, not their (always empty) intersection


def test_participants_still_combine_with_and(env):
    e, _ = env
    plan = {"intent": "count", "metrics": ["deal_count"],
            "entities": [{"kind": "participant", "text": "Gamma Logistic"}, {"kind": "participant", "text": "Zeta Holding"}]}
    r = ask(e, "Gamma Logistic va Zeta Holding o'rtasidagi bitimlar", plan)
    assert "several_entities_of_one_kind_combined_with_or" not in r.evidence.notes


# ---------------------------------------------------------------- product names follow the answer language
CYR = re.compile(r"[А-Яа-яЁё]")
PETROL_BY_VARIANT = {"intent": "aggregate", "metrics": ["deal_count", "weighted_avg_price_uzs"],
                     "entities": [{"kind": "product", "text": "бензин"}]}


def test_uzbek_answer_names_products_in_uzbek(env):
    e, _ = env
    r = ask(e, "benzin bo'yicha bitimlar soni va o'rtacha narx", PETROL_BY_VARIANT)
    assert r.status == "success"
    b = r.rendered["blocks"]
    labels = [row[0] for t in b["tables"] for row in t["rows"]]
    assert labels and all(not CYR.search(lb) for lb in labels), labels
    assert any("Benzin" in lb and "AI-92" in lb for lb in labels), labels
    shartlar = next(n for n in b["notes"] if n.startswith("Shartlar"))
    assert "tovar: Benzin" in shartlar and not CYR.search(shartlar)


def test_russian_answer_keeps_original_product_names(env):
    e, _ = env
    r = ask(e, "Количество сделок и средняя цена по бензину", PETROL_BY_VARIANT)
    labels = [row[0] for t in r.rendered["blocks"]["tables"] for row in t["rows"]]
    assert any("Бензин" in lb and "АИ-92" in lb for lb in labels), labels


def test_uzbek_cyrillic_answer_transliterates_product_labels(env):
    e, _ = env
    r = ask(e, "бензин бўйича битимлар сони ва ўртача нарх", PETROL_BY_VARIANT)
    labels = [row[0] for t in r.rendered["blocks"]["tables"] for row in t["rows"]]
    assert any("Бензин" in lb and "АИ-92" in lb for lb in labels), labels
    assert not any(re.search(r"[A-Za-z]{3,}", lb.replace("[", "").replace("]", "")) for lb in labels), labels


def test_comparison_table_moves_a_shared_unit_to_the_caption():
    from uzex_assistant.render import _common_suffix
    same = [["a", "⟦1⟧ so'm / 1 kg", "⟦2⟧ so'm / 1 kg", "+⟦1⟧ so'm / 1 kg", "+⟦100⟧%"], ["b", "⟦3⟧ so'm / 1 kg", "—", "—", "—"]]
    assert _common_suffix(same) == " so'm / 1 kg"
    mixed = [["a", "⟦1⟧ l", "⟦2⟧ l", "+⟦1⟧ l", "x"], ["b", "⟦3⟧ t", "⟦4⟧ t", "+⟦1⟧ t", "x"]]
    assert _common_suffix(mixed) is None  # different units stay in every cell
    assert _common_suffix([["a", "⟦1⟧", "⟦2⟧", "+⟦1⟧", "x"]]) is None  # nothing to move
