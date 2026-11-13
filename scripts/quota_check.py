"""Will the LLM quota cover judging?  Offline by default (no API call).

    python scripts/quota_check.py --judges 5 --questions 15            # offline estimate
    python scripts/quota_check.py --judges 5 --questions 15 --probe    # + ONE tiny real call (~100 tokens)

Inputs: the shared gate file (UZEX_LLM_GATE_PATH): tokens this deployment spent in the last 24 h, the learned
tokens/question and TPM limit, and the last limit the provider reported (TPD is only reported in a 429).
The account balance / billing tier is NOT exposed by the API: check it in the provider console by hand.
Prints no secrets. Exit code 0 = fits, 1 = does not fit (or the probe failed), 2 = usage error.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judges", type=int, required=True)
    ap.add_argument("--questions", type=int, required=True, help="questions per judge")
    ap.add_argument("--tpd", type=int, default=None, help="daily token limit if known (default: last reported, else 200000)")
    ap.add_argument("--safety", type=float, default=1.5, help="factor for clarifications, repairs and retries")
    ap.add_argument("--probe", action="store_true", help="make ONE tiny real call through the shared gate")
    a = ap.parse_args()
    from uzex_assistant.config import get_settings
    from uzex_assistant.rategate import usage_last_24h
    s = get_settings()
    gate = Path(s.llm_gate_path) if s.llm_gate_path else None
    u = usage_last_24h(gate) if gate and gate.is_file() else {"tokens": 0, "calls": 0, "ledger": "no gate file"}
    last = json.loads(u["last_limit"]) if u.get("last_limit") else None
    per_q = int(float(u.get("estimate") or 2950))
    tpm = int(u.get("tpm_limit") or 8000)
    tpd = int(a.tpd or ((last or {}).get("limit") if (last or {}).get("kind") == "TPD" else None) or 200000)
    need = math.ceil(a.judges * a.questions * per_q * a.safety)
    left = tpd - u["tokens"]
    out = {"provider": s.llm_provider, "model": s.llm_model, "tokens_per_question": per_q, "tpm_limit": tpm, "tpd_limit": tpd,
           "used_last_24h_by_this_deployment": u["tokens"], "calls_last_24h": u["calls"], "ledger": u.get("ledger"),
           "last_reported_limit": last, "needed_for_judging": need, "left_by_ledger": left,
           "fits": need <= left, "questions_per_minute_max": round(tpm / per_q, 1),
           "note": ("Ledger counts only calls made through this gate file since it was created; usage from other "
                    "machines or keys is invisible. TPD is a rolling 24 h window. Check the billing/limits page "
                    "of the provider console by hand: the API does not report balance.")}
    if a.probe:
        from uzex_assistant.llm import LLMError, make_provider
        from uzex_assistant.rategate import SharedRateGate
        prov = make_provider(s)
        if gate:
            prov = SharedRateGate(prov, gate)
        schema = {"type": "object", "properties": {"ok": {"type": "string"}}, "required": ["ok"], "additionalProperties": False}
        t0 = time.perf_counter()
        try:
            r = prov.generate_json("Reply with JSON.", 'Return {"ok": "yes"}.', schema, 20)
            h = r.rate_limit_headers or {}
            out["probe"] = {"ok": True, "ms": round((time.perf_counter() - t0) * 1000), "served_by": r.served_by,
                            "usage": r.usage, "remaining_requests_today": h.get("x-ratelimit-remaining-requests"),
                            "remaining_tokens_this_minute": h.get("x-ratelimit-remaining-tokens")}
        except LLMError as e:
            out["probe"] = {"ok": False, "kind": e.kind, "limit": (e.meta or {}).get("limit"),
                            "wait_s": (e.meta or {}).get("last_wait_hint_s")}
            if (e.meta or {}).get("limit", {}) and e.meta["limit"].get("kind") == "TPD":
                out["fits"] = False
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0 if out["fits"] and out.get("probe", {"ok": True})["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
