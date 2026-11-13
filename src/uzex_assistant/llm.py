"""LLM provider adapter.

``LLMProvider.generate_json`` returns one JSON object that conforms to a JSON schema. The engine
validates it again with pydantic and never trusts it beyond the typed plan.

Providers:
* ``anthropic`` – official ``anthropic`` SDK, Messages API with structured outputs
  (``output_config.format`` = json_schema), explicit ``effort``, explicit timeout, and server-side
  refusal fallback (``fallbacks: "default"``, beta ``server-side-fallback-2026-07-01``) unless
  disabled. The API key and base URL come only from ``UZEX_LLM_API_KEY`` / ``UZEX_LLM_BASE_URL``
  (``.env``). They are passed explicitly so ambient ``ANTHROPIC_*`` variables of other tools are
  not picked up. The key is never logged.
* ``groq`` – official ``groq`` SDK, Chat Completions with strict JSON-Schema structured outputs
  (``response_format`` json_schema, ``strict: true``), ``reasoning_effort``, explicit timeout. No
  Anthropic options are sent. 429/5xx are retried at most ``llm_max_attempts`` times, waiting
  ``Retry-After`` (or the ``x-ratelimit-reset-*`` hint), never beyond the caller's deadline.
  The model must be set explicitly and is never substituted; ``verify_model`` checks availability.
* ``mock`` – scripted responses for tests and offline CLI runs. Never presented as a real provider.
"""
from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import Settings

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    def __init__(self, kind: str, detail: str = ""):
        # kind: not_configured|config_error|model_unavailable|timeout|rate_limited|api_error|refusal|truncated|bad_output
        self.kind, self.detail = kind, detail
        self.meta: dict = {}
        super().__init__(f"{kind}: {detail}" if detail else kind)


@dataclass
class LLMResponse:
    data: dict
    provider: str
    model: str
    elapsed_ms: int
    usage: dict = field(default_factory=dict)
    request_id: str | None = None
    served_by: str | None = None
    attempts: int = 1
    rate_limit_waits_s: list = field(default_factory=list)
    rate_limit_headers: dict = field(default_factory=dict)
    defaults_applied: list = field(default_factory=list)


class LLMProvider(Protocol):
    name: str
    model: str

    def generate_json(self, system: str, user: str, schema: dict, timeout_s: float) -> LLMResponse: ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings):
        import anthropic  # optional dependency, imported lazily

        if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value():
            raise LLMError("not_configured", "UZEX_LLM_API_KEY is not set (.env)")
        self._anthropic = anthropic
        self.model = settings.llm_model
        self.effort = settings.llm_effort
        self.max_tokens = settings.llm_max_tokens
        self.fallbacks = settings.llm_fallbacks
        self.client = anthropic.Anthropic(api_key=settings.llm_api_key.get_secret_value(),
                                          base_url=settings.llm_base_url or "https://api.anthropic.com", max_retries=1,
                                          timeout=settings.llm_timeout_s)

    def generate_json(self, system: str, user: str, schema: dict, timeout_s: float) -> LLMResponse:
        a = self._anthropic
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": anthropic_schema(schema)}},
        )
        if self.fallbacks:
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        t0 = time.monotonic()
        try:
            resp = self.client.with_options(timeout=max(1.0, timeout_s), max_retries=0 if timeout_s < 20 else 1) \
                .beta.messages.create(**kwargs)
        except a.APITimeoutError as e:
            raise LLMError("timeout", str(e)) from e
        except a.RateLimitError as e:
            raise LLMError("rate_limited", str(e)) from e
        except a.AuthenticationError as e:
            raise LLMError("api_error", "authentication failed") from e
        except a.APIStatusError as e:
            raise LLMError("api_error", f"HTTP {e.status_code}: {getattr(e, 'message', '')}"[:300]) from e
        except a.APIConnectionError as e:
            raise LLMError("api_error", f"connection error: {e}") from e
        ms = int((time.monotonic() - t0) * 1000)
        if resp.stop_reason == "refusal":
            raise LLMError("refusal", str(getattr(resp, "stop_details", "")))
        if resp.stop_reason == "max_tokens":
            raise LLMError("truncated", "max_tokens reached")
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), None)
        if text is None:
            raise LLMError("bad_output", "no text block")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError("bad_output", f"invalid JSON: {e}") from e
        usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else {}
        return LLMResponse(data, self.name, self.model, ms, usage, getattr(resp, "_request_id", None), resp.model)


def anthropic_schema(schema: dict) -> dict:
    """Claude structured outputs limit union-typed parameters (live 2026-11-05: the plan schema had 24 and was
    rejected with HTTP 400 before inference). A nullable field ``anyOf[X, null]`` becomes an OPTIONAL field of type X
    (dropped from ``required``); real unions (filter value, custom params) stay. parse_plan treats a missing key
    exactly like null, and QueryPlan still validates every response."""
    def is_null(n):
        return isinstance(n, dict) and n.get("type") == "null"

    def walk(node):
        if isinstance(node, list):
            return [walk(v) for v in node]
        if not isinstance(node, dict):
            return node
        node = {k: walk(v) for k, v in node.items()}
        props = node.get("properties")
        if isinstance(props, dict):
            req = list(node.get("required", []))
            for k, v in list(props.items()):
                alts = v.get("anyOf") if isinstance(v, dict) else None
                if alts and len(alts) == 2 and any(is_null(a) for a in alts):
                    props[k] = next(a for a in alts if not is_null(a))
                    if k in req:
                        req.remove(k)
            node["required"] = req
        return node
    return walk(transport_schema(schema))


