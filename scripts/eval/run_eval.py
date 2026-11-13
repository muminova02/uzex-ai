"""Prompt 6 evaluation runner (layers 2-3 + robustness + mock judging round). MOCKED planner only:
no network LLM call is made here. Expected values come from eval/golden_v1.json (scripts/eval/reference.py).

    .venv/bin/python scripts/eval/run_eval.py            -> eval/results/eval_<ts>.json + summary on stdout
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import platform
import random
import re
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import CASES  # noqa: E402

from uzex_assistant.bot_core import BotCore  # noqa: E402
from uzex_assistant.config import get_settings  # noqa: E402
from uzex_assistant.engine import Engine  # noqa: E402
from uzex_assistant.llm import LLMError, MockProvider  # noqa: E402
from uzex_assistant.rategate import RateGate  # noqa: E402
from uzex_assistant.render import NB, fmt_num  # noqa: E402
from uzex_assistant.service import QueryService  # noqa: E402

GOLD = json.loads((ROOT / "eval" / "golden_v1.json").read_text(encoding="utf-8"))["answers"]
ABSTAIN = {"ambiguous", "unsupported", "missing_field", "out_of_period", "no_matching_rows", "execution_error"}


def variants():
    for c in CASES:
        for lang in ("ru", "uz", "cyrl"):
            if c.get(lang):
                yield c, lang, c[lang]


PLAN_BY_Q = {q: c["plan"] for c, _, q in variants()}


class ScriptedPlanner:
    """Mock planner: returns the case's typed plan; honours timeout_s like the real adapter; optional delay/failure."""

    def __init__(self, delay=0.0, fail=None):
        self.delay, self.fail, self.calls = delay, fail, 0

    def __call__(self, system, user):
        raise RuntimeError("use generate_json")


class TimedMock(MockProvider):
    def __init__(self, delay=0.0, fail=None):
        super().__init__(self._plan)
        self.delay, self.fail, self.n = delay, fail, 0

    def _plan(self, system, user):
        q = re.search(r"<question>\n(.*)\n</question>", user, re.S).group(1)
        q = re.sub(r" \((Контекст|Kontekst):.*\)$", "", q)
        return PLAN_BY_Q[q]

    def generate_json(self, system, user, schema, timeout_s):
        self.n += 1
        if self.delay:
            time.sleep(min(self.delay, timeout_s))
            if self.delay > timeout_s:
                raise LLMError("timeout", "mock planner exceeded the timeout")
        if self.fail:
            raise self.fail
        return super().generate_json(system, user, schema, timeout_s)


D2 = lambda x: Decimal(str(x)).quantize(Decimal("0.01"))  # noqa: E731


def facts(res, name):
    return [f for f in res.facts if f.name == name]


