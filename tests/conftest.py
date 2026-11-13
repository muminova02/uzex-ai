"""Test-wide safety: tests must never write into the production question journal (data/logs/questions.jsonl).
Tests that exercise the journal enable it explicitly with their own temporary path."""
import os

os.environ["UZEX_QUESTION_LOG_ENABLED"] = "false"
try:
    from uzex_assistant.config import get_settings
    get_settings.cache_clear()
except Exception:
    pass
