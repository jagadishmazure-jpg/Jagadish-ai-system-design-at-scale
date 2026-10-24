"""In-process pub/sub for order-status changes (Redis pub/sub plays this role across replicas)."""
from __future__ import annotations

import asyncio
from collections import defaultdict


class OrderEvents:
    def __init__(self) -> None:
        self.subs: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, order_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.subs[order_id].append(q)
        return q

    def unsubscribe(self, order_id: str, q: asyncio.Queue) -> None:
        if q in self.subs.get(order_id, []):
            self.subs[order_id].remove(q)

    def publish(self, order_id: str, status: str) -> int:
        for q in self.subs.get(order_id, []):
            q.put_nowait({"order_id": order_id, "status": status})
        return len(self.subs.get(order_id, []))