def check(case, res) -> tuple[bool, str]:
    kind, *a = case["check"]
    try:
        if kind == "status":
            return res.status in a[0], f"status={res.status} reason={res.reason_code}"
        if res.status != "success":
            return False, f"status={res.status} reason={res.reason_code}"
        if kind == "fact":
            f = facts(res, a[0])
            return (len(f) == 1 and int(f[0].value) == int(GOLD[a[1]])), f"got={[x.value for x in f]} exp={GOLD[a[1]]}"
        if kind == "forms":
            got = {f.group["contract_form_code"]: f.value for f in facts(res, "deal_count")}
            return got == GOLD[a[0]], f"got={got}"
        if kind == "statuses":
            got = {f.group["status"]: f.value for f in facts(res, "deal_count")}
            return got == GOLD[a[0]], f"got={got}"
        if kind == "deal":
            g, fs = GOLD[a[0]], {f.name: f.value for f in res.facts}
            ok = (fs["quantity_native"], fs["delivered_quantity"], fs["remaining_quantity"], fs["quantity_unit"]) == (
                g["quantity"], g["delivered"], g["remaining"], g["unit"]) and D2(fs["unit_price"]) == D2(g["unit_price"])
            return ok, f"qty={fs['quantity_native']} price={fs['unit_price']} delivered={fs['delivered_quantity']}"
        if kind == "contract":
            g, fs = GOLD[a[0]], {f.name: f.value for f in res.facts}
            ok = (fs["product_name"], fs["brand"], fs["currency_name"], fs["lot_size"], fs["payment_term_days"],
                  fs["delivery_term_days"], fs["seller_inn"], fs["deal_count"]) == (
                g["product"], g["brand"], g["currency"], g["lot"], g["pay_term"], g["del_term"], g["seller"], g["deals"])
            return ok, f"deals={fs.get('deal_count')} lot={fs.get('lot_size')}"
        if kind == "ranking":
            metric, key, gk = a
            got = [[f.group[key], f.value] for f in facts(res, metric)]
            exp = GOLD[gk]
            norm = lambda v: str(D2(v)) if isinstance(v, str) else v  # noqa: E731
            return [[k, norm(v)] for k, v in got] == [[k, norm(v)] for k, v in exp], f"got={got}"
        if kind == "count_value":
            g = GOLD[a[0]]
            n, v = facts(res, "deal_count")[0].value, facts(res, "trade_value_uzs")[0].value
            return n == g["deals"] and D2(v) == D2(g["value_uzs_not_cancelled"]), f"n={n} v={v}"
        if kind == "wavg":
            f = facts(res, "weighted_avg_price")
            return len(f) == 1 and D2(f[0].value) == D2(GOLD[a[0]]["price"]) and f[0].unit == "UZS/тонна", f"got={[(x.value, x.unit) for x in f]}"
        if kind == "usd":
            g = GOLD[a[0]]
            u = facts(res, "trade_value_uzs")[0].value
            n = facts(res, "trade_value_native")[0]
            return D2(u) == D2(g["value_uzs"]) and D2(n.value) == D2(g["value_usd"]) and n.unit == "USD", f"uzs={u} usd={n.value}"
        if kind == "compare":
            g = GOLD[a[0]]
            got = {f.group["period_label"]: f.value for f in facts(res, "deal_count")}
            return got == g, f"got={got}"
        if kind == "units":
            got: dict = {}
            for f in facts(res, "quantity"):
                got[f.group["quantity_unit"]] = got.get(f.group["quantity_unit"], 0) + f.value
            totals = {f.group["quantity_unit"]: f.value for f in facts(res, "quantity__unit_total")}
            return got == GOLD[a[0]] and totals == GOLD[a[0]], f"variant_sum={got} unit_totals={totals}"
        if kind == "list":
            g = GOLD[a[0]]
            ids = [r[0] for r in res.table["rows"]]
            total = facts(res, "total_rows")[0].value
            return ids == g["ids"] and total == g["total"], f"ids={ids} total={total}"
    except (KeyError, IndexError, TypeError) as e:
        return False, f"{type(e).__name__}: {e}"
    return False, "unknown check"


def headline(case) -> str | None:
    """A number/name that must appear in the rendered text (formatted like the renderer)."""
    kind, *a = case["check"]
    g = GOLD.get(a[-1]) if kind != "status" else None
    f = lambda x, p=None: fmt_num(x, p).replace(NB, " ")  # noqa: E731
    return {"fact": lambda: f(g), "forms": lambda: f(g["spot"]), "statuses": lambda: f(g["Исполнен"]), "deal": lambda: "22 000",
            "contract": lambda: g["product"], "ranking": lambda: g[0][0], "count_value": lambda: f(g["deals"]),
            "wavg": lambda: f(g["price"], 2), "usd": lambda: f(g["value_usd"], 2), "compare": lambda: f(g["p2"]),
            "units": lambda: f(g["литр"]), "list": lambda: g["ids"][0]}.get(kind, lambda: None)()


def lat(xs):
    xs = sorted(xs)
    if not xs:
        return {}
    p95 = xs[max(0, int(round(0.95 * len(xs))) - 1)]
    return {"n": len(xs), "p50_ms": round(statistics.median(xs)), "p95_ms": round(p95), "max_ms": round(xs[-1]),
            "p95_note": "nearest-rank p95; unreliable for n < 20" if len(xs) < 20 else ""}


