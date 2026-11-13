"""Bounded, read-only query execution in worker processes with hard timeouts.

Each worker process opens the prepared DuckDB file read-only with external access disabled,
extension loading disabled, configuration locked, and limits on memory and threads. A query that
exceeds its timeout is not abandoned: its worker process is killed (SIGKILL after a short
SIGTERM grace period) and a fresh worker is started. At most ``workers`` queries run at once;
callers wait up to ``queue_timeout_s`` for a free worker. Result size is capped at
``max_rows`` (one extra row is fetched to detect truncation) and ``max_cell_chars``.
"""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DUCKDB_CONFIG = {
    "access_mode": "READ_ONLY",
    "enable_external_access": False,
    "autoload_known_extensions": False,
    "autoinstall_known_extensions": False,
    "allow_community_extensions": False,
    "lock_configuration": True,
}


@dataclass
class QueryOutcome:
    ok: bool
    columns: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False
    elapsed_ms: int = 0
    error_kind: str | None = None  # timeout | busy | db_error | worker_crash
    error: str | None = None


def _worker_main(conn, db_path: str, memory_limit: str, threads: int, max_cell_chars: int) -> None:
    import duckdb  # imported in the child

    cfg = dict(DUCKDB_CONFIG, memory_limit=memory_limit, threads=threads)
    try:
        con = duckdb.connect(db_path, read_only=True, config=cfg)
    except Exception as e:  # report and exit
        conn.send(("fatal", type(e).__name__, str(e)))
        return
    conn.send(("ready",))
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg is None:
            return
        sql, params, max_rows = msg
        t0 = time.monotonic()
        try:
            cur = con.execute(sql, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            types = [str(d[1]) for d in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows + 1)
            truncated = len(rows) > max_rows
            rows = rows[:max_rows]
            rows = [tuple(v[:max_cell_chars] + "…" if isinstance(v, str) and len(v) > max_cell_chars else v for v in r)
                    for r in rows]
            conn.send(("ok", cols, types, rows, truncated, int((time.monotonic() - t0) * 1000)))
        except Exception as e:  # duckdb.Error and anything unexpected
            conn.send(("error", type(e).__name__, str(e)[:2000], int((time.monotonic() - t0) * 1000)))


class _Worker:
    def __init__(self, ctx, args):
        self.parent, child = ctx.Pipe()
        self.proc = ctx.Process(target=_worker_main, args=(child,) + args, daemon=True)
        self.proc.start()
        child.close()
        if not self.parent.poll(30):
            self.kill()
            raise RuntimeError("query worker did not start within 30 s")
        msg = self.parent.recv()
        if msg[0] != "ready":
            self.kill()
            raise RuntimeError(f"query worker failed to open the database: {msg[1]}: {msg[2]}")

    def kill(self) -> None:
        if self.proc.is_alive():
            self.proc.terminate()
            self.proc.join(0.5)
            if self.proc.is_alive():
                self.proc.kill()
                self.proc.join(2)
        self.parent.close()


class QueryExecutor:
    def __init__(self, db_path: Path, workers: int = 2, timeout_s: float = 20.0, max_rows: int = 500,
                 queue_timeout_s: float = 10.0, memory_limit: str = "1GB", threads: int = 2,
                 max_cell_chars: int = 2000):
        if not Path(db_path).is_file():
            raise FileNotFoundError(f"prepared database not found: {db_path}")
        self.timeout_s, self.max_rows, self.queue_timeout_s = timeout_s, max_rows, queue_timeout_s
        self._ctx = mp.get_context("spawn")
        self._args = (str(db_path), memory_limit, threads, max_cell_chars)
        self._pool: queue.Queue[_Worker] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self.kills = 0
        for _ in range(max(1, workers)):
            self._pool.put(_Worker(self._ctx, self._args))
        self.size = max(1, workers)

    def execute(self, sql: str, params: list | tuple = (), timeout_s: float | None = None,
                max_rows: int | None = None) -> QueryOutcome:
        if self._closed:
            raise RuntimeError("executor closed")
        timeout = min(timeout_s if timeout_s is not None else self.timeout_s, self.timeout_s)
        if timeout <= 0:
            return QueryOutcome(False, error_kind="timeout", error="no time budget left")
        try:
            w = self._pool.get(timeout=self.queue_timeout_s)
        except queue.Empty:
            return QueryOutcome(False, error_kind="busy", error=f"all {self.size} query workers busy")
        t0 = time.monotonic()
        try:
            w.parent.send((sql, list(params), max_rows or self.max_rows))
            if not w.parent.poll(timeout):
                w.kill()
                self.kills += 1
                w = _Worker(self._ctx, self._args)
                return QueryOutcome(False, error_kind="timeout", elapsed_ms=int((time.monotonic() - t0) * 1000),
                                    error=f"query exceeded {timeout:.1f} s and its worker was terminated")
            msg = w.parent.recv()
        except (EOFError, OSError, BrokenPipeError) as e:
            w.kill()
            w = _Worker(self._ctx, self._args)
            return QueryOutcome(False, error_kind="worker_crash", error=str(e))
        finally:
            self._pool.put(w)
        if msg[0] == "ok":
            _, cols, types, rows, truncated, ms = msg
            return QueryOutcome(True, cols, types, rows, truncated, ms)
        return QueryOutcome(False, elapsed_ms=msg[3], error_kind="db_error", error=f"{msg[1]}: {msg[2]}")

    async def aexecute(self, sql: str, params: list | tuple = (), timeout_s: float | None = None,
                       max_rows: int | None = None) -> QueryOutcome:
        """Async wrapper: the waiting happens in a thread; the query itself runs (and is killed) in a process."""
        return await asyncio.to_thread(self.execute, sql, params, timeout_s, max_rows)

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                w = self._pool.get_nowait()
            except queue.Empty:
                break
            try:
                w.parent.send(None)
            except OSError:
                pass
            w.kill()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
