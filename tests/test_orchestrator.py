"""Tests for the orchestrator primitives."""

from __future__ import annotations

from agentsos.orchestrator import EventBus, GraphEdge, GraphNode, GraphRunner, Scheduler


async def test_event_bus_delivers_to_subscribers() -> None:
    bus = EventBus()
    received: list[int] = []
    bus.subscribe("count", lambda p: received.append(p))
    await bus.publish("count", 1)
    await bus.publish("count", 2)
    assert received == [1, 2]


async def test_scheduler_runs_in_priority_order() -> None:
    s = Scheduler(concurrency=1)
    order: list[int] = []
    s.submit("hi", lambda: _async_noop_then(order.append, 1), priority=0)
    s.submit("lo", lambda: _async_noop_then(order.append, 10), priority=10)
    s.submit("mid", lambda: _async_noop_then(order.append, 5), priority=5)
    await s.run_all()
    assert order == [1, 5, 10]


async def _async_noop_then(fn, value):
    fn(value)
    return None


async def test_graph_runner_executes_topologically() -> None:
    async def step_a(state):
        state["a"] = 1
        return state

    async def step_b(state):
        state["b"] = state["a"] + 1
        return state

    async def step_c(state):
        state["c"] = state["b"] * 2
        return state

    nodes = [GraphNode("a", step_a), GraphNode("b", step_b), GraphNode("c", step_c)]
    edges = [GraphEdge("a", "b"), GraphEdge("b", "c")]
    g = GraphRunner(nodes, edges)
    out = await g.run("a")
    assert out == {"a": 1, "b": 2, "c": 4}
