"""AgentsOS orchestrator kernel.

The orchestrator owns the event bus, scheduler, and graph runner.
The first version provides:
- A FIFO scheduler with priority.
- An in-process pub/sub event bus.
- A simple graph runner that executes nodes in topological order.

It is intentionally minimal — the real reactive engine is v0.3 work.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(order=True)
class ScheduledItem:
    priority: int
    seq: int
    name: str = field(compare=False)
    coro_factory: Callable[[], Awaitable[Any]] = field(compare=False)


class Scheduler:
    def __init__(self, concurrency: int = 4) -> None:
        self._queue: asyncio.PriorityQueue[ScheduledItem] = asyncio.PriorityQueue()
        self._seq = 0
        self._sem = asyncio.Semaphore(concurrency)

    def submit(
        self, name: str, coro_factory: Callable[[], Awaitable[Any]], priority: int = 0
    ) -> None:
        self._seq += 1
        self._queue.put_nowait(ScheduledItem(priority, self._seq, name, coro_factory))

    async def run_all(self) -> list[Any]:
        results: list[Any] = []

        async def worker() -> None:
            while True:
                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                async with self._sem:
                    try:
                        results.append(await item.coro_factory())
                    finally:
                        self._queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(self._sem._value)]
        await asyncio.gather(*workers)
        return results


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[Any], Awaitable[None] | None]]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Callable[[Any], Awaitable[None] | None]) -> None:
        self._subs[topic].append(handler)

    async def publish(self, topic: str, payload: Any) -> None:
        for handler in list(self._subs.get(topic, [])):
            res = handler(payload)
            if asyncio.iscoroutine(res):
                await res


@dataclass
class GraphNode:
    id: str
    run: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class GraphEdge:
    src: str
    dst: str
    when: Callable[[dict[str, Any]], bool] | None = None


class GraphRunner:
    def __init__(self, nodes: Iterable[GraphNode], edges: Iterable[GraphEdge] = ()) -> None:
        self.nodes = {n.id: n for n in nodes}
        self.out: dict[str, list[GraphEdge]] = defaultdict(list)
        for e in edges:
            self.out[e.src].append(e)

    async def run(self, start: str, initial: dict[str, Any] | None = None) -> dict[str, Any]:
        state: dict[str, Any] = dict(initial or {})
        frontier: list[str] = [start]
        visited: set[str] = set()

        while frontier:
            current = frontier.pop(0)
            if current in visited or current not in self.nodes:
                continue
            visited.add(current)
            state.update(await self.nodes[current].run(state))
            for edge in self.out.get(current, []):
                if edge.when is None or edge.when(state):
                    frontier.append(edge.dst)
        return state
