"""CLI for the query engine (independent of any Telegram bot).

  python -m uzex_assistant.cli ask "Сколько сделок в 2025 году?"     # uses the configured LLM provider
  python -m uzex_assistant.cli ask "..." --mock-plan plan.json        # scripted MOCK planner (offline)
  python -m uzex_assistant.cli plan examples/plans/count_2025.json    # typed plan, no LLM
  python -m uzex_assistant.cli examples                               # run every plan in examples/plans
  python -m uzex_assistant.cli sql "select count(*) from deal_facts where deal_year = ?" --params '[2025]'
  python -m uzex_assistant.cli resolve product "бензин АИ-92"
Add --json for the full AnswerResult (including evidence).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import PROJECT_ROOT, get_settings
from .engine import AnswerResult, Engine
from .llm import MockProvider
from .plan import EntityRef, QueryPlan
from .sqlguard import SQLRejected

EXAMPLES = PROJECT_ROOT / "examples" / "plans"


def _print(res: AnswerResult, as_json: bool) -> None:
    if as_json:
        print(res.model_dump_json(indent=2))
        return
    ev = res.evidence
    if res.rendered:
        print("----- answer (" + res.rendered["lang"] + "/" + res.rendered["script"] + ", grounded=" + str(res.rendered["grounded"]) + ") -----")
        print(res.rendered["text"])
        print("-----")
    print(f"status: {res.status}" + (f"  reason: {res.reason_code}" if res.reason_code else "")
          + (f" ({res.reason})" if res.reason else ""))
    if res.llm:
        print(f"llm: {res.llm.get('provider')} {res.llm.get('model')} repair_used={res.llm.get('repair_used')}")
    if res.verified_zero:
        print("verified zero: COUNT = 0 over a resolved scope")
    for f in res.facts[:40]:
        g = (" " + json.dumps(f.group, ensure_ascii=False)) if f.group else ""
        print(f"  {f.name} = {f.value} {f.unit or ''}{g}  [q{f.query_index}.{'/'.join(f.columns)} row {f.row_index}, rows_used={f.rows_used}]")
    if len(res.facts) > 40:
        print(f"  ... {len(res.facts) - 40} more facts")
    if res.clarification:
        print("clarify (ru):", res.clarification.question_ru)
        print("clarify (uz):", res.clarification.question_uz)
        for c in res.clarification.candidates[:8]:
            print("   -", json.dumps(c, ensure_ascii=False))
    if res.table and "rows" in res.table:
        print(f"table: {len(res.table['rows'])} rows, truncated={res.table.get('truncated')}"
              + (f", total_groups={res.table.get('total_groups')}" if res.table.get("total_groups") else ""))
        for r in res.table["rows"][:5]:
            print("   ", r)
    print(f"evidence: query_id={ev.query_id} source_rows={ev.source_row_count} period={ev.period_effective or ev.compare_periods}"
          f" samples={ev.sample_ids}")
    if ev.status_policy:
        print("  status policy:", {k: len(v) for k, v in ev.status_policy.items()}, " notes:", ev.notes)
    if ev.truncation:
        print("  truncation:", ev.truncation)
    for n in ev.normalization:
        print(f"  entity: {n['kind']} '{n['text']}' -> {n['status']} via {n['method']} ids={n['ids']}")
    for q in ev.queries:
        print(f"  q{q.query_index} [{q.purpose}] rows={q.row_count} {q.elapsed_ms}ms{' ERROR ' + q.error if q.error else ''}")
    if res.timings_ms:
        print("  timings_ms:", res.timings_ms)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m uzex_assistant.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask"); a.add_argument("question"); a.add_argument("--mock-plan", type=Path); a.add_argument("--json", action="store_true")
    p = sub.add_parser("plan"); p.add_argument("plan"); p.add_argument("--json", action="store_true")
    e = sub.add_parser("examples"); e.add_argument("--json", action="store_true"); e.add_argument("--dir", type=Path, default=EXAMPLES)
    s = sub.add_parser("sql"); s.add_argument("sql"); s.add_argument("--params", default="[]")
    r = sub.add_parser("resolve"); r.add_argument("kind"); r.add_argument("text"); r.add_argument("--role", default="any")
    sub.add_parser("llm-check", help="verify the configured provider/model is available (no substitution)")
    args = ap.parse_args(argv)

    if args.cmd == "llm-check":
        from .llm import LLMError, make_provider
        try:
            prov = make_provider(get_settings())
            if prov is None:
                print("provider: none (UZEX_LLM_PROVIDER not set)")
                return 2
            info = prov.verify_model() if hasattr(prov, "verify_model") else {"id": prov.model, "note": "no check API"}
            print(json.dumps({"provider": prov.name, "configured_model": prov.model, "available": True, **info}, indent=1))
            return 0
        except LLMError as ex:
            print(f"llm-check FAILED: {ex}")
            return 2

    settings = get_settings()
    provider = None
    if args.cmd == "ask" and args.mock_plan:
        scripted = json.loads(args.mock_plan.read_text(encoding="utf-8"))
        scripted.pop("_question", None)
        provider = MockProvider([scripted])
        print("NOTE: mock planner (scripted plan), not a real LLM", file=sys.stderr)
    engine = Engine(settings, provider=provider)
    try:
        if args.cmd == "ask":
            _print(engine.answer(args.question), args.json)
        elif args.cmd == "plan":
            raw = Path(args.plan).read_text(encoding="utf-8") if Path(args.plan).is_file() else args.plan
            data = json.loads(raw)
            question = data.pop("_question", None)
            _print(engine.run_plan(QueryPlan.model_validate(data), question), args.json)
        elif args.cmd == "examples":
            for f in sorted(args.dir.glob("*.json")):
                data = json.loads(f.read_text(encoding="utf-8"))
                q = data.pop("_question", None)
                print(f"\n=== {f.name}: {q}")
                _print(engine.run_plan(QueryPlan.model_validate(data), q), args.json)
        elif args.cmd == "sql":
            params = json.loads(args.params)
            try:
                engine.guard.validate(args.sql, params)
            except SQLRejected as ex:
                print(f"REJECTED: {ex}")
                return 3
            out = engine.executor.execute(args.sql, params)
            print(json.dumps({"ok": out.ok, "columns": out.columns, "rows": out.rows[:20], "truncated": out.truncated,
                              "error": out.error}, ensure_ascii=False, default=str, indent=1))
        elif args.cmd == "resolve":
            res = engine.resolver.resolve(EntityRef(kind=args.kind, text=args.text, role=args.role))
            print(json.dumps(res.__dict__, ensure_ascii=False, default=str, indent=1))
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
