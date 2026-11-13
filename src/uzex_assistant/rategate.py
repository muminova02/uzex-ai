"""Provider-wide LLM rate control shared by all users (thread-safe).

* Cooldown: after a 429 with a wait hint, no request is sent until the hint has passed.
* Token budget: the per-minute token limit learned from ``x-ratelimit-limit-tokens`` is enforced
  over a sliding 60 s window. Each call reserves its estimated tokens before it is sent, so
  concurrent requests cannot all pass at once. The reservation is replaced by the real usage.
* Deadline: if the required wait is longer than the caller's remaining time, the call fails at once
  with ``LLMError("rate_limited")`` carrying ``last_wait_hint_s`` and ``gated=True``. No request is
  sent, nothing is retried indefinitely, and the failure is never turned into a no-data answer.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from .llm import LLMError, LLMResponse

WINDOW_S = 60.0


class RateGate:
    def __init__(self, inner, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 default_estimate: int = 3000, tpm_limit: int | None = None):
        self.inner, self._clock, self._sleep = inner, clock, sleep
        self.name, self.model = inner.name, inner.model
        self._lock = threading.Lock()
        self._window: deque[list] = deque()  # [timestamp, tokens]
        self.cooldown_until = 0.0
        self.tpm_limit = tpm_limit
        self.estimate = default_estimate
        self.gated_failures = 0
        self.last_limit: dict | None = None
        # timeout_s passed to generate_json is the caller's whole remaining budget (wait + call); the provider
        # call itself is additionally capped here, so a TPM wait can use budget a 30 s call cap would forbid.
        self.call_timeout_s: float | None = None

    def __getattr__(self, item):  # verify_model etc.
        return getattr(self.inner, item)

    # ----------------------------------------------------------------- bookkeeping
    def _prune(self, now: float) -> None:
        while self._window and now - self._window[0][0] >= WINDOW_S:
            self._window.popleft()

    def wait_needed(self, tokens: int | None = None) -> float:
        with self._lock:
            return self._wait_locked(self._clock(), tokens or self.estimate)

    def _wait_locked(self, now: float, tokens: int) -> float:
        self._prune(now)
        wait = max(0.0, self.cooldown_until - now)
        if self.tpm_limit:
            used = sum(t for _, t in self._window)
            if used + tokens > self.tpm_limit and self._window:
                # wait until enough of the window has expired
                need, freed = used + tokens - self.tpm_limit, 0
                for ts, t in self._window:
                    freed += t
                    if freed >= need:
                        wait = max(wait, ts + WINDOW_S - now)
                        break
        return wait

    def cooldown_remaining(self) -> float:
        return max(0.0, self.cooldown_until - self._clock())

    def _call_timeout(self, deadline: float) -> float:
        t = max(1.0, deadline - self._clock())
        return min(t, self.call_timeout_s) if self.call_timeout_s else t

    # ----------------------------------------------------------------- call
    def generate_json(self, system: str, user: str, schema: dict, timeout_s: float) -> LLMResponse:
        deadline = self._clock() + timeout_s
        while True:
            with self._lock:
                now = self._clock()
                wait = self._wait_locked(now, self.estimate)
                if wait <= 0:
                    slot = [now, self.estimate]
                    self._window.append(slot)  # reservation
                    break
            if now + wait > deadline - 1:
                self.gated_failures += 1
                err = LLMError("rate_limited", f"provider limit: next slot in {wait:.0f}s exceeds the remaining budget")
                err.meta = {"attempts": 0, "last_wait_hint_s": round(wait, 1), "gated": True, "limit": self.last_limit}
                raise err
            self._sleep(wait)
        try:
            resp = self.inner.generate_json(system, user, schema, self._call_timeout(deadline))
        except LLMError as e:
            with self._lock:
                hint = e.meta.get("last_wait_hint_s") if e.kind == "rate_limited" else None
                if e.kind == "rate_limited" and e.meta.get("limit"):
                    self.last_limit = e.meta["limit"]
                if hint:
                    self.cooldown_until = max(self.cooldown_until, self._clock() + float(hint))
                slot[1] = 0 if e.kind not in ("rate_limited",) else slot[1]
            raise
        with self._lock:
            total = (resp.usage or {}).get("total_tokens")
            if total:
                slot[1] = int(total)
                self.estimate = int(0.7 * self.estimate + 0.3 * int(total))
            h = resp.rate_limit_headers or {}
            if h.get("x-ratelimit-limit-tokens", "").isdigit():
                self.tpm_limit = int(h["x-ratelimit-limit-tokens"])
            rem = h.get("x-ratelimit-remaining-tokens", "")
            if rem.isdigit() and int(rem) < self.estimate:
                from .llm import parse_wait
                reset = parse_wait({"x-ratelimit-reset-tokens": h.get("x-ratelimit-reset-tokens", "")})
                if reset:
                    self.cooldown_until = max(self.cooldown_until, self._clock() + reset)
        return resp


# ============================================================================ cross-process gate
class SharedRateGate(RateGate):
    """Same policy as RateGate, but the cooldown, token window, learned limits and last limit info live in
    a SQLite file that every process using the same credential opens: web server, Telegram bot and
    scripts. ``BEGIN IMMEDIATE`` serialises the read-check-reserve step across processes. Wall-clock
    time is used, because monotonic clocks are not shared between processes."""

    def __init__(self, inner, path, clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 default_estimate: int = 3000):
        super().__init__(inner, clock=clock, sleep=sleep, default_estimate=default_estimate)
        import sqlite3
        from pathlib import Path
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._sqlite3 = sqlite3
        with self._db() as db:
            db.execute("create table if not exists state (k text primary key, v text)")
            db.execute("create table if not exists window (id integer primary key autoincrement, ts real, tokens integer)")
            # 24 h usage ledger of THIS deployment's key (all processes); TPD is only reported by the provider in a 429
            db.execute("create table if not exists usage (ts real, tokens integer)")

    def _db(self):
        con = self._sqlite3.connect(self.path, timeout=10, isolation_level=None)
        con.execute("pragma journal_mode=wal")
        return _Tx(con)

    @staticmethod
    def _get(db, k, default=None):
        r = db.execute("select v from state where k = ?", (k,)).fetchone()
        return r[0] if r else default

    @staticmethod
    def _set(db, k, v):
        db.execute("insert into state(k, v) values (?, ?) on conflict(k) do update set v = excluded.v", (k, str(v)))

    def _wait_db(self, db, now: float, tokens: int) -> float:
        db.execute("delete from window where ts <= ?", (now - WINDOW_S,))
        wait = max(0.0, float(self._get(db, "cooldown_until", 0)) - now)
        limit = self._get(db, "tpm_limit")
        if limit:
            rows = db.execute("select ts, tokens from window order by ts").fetchall()
            used = sum(t for _, t in rows)
            if used + tokens > int(limit) and rows:
                need, freed = used + tokens - int(limit), 0
                for ts, t in rows:
                    freed += t
                    if freed >= need:
                        wait = max(wait, ts + WINDOW_S - now)
                        break
        return wait

    def cooldown_remaining(self) -> float:
        with self._db() as db:
            return max(0.0, float(self._get(db, "cooldown_until", 0)) - self._clock())

    def wait_needed(self, tokens: int | None = None) -> float:
        with self._db() as db:
            est = int(float(self._get(db, "estimate", self.estimate)))
            return self._wait_db(db, self._clock(), tokens or est)

    @property
    def last_limit(self):
        import json
        try:
            with self._db() as db:
                v = self._get(db, "last_limit")
            return json.loads(v) if v else None
        except Exception:
            return None

    @last_limit.setter
    def last_limit(self, value):  # assigned by RateGate.__init__; persisted only when set explicitly later
        pass

    def generate_json(self, system: str, user: str, schema: dict, timeout_s: float):
        import json
        deadline = self._clock() + timeout_s
        while True:
            with self._db() as db:
                db.execute("begin immediate")
                now = self._clock()
                est = int(float(self._get(db, "estimate", self.estimate)))
                wait = self._wait_db(db, now, est)
                if wait <= 0:
                    slot = db.execute("insert into window(ts, tokens) values (?, ?)", (now, est)).lastrowid
                    db.execute("commit")
                    break
                db.execute("commit")
            if now + wait > deadline - 1:
                self.gated_failures += 1
                err = LLMError("rate_limited", f"provider limit: next slot in {wait:.0f}s exceeds the remaining budget")
                err.meta = {"attempts": 0, "last_wait_hint_s": round(wait, 1), "gated": True, "limit": self.last_limit}
                raise err
            self._sleep(wait)
        try:
            resp = self.inner.generate_json(system, user, schema, self._call_timeout(deadline))
        except LLMError as e:
            with self._db() as db:
                db.execute("begin immediate")
                if e.kind == "rate_limited":
                    hint = e.meta.get("last_wait_hint_s")
                    if hint:
                        cur = float(self._get(db, "cooldown_until", 0))
                        self._set(db, "cooldown_until", max(cur, self._clock() + float(hint)))
                    if e.meta.get("limit"):
                        self._set(db, "last_limit", json.dumps(e.meta["limit"]))
                else:
                    db.execute("update window set tokens = 0 where id = ?", (slot,))
                db.execute("commit")
            raise
        with self._db() as db:
            db.execute("begin immediate")
            total = (resp.usage or {}).get("total_tokens")
            if total:
                db.execute("update window set tokens = ? where id = ?", (int(total), slot))
                db.execute("insert into usage(ts, tokens) values (?, ?)", (self._clock(), int(total)))
                db.execute("delete from usage where ts < ?", (self._clock() - 86400,))
                est = int(float(self._get(db, "estimate", self.estimate)))
                self._set(db, "estimate", int(0.7 * est + 0.3 * int(total)))
            h = resp.rate_limit_headers or {}
            if str(h.get("x-ratelimit-limit-tokens", "")).isdigit():
                self._set(db, "tpm_limit", int(h["x-ratelimit-limit-tokens"]))
            rem = str(h.get("x-ratelimit-remaining-tokens", ""))
            est = int(float(self._get(db, "estimate", self.estimate)))
            if rem.isdigit() and int(rem) < est:
                from .llm import parse_wait
                reset = parse_wait({"x-ratelimit-reset-tokens": h.get("x-ratelimit-reset-tokens", "")})
                if reset:
                    cur = float(self._get(db, "cooldown_until", 0))
                    self._set(db, "cooldown_until", max(cur, self._clock() + reset))
            db.execute("commit")
        return resp


def usage_last_24h(path, now: float | None = None) -> dict:
    """Tokens this deployment spent in the last 24 h, from the shared gate file (no network)."""
    import sqlite3
    now = time.time() if now is None else now
    con = sqlite3.connect(str(path))
    try:
        has = lambda t: con.execute("select 1 from sqlite_master where name=?", (t,)).fetchone() is not None  # noqa: E731
        state = dict(con.execute("select k, v from state").fetchall()) if has("state") else {}
        if has("usage"):
            n, t, first = con.execute("select count(*), coalesce(sum(tokens), 0), min(ts) from usage where ts >= ?",
                                      (now - 86400,)).fetchone()
            ledger = "ok"
        else:
            n, t, first, ledger = 0, 0, None, "absent (created by the next process start; earlier usage not counted)"
        return {"tokens": int(t), "calls": int(n), "since": first, "ledger": ledger, "estimate": state.get("estimate"),
                "tpm_limit": state.get("tpm_limit"), "last_limit": state.get("last_limit"),
                "cooldown_until": state.get("cooldown_until")}
    finally:
        con.close()


class _Tx:
    def __init__(self, con):
        self.con = con

    def __enter__(self):
        return self.con

    def __exit__(self, et, ev, tb):
        if et is not None:
            try:
                self.con.execute("rollback")
            except Exception:
                pass
        self.con.close()
