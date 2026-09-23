from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from awp import schema
from awp.client import ActionUpdated, WorldEvent
from awp_sim.config import WorldConfig
from awp_sim.loopback import Loopback, LoopbackAgent
from awp_sim.world import World

FIXED_WALL = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def make_net(**config: Any) -> Loopback:
    return Loopback(World(WorldConfig(**config), wall_clock=lambda: FIXED_WALL))


def pose(x: float, y: float, z: float) -> dict[str, Any]:
    return {"pose": {"frame": "base", "p_m": [x, y, z], "q": [0, 0, 0, 1]}}


def statuses(agent: LoopbackAgent, action_id: str) -> list[tuple[str, str | None]]:
    return [
        (e.status["state"], e.status.get("reason"))
        for e in agent.of(ActionUpdated)
        if e.action.action_id == action_id
    ]


def states(agent: LoopbackAgent, action_id: str) -> list[str]:
    out: list[str] = []
    for state, _ in statuses(agent, action_id):
        if not out or out[-1] != state:
            out.append(state)
    return out


def events(agent: LoopbackAgent) -> list[str]:
    return [e.event for e in agent.of(WorldEvent)]


def assert_wire_valid(agent: LoopbackAgent) -> None:
    """Every message on the wire validates against the sender form of its schema."""
    pending: dict[tuple[str, Any], str] = {}
    for line in agent.trace:
        msg, sender = line.msg, line.sender
        method = msg.get("method")
        if method is not None:
            part = "params" if "id" in msg else "notification"
            if "id" in msg:
                pending[(sender, msg["id"])] = method
            name = schema.schema_for(method, part)
            value = msg.get("params", {})
        else:
            other = "world" if sender == "agent" else "agent"
            method = pending.pop((other, msg["id"]), "?")
            name = "error" if "error" in msg else schema.schema_for(method, "result")
            value = msg.get("error", msg.get("result"))
        if name is not None:
            problems = schema.errors(name, value, sender=True)
            assert problems == [], f"{sender} {method}: {problems}"