async def layer3_and_more(eng, prov, out):
    service = QueryService(eng, max_concurrent=2, max_waiting=20)
    core = BotCore(service)
    # ---- Telegram core: every case through on_text (fresh chat per case: no carried context)
    tg = []
    for i, (c, lang, q) in enumerate(variants()):
        outs = await core.on_text(10_000 + i, 1, q)
        text = "\n".join(o.html for o in outs).replace(NB, " ")
        h = headline(c)
        exp_status = c["check"][1] if c["check"][0] == "status" else ["success"]
        got_status = core.last.get((10_000 + i, 1), {}).get("status") if core.last_kind.get((10_000 + i, 1)) == "answer" else None
        ok = (got_status in exp_status) and (h is None or h in text)
        tg.append({"case": c["id"], "lang": lang, "ok": ok, "status": got_status, "headline_found": h is None or h in text})
    out["layer3_telegram_core"] = {"total": len(tg), "passed": sum(t["ok"] for t in tg), "failures": [t for t in tg if not t["ok"]]}

    # ---- web chat (same engine) through the FastAPI app
    from fastapi.testclient import TestClient

    from uzex_assistant.web.app import create_app
    s = get_settings()
    app = create_app(s, eng)
    web = []
    with TestClient(app) as cl:
        for c, lang, q in variants():
            r = cl.post("/api/chat/ask", json={"question": q}).json()
            h = headline(c)
            exp_status = c["check"][1] if c["check"][0] == "status" else ["success"]
            ok = r.get("status") in exp_status and (h is None or h in r["text"].replace(NB, " "))
            web.append({"case": c["id"], "lang": lang, "ok": ok, "status": r.get("status"), "kind": r.get("kind")})
        out["layer3_web_chat"] = {"total": len(web), "passed": sum(w["ok"] for w in web), "failures": [w for w in web if not w["ok"]]}

        # ---- dashboard / table / export agreement under identical filters
        agree = []
        for params in ({}, {"start": "2025-01-01", "end": "2025-12-31"}, {"commodity": "gasoline", "region": "tashkent_city"},
                       {"policy": "not_cancelled", "currency": "USD", "form": "spot"}, {"commodity": "argon", "start": "2024-03-01", "end": "2024-03-31"}):
            ov = cl.get("/api/analytics/overview", params=params).json()
            tr = cl.get("/api/analytics/trend", params=params).json()
            dl = cl.get("/api/deals", params={**params, "size": 1}).json()
            stt = cl.get("/api/analytics/status", params=params).json()
            ex = cl.get("/api/deals/export.csv", params=params)
            ex_rows = (len(ex.content.decode("utf-8-sig").strip().splitlines()) - 1) if ex.status_code == 200 else f"413 (limit {ex.json()['limit']})"
            vals = [ov["deals"], sum(m["deals"] for m in tr["months"]), dl["total"], stt["denominator"]]
            ok = len(set(vals)) == 1 and (ex_rows == vals[0] if isinstance(ex_rows, int) else vals[0] > 50000)
            agree.append({"filters": params, "kpi": ov["deals"], "trend_sum": vals[1], "table_total": vals[2], "status_denominator": vals[3],
                          "export_rows": ex_rows, "ok": ok})
        out["dashboard_table_export_agreement"] = agree

        # ---- local routes make no LLM call
        before = prov.n
        cl.get("/api/meta"); cl.get("/api/filters/options"); cl.get("/api/analytics/rank?dim=region&limit=14")
        cl.post("/api/chat/ask", json={"question": "nimalarni so'ray olaman"})
        cl.post("/api/chat/ask", json={"question": "Что ты умеешь?"})
        r = cl.post("/api/chat/ask", json={"question": CASES[1]["ru"]}).json()
        mid = prov.n
        cl.get(f"/api/chat/evidence/{r['evidence_id']}")
        e1 = cl.post("/api/chat/ask", json={"question": "Объясни этот результат"}).json()
        e2 = cl.post("/api/chat/ask", json={"question": "bu natijani tushuntir"}).json()
        out["local_routes_llm_calls"] = {"llm_calls_on_meta_options_rank_help": mid - before - 1, "llm_calls_on_evidence_and_explanations": prov.n - mid,
                                         "explain_kinds": [e1["kind"], e2["kind"]]}

    # ---- deadline includes queue time (1 worker, 3 concurrent, planner 3 s, budget 12 s): the third waits ~6 s
    slow = TimedMock(delay=3.0)
    eng.provider = eng.planner.provider = slow
    svc = QueryService(eng, max_concurrent=1, max_waiting=10, budget_s=12.0)
    res = await asyncio.gather(*(svc.ask(CASES[1]["ru"]) for _ in range(3)))
    out["deadline_includes_queue"] = [{"status": r.status, "reason": r.reason_code, "queue_ms": r.timings_ms.get("queue"),
                                       "budget_given_ms": r.timings_ms.get("budget_given"), "service_total_ms": r.timings_ms.get("service_total")}
                                      for r in res]

    # ---- provider failures and rate limits
    fails = {}
    for label, err in (("timeout", LLMError("timeout", "x")), ("api_error", LLMError("api_error", "HTTP 500")),
                       ("bad_output", LLMError("bad_output", "x")), ("unexpected_exception", RuntimeError("boom"))):
        p = TimedMock(fail=err)
        eng.provider = eng.planner.provider = p
        r = eng.answer(CASES[1]["ru"])
        fails[label] = {"status": r.status, "reason": r.reason_code, "facts": len(r.facts),
                        "says_not_missing_data": "не отсутствие данных" in (r.rendered or {}).get("text", "")}
    over = TimedMock(delay=5.0)
    eng.provider = eng.planner.provider = over
    t0 = time.monotonic()
    r = eng.answer(CASES[1]["ru"], budget_s=9.0)  # LLM timeout = budget - 5 s reserve = 4 s < planner 5 s
    fails["planner_slower_than_budget"] = {"status": r.status, "reason": r.reason_code, "elapsed_ms": int((time.monotonic() - t0) * 1000)}
    gate = RateGate(TimedMock())
    gate.cooldown_until = 10 ** 12
    gate.last_limit = {"kind": "TPD"}
    eng.provider = eng.planner.provider = gate
    t0 = time.monotonic()
    r = await QueryService(eng).ask(CASES[1]["ru"])
    fails["rate_limited"] = {"status": r.status, "reason": r.reason_code, "classified_as": "blocked" if r.reason_code == "llm_rate_limited" else "?",
                             "elapsed_ms": int((time.monotonic() - t0) * 1000), "facts": len(r.facts)}
    out["provider_failures"] = fails
    eng.provider = eng.planner.provider = prov


