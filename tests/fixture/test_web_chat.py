"""Web chat on the synthetic fixture with a MOCKED planner: sessions, clarification, evidence isolation,
provider unavailability. No network, no real LLM."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from uzex_assistant.config import Settings
from uzex_assistant.engine import Engine
from uzex_assistant.llm import LLMError, MockProvider
from uzex_assistant.rategate import RateGate
from uzex_assistant.web.app import create_app

from .test_bot import Planner


@pytest.fixture(scope="module")
def app_engine(tmp_path_factory):
    from .engine_fixture import build
    data, db = build(tmp_path_factory.mktemp("web"))
    planner = Planner()
    eng = Engine(Settings(_env_file=None, data_dir=data, db_path=db, sql_workers=2, request_budget_s=60, llm_gate_path=None),
                 provider=MockProvider(planner))
    app = create_app(Settings(_env_file=None, data_dir=data, db_path=db, llm_gate_path=None), eng)
    with TestClient(app) as a, TestClient(app) as b:   # two browsers = two cookie jars
        yield a, b, eng, planner
    eng.close()


def test_answer_with_evidence_scoped_to_session(app_engine):
    a, b, eng, _ = app_engine
    r = a.post("/api/chat/ask", json={"question": "Сколько сделок в 2025 году?"}).json()
    assert r["kind"] == "answer" and r["status"] == "success" and "Количество сделок: 9." in r["text"] and "ТЕСТОВЫЙ РЕЖИМ" in r["text"]
    ev = a.get(f"/api/chat/evidence/{r['evidence_id']}")
    assert ev.status_code == 200 and ev.json()["source_row_count"] == 9 and ev.json()["queries"]
    assert b.get(f"/api/chat/evidence/{r['evidence_id']}").status_code == 404     # other browser session
    assert TestClient(a.app).get(f"/api/chat/evidence/{r['evidence_id']}").status_code == 404  # no cookie
    assert a.get("/api/chat/evidence/guessed-id").status_code == 404


def test_clarification_choice_and_isolation(app_engine):
    a, b, eng, planner = app_engine
    n0 = len(planner.calls)
    r = a.post("/api/chat/ask", json={"question": "Сколько сделок в Ташкенте?"}).json()
    assert r["options"][0]["label"] == "1) г. Ташкент" and r["sid"]
    stolen = b.post("/api/chat/choose", json={"sid": r["sid"], "index": 0}).json()
    assert stolen["kind"] == "notice" and "другого пользователя" in stolen["text"]
    ok = a.post("/api/chat/choose", json={"sid": r["sid"], "index": 0}).json()
    assert "Количество сделок: 1." in ok["text"] and ok["evidence_id"]
    assert len(planner.calls) == n0 + 1   # the choice re-used the stored plan (no LLM call)
    again = a.post("/api/chat/choose", json={"sid": r["sid"], "index": 0}).json()
    assert again["kind"] == "notice"


def test_follow_up_explanation_and_help_are_local(app_engine):
    a, _, eng, planner = app_engine
    a.post("/api/chat/ask", json={"question": "Сколько сделок в 2025 году?"})
    n0 = len(planner.calls)
    e = a.post("/api/chat/ask", json={"question": "Объясни этот результат"}).json()
    assert e["kind"] == "explain" and "Количество сделок = 9" in e["text"]
    h = a.post("/api/chat/ask", json={"question": "nimalarni so'ray olaman"}).json()
    assert h["kind"] == "help" and "Nimalarni qila olaman" in h["text"]
    assert len(planner.calls) == n0


def test_dashboard_context_is_visible_in_the_sent_question(app_engine):
    a, _, _, _ = app_engine
    from .test_bot import PLANS
    # the attached context is validated and clipped to data coverage (this fixture starts 2025-03-10)
    q = "Сколько сделок в 2025 году? (Контекст: 2025-03-10–2025-12-31; склад: г. Ташкент)"
    PLANS[q] = PLANS["Сколько сделок в 2025 году?"]
    r = a.post("/api/chat/ask", json={"question": "Сколько сделок в 2025 году?", "context_lang": "ru",
                                       "context": {"start": "2025-01-01", "end": "2025-12-31", "region": "tashkent_city"}}).json()
    assert r["question_sent"] == q
    bad = a.post("/api/chat/ask", json={"question": "x", "context": {"region": "moon"}})
    assert bad.status_code == 422


def test_dashboard_usable_while_provider_rate_limited(app_engine):
    a, _, eng, planner = app_engine
    saved = eng.provider
    gate = RateGate(MockProvider(planner))
    gate.cooldown_until = 10 ** 12
    gate.last_limit = {"kind": "TPD", "limit": 200000, "used": 199999, "requested": 2976}
    eng.provider = eng.planner.provider = gate
    try:
        m = a.get("/api/meta").json()["assistant"]
        assert m["cooldown_s"] > 0 and m["limit_kind"] == "TPD"
        for url in ("/api/analytics/overview", "/api/analytics/trend", "/api/deals?size=3", "/api/filters/options"):
            assert a.get(url).status_code == 200
        r = a.post("/api/chat/ask", json={"question": "Сколько сделок в 2026 году?"}).json()
        assert "временно недоступен" in r["text"] and "дневной лимит токенов" in r["text"] and r["status"] == "execution_error"
        assert "Количество сделок" not in r["text"]
    finally:
        eng.provider = eng.planner.provider = saved


def test_db_failure_is_an_error_not_empty(app_engine):
    a, _, eng, _ = app_engine
    from uzex_assistant.executor import QueryOutcome
    real = eng.executor.execute
    eng.executor.execute = lambda *x, **k: QueryOutcome(False, error_kind="db_error", error="boom")
    try:
        r = a.get("/api/analytics/overview", params={"start": "2025-03-11"})   # not cached
        assert r.status_code == 500 and r.json()["error"] == "db_error" and "boom" not in r.text
        r = a.get("/api/deals", params={"start": "2025-03-12"})
        assert r.status_code == 500
    finally:
        eng.executor.execute = real


def test_security_headers_and_no_sql_endpoint(app_engine):
    a, _, _, _ = app_engine
    r = a.get("/api/meta")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert a.post("/api/sql", json={"sql": "select 1"}).status_code in (404, 405)
    paths = {p for p in a.app.openapi()["paths"]}
    assert not any("sql" in p for p in paths)


def test_demo_mode_is_reported(app_engine):
    a, _, _, _ = app_engine   # this app runs with a MockProvider -> demo
    assert a.get("/api/meta").json()["mode"] == "demo"


def test_production_settings_cannot_select_scripted_planner(tmp_path):
    from uzex_assistant.llm import make_provider
    # "mock" is not a production provider: it yields no planner at all (never the scripted demo planner)
    assert make_provider(Settings(_env_file=None, llm_provider="mock")) is None
    import inspect

    import uzex_assistant.web.__main__ as wm
    src = inspect.getsource(wm)
    assert "MockProvider" not in src and "Engine(" not in src or "create_app(s)" in src


def test_health_and_build_identity(app_engine):
    c = app_engine[0]
    if True:
        h = c.get("/api/health")
        assert h.status_code == 200 and h.json()["ok"] and h.json()["prompt_version"]
        m = c.get("/api/meta").json()
        assert m["build"]["build_id"] and m["build"]["prompt_version"] == h.json()["prompt_version"]


def test_region_choice_settles_every_occurrence(app_engine):
    a, _b, _eng, planner = app_engine
    r = a.post("/api/chat/ask", json={"question": "Toshkentda omborli nechta bitim bor?"}).json()
    assert r["status"] == "ambiguous" and r["options"]
    n = len(planner.calls)
    city = next(o["index"] for o in r["options"] if "shahri" in o["label"])
    ok = a.post("/api/chat/choose", json={"sid": r["sid"], "index": city}).json()
    assert "Aniqlashtiring" not in ok["text"] and ok["status"] == "success", ok["text"]
    assert len(planner.calls) == n
