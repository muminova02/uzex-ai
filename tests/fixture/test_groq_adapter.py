"""Groq adapter with a FAKE SDK client (no network). Live Groq tests are in tests/live/."""
from __future__ import annotations

import json

import groq
import httpx
import pytest

from uzex_assistant.config import Settings
from uzex_assistant.llm import GroqProvider, LLMError, make_provider, parse_wait, transport_schema
from uzex_assistant.plan import QueryPlan, llm_json_schema
from uzex_assistant.planner import Planner

PLAN = {"intent": "count", "metrics": ["deal_count"]}


def _http(status, headers=None):
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return httpx.Response(status, headers=headers or {}, request=req)


def rate_limit(headers):
    return groq.RateLimitError("rate limited", response=_http(429, headers), body=None)


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content, finish="stop"):
        self.message, self.finish_reason = _Msg(content), finish


class _Completion:
    def __init__(self, content, finish="stop", model="openai/gpt-oss-120b"):
        self.choices, self.model, self.id = [_Choice(content, finish)], model, "chatcmpl-1"
        self.usage = type("U", (), {"model_dump": lambda self: {"prompt_tokens": 10, "completion_tokens": 5}})()


class _Raw:
    def __init__(self, completion, headers):
        self._c, self.headers = completion, httpx.Headers(headers)

    def parse(self):
        return self._c