async def judging_round(eng, prov, out, seed=20261105):
    pool = list(variants())
    rnd = random.Random(seed)
    pick = rnd.sample(pool, 10)
    svc = QueryService(eng, max_concurrent=2)
    rows, lats = [], []
    for c, lang, q in pick:
        t0 = time.perf_counter()
        r = await svc.ask(q)
        ms = (time.perf_counter() - t0) * 1000
        ok, detail = check(c, r)
        lats.append(ms)
        rows.append({"case": c["id"], "lang": lang, "kind": c["kind"], "ok": ok, "status": r.status, "ms": round(ms), "detail": detail[:160]})
    t0 = time.perf_counter()
    conc = await asyncio.gather(*(svc.ask(q) for _, _, q in pick))
    wall = (time.perf_counter() - t0) * 1000
    conc_ok = sum(check(c, r)[0] for (c, _, _), r in zip(pick, conc))
    out["mock_judging_round"] = {"seed": seed, "questions": rows, "correct": sum(r["ok"] for r in rows), "latency": lat(lats),
                                 "concurrent_10": {"correct": conc_ok, "wall_ms": round(wall), "max_concurrent": 2},
                                 "note": "mocked planner: measures engine + SQL + rendering, not real LLM latency"}


def main() -> int:
    s = get_settings()
    out: dict = {"run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 "environment": {"machine": platform.machine(), "os": platform.platform(), "python": platform.python_version(),
                                 "cpu": platform.processor()},
                 "golden": "eval/golden_v1.json", "planner": "MOCKED (typed plans from scripts/eval/cases.py); no network LLM call"}
    t0 = time.perf_counter()
    prov = TimedMock()
    eng = Engine(s, provider=prov)
    out["cold_start"] = {"engine_init_ms": round((time.perf_counter() - t0) * 1000)}
    t1 = time.perf_counter()
    first = eng.answer(CASES[0]["ru"])
    out["cold_start"]["first_answer_ms"] = round((time.perf_counter() - t1) * 1000)
    out["cold_start"]["first_answer_status"] = first.status
    # ---------------- layer 2: engine + rendering, every case
    rows, lats = [], []
    for c, lang, q in variants():
        t = time.perf_counter()
        r = eng.answer(q)
        ms = (time.perf_counter() - t) * 1000
        ok, detail = check(c, r)
        rend = r.rendered or {}
        lang_ok = rend.get("lang") == ("uz" if lang in ("uz", "cyrl") else "ru") and (lang != "cyrl" or rend.get("script") == "cyrl")
        h = headline(c)
        text_ok = h is None or h in rend.get("text", "").replace(NB, " ")
        all_ok = ok and lang_ok and rend.get("grounded", False) and text_ok
        lats.append(ms)
        rows.append({"case": c["id"], "lang": lang, "kind": c["kind"], "topic": c["topic"], "ok": all_ok, "facts_ok": ok, "lang_ok": lang_ok,
                     "grounded": rend.get("grounded"), "text_has_value": text_ok, "status": r.status, "abstained": r.status in ABSTAIN and c["kind"] == "positive",
                     "ms": round(ms), "detail": detail[:200]})
    pos = [x for x in rows if x["kind"] == "positive"]
    out["layer2_engine_render"] = {"total": len(rows), "passed": sum(x["ok"] for x in rows), "positive": len(pos),
                                   "positive_correct": sum(x["ok"] for x in pos), "abstained_on_answerable": sum(x["abstained"] for x in pos),
                                   "by_kind": {k: f"{sum(x['ok'] for x in rows if x['kind'] == k)}/{sum(1 for x in rows if x['kind'] == k)}"
                                               for k in ("positive", "no_data", "refusal", "ambiguity")},
                                   "latency": lat(lats), "rows": rows, "failures": [x for x in rows if not x["ok"]]}
    asyncio.run(layer3_and_more(eng, prov, out))
    asyncio.run(judging_round(eng, prov, out))
    # SQL timeout: a runaway query is killed and the pool recovers
    ex = eng.executor
    o = ex.execute("select sum(a.range * b.range) from range(200000000) a, range(100000) b", timeout_s=1.0)
    o2 = ex.execute("select count(*) from deals")
    out["sql_timeout_kill"] = {"first": o.error_kind, "after_kill_ok": o2.ok and o2.rows == [(500000,)], "kills": ex.kills}
    eng.close()
    path = ROOT / "eval" / "results" / f"eval_{out['run_at'].replace(':', '')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    L2 = out["layer2_engine_render"]
    print(f"layer2: {L2['passed']}/{L2['total']} passed; positive {L2['positive_correct']}/{L2['positive']}; abstained {L2['abstained_on_answerable']}; by kind {L2['by_kind']}; latency {L2['latency']}")
    for f in L2["failures"]:
        print("  L2 FAIL", f["case"], f["lang"], f["status"], f["detail"][:150], "lang_ok", f["lang_ok"], "text", f["text_has_value"])
    for k in ("layer3_telegram_core", "layer3_web_chat"):
        print(f"{k}: {out[k]['passed']}/{out[k]['total']}", out[k]["failures"][:6])
    print("agreement:", [(a["kpi"], a["export_rows"], a["ok"]) for a in out["dashboard_table_export_agreement"]])
    print("local routes:", out["local_routes_llm_calls"])
    print("deadline/queue:", out["deadline_includes_queue"])
    print("failures:", json.dumps(out["provider_failures"], ensure_ascii=False))
    print("judging:", out["mock_judging_round"]["correct"], "/10", out["mock_judging_round"]["latency"], out["mock_judging_round"]["concurrent_10"])
    print("cold start:", out["cold_start"], "| sql timeout:", out["sql_timeout_kill"])
    print("saved", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
