"""Run agents against a World in-process on a virtual clock, recording wire traces.

The loopback delivers messages instantly and in order; time moves only when told to. Scenarios and
tests use it to exercise the real client and world deterministically, including failures.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from awp.client import ClientConnection, ErrorResponse, Event, Response
from awp.jsonrpc import Message

from .world import Close, Output, Send, World

MS = 1_000_000


@dataclass
class TraceLine:
    sender: str
    msg: Message
    delivered: bool = True

    def to_json(self) -> dict[str, Any]:
        method = self.msg.get("method") or ("error" if "error" in self.msg else "result")
        line: dict[str, Any] = {"from": self.sender, "step": method, "msg": self.msg}
        if not self.delivered:
            line["delivered"] = False
        return line


class Loopback:
    def __init__(self, world: World, *, step_ms: float = 1.0, start_ns: int = 1_000 * MS) -> None:
        self.world = world
        self.now = start_ns
        self.step_ns = int(step_ms * MS)
        self.agents: list[LoopbackAgent] = []
        self._conn_ids = itertools.count(1)

    def agent(
        self, name: str = "agent", *, clock_offset_ns: int = -5 * MS, **kw: Any
    ) -> LoopbackAgent:
        a = LoopbackAgent(self, name, clock_offset_ns, **kw)
        self.agents.append(a)
        return a

    def advance(self, ms: float) -> None:
        end = self.now + int(ms * MS)
        while self.now < end:
            self.now = min(end, self.now + self.step_ns)
            for a in self.agents:
                a.tick()
            self.deliver(self.world.advance(self.now))
            self.settle()

    def run_until(self, predicate: Callable[[], bool], timeout_ms: float) -> bool:
        end = self.now + int(timeout_ms * MS)
        while not predicate():
            if self.now >= end:
                return False
            self.advance(self.step_ns / MS)
        return True

    def settle(self) -> None:
        """Exchange messages until neither side has anything left to send."""
        for _ in range(10_000):
            if not any(a.flush() for a in self.agents):
                return
        raise RuntimeError("loopback did not settle")

    def deliver(self, outputs: Iterable[Output]) -> None:
        for out in outputs:
            agent = next((a for a in self.agents if a.conn == out.conn), None)
            if agent is None:
                continue
            if isinstance(out, Close):
                agent.transport_closed()
            else:
                agent.on_world(out)


class LoopbackAgent:
    def __init__(
        self,
        net: Loopback,
        name: str,
        clock_offset_ns: int,
        *,
        agent: dict[str, str] | None = None,
        modalities: Iterable[str] = ("proprio/json", "text/event+json"),
        heartbeat_ms: float | None = 500,
    ) -> None:
        self.net = net
        self.name = name
        self.offset = clock_offset_ns
        self.client = ClientConnection(
            agent or {"name": name, "version": "0.1.0", "vendor": "awp-sim"},
            modalities,
            clock_ns=lambda: self.net.now + self.offset,
        )
        self.heartbeat_ms = heartbeat_ms
        self.conn: int | None = None
        self.events: list[Event] = []
        self.trace: list[TraceLine] = []
        # Fault: the first world message matching this is lost and the connection goes half-open.
        self.lose: Callable[[Message], bool] | None = None
        self._last_ping = 0

    # ------------------------------------------------------------ transport

    def connect(self) -> None:
        if self.conn is not None:
            self.drop()
        self.conn = next(self.net._conn_ids)
        self.net.deliver(self.net.world.connect(self.conn, self.net.now))

    def drop(self) -> None:
        """The agent's side of the connection dies."""
        if self.conn is None:
            return
        conn, self.conn = self.conn, None
        self.client.connection_lost()
        self.net.deliver(self.net.world.disconnect(conn, self.net.now))

    def abandon(self) -> None:
        """The agent loses the connection but the world has not noticed: it is half-open."""
        self.conn = None
        self.client.connection_lost()

    def transport_closed(self) -> None:
        self.conn = None
        self.client.connection_lost()

    def flush(self) -> bool:
        out = self.client.outgoing()
        if self.conn is None:
            return False
        for msg in out:
            self.trace.append(TraceLine("agent", msg))
            self.net.deliver(self.net.world.receive(self.conn, msg, self.net.now))
        return bool(out)

    def on_world(self, send: Send) -> None:
        msg = (
            self.net.world.frame_sent(send, self.net.now)
            if send.msg.get("method") == "obs.frame"
            else send.msg
        )
        if self.lose is not None and self.lose(msg):
            self.lose = None
            self.trace.append(TraceLine("world", msg, delivered=False))
            self.abandon()
            return
        self.trace.append(TraceLine("world", msg))
        self.events += self.client.receive(msg)

    def tick(self) -> None:
        if self.heartbeat_ms is None or self.conn is None or self.client.ready is None:
            return
        if self.net.now - self._last_ping >= self.heartbeat_ms * MS:
            self._last_ping = self.net.now
            self.client.ping()

    # ------------------------------------------------------------ requests

    def call(self, rid: int) -> dict[str, Any]:
        """Settle and return the result of request `rid`; raise AwpError on an error response."""
        self.net.settle()
        for e in self.events:
            if isinstance(e, Response) and e.id == rid:
                return e.result
            if isinstance(e, ErrorResponse) and e.id == rid:
                raise e.error
        raise TimeoutError(f"no response to request {rid}")

    def submit(self, type: str, params: dict[str, Any], **kw: Any) -> str:
        action_id = self.client.submit(type, params, **kw)
        self.call(self.client.last_id)
        return action_id

    def open(self, **kw: Any) -> dict[str, Any]:
        self.connect()
        self.call(self.client.initialize())
        ready = self.call(self.client.open_session(**kw))
        self.call(self.client.ping())  # a clock sample on the new session clock (AWP-CLK-008)
        return ready

    def of(self, kind: type[Any]) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]

    def trace_json(self) -> list[dict[str, Any]]:
        return [line.to_json() for line in self.trace]
