"""Configuration. Values come from environment variables (prefix ``UZEX_``) or a git-ignored ``.env``."""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path

from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Authoritative snapshot date of the dataset (README: statuses as of the end of 30.10.2026).
AS_OF_DATE = dt.date(2026, 10, 30)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="UZEX_", env_file=PROJECT_ROOT / ".env", extra="ignore")

    data_dir: Path = PROJECT_ROOT / "uzex_spot_data"
    db_path: Path = PROJECT_ROOT / "data" / "uzex.duckdb"
    as_of: dt.date = AS_OF_DATE

    # LLM provider (only UZEX_* variables are read; ambient vendor variables are ignored on purpose)
    llm_provider: Literal["none", "anthropic", "groq", "mock"] = "none"
    llm_model: str = "claude-opus-5-5"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None  # None = the provider's official endpoint
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    llm_timeout_s: float = 30.0
    llm_max_tokens: int = 8000
    llm_fallbacks: bool = True  # Anthropic only: server-side refusal fallback ("default" routing)
    llm_gate_path: Path | None = PROJECT_ROOT / "data" / "llm_gate.sqlite"  # shared by web, bot, scripts
    llm_max_attempts: int = 3  # per LLM call, including retries after 429/5xx (bounded by the request deadline)

    # request limits
    request_budget_s: float = 70.0  # total per question (judges allow 90 s)
    sql_timeout_s: float = 20.0
    sql_workers: int = 2
    sql_queue_timeout_s: float = 10.0
    sql_memory_limit: str = "1GB"
    sql_threads: int = 2
    max_result_rows: int = 200
    allow_custom_sql: bool = True
    # Runtime processes (web, bot) never import unless this is set: the import is a separate one-shot step
    # (python -m uzex_assistant.importer ensure). A missing or stale DB stops the runtime with a clear error.
    runtime_import: bool = False
    build_info_path: Path | None = None  # BUILD_INFO.json written at image build; default <root>/BUILD_INFO.json
    tg_rich_tables: bool = False  # Telegram sendRichMessage tables (Bot API rich messages); off until checked in real clients
    bot_heartbeat_path: Path | None = None  # touched every 30 s by the bot for container health checks
    # Submission freeze: after this moment the import CLI will not ingest changed data, and once FREEZE.json
    # exists (scripts/freeze.py) runtimes refuse a different build/prompt. Override only with organizer approval.
    freeze_at: dt.datetime = dt.datetime(2026, 11, 5, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=5)))
    allow_after_freeze: bool = False
    freeze_path: Path | None = None  # default: <db dir>/FREEZE.json

    # web server (FastAPI + built frontend, same origin)
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # Telegram bot
    bot_token: SecretStr | None = None  # UZEX_BOT_TOKEN (never logged)
    question_log_enabled: bool = True  # UZEX_QUESTION_LOG_ENABLED: journal of questions (web + Telegram), written off the request path
    question_log: Path | None = None  # UZEX_QUESTION_LOG; default <db dir>/logs/questions.jsonl
    bot_max_concurrent: int = 2  # questions processed at once (all chats)
    bot_max_waiting: int = 20  # questions allowed to wait in the global queue
    bot_session_ttl_s: float = 600.0  # clarification sessions expire after 10 min
    bot_context_ttl_s: float = 1800.0  # "explain this result" works for 30 min after an answer


@lru_cache
def get_settings() -> Settings:
    return Settings()
