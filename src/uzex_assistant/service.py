"""Async query service shared by all chats: bounded global queue, controlled concurrency, one deadline.

The total budget (``request_budget_s``) starts when a question arrives, so time spent waiting in the
queue and any rate-limit waits are part of it. Engine work (LLM planning and SQL) runs in worker
threads (``asyncio.to_thread``); SQL itself runs in the executor's killable processes. The event loop
never blocks.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .engine import AnswerResult, Engine, Evidence, QueryPlan


@dataclass
class ServiceStats:
    accepted: int = 0
    rejected_busy: int = 0
    gated: int = 0


class QueryService:
    def __init__(self, engine: Engine, max_concurrent: int = 2, max_waiting: int = 20, budget_s: float | None = None):
        self.engine = engine
        self.budget_s = budget_s if budget_s is not None else engine.s.request_budget_s
        self.max_waiting = max_waiting
        self._sem = asyncio.Semaphore(max(1, max_concurrent))
        self._waiting = 0
        self.stats = ServiceStats()

    @property
    def test_mode(self) -> bool:
        p = self.engine.provider
        return p is None or getattr(p, "name", "") == "mock"

    def _error(self, code: str, question: str | None, extra_llm: dict | None = None, plan: QueryPlan | None = None) -> AnswerResult:
        res = AnswerResult(status="execution_error", reason_code=code, question=question, plan=plan,
                           evidence=Evidence(query_id="-", notes=[f"reason_code={code}"]), llm=extra_llm or {})
        return self.engine._finish(res, question)

    async def _run(self, question: str | None, fn, uses_llm: bool):
        t0 = time.monotonic()
        if self._waiting >= self.max_waiting:
            self.stats.rejected_busy += 1
            return self._error("service_busy", question)
        self._waiting += 1
        try:
            try:
                # wait only while enough budget remains for planning (MIN_LLM_TIMEOUT_S) + SQL (SQL_RESERVE_S)
                from .engine import MIN_LLM_TIMEOUT_S, SQL_RESERVE_S
                await asyncio.wait_for(self._sem.acquire(), timeout=max(0.1, self.budget_s - SQL_RESERVE_S - MIN_LLM_TIMEOUT_S))
            except asyncio.TimeoutError:
                self.stats.rejected_busy += 1
                return self._error("service_busy", question)
        finally:
            self._waiting -= 1
        queue_ms = int((time.monotonic() - t0) * 1000)
        try:
            remaining = self.budget_s - (time.monotonic() - t0)
            gate = getattr(self.engine.provider, "cooldown_remaining", None)
            if uses_llm and gate is not None:
                cd = gate()
                if cd > remaining - 15:  # the provider will not accept a request before the deadline
                    self.stats.gated += 1
                    return self._error("llm_rate_limited", question, {"last_wait_hint_s": round(cd, 1), "gated": True,
                                                                       "attempts": 0,
                                                                       "limit": getattr(self.engine.provider, "last_limit", None)})
            self.stats.accepted += 1
            res = await asyncio.to_thread(fn, remaining)
            res.timings_ms = {**res.timings_ms, "queue": queue_ms, "service_total": int((time.monotonic() - t0) * 1000),
                              "budget_given": int(remaining * 1000)}
            return res
        finally:
            self._sem.release()

    async def ask(self, question: str) -> AnswerResult:
        return await self._run(question, lambda remaining: self.engine.answer(question, budget_s=remaining), True)

    async def follow_up(self, question: str, plan: QueryPlan, reason_code: str, candidate: dict,
                        entity_text: str | None, prior_skip: set | None = None) -> tuple[AnswerResult, set]:
        """Apply a clarification choice to the stored plan; SQL only, never an LLM call.
        Returns the result and the accumulated checks to skip (kept across chained clarifications)."""
        try:
            new_plan, skip = self.engine.apply_choice(plan, reason_code, candidate, entity_text)
        except (ValueError, KeyError):
            return self._error("session_invalid", question, plan=plan), set(prior_skip or ())
        skip = set(prior_skip or ()) | skip
        from .engine import Budget

        def run(remaining):
            return self.engine.run_plan(new_plan, question, Budget(remaining), skip_checks=skip)
        return await self._run(question, run, False), skip
