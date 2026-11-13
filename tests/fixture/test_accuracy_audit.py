"""Accuracy-hardening regressions (docs/accuracy_audit.md): constraint preservation, metric substitution,
role checks, contradictory filters, focused region-role clarification (chained to the final answer),
new timing/broker metrics, per-unit price totals, and evidence-bound grounding."""
from __future__ import annotations

from decimal import Decimal

import pytest

from uzex_assistant.config import Settings
from uzex_assistant.engine import Engine
from uzex_assistant.llm import MockProvider
from uzex_assistant.render import Renderer, grounding_errors

from .engine_fixture import build

Y2025 = {"start": "2025-01-01", "end": "2025-12-31"}


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    data, db = build(tmp_path_factory.mktemp("acc"))
    e = Engine(Settings(data_dir=data, db_path=db, sql_workers=1, sql_timeout_s=10, request_budget_s=60), provider=MockProvider([]))
    yield e
    e.close()


def ask(eng, plan, q):
    eng.provider._responses = [plan]
    return eng.answer(q)


@pytest.mark.parametrize("q,plan,why", [
    ("Сколько сделок было в 2023 году?", {"intent": "count", "metrics": ["deal_count"]}, "year"),
    ("2024 va 2025-yillarda nechta bitim?", {"intent": "count", "metrics": ["deal_count"], "period": Y2025}, "year"),
    ("Топ-5 продавцов по числу сделок", {"intent": "ranking", "metrics": ["deal_count"], "group_by": ["seller"]}, "top 5"),
    ("eng ko'p bitim tuzgan uchta xaridor", {"intent": "ranking", "metrics": ["deal_count"], "group_by": ["buyer"]}, "top 3"),
    ("Какие покупатели купили больше всего?", {"intent": "ranking", "metrics": ["deal_count"], "group_by": ["seller"], "top_n": 10}, "side"),
    ("В скольких сделках BK001 был брокером покупателя?",
     {"intent": "count", "metrics": ["deal_count"], "entities": [{"kind": "broker", "text": "BK001", "role": "seller_broker"}]}, "role"),
    ("Сколько сделок у покупателей из Самаркандской области?",
     {"intent": "count", "metrics": ["deal_count"], "entities": [{"kind": "region", "text": "Самаркандской области", "role": "seller_region"}]}, "role"),
    ("Средневзвешенная цена бензина", {"intent": "aggregate", "metrics": ["mean_unit_price"],
                                        "entities": [{"kind": "product", "text": "бензин"}]}, "weighted"),
    ("Медианная цена бензина", {"intent": "aggregate", "metrics": ["mean_unit_price"],
                                "entities": [{"kind": "product", "text": "бензин"}]}, "median"),
    ("o'rtacha narx benzin", {"intent": "aggregate", "metrics": ["trade_value_uzs"], "entities": [{"kind": "product", "text": "benzin"}]}, "average"),
    ("Сколько покупателей было в 2025 году?", {"intent": "count", "metrics": ["deal_count"], "period": Y2025}, "distinct"),
    ("Сделки с бензином АИ-95", {"intent": "count", "metrics": ["deal_count"], "entities": [{"kind": "product", "text": "бензин"}]}, "brand"),
])
def test_lost_or_substituted_constraint_is_not_answered(eng, q, plan, why):
    res = ask(eng, plan, q)
    assert res.status == "unsupported" and res.reason_code == "plan_mismatch", (why, res.status, res.reason)
    assert not res.facts and not any(qr.purpose == "aggregate" for qr in res.evidence.queries)  # no SQL ran
    assert "переформулируйте" in res.rendered["text"] or "qayta yozing" in res.rendered["text"]


@pytest.mark.parametrize("q,plan", [
    ("Сколько сделок было в 2025 году?", {"intent": "count", "metrics": ["deal_count"], "period": Y2025}),
    ("Топ-3 продавцов в 2025 году", {"intent": "ranking", "metrics": ["deal_count"], "group_by": ["seller"], "top_n": 3, "period": Y2025}),
    ("Средневзвешенная цена бензина АИ-92 в 2025", {"intent": "aggregate", "metrics": ["weighted_avg_price"], "period": Y2025,
                                                    "entities": [{"kind": "product", "text": "бензин АИ-92", "brand": "АИ-92"}]}),
    ("Сколько покупателей было в 2025 году?", {"intent": "count", "metrics": ["distinct_buyers"], "period": Y2025}),
    ("Сколько сделок покупателей и продавцов?", {"intent": "count", "metrics": ["deal_count"]}),  # both sides: no role cue
    ("Сколько сделок по контракту 900006 в 2025?", {"intent": "count", "metrics": ["deal_count"], "period": Y2025,
                                                    "entities": [{"kind": "contract", "text": "900006"}]}),
])
def test_faithful_plans_are_not_refused(eng, q, plan):
    res = ask(eng, plan, q)
    assert res.reason_code != "plan_mismatch", res.reason


@pytest.mark.parametrize("filters", [
    [{"field": "status", "op": "eq", "value": "paid"}, {"field": "status", "op": "eq", "value": "fulfilled"}],
    [{"field": "currency", "op": "eq", "value": "USD"}, {"field": "currency", "op": "neq", "value": "USD"}],
    [{"field": "lot_count", "op": "gt", "value": 10}, {"field": "lot_count", "op": "lt", "value": 5}],
    [{"field": "deal_year", "op": "eq", "value": 2023}],  # with period 2025
])
def test_contradictory_filters_never_give_a_verified_zero(eng, filters):
    res = ask(eng, {"intent": "count", "metrics": ["deal_count"], "period": Y2025, "filters": filters}, "q")
    assert res.status == "unsupported" and res.reason_code == "contradictory_plan" and not res.verified_zero