def count_unions(schema) -> int:
    if isinstance(schema, list):
        return sum(count_unions(v) for v in schema)
    if not isinstance(schema, dict):
        return 0
    own = 1 if ("anyOf" in schema or isinstance(schema.get("type"), list)) else 0
    return own + sum(count_unions(v) for v in schema.values())


def transport_schema(schema: dict) -> dict:
    """Schema sent over the wire: same structure and constraints, minus non-validating keywords
    (``description``), to save tokens. QueryPlan (pydantic) still validates every response."""
    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k not in ("description", "title", "examples")}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node
    return strip(copy.deepcopy(schema))


_DUR = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$")


def parse_wait(headers) -> float | None:
    """Seconds to wait from Retry-After, else from Groq's x-ratelimit-reset-* hints ("7.66s", "2m59.5s", "120ms")."""
    if headers is None:
        return None
    ra = headers.get("retry-after")
    if ra:
        try:
            return max(0.0, float(ra))
        except ValueError:
            pass
    waits = []
    for h in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        v = headers.get(h)
        m = _DUR.match(v.strip()) if v else None
        if m and any(m.groups()):
            hh, mm, ss, ms = (float(x) if x else 0.0 for x in m.groups())
            waits.append(hh * 3600 + mm * 60 + ss + ms / 1000)
    return max(waits) if waits else None


_LIMIT_RE = re.compile(r"on (tokens|requests) per (minute|day) \((\w+)\): Limit (\d+), Used (\d+), Requested (\d+)")


def parse_limit_message(exc) -> dict | None:
    """Which provider limit was hit, from the 429 body (Groq states e.g. 'tokens per day (TPD): Limit 200000,
    Used 198315, Requested 2976'). Headers do not expose the daily token limit. No identifiers are kept."""
    body = getattr(exc, "body", None)
    err = body.get("error", body) if isinstance(body, dict) else {}
    msg = err.get("message", "") if isinstance(err, dict) else ""
    m = _LIMIT_RE.search(msg or "")
    if not m:
        return None
    return {"kind": m.group(3), "unit": m.group(1), "window": m.group(2), "limit": int(m.group(4)),
            "used": int(m.group(5)), "requested": int(m.group(6))}


