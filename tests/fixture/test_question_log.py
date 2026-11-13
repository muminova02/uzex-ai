"""Question journal: records real questions (web + Telegram) without slowing or breaking answers. No network."""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from uzex_assistant.config import Settings
from uzex_assistant.engine import Engine
from uzex_assistant.llm import MockProvider
from uzex_assistant.qlog import QuestionLog, _journals
from uzex_assistant.web.app import create_app

from .engine_fixture import build
from .test_bot import Planner


def _read(path: Path, n: int, timeout: float = 3.0) -> list[dict]:
    end = time.time() + timeout
    while time.time() < end:
        if path.exists():
            lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            if len(lines) >= n:
                return [json.loads(l) for l in lines]
        time.sleep(0.02)
    raise AssertionError(f"journal {path} has fewer than {n} lines")


def test_web_questions_are_journaled_without_cookie_values(tmp_path):
    data, db = build(tmp_path / "fx")
    log = tmp_path / "q" / "questions.jsonl"
    s = Settings(_env_file=None, data_dir=data, db_path=db, llm_gate_path=None, question_log=log, question_log_enabled=True)
    eng = Engine(Settings(_env_file=None, data_dir=data, db_path=db, sql_workers=1, request_budget_s=60, llm_gate_path=None),
                 provider=MockProvider(Planner()))
    try:
        with TestClient(create_app(s, eng)) as c:
            r = c.post("/api/chat/ask", json={"question": "Сколько сделок в 2025 году?"}).json()
            cookie = next(iter(c.cookies.values()))
        rec = _read(log, 1)[-1]
        assert rec["ch"] == "web" and rec["q"] == "Сколько сделок в 2025 году?" and rec["status"] == r["status"] == "success"
        assert rec["kind"] == "answer" and rec["lang"] and rec["query_id"] and rec["ms"] >= 0 and rec["answer_head"]
        assert rec["ip"] and len(rec["session"]) == 10
        assert cookie not in log.read_text(encoding="utf-8")  # only a hash of the session, never the cookie
    finally:
        eng.close()


def test_journal_write_is_off_the_request_path_and_never_raises(tmp_path):
    ql = QuestionLog(Settings(_env_file=None, db_path=tmp_path / "x.duckdb", question_log=tmp_path / "j.jsonl", question_log_enabled=True))
    t0 = time.perf_counter()
    for i in range(500):
        ql.write("telegram", {"user": i}, f"savol {i}", "answer", {"status": "success"}, 12.3)
    per_call_ms = (time.perf_counter() - t0) * 1000 / 500
    assert per_call_ms < 1.0, per_call_ms  # queued; the file is written by a background thread
    assert len(_read(tmp_path / "j.jsonl", 500)) == 500
    ql.write("web", {}, "x", None, {"blocks": object()}, 1)  # malformed input: swallowed, never raised
    off = QuestionLog(Settings(_env_file=None, db_path=tmp_path / "y.duckdb", question_log_enabled=False))
    assert off.lg is None
    off.write("web", {}, "x", None, None, 1)  # disabled: a no-op


def test_unwritable_journal_path_does_not_break_startup(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    ql = QuestionLog(Settings(_env_file=None, db_path=tmp_path / "z.duckdb", question_log=blocker / "q.jsonl", question_log_enabled=True))
    assert ql.lg is None
    ql.write("web", {}, "x", None, None, 1)
    assert str(blocker / "q.jsonl") not in _journals