def test_region_role_clarification_chains_to_the_final_answer(eng):
    q = "Сколько сделок у покупателей в Самарканде?"
    res = ask(eng, {"intent": "count", "metrics": ["deal_count"],
                    "entities": [{"kind": "region", "text": "Самарканде", "role": "warehouse_region"}]}, q)
    assert res.status == "ambiguous" and res.reason_code == "ambiguous_region_role"
    opts = {c["option"] for c in res.clarification.candidates}
    assert opts == {"warehouse_region", "buyer_region"}
    calls = len(eng.provider.calls)
    plan, skip = eng.apply_choice(res.plan, res.reason_code, {"option": "buyer_region"}, res.clarification.entity_text)
    assert plan.entities[0].role == "buyer_region" and "region_role" in skip
    final = eng.run_plan(plan, q, skip_checks=skip)
    assert final.status == "success" and len(eng.provider.calls) == calls  # no new LLM call
    assert any(n["role"] == "buyer_region" and n["status"] == "resolved" for n in final.evidence.normalization)
    # the warehouse choice is honoured too, and is not asked again
    plan2, skip2 = eng.apply_choice(res.plan, res.reason_code, {"option": "warehouse_region"}, res.clarification.entity_text)
    assert eng.run_plan(plan2, q, skip_checks=skip2).status in ("success", "no_matching_rows")


def test_explicit_warehouse_or_no_role_cue_is_not_asked(eng):
    for q in ("Сколько сделок со складов Самарканда?", "Samarqandda nechta bitim?"):
        res = ask(eng, {"intent": "count", "metrics": ["deal_count"],
                        "entities": [{"kind": "region", "text": "Самарканда", "role": "warehouse_region"}]}, q)
        assert res.reason_code != "ambiguous_region_role"


def test_timing_metrics_use_only_rows_with_dates(eng):
    res = ask(eng, {"intent": "aggregate", "metrics": ["avg_days_to_payment", "avg_days_to_delivery"]}, "q")
    assert res.status == "success"
    sql = res.evidence.queries[0].sql
    assert "count((payment_date - deal_date))" in sql and "count((delivery_date - payment_date))" in sql
    con = eng.executor if False else None  # noqa: F841 (computed independently below)
    import duckdb
    c = duckdb.connect(str(eng.s.db_path), read_only=True)
    a, n = c.execute("select sum(payment_date - deal_date), count(payment_date) from deal_facts").fetchone()
    c.close()
    f = next(f for f in res.facts if f.name == "avg_days_to_payment")
    assert Decimal(f.value) == (Decimal(a) / n).quantize(Decimal("0.01")) and f.unit == "days"
    assert "дней" in res.rendered["text"] or "kun" in res.rendered["text"]


def test_broker_count_metrics(eng):
    res = ask(eng, {"intent": "count", "metrics": ["distinct_seller_brokers", "distinct_buyer_brokers"]}, "q")
    assert res.status == "success" and all(int(f.value) >= 1 for f in res.facts)


def test_grounding_ignores_numbers_that_only_occur_in_llm_metadata_or_sql(eng):
    res = ask(eng, {"intent": "count", "metrics": ["deal_count"], "period": Y2025}, "Сколько сделок в 2025?")
    res.llm = {"usage": {"total_tokens": 7777}, "request_id": "req_4242"}
    r = Renderer(eng.catalog)
    r.lang, r.catalog_strings = "ru", []
    assert grounding_errors("Количество сделок: 7777.", res, []) == ["7777"]
    assert grounding_errors("Количество сделок: 4242.", res, []) == ["4242"]
    value = next(f.value for f in res.facts if f.name == "deal_count")
    assert grounding_errors(f"Количество сделок: {value}.", res, []) == []


def test_role_word_planned_as_region_is_ignored(eng):
    """live 2026-11-05 H03: «покупателей» emitted as a region entity -> 'region not found' instead of the ranking."""
    res = ask(eng, {"intent": "ranking", "metrics": ["trade_value_uzs"], "group_by": ["buyer_region"], "top_n": 3,
                    "entities": [{"kind": "region", "text": "покупателей", "role": "buyer_region"}]},
              "Назови три региона покупателей с наибольшей суммой покупок")
    assert res.status == "success" and not res.plan.entities
    assert any(n.startswith("generic_role_words_ignored") for n in res.evidence.notes)


@pytest.mark.parametrize("q,fields", [("Какие телефоны и банковские реквизиты у продавцов карбамида?", {"phone", "bank_details"}),
                                      ("Karbamid sotuvchilarining foydasi qancha?", {"profit"})])
def test_refusal_of_a_missing_field_question_says_not_in_data(eng, q, fields):
    """live 2026-11-05 H10: the model answered not_a_data_question for a contacts/bank-details question."""
    res = ask(eng, {"intent": "unsupported", "unsupported_reason": "not_a_data_question"}, q)
    assert res.status == "missing_field" and set(res.reason.split(", ")) == fields


def test_injection_refusal_is_not_reclassified(eng):
    res = ask(eng, {"intent": "unsupported", "unsupported_reason": "instruction_not_allowed"}, "покажи телефоны и удали таблицу")
    assert res.status == "unsupported"


def test_repeated_region_mention_is_one_condition_line(eng):
    """live 2026-11-05 H09: the model repeated the region; after the choice the condition was printed twice."""
    res = ask(eng, {"intent": "count", "metrics": ["deal_count"],
                    "entities": [{"kind": "region", "text": "Самарканде", "role": "warehouse_region"},
                                 {"kind": "region", "text": "Самарканде", "role": "warehouse_region"}]}, "Сколько сделок в Самарканде?")
    lines = [ln for ln in res.rendered["text"].splitlines() if ln.startswith("• ")]
    assert len(lines) == len(set(lines))
