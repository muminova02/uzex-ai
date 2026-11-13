"""Accuracy-audit runner for eval/cases_v2.py.

    .venv/bin/python scripts/eval/run_v2.py offline [--split=dev|holdout|all] [--regression]
    .venv/bin/python scripts/eval/run_v2.py live --confirm --ids=A04:ru,H03:uz,...   (spends LLM quota)

offline: the planner is a MockProvider returning the case's typed plan -> tests resolution, semantic checks,
         SQL, arithmetic and rendering; it does NOT measure language understanding.
live:    the production provider + shared quota gate plan the question (one sequential call each).
Every answer is graded on the FINAL plan + evidence + facts (metric, period, roles/ids, filters, top_n,
unit/currency, status/clarification) and on the value against eval/golden_v2.json.
Writes eval/results/v2_<mode>_<ts>.json.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
import os  # noqa: E402
# UZEX_EVAL_SRC: run the same cases against another source tree (e.g. the pre-fix code from the Prompt 7 image)
sys.path.insert(0, os.environ.get("UZEX_EVAL_SRC") or str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cases_v2 import CASES, REGRESSION  # noqa: E402

G = json.loads((ROOT / "eval" / "golden_v2.json").read_text(encoding="utf-8"))["answers"]
BLOCKED = {"llm_rate_limited", "deadline_exhausted", "service_busy"}
REGION_KEYS = {}  # raw region string -> engine region key, filled from the catalog (display only)


def D(x) -> Decimal:
    from decimal import ROUND_HALF_UP
    d = Decimal(str(x))
    return d if d == d.to_integral_value() else d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def facts(res, name, **grp):
    return [f for f in res.facts if f.name == name and all(f.group.get(k) == v for k, v in grp.items())]


def check_value(case, res, cat) -> tuple[bool, str]:
    c = case["check"]
    kind = c[0]
    if kind == "status":
        return True, "status-only"
    ref = G[c[-1]]
    if kind == "scalar":
        fs = [f for f in facts(res, c[1]) if not f.group or set(f.group) <= {"currency"}]
        if len(fs) != 1:
            return False, f"expected one {c[1]} fact, got {[(f.group, f.value) for f in facts(res, c[1])]}"
        return D(fs[0].value) == D(ref), f"{fs[0].value} vs ref {ref}"
    if kind == "scalar_unit":
        cand = facts(res, f"{c[1]}__unit_total") or facts(res, c[1])
        fs = [f for f in cand if f.unit == c[2] or f.group.get("quantity_unit") == c[2] or f.group.get("currency") == c[2]]
        others = [f for f in cand if f not in fs and f.value not in (None, 0, "0")]
        if len(fs) != 1:
            return False, f"no single {c[1]} in {c[2]}: {[(f.group, f.unit, f.value) for f in facts(res, c[1])]}"
        return D(fs[0].value) == D(ref) and not others, f"{fs[0].value} vs ref {ref}; other units {len(others)}"
    if kind == "by_unit":
        got = {}
        for f in facts(res, c[1]):
            u = f.group.get("quantity_unit") or (f.unit or "").split("/")[-1]
            if "product_variant_key" in f.group:
                continue  # per-variant rows; the per-unit totals are checked below
            got[u] = f.value
        for f in facts(res, f"{c[1]}__unit_total"):
            got[f.group.get("quantity_unit")] = f.value
        ok = set(got) == set(ref) and all(D(got[u]) == D(ref[u]) for u in ref)
        return ok, f"{got} vs ref {ref}"
    if kind in ("ranking", "ranking_region"):
        col = c[2] if kind == "ranking" else c[2] + "_key"
        rows = [(f.group.get(col), f.value) for f in facts(res, c[1])]
        want = ref
        if kind == "ranking_region":
            want = [[cat.get(k, k), v] for k, v in ref]
        ok = len(rows) == len(want) and all(str(a) == str(k) and D(b) == D(v) for (a, b), (k, v) in zip(rows, want))
        return ok, f"{rows} vs ref {want}"
    if kind == "compare":
        p = {f.group.get("period_label"): f.value for f in facts(res, c[1])}
        ok = set(ref) <= set(p) and all(D(p[k]) == D(ref[k]) for k in ref)
        return ok, f"{p} vs ref {ref}"
    raise ValueError(kind)


def check_plan(case, res) -> list[str]:
    """Constraint preservation on the FINAL plan and evidence; returns the list of violations."""
    e, ev, plan = case["expect"], res.evidence, res.plan
    bad = []
    if plan is None:
        return ["no plan"]
    if e.get("metric") and e["metric"] not in plan.metrics:
        bad.append(f"metric {e['metric']} not in {plan.metrics}")
    if "period" in e:
        pe = ev.period_effective or {}
        if (str(pe.get("start")), str(pe.get("end"))) != (e["period"]["start"], e["period"]["end"]) and not (
                e["period"]["end"] > "2026-10-30" and str(pe.get("start")) == e["period"]["start"]):
            bad.append(f"period {pe} != {e['period']}")
    elif "compare" in e:
        cp = ev.compare_periods or {}
        got = [tuple(str(x) for x in (v.get("requested") or v.get("effective") or [None, None])) for k, v in sorted(cp.items())] \
            if isinstance(cp, dict) else []
        want = [(p["start"], p["end"]) for p in e["compare"]]
        if got[:2] != want and not all(g[0] == w[0] for g, w in zip(got, want)):
            bad.append(f"compare periods {got} != {want}")
    elif e.get("metric"):
        if plan.period and (plan.period.start or plan.period.end):
            bad.append(f"unexpected period {plan.period}")
    for kind, role, ident in e.get("entities", []):
        hits = [n for n in ev.normalization if n["kind"].startswith(kind) and n["status"] == "resolved"
                and (role is None or n.get("role") == role) and (ident is None or ident in (n.get("ids") or []))]
        if not hits:
            bad.append(f"entity {kind}/{role}/{ident} not resolved as expected: "
                       f"{[(n['kind'], n.get('role'), n.get('ids')) for n in ev.normalization]}")
    for field, val in e.get("filters", {}).items():
        if not any(f["field"] == field and (f["value"] == val or (isinstance(f["value"], list) and val in f["value"])) for f in ev.filters):
            bad.append(f"filter {field}={val} missing: {[(f['field'], f['value']) for f in ev.filters]}")
    if "top_n" in e and (plan.top_n or 10) != e["top_n"]:
        bad.append(f"top_n {plan.top_n} != {e['top_n']}")
    if "group_by" in e and e["group_by"] not in plan.group_by:
        bad.append(f"group_by {plan.group_by} lacks {e['group_by']}")
    if "unit" in e:
        units = {f.unit for f in res.facts if f.name.split("__")[0] == e.get("metric")}
        if not any(u == e["unit"] or (u or "").startswith(e["unit"]) or (u or "").endswith(e["unit"]) for u in units):
            bad.append(f"unit {units} lacks {e['unit']}")
    return bad


def stage(res, plan_bad, value_ok) -> str:
    """Where it went wrong (first failing layer)."""
    if res.status != "success":
        return {"ambiguous": "clarification", "unsupported": "semantic_validation" if res.reason_code == "plan_mismatch" else "routing/intent",
                "missing_field": "routing/intent", "out_of_period": "period", "no_matching_rows": "entity_resolution"}.get(res.status, "execution")
    if plan_bad:
        b = plan_bad[0]
        return ("rendering" if b.startswith("internal label") else "metric" if b.startswith("metric") else "period" if "period" in b else "entity_resolution" if b.startswith("entity")
                else "filters/status" if b.startswith("filter") else "grouping/top_n" if b.startswith(("top_n", "group_by")) else "currency/unit")
    return "ok" if value_ok else "sql/arithmetic"


def grade(case, res, eng, cat, choose_trace) -> dict:
    e = case["expect"]
    out = {"status": res.status, "reason_code": res.reason_code, "reason": res.reason}
    want_status = e.get("status", "success")
    if res.reason_code in BLOCKED or res.reason_code in ("llm_timeout", "llm_unavailable", "llm_unexpected_error"):
        out.update(outcome="blocked")
        return out
    if res.reason_code == "llm_bad_output":  # the plan does not validate: the meaning is not expressible
        out.update(outcome="incorrect", stage="plan_not_expressible")
        return out
    if e.get("clarify") and res.status == "ambiguous":
        out["clarified"] = res.reason_code
        res = choose_trace(res)
        out["after_choice"] = {"status": res.status, "reason_code": res.reason_code}
        reasons = [t.get("reason_code") for t in choose_trace.__defaults__[-1] if "reason_code" in t]
        if len(reasons) != len(set(reasons)):  # the user was asked the same question again after answering it
            out.update(outcome="incorrect", stage="clarification_loop", clarifications=reasons)
            return out
    elif e.get("clarify"):
        out["clarification_missing"] = True
    if want_status != "success":
        ok = res.status == want_status and (not e.get("reason") or res.reason_code == e["reason"])
        out.update(outcome="correct" if ok else "incorrect", stage="ok" if ok else "routing/intent")
        return out
    pb = check_plan(case, res) if res.status == "success" else []
    vok, vmsg = check_value(case, res, cat) if res.status == "success" and "check" in case else (res.status == "success", "")
    grounded = (res.rendered or {}).get("grounded", False)
    out.update(plan_violations=pb, value_ok=vok, value=vmsg, grounded=grounded, text=(res.rendered or {}).get("text", "")[:500])
    import re as _re
    leaked = _re.findall(r"\b[a-z]+(?:_[a-z]+)*__[a-z_]+", (res.rendered or {}).get("text", ""))  # internal names shown to the user
    if leaked:
        pb = pb + [f"internal label in the answer: {leaked[:3]}"]
        out["plan_violations"] = pb
    ok = res.status == "success" and not pb and vok and grounded and not out.get("clarification_missing")
    out["outcome"] = "correct" if ok else ("refused" if res.status in ("unsupported", "ambiguous", "missing_field") else "incorrect")
    if out.get("clarification_missing") and res.status == "success" and not pb and vok:
        out["outcome"] = "correct_without_clarification"
    out["stage"] = stage(res, pb, vok)
    return out


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "offline"
    args = dict(a.lstrip("-").split("=", 1) if "=" in a else (a.lstrip("-"), True) for a in sys.argv[2:])
    from uzex_assistant.config import get_settings
    from uzex_assistant.engine import Budget, Engine
    from uzex_assistant.llm import MockProvider
    s = get_settings()
    cur: dict = {}
    if mode == "offline":
        eng = Engine(s, provider=MockProvider(lambda sys_, user: {"language": cur["lang"], **cur["plan"]}))
    elif mode == "live":
        if not args.get("confirm"):
            print(__doc__)
            print("Refusing to spend quota without --confirm.")
            return 2
        eng = Engine(s)
        assert eng.planner is not None and eng.provider.name != "mock", "production provider required"
    else:
        print(__doc__)
        return 2
    cat = {}
    for key, r in eng.catalog.regions.items():
        for raw in r.get("raw", []) or []:
            cat[raw] = key
        cat[r.get("uz", key)] = key
    split = args.get("split", "all")
    items = []
    if mode == "live":
        want = [x.split(":") for x in str(args["ids"]).split(",")]
        by = {c["id"]: c for c in CASES + REGRESSION}
        items = [(by[i], lang) for i, lang in want]
    else:
        pool = REGRESSION if args.get("regression") else [c for c in CASES if split in ("all", c["split"])]
        items = [(c, lang) for c in pool for lang in ("ru", "uz") if lang in c]
    rows = []
    for case, lang in items:
        q = case[lang]
        cur.update(lang=lang, plan=case["plan"])
        t0 = time.perf_counter()
        res = eng.answer(q)
        ms = round((time.perf_counter() - t0) * 1000)
        trace = []

        def choose(r, case=case, q=q, trace=trace):
            want = case["expect"]["choose"]
            for _ in range(3):  # chained clarifications: each choice applied, then the final answer
                if r.status != "ambiguous":
                    return r
                cands = r.clarification.candidates
                pick = next((c for c in cands if want == "city" and c.get("region_key") == "tashkent_city"), None) or \
                    next((c for c in cands if c.get("option") == want), None)
                if pick is None:
                    trace.append({"no_matching_option": [str(c)[:80] for c in cands]})
                    return r
                plan, skip = eng.apply_choice(r.plan, r.reason_code, pick, r.clarification.entity_text)
                trace.append({"reason_code": r.reason_code, "picked": {k: v for k, v in pick.items() if k != "label"}})
                r = eng.run_plan(plan, q, Budget(30), skip_checks=skip | set(trace[0].get("skip", [])))
            return r
        g = grade(case, res, eng, cat, choose)
        llm = res.llm or {}
        rows.append({"id": case["id"], "split": case.get("split", "regression"), "lang": lang, "question": q, "ms": ms,
                     "handled": "llm" if llm.get("provider") and not llm.get("local") else "local",
                     "model": llm.get("model"), "llm_ms": llm.get("elapsed_ms"), "tokens": (llm.get("usage") or {}).get("total_tokens"),
                     "rate_limit_waits_s": llm.get("rate_limit_waits_s"), "choice_trace": trace,
                     "plan": res.plan.model_dump(mode="json", exclude_none=True) if (res.plan and mode == "live") else None, **g})
        print(f"{case['id']} {lang} {g['outcome']:12s} {g.get('stage', '')} {g['status']} {g.get('reason_code') or ''} {ms} ms"
              + ("" if g["outcome"] == "correct" else f"  | {g.get('plan_violations') or ''} {g.get('value', '')[:200]} {g.get('reason') or ''}"))
    eng.close()
    summ = {k: sum(r["outcome"] == k for r in rows) for k in ("correct", "correct_without_clarification", "incorrect", "refused", "blocked")}
    import uzex_assistant
    out = {"run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "mode": mode, "split": split,
           "source_tree": str(Path(uzex_assistant.__file__).parent),
           "planner": "MockProvider(fixed case plans)" if mode == "offline" else f"{s.llm_provider}:{s.llm_model}",
           "summary": summ, "n": len(rows), "rows": rows}
    tag = ("regression" if args.get("regression") else split) + (f"_{args['tag']}" if args.get("tag") else "")
    p = ROOT / "eval" / "results" / f"v2_{mode}_{tag}_{out['run_at'].replace(':', '')}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summ), "n=", len(rows), "saved", p.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