RATE_HEADERS = ("x-ratelimit-limit-requests", "x-ratelimit-limit-tokens", "x-ratelimit-remaining-requests",
                "x-ratelimit-remaining-tokens", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens")


class GroqProvider:
    name = "groq"
    EFFORTS = {"low", "medium", "high"}  # reasoning_effort values of gpt-oss models

    def __init__(self, settings: Settings, sleep: Callable[[float], None] = time.sleep):
        import groq  # optional dependency, imported lazily

        if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value():
            raise LLMError("not_configured", "UZEX_LLM_API_KEY is not set (.env)")
        if not settings.llm_model or settings.llm_model.startswith("claude"):
            raise LLMError("config_error", f"UZEX_LLM_MODEL={settings.llm_model!r} is not a Groq model; set it explicitly")
        if settings.llm_effort not in self.EFFORTS:
            raise LLMError("config_error", f"UZEX_LLM_EFFORT={settings.llm_effort!r} not supported by Groq (low|medium|high)")
        self._groq = groq
        self.model = settings.llm_model
        self.effort = settings.llm_effort
        self.max_tokens = settings.llm_max_tokens
        self.max_attempts = max(1, settings.llm_max_attempts)
        self._sleep = sleep
        self.client = groq.Groq(api_key=settings.llm_api_key.get_secret_value(),
                                base_url=settings.llm_base_url or "https://api.groq.com",
                                max_retries=0, timeout=settings.llm_timeout_s)

    def verify_model(self) -> dict:
        """Confirm the configured model is served to this account (exact id in ``models.list``).

        ``models.retrieve`` is not used: SDK 1.7.0 percent-encodes the "/" in ids such as
        ``openai/gpt-oss-120b`` and the API answers 404 for an available model (observed 2026-11-04).
        Never substitutes another model.
        """
        g = self._groq
        try:
            listed = self.client.models.list().data
        except g.AuthenticationError as e:
            raise LLMError("api_error", "authentication failed") from e
        except g.APIStatusError as e:
            raise LLMError("api_error", f"HTTP {e.status_code}") from e
        except g.APIConnectionError as e:
            raise LLMError("api_error", f"connection error: {e}") from e
        infos = [m.model_dump() if hasattr(m, "model_dump") else dict(m) for m in listed]
        info = next((i for i in infos if i.get("id") == self.model), None)
        if info is None:
            raise LLMError("model_unavailable", f"{self.model} is not in this API key's model list")
        if info.get("active") is False:
            raise LLMError("model_unavailable", f"{self.model} is listed but inactive")
        return {k: info.get(k) for k in ("id", "owned_by", "active", "context_window", "max_completion_tokens")}

    def generate_json(self, system: str, user: str, schema: dict, timeout_s: float) -> LLMResponse:
        g = self._groq
        deadline = time.monotonic() + timeout_s
        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema",
                             "json_schema": {"name": "query_plan", "strict": True, "schema": transport_schema(schema)}},
            reasoning_effort=self.effort,
            include_reasoning=False,
            max_completion_tokens=self.max_tokens,
            temperature=0,
        )
        waits: list[float] = []
        schema_retry_used = False
        t0 = time.monotonic()
        for attempt in range(1, self.max_attempts + 1):
            left = deadline - time.monotonic()
            if left < 1:
                err = LLMError("timeout", "LLM deadline reached")
                err.meta = {"attempts": attempt - 1, "rate_limit_waits_s": waits}
                raise err
            try:
                raw = self.client.with_options(timeout=left).chat.completions.with_raw_response.create(**kwargs)
            except g.RateLimitError as e:
                limit = parse_limit_message(e)
                wait = parse_wait(getattr(e.response, "headers", None))
                wait = wait if wait is not None else min(2.0 ** attempt, 8.0)
                if attempt == self.max_attempts or time.monotonic() + wait > deadline - 1:
                    err = LLMError("rate_limited", f"HTTP 429; wait {wait:.1f}s; attempt {attempt}/{self.max_attempts}")
                    err.meta = {"attempts": attempt, "rate_limit_waits_s": waits, "last_wait_hint_s": wait, "limit": limit}
                    raise err from e
                waits.append(round(wait, 2))
                self._sleep(wait)
                continue
            except g.APITimeoutError as e:
                raise LLMError("timeout", str(e)) from e
            except g.AuthenticationError as e:
                raise LLMError("api_error", "authentication failed") from e
            except g.NotFoundError as e:
                raise LLMError("model_unavailable", f"{self.model}: HTTP 404") from e
            except g.BadRequestError as e:
                body = getattr(e, "body", None) or {}
                err = body.get("error", body) if isinstance(body, dict) else {}
                code = err.get("code", "") if isinstance(err, dict) else ""
                if code in ("json_validate_failed", "output_parse_failed"):
                    # the generation did not match the schema: one bounded re-generation (same request)
                    if not schema_retry_used and attempt < self.max_attempts and time.monotonic() + 2 < deadline:
                        schema_retry_used = True
                        continue
                    err_ = LLMError("bad_output", f"HTTP 400 {code}")
                    err_.meta = {"attempts": attempt, "schema_retry_used": schema_retry_used,
                                 "validation_error": (err.get("message") or "")[-200:] if isinstance(err, dict) else ""}
                    raise err_ from e
                raise LLMError("api_error", f"HTTP 400 {code}".strip()) from e
            except g.InternalServerError as e:
                if attempt == self.max_attempts or time.monotonic() + 1 > deadline - 1:
                    raise LLMError("api_error", f"HTTP {e.status_code}") from e
                self._sleep(1.0)
                continue
            except g.APIStatusError as e:
                raise LLMError("api_error", f"HTTP {e.status_code}") from e
            except g.APIConnectionError as e:
                raise LLMError("api_error", f"connection error: {e}") from e
            resp = raw.parse()
            headers = {h: raw.headers.get(h) for h in RATE_HEADERS if raw.headers.get(h) is not None}
            choice = resp.choices[0]
            if choice.finish_reason == "length":
                raise LLMError("truncated", "max_completion_tokens reached")
            text = choice.message.content
            if not text:
                raise LLMError("bad_output", "empty content")
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                raise LLMError("bad_output", f"invalid JSON: {e}") from e
            usage = resp.usage.model_dump() if getattr(resp, "usage", None) is not None else {}
            return LLMResponse(data, self.name, self.model, int((time.monotonic() - t0) * 1000), usage,
                               getattr(resp, "id", None), resp.model, attempt, waits, headers)
        raise LLMError("rate_limited", "attempts exhausted")  # pragma: no cover


class MockProvider:
    """Scripted provider: a list of responses (dict or Exception) or a callable(system, user) -> dict."""

    name = "mock"

    def __init__(self, responses: list | Callable[[str, str], dict], model: str = "mock-planner"):
        self.model = model
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    def generate_json(self, system: str, user: str, schema: dict, timeout_s: float) -> LLMResponse:
        self.calls.append((system, user))
        if callable(self._responses):
            out = self._responses(system, user)
        else:
            if not self._responses:
                raise LLMError("bad_output", "mock has no scripted response left")
            out = self._responses.pop(0)
        if isinstance(out, Exception):
            raise out
        return LLMResponse(out, self.name, self.model, 0)


def make_provider(settings: Settings) -> LLMProvider | None:
    if settings.llm_provider == "anthropic":
        return AnthropicProvider(settings)
    if settings.llm_provider == "groq":
        return GroqProvider(settings)
    return None  # "none" (and "mock", which tests construct directly)
