"""Cross-process quota gate: two gate instances (as in two processes) share one SQLite state."""
import multiprocessing as mp
import time

import pytest

from uzex_assistant.llm import LLMError, LLMResponse
from uzex_assistant.rategate import SharedRateGate


class Inner:
    name, model = "groq", "openai/gpt-oss-120b"

    def __init__(self, fail=None):
        self.calls, self.fail = 0, fail

    def generate_json(self, *a):
        self.calls += 1
        if self.fail:
            raise self.fail
        return LLMResponse({}, "groq", self.model, 1, {"total_tokens": 3000}, None, self.model,
                           rate_limit_headers={"x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "5000"})


def test_cooldown_from_one_gate_blocks_the_other(tmp_path):
    err = LLMError("rate_limited", "429")
    err.meta = {"attempts": 1, "last_wait_hint_s": 600.0, "limit": {"kind": "TPD", "limit": 200000, "used": 199000, "requested": 3000}}
    web = SharedRateGate(Inner(fail=err), tmp_path / "gate.sqlite")
    bot_inner = Inner()
    bot = SharedRateGate(bot_inner, tmp_path / "gate.sqlite")
    with pytest.raises(LLMError):
        web.generate_json("s", "u", {}, 30)
    assert 590 < bot.cooldown_remaining() <= 600 and bot.last_limit["kind"] == "TPD"
    with pytest.raises(LLMError) as e:
        bot.generate_json("s", "u", {}, 30)
    assert e.value.meta["gated"] and bot_inner.calls == 0


def test_token_window_shared(tmp_path):
    a, b = SharedRateGate(Inner(), tmp_path / "g.sqlite"), SharedRateGate(Inner(), tmp_path / "g.sqlite")
    a.generate_json("s", "u", {}, 30)
    b.generate_json("s", "u", {}, 30)   # 6000 tokens used across both "processes"
    assert a.wait_needed(3000) > 50     # a third request would exceed 8000 TPM


def _reserve(path, q):
    g = SharedRateGate(Inner(), path)
    try:
        g.generate_json("s", "u", {}, 3)
        q.put("ok")
    except LLMError:
        q.put("gated")


def test_real_processes_cannot_both_burst(tmp_path):
    path = tmp_path / "p.sqlite"
    g = SharedRateGate(Inner(), path)
    g.generate_json("s", "u", {}, 30)
    g.generate_json("s", "u", {}, 30)   # learn TPM 8000, 6000 used
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    ps = [ctx.Process(target=_reserve, args=(str(path), q)) for _ in range(2)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(20)
    assert sorted(q.get(timeout=5) for _ in ps) == ["gated", "gated"]


def test_usage_ledger_counts_last_24h(tmp_path):
    from uzex_assistant.rategate import SharedRateGate, usage_last_24h

    class Inner:
        name, model = "groq", "m"

        def generate_json(self, system, user, schema, timeout_s):
            from uzex_assistant.llm import LLMResponse
            return LLMResponse({}, "groq", "m", 1, {"total_tokens": 1234})

    t = [1_000_000.0]
    g = SharedRateGate(Inner(), tmp_path / "g.sqlite", clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
    g.generate_json("s", "u", {}, 30)
    t[0] += 90_000
    g.generate_json("s", "u", {}, 30)
    u = usage_last_24h(tmp_path / "g.sqlite", now=t[0])
    assert u["tokens"] == 1234 and u["calls"] == 1 and u["ledger"] == "ok"
