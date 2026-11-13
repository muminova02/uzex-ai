"""Local web server in DEMO mode for browser verification WITHOUT any real LLM call.

The planner is a scripted mock (answers are labelled "TEST REJIMI / ТЕСТОВЫЙ РЕЖИМ" by the bot core);
everything else (dashboard, explorer, export, help, explanations, evidence) uses the real prepared DB.
    .venv/bin/python scripts/web_demo.py        -> http://127.0.0.1:8000  (UZEX_DEMO_PORT overrides)
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import re  # noqa: E402

import uvicorn  # noqa: E402

from uzex_assistant.config import get_settings  # noqa: E402
from uzex_assistant.engine import Engine  # noqa: E402
from uzex_assistant.importer import ensure_database  # noqa: E402
from uzex_assistant.llm import LLMError, MockProvider  # noqa: E402
from uzex_assistant.web.app import create_app  # noqa: E402

Y25 = {"start": "2025-01-01", "end": "2025-12-31"}
DEMO = [
    (r"2025.*(nechta|сколько)", {"intent": "count", "metrics": ["deal_count"], "period": Y25}),
    (r"(ташкент|toshkent)", {"intent": "count", "metrics": ["deal_count"], "period": Y25,
                             "entities": [{"kind": "region", "text": "Toshkent"}]}),
    (r"(sotuvchi|продавц)", {"intent": "ranking", "metrics": ["trade_value_uzs", "deal_count"], "group_by": ["seller"],
                             "top_n": 5, "period": Y25}),
    (r"387831", {"intent": "lookup", "entities": [{"kind": "deal", "text": "387831"}]}),
]


def planner(system, user):
    q = re.search(r"<question>\n(.*)\n</question>", user, re.S).group(1).lower()
    for pat, plan in DEMO:
        if re.search(pat, q):
            return plan
    if "limit-test" in q or "лимит-тест" in q:  # UI check of the provider-quota state (no real provider involved)
        e = LLMError("rate_limited", "demo: simulated provider rate limit")
        e.meta = {"limit": {"kind": "TPM"}, "last_wait_hint_s": 42.0}
        raise e
    raise LLMError("bad_output", "demo planner has no scripted plan for this question")


if __name__ == "__main__":
    s = get_settings()
    ensure_database(s.data_dir, s.db_path, s.as_of)
    eng = Engine(s, provider=MockProvider(planner, model="demo-mock-planner"))
    uvicorn.run(create_app(s, eng), host="127.0.0.1", port=int(os.environ.get("UZEX_DEMO_PORT", "8000")), log_level="warning")
