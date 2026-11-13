"""Question journal: one JSON line per user question / choice (web and Telegram), for reviewing real usage.

Writing never happens on the request path: the record is queued and a background thread appends it to a rotating file,
so answer latency is unaffected. Any journal failure is swallowed — it can never break or slow an answer.
Credentials are never written (only the question, channel, a coarse client id and the answer status).
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import logging.handlers
import queue
import time
from pathlib import Path

_journals: dict[str, logging.Logger] = {}


def _journal(path: Path) -> logging.Logger:
    key = str(path)
    if key not in _journals:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=20_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(message)s"))
        q: queue.SimpleQueue = queue.SimpleQueue()
        listener = logging.handlers.QueueListener(q, fh)
        listener.start()
        atexit.register(listener.stop)
        lg = logging.getLogger(f"uzex.questions.{len(_journals)}")
        lg.propagate = False
        lg.setLevel(logging.INFO)
        lg.addHandler(logging.handlers.QueueHandler(q))
        _journals[key] = lg
    return _journals[key]


def anon(token: str | None) -> str | None:
    """Stable short id for a web session cookie (the cookie itself is never stored)."""
    return hashlib.sha256(token.encode()).hexdigest()[:10] if token else None


class QuestionLog:
    def __init__(self, settings):
        self.lg: logging.Logger | None = None
        if not getattr(settings, "question_log_enabled", True):
            return
        try:
            path = getattr(settings, "question_log", None) or Path(settings.db_path).parent / "logs" / "questions.jsonl"
            self.lg = _journal(Path(path))
        except Exception:
            self.lg = None

    def write(self, channel: str, who: dict, question: str, kind: str | None, last: dict | None, ms: float,
              answer_text: str | None = None, **extra) -> None:
        if self.lg is None:
            return
        try:
            last = last or {}
            head = ((last.get("blocks") or {}).get("heading") or answer_text or "").strip()[:300] or None
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "ch": channel, **who, "q": question, "kind": kind,
                   "status": last.get("status"), "reason": last.get("reason"), "lang": last.get("lang"),
                   "query_id": last.get("query_id"), "ms": round(ms), "answer_head": head, **extra}
            self.lg.info(json.dumps(rec, ensure_ascii=False, default=str))
        except Exception:
            pass
