"""Durable state outside the replicas: workflow checkpoints, job status, approvals, memory.

One small SQL adapter with two backends: sqlite (in-process tests, CI) and Postgres (compose,
plus the CI integration job). SQL is written once with '?' placeholders and translated.

Workflow STATE (step, tool results, confirmation status) and MEMORY (preferences, summaries)
live in separate tables with separate APIs on purpose: state is exact and per-workflow, memory
is lossy, per-user and never used to authorise an action.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS checkpoints (
        workflow_id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL, step TEXT NOT NULL,
        state TEXT NOT NULL, status TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY, idem_key TEXT UNIQUE, pool TEXT NOT NULL, status TEXT NOT NULL,
        attempts INTEGER NOT NULL, result TEXT, updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS side_effects (
        effect_key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS memory (
        user_id TEXT NOT NULL, mkey TEXT NOT NULL, value TEXT NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL, PRIMARY KEY (user_id, mkey))""",
]


class Store:
    def __init__(self, dsn: str = ":memory:") -> None:
        self.pg = dsn.startswith("postgres")
        self.lock = threading.Lock()
        if self.pg:
            import psycopg  # only needed for the Postgres backend
            self.conn = psycopg.connect(dsn, autocommit=True)
        else:
            self.conn = sqlite3.connect(dsn, check_same_thread=False, isolation_level=None)
        for stmt in SCHEMA:
            self._exec(stmt)

    def _exec(self, sql: str, params: tuple = ()):
        if self.pg:
            sql = sql.replace("?", "%s")
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(sql, params)
            try:
                return cur.fetchall()
            except Exception:  # noqa: BLE001 - statements without a result set
                return []

    # --- workflow state -------------------------------------------------------------------
    def save_checkpoint(self, wid: str, kind: str, owner: str, step: str, state: dict, status: str) -> None:
        self._exec("""INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)
                      ON CONFLICT (workflow_id) DO UPDATE SET step=excluded.step, state=excluded.state,
                      status=excluded.status, updated_at=excluded.updated_at""",
                   (wid, kind, owner, step, json.dumps(state), status, time.time()))

    def load_checkpoint(self, wid: str) -> dict | None:
        rows = self._exec("SELECT kind, owner, step, state, status FROM checkpoints WHERE workflow_id=?", (wid,))
        if not rows:
            return None
        kind, owner, step, state, status = rows[0]
        return {"kind": kind, "owner": owner, "step": step, "state": json.loads(state), "status": status}

    # --- exactly-once side effects ----------------------------------------------------------
    def record_effect_once(self, key: str, payload: dict) -> bool:
        """True if this call performed the effect; False if it was already done (idempotent)."""
        rows = self._exec("INSERT INTO side_effects VALUES (?,?,?) ON CONFLICT (effect_key) DO NOTHING "
                          "RETURNING effect_key", (key, json.dumps(payload), time.time()))
        return bool(rows)

    def effects(self, prefix: str = "") -> list[dict]:
        rows = self._exec("SELECT effect_key, payload FROM side_effects WHERE effect_key LIKE ?", (prefix + "%",))
        return [{"key": k, **json.loads(p)} for k, p in rows]

    # --- jobs ----------------------------------------------------------------------------------
    def upsert_job(self, job_id: str, idem_key: str, pool: str, status: str, attempts: int,
                   result: dict | None = None) -> None:
        self._exec("""INSERT INTO jobs VALUES (?,?,?,?,?,?,?) ON CONFLICT (job_id) DO UPDATE SET
                      status=excluded.status, attempts=excluded.attempts, result=excluded.result,
                      updated_at=excluded.updated_at""",
                   (job_id, idem_key, pool, status, attempts, json.dumps(result) if result else None, time.time()))

    def job(self, job_id: str) -> dict | None:
        rows = self._exec("SELECT job_id, pool, status, attempts, result FROM jobs WHERE job_id=?", (job_id,))
        if not rows:
            return None
        j, pool, st, att, res = rows[0]
        return {"job_id": j, "pool": pool, "status": st, "attempts": att, "result": json.loads(res) if res else None}

    # --- memory (separate from state) ------------------------------------------------------
    def remember(self, user_id: str, key: str, value: str) -> None:
        self._exec("""INSERT INTO memory VALUES (?,?,?,?) ON CONFLICT (user_id, mkey) DO UPDATE
                      SET value=excluded.value, updated_at=excluded.updated_at""",
                   (user_id, key, value, time.time()))

    def recall(self, user_id: str) -> dict:
        return dict(self._exec("SELECT mkey, value FROM memory WHERE user_id=?", (user_id,)))
