"""Workflow checkpoints in a SQL store: Postgres in docker compose, SQLite in-process for tests/CI.

Replicas hold no workflow state, so any healthy instance can resume a
workflow from its last committed step.
"""
from __future__ import annotations

import json
import sqlite3

DDL = """CREATE TABLE IF NOT EXISTS checkpoints (
  workflow_id TEXT PRIMARY KEY,
  step INTEGER NOT NULL,
  state TEXT NOT NULL,
  status TEXT NOT NULL,
  owner TEXT
)"""


class CheckpointStore:
    def __init__(self, conn, paramstyle: str = "qmark") -> None:
        self.conn = conn
        self.ph = "?" if paramstyle == "qmark" else "%s"
        cur = conn.cursor()
        if paramstyle != "qmark":
            # Several replicas start at once; serialise the DDL so concurrent CREATE TABLE IF NOT EXISTS
            # calls cannot race on Postgres's catalog (a real failure seen in the compose smoke test).
            cur.execute("SELECT pg_advisory_xact_lock(4242)")
        cur.execute(DDL)
        conn.commit()

    @classmethod
    def sqlite(cls, path: str = ":memory:") -> CheckpointStore:
        return cls(sqlite3.connect(path, check_same_thread=False))

    @classmethod
    def postgres(cls, dsn: str) -> CheckpointStore:  # pragma: no cover - exercised in the compose/CI integration job
        import psycopg

        return cls(psycopg.connect(dsn), paramstyle="format")

    def _q(self, sql: str) -> str:
        return sql.replace("?", self.ph)

    def save(self, workflow_id: str, step: int, state: dict, status: str = "running", owner: str | None = None) -> None:
        cur = self.conn.cursor()
        cur.execute(self._q(
            "INSERT INTO checkpoints(workflow_id, step, state, status, owner) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(workflow_id) DO UPDATE SET step=excluded.step, state=excluded.state, "
            "status=excluded.status, owner=excluded.owner"),
            (workflow_id, step, json.dumps(state), status, owner))
        self.conn.commit()

    def load(self, workflow_id: str) -> dict | None:
        cur = self.conn.cursor()
        cur.execute(self._q("SELECT step, state, status, owner FROM checkpoints WHERE workflow_id = ?"),
                    (workflow_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return {"step": row[0], "state": json.loads(row[1]), "status": row[2], "owner": row[3]}