class FakeGroq:
    """Mimics client.with_options(...).chat.completions.with_raw_response.create(...) and models.retrieve."""

    def __init__(self, script, model_info=None):
        self.script, self.calls, self.timeouts = list(script), [], []
        self.model_info = model_info
        self.chat = self
        self.completions = self
        self.with_raw_response = self
        self.models = self

    def with_options(self, **kw):
        self.timeouts.append(kw.get("timeout"))
        return self

    def create(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def list(self):
        if isinstance(self.model_info, Exception):
            raise self.model_info
        infos = self.model_info if isinstance(self.model_info, list) else [self.model_info]
        return type("L", (), {"data": [type("M", (), {"model_dump": lambda s, i=i: i})() for i in infos]})()


def provider(script, model_info=None, **kw):
    sleeps = []
    s = Settings(_env_file=None, llm_provider="groq", llm_api_key="gsk_test_not_real", llm_model="openai/gpt-oss-120b", **kw)
    p = GroqProvider(s, sleep=sleeps.append)
    p.client = FakeGroq(script, model_info)
    return p, sleeps


def ok(content=json.dumps(PLAN), headers=None, **kw):
    return _Raw(_Completion(content, **kw), headers or {"x-ratelimit-remaining-tokens": "7000"})


# ------------------------------------------------------------------ request shape
def test_request_shape_strict_schema_no_anthropic_options():
    p, _ = provider([ok()])
    out = p.generate_json("SYS", "<question>q</question>", llm_json_schema(), timeout_s=20)
    kw = p.client.calls[0]
    assert out.data == PLAN and out.provider == "groq" and out.served_by == "openai/gpt-oss-120b"
    assert kw["model"] == "openai/gpt-oss-120b"
    assert kw["messages"][0] == {"role": "system", "content": "SYS"}
    rf = kw["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True and rf["json_schema"]["name"] == "query_plan"
    assert kw["reasoning_effort"] == "low" and kw["include_reasoning"] is False and kw["temperature"] == 0
    for anthropic_only in ("output_config", "betas", "fallbacks", "system", "thinking", "cache_control", "max_tokens"):
        assert anthropic_only not in kw
    assert "cache_control" not in json.dumps(kw, ensure_ascii=False)
    assert out.rate_limit_headers == {"x-ratelimit-remaining-tokens": "7000"} and out.attempts == 1


def test_transport_schema_keeps_constraints():
    full = llm_json_schema()
    t = transport_schema(full)
    assert "description" not in json.dumps(t)
    # same required fields, enums and additionalProperties everywhere
    def walk(a, b):
        if isinstance(a, dict):
            for k in a:
                if k == "description":
                    continue
                assert k in b
                walk(a[k], b[k])
        elif isinstance(a, list):
            for x, y in zip(a, b):
                walk(x, y)
        else:
            assert a == b
    walk(full, t)
    assert t["additionalProperties"] is False and set(t["required"]) == set(t["properties"])


def test_pydantic_validation_still_applies_after_parse():
    bad = {**PLAN, "metrics": ["deal_count"], "intent": "drop_tables"}
    p, _ = provider([ok(json.dumps(bad))])
    with pytest.raises(LLMError) as e:
        Planner(p, "SYS").plan("q", timeout_s=10)
    assert e.value.kind == "bad_output"


def test_planner_accepts_valid_plan():
    p, _ = provider([ok()])
    plan, resp = Planner(p, "SYS").plan("q", timeout_s=10)
    assert isinstance(plan, QueryPlan) and plan.intent == "count" and resp.served_by == "openai/gpt-oss-120b"


# ------------------------------------------------------------------ 429 handling
def test_429_waits_retry_after_then_succeeds():
    p, sleeps = provider([rate_limit({"retry-after": "2"}), ok()])
    out = p.generate_json("S", "U", {}, timeout_s=20)
    assert sleeps == [2.0] and out.attempts == 2 and out.rate_limit_waits_s == [2.0]


def test_429_uses_reset_header_when_no_retry_after():
    p, sleeps = provider([rate_limit({"x-ratelimit-reset-tokens": "1.5s"}), ok()])
    p.generate_json("S", "U", {}, timeout_s=20)
    assert sleeps == [1.5]


def test_429_wait_beyond_deadline_fails_fast():
    p, sleeps = provider([rate_limit({"retry-after": "45"}), ok()])
    with pytest.raises(LLMError) as e:
        p.generate_json("S", "U", {}, timeout_s=20)
    assert e.value.kind == "rate_limited" and sleeps == [] and e.value.meta["last_wait_hint_s"] == 45.0
    assert len(p.client.calls) == 1


def test_429_bounded_attempts():
    p, sleeps = provider([rate_limit({"retry-after": "1"})] * 5, llm_max_attempts=3)
    with pytest.raises(LLMError) as e:
        p.generate_json("S", "U", {}, timeout_s=30)
    assert e.value.kind == "rate_limited" and len(p.client.calls) == 3 and sleeps == [1.0, 1.0]


def test_per_call_timeout_shrinks_with_deadline():
    p, _ = provider([rate_limit({"retry-after": "0"}), ok()])
    p.generate_json("S", "U", {}, timeout_s=12)
    assert all(t <= 12 for t in p.client.timeouts) and len(p.client.timeouts) == 2


def test_parse_wait_formats():
    assert parse_wait({"retry-after": "3"}) == 3.0
    assert parse_wait({"x-ratelimit-reset-tokens": "2m59.5s"}) == 179.5
    assert parse_wait({"x-ratelimit-reset-requests": "120ms"}) == 0.12
    assert parse_wait({}) is None and parse_wait(None) is None


# ------------------------------------------------------------------ other errors
@pytest.mark.parametrize("item, kind", [
    (ok(finish="length"), "truncated"),
    (ok("not json"), "bad_output"),
    (ok(""), "bad_output"),
    (groq.BadRequestError("bad", response=_http(400), body={"error": {"code": "invalid_request"}}), "api_error"),
    (groq.AuthenticationError("no", response=_http(401), body=None), "api_error"),
    (groq.NotFoundError("no", response=_http(404), body=None), "model_unavailable"),
])
def test_error_mapping(item, kind):
    p, _ = provider([item])
    with pytest.raises(LLMError) as e:
        p.generate_json("S", "U", {}, timeout_s=10)
    assert e.value.kind == kind


def test_5xx_retried_once_then_succeeds():
    p, sleeps = provider([groq.InternalServerError("x", response=_http(503), body=None), ok()])
    assert p.generate_json("S", "U", {}, timeout_s=10).attempts == 2 and sleeps == [1.0]


# ------------------------------------------------------------------ model availability and config
def test_verify_model_available():
    p, _ = provider([], {"id": "openai/gpt-oss-120b", "active": True, "owned_by": "OpenAI", "context_window": 131072})
    assert p.verify_model()["id"] == "openai/gpt-oss-120b"


@pytest.mark.parametrize("info", [
    [],
    {"id": "openai/gpt-oss-120b", "active": False},
    [{"id": "openai/gpt-oss-20b", "active": True}, {"id": "openai/gpt-oss-120b-preview", "active": True}],
])
def test_verify_model_unavailable_never_substitutes(info):
    p, _ = provider([], info)
    with pytest.raises(LLMError) as e:
        p.verify_model()
    assert e.value.kind == "model_unavailable" and p.model == "openai/gpt-oss-120b"


def test_verify_model_auth_error():
    p, _ = provider([], groq.AuthenticationError("no", response=_http(401), body=None))
    with pytest.raises(LLMError) as e:
        p.verify_model()
    assert e.value.kind == "api_error"


@pytest.mark.parametrize("kw, kind", [
    ({"llm_api_key": None}, "not_configured"),
    ({"llm_model": "claude-opus-5-5"}, "config_error"),     # default Anthropic model is not silently used
    ({"llm_effort": "max"}, "config_error"),
])
def test_config_errors(kw, kind):
    base = {"llm_provider": "groq", "llm_api_key": "gsk_x", "llm_model": "openai/gpt-oss-120b"}
    with pytest.raises(LLMError) as e:
        make_provider(Settings(_env_file=None, **{**base, **kw}))
    assert e.value.kind == kind


def test_ambient_groq_variables_ignored(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "ambient")
    monkeypatch.setenv("GROQ_BASE_URL", "https://evil.example")
    p = GroqProvider(Settings(_env_file=None, llm_provider="groq", llm_api_key="gsk_explicit", llm_model="openai/gpt-oss-120b"))
    assert str(p.client.base_url).startswith("https://api.groq.com") and p.client.api_key == "gsk_explicit"


def test_key_not_in_repr_or_errors():
    s = Settings(_env_file=None, llm_provider="groq", llm_api_key="gsk_secret_value_123", llm_model="openai/gpt-oss-120b")
    assert "gsk_secret_value_123" not in repr(s)
    p, _ = provider([groq.AuthenticationError("no", response=_http(401), body=None)])
    with pytest.raises(LLMError) as e:
        p.generate_json("S", "U", {}, timeout_s=5)
    assert "gsk_" not in str(e.value)


def _schema_fail():
    return groq.BadRequestError("bad", response=_http(400), body={"error": {"code": "json_validate_failed",
                                "message": "'/currency' expected string, but got null"}})


def test_schema_failure_regenerated_once_then_succeeds():
    p, sleeps = provider([_schema_fail(), ok()])
    out = p.generate_json("S", "U", {}, timeout_s=20)
    assert out.attempts == 2 and sleeps == [] and len(p.client.calls) == 2


def test_schema_failure_only_one_regeneration():
    p, _ = provider([_schema_fail(), _schema_fail(), ok()], llm_max_attempts=3)
    with pytest.raises(LLMError) as e:
        p.generate_json("S", "U", {}, timeout_s=20)
    assert e.value.kind == "bad_output" and len(p.client.calls) == 2 and e.value.meta["schema_retry_used"] is True


def test_null_defaults_apply_but_invalid_values_still_fail():
    from uzex_assistant.planner import parse_plan
    plan = parse_plan({"intent": "count", "currency": None, "language": None, "align_equivalent_periods": None,
                       "entities": [{"kind": "region", "text": "Namangan", "role": None}], "metrics": ["deal_count"]})
    assert plan.currency == "UZS" and plan.language == "ru" and plan.align_equivalent_periods is True
    assert plan.entities[0].role == "any"
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        parse_plan({"intent": "count", "currency": "EUR"})
    with pytest.raises(ValidationError):
        parse_plan({"intent": None})


def test_anthropic_schema_keeps_few_unions_and_plans_still_parse():
    """live 2026-11-05: Claude rejected the plan schema (24 union-typed parameters) with HTTP 400."""
    from uzex_assistant.llm import anthropic_schema, count_unions, transport_schema
    from uzex_assistant.plan import llm_json_schema
    from uzex_assistant.planner import parse_plan
    full = transport_schema(llm_json_schema())
    claude = anthropic_schema(llm_json_schema())
    assert count_unions(full) >= 20 and count_unions(claude) <= 3
    assert "period" not in claude["required"] and "intent" in claude["required"]
    ent = claude["properties"]["entities"]["items"]
    assert ent["required"] == ["kind", "text"] and ent["properties"]["role"]["type"] == "string"
    # a Claude-shaped answer that omits optional fields validates to the same plan as the null-filled one
    a = parse_plan({"intent": "count", "metrics": ["deal_count"], "entities": [{"kind": "region", "text": "Toshkent"}],
                    "compare_periods": [], "filters": [], "group_by": [], "missing_fields": [], "custom_params": []})
    b = parse_plan({"intent": "count", "metrics": ["deal_count"], "entities": [{"kind": "region", "text": "Toshkent",
                    "catalog_hint": None, "role": None, "brand": None, "grade": None, "product_class": None, "size": None}],
                    "period": None, "compare_periods": [], "filters": [], "group_by": [], "order": None, "top_n": None,
                    "currency": None, "status_policy": None, "missing_fields": [], "custom_params": [], "language": None,
                    "align_equivalent_periods": None, "unsupported_reason": None, "clarification": None,
                    "custom_sql": None, "llm_confidence": None})
    assert a == b
