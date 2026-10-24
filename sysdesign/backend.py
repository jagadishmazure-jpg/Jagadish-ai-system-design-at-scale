"""Backend fundamentals that the bigger patterns sit on: atomic transactions, indexes, object
storage for attachments, and why in-process memory is not a place to keep state."""
from __future__ import annotations

import hashlib
import hmac
import os
import pathlib
import sqlite3
import time


class CrashBetweenWrites(Exception):
    pass


def refund_with_record(db_path: str, order_id: str, amount: float, crash_between: bool = False) -> None:
    """Payment ledger row + order status update in ONE transaction: both persist or neither does."""
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS ledger (order_id TEXT, amount REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS order_state (order_id TEXT PRIMARY KEY, status TEXT)")
    conn.commit()
    try:
        with conn:  # BEGIN ... COMMIT, or ROLLBACK on exception
            conn.execute("INSERT INTO ledger VALUES (?, ?)", (order_id, amount))
            if crash_between:
                raise CrashBetweenWrites("process died between the two writes")
            conn.execute("INSERT INTO order_state VALUES (?, 'refunded') ON CONFLICT(order_id) DO UPDATE "
                         "SET status='refunded'", (order_id,))
    finally:
        conn.close()


def index_proof(rows: int = 200_000, lookups: int = 2_000, seed: int = 3) -> dict:
    """Order lookup by customer before/after an index: query plan and measured time."""
    import random
    rng = random.Random(seed)
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE orders (order_id TEXT, customer TEXT, status TEXT, total REAL)")
    db.executemany("INSERT INTO orders VALUES (?,?,?,?)",
                   ((f"QM-{i:07d}", f"u-{rng.randint(1, 50_000)}", "delivered", 10.0) for i in range(rows)))
    q = "SELECT order_id, status FROM orders WHERE customer = ?"
    targets = [f"u-{rng.randint(1, 50_000)}" for _ in range(lookups)]

    def timed():
        t0 = time.perf_counter()
        for c in targets:
            db.execute(q, (c,)).fetchall()
        return (time.perf_counter() - t0) / lookups * 1000

    plan_before = " ".join(r[-1] for r in db.execute("EXPLAIN QUERY PLAN " + q, ("u-1",)))
    before = timed() if lookups <= 200 else None
    if before is None:  # a full scan per lookup is slow; time a subset honestly and say so
        sub = targets[:200]
        t0 = time.perf_counter()
        for c in sub:
            db.execute(q, (c,)).fetchall()
        before = (time.perf_counter() - t0) / len(sub) * 1000
    db.execute("CREATE INDEX idx_orders_customer ON orders(customer)")
    plan_after = " ".join(r[-1] for r in db.execute("EXPLAIN QUERY PLAN " + q, ("u-1",)))
    after = timed()
    return {"rows": rows, "plan_before": plan_before, "plan_after": plan_after,
            "ms_per_lookup_before": round(before, 4), "ms_per_lookup_after": round(after, 4),
            "speedup_x": round(before / after, 1) if after else None}


class LocalObjectStore:
    """S3-shaped local object store (buckets/keys on disk, signed expiring links). In compose the same
    interface points at MinIO; the DB keeps only the object key/link, never the bytes."""

    def __init__(self, root: pathlib.Path, secret: bytes | None = None) -> None:
        self.root = root
        self.secret = secret or os.environ.get("OBJECT_LINK_KEY", "dev-only").encode()

    def _path(self, bucket: str, key: str) -> pathlib.Path:
        if ".." in key or key.startswith("/") or not bucket.isalnum():
            raise ValueError("bad key")
        return self.root / bucket / key

    def put_object(self, bucket: str, key: str, data: bytes) -> str:
        p = self._path(bucket, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return f"s3://{bucket}/{key}"

    def get_object(self, bucket: str, key: str) -> bytes:
        return self._path(bucket, key).read_bytes()

    def signed_url(self, bucket: str, key: str, expires_at: int) -> str:
        sig = hmac.new(self.secret, f"{bucket}/{key}:{expires_at}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"/objects/{bucket}/{key}?exp={expires_at}&sig={sig}"

    def verify_url(self, bucket: str, key: str, expires_at: int, sig: str, now: float) -> bool:
        good = hmac.new(self.secret, f"{bucket}/{key}:{expires_at}".encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(good, sig) and now < expires_at


class InProcessSessionStore:
    """What NOT to do with replicas: state in a Python dict dies with the process."""

    def __init__(self) -> None:
        self.data: dict = {}

    def put(self, k, v):
        self.data[k] = v

    def get(self, k):
        return self.data.get(k)
