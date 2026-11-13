"""Provider-wide rate gate (fake clock, fake provider; no network)."""
import pytest

from uzex_assistant.llm import LLMError, LLMResponse
from uzex_assistant.rategate import RateGate


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Inner:
    name, model = "groq", "openai/gpt-oss-120b"

    def __init__(self, tokens=3000, limit="8000", fail=None):
        self.calls, self.tokens, self.limit, self.fail = 0, tokens, limit, fail

    def generate_json(self, system, user, schema, timeout_s):
        self.calls += 1
        if self.fail:
            raise self.fail
        return LLMResponse({}, "groq", self.model, 1, {"total_tokens": self.tokens}, None, self.model,
                           rate_limit_headers={"x-ratelimit-limit-tokens": self.limit, "x-ratelimit-remaining-tokens": "5000"})


def gate(inner, clock):
    return RateGate(inner, clock=clock, sleep=clock.sleep)


def test_learns_tpm_limit_and_waits_within_deadline():
    c, inner = Clock(), Inner()
    g = gate(inner, c)
    g.generate_json("s", "u", {}, 60)
    g.generate_json("s", "u", {}, 60)
    assert g.tpm_limit == 8000 and c.t == 0          # 6000 tokens in the window: still fits
    g.generate_json("s", "u", {}, 70)                 # 9000 > 8000: waits for the first call to leave the window
    assert c.t == pytest.approx(60.0) and inner.calls == 3


def test_wait_beyond_deadline_fails_fast_without_calling():
    c, inner = Clock(), Inner()
    g = gate(inner, c)
    g.generate_json("s", "u", {}, 60)
    g.generate_json("s", "u", {}, 60)
    with pytest.raises(LLMError) as e:
        g.generate_json("s", "u", {}, 20)
    assert e.value.kind == "rate_limited" and e.value.meta["gated"] and e.value.meta["last_wait_hint_s"] == 60.0
    assert inner.calls == 2 and c.t == 0


def test_429_hint_sets_shared_cooldown():
    c = Clock()
    err = LLMError("rate_limited", "429")
    err.meta = {"attempts": 1, "last_wait_hint_s": 286.0}
    inner = Inner(fail=err)
    g = gate(inner, c)
    with pytest.raises(LLMError):
        g.generate_json("s", "u", {}, 30)
    assert g.cooldown_remaining() == pytest.approx(286.0)
    with pytest.raises(LLMError) as e:
        g.generate_json("s", "u", {}, 30)
    assert e.value.meta["gated"] and inner.calls == 1


def test_low_remaining_tokens_header_sets_cooldown():
    c = Clock()

    class Low(Inner):
        def generate_json(self, *a):
            r = super().generate_json(*a)
            r.rate_limit_headers = {"x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "100",
                                    "x-ratelimit-reset-tokens": "7.5s"}
            return r
    g = gate(Low(), c)
    g.generate_json("s", "u", {}, 30)
    assert g.cooldown_remaining() == pytest.approx(7.5)


def test_reservation_blocks_concurrent_burst():
    c, inner = Clock(), Inner()
    g = gate(inner, c)
    g.tpm_limit = 8000
    g.estimate = 3000
    with g._lock:  # two in-flight reservations, not yet completed
        g._window.append([0.0, 3000])
        g._window.append([0.0, 3000])
    assert g.wait_needed() == pytest.approx(60.0)


def test_tpm_wait_uses_request_budget_but_call_stays_capped():
    """Live round 2026-11-05: the 3rd question in a minute was refused because the gate's wait limit was the
    30 s call cap, although the 70 s request budget allowed waiting ~58 s. The wait now uses the budget."""
    c = Clock()

    class Rec(Inner):
        def generate_json(self, system, user, schema, timeout_s):
            self.last_timeout = timeout_s
            return super().generate_json(system, user, schema, timeout_s)

    inner = Rec()
    g = gate(inner, c)
    g.call_timeout_s = 30
    g.generate_json("s", "u", {}, 65)
    assert inner.last_timeout == 30                    # fresh budget: the call cap applies
    g.generate_json("s", "u", {}, 65)
    g.generate_json("s", "u", {}, 65)                  # waits 60 s for the window, then calls
    assert c.t == pytest.approx(60.0) and inner.calls == 3
    assert inner.last_timeout == pytest.approx(5.0)    # only what is left of the budget


def test_engine_gives_gate_the_request_budget():
    from uzex_assistant import engine as E
    src = open(E.__file__, encoding="utf-8").read()
    assert "self.provider.call_timeout_s = self.s.llm_timeout_s" in src
