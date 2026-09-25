"""Serve a World over the WebSocket control binding (AWP-TRN-001) with inline frames."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import itertools
import json
import logging
import ssl
import time
from collections import deque
from collections.abc import Hashable, Sequence
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit

from websockets.asyncio.server import Server as WsServer
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.typing import Subprotocol

from awp import jsonrpc

from .recorder import TraceRecorder
from .world import Close, Output, Send, SendFrame, World

log = logging.getLogger("awp_sim")

SUBPROTOCOL = Subprotocol("awp")
BEARER_PREFIX = "awp.bearer."
STREAM_PATH = "/stream"

Item = Send | SendFrame | Close


def _channel(item: Item) -> int | None:
    """The latest-wins channel an item may be replaced on, if any."""
    if isinstance(item, SendFrame) and item.latest_wins:
        return item.frame.channel_id
    if isinstance(item, Send) and item.latest_wins:
        return int(item.msg["params"]["channel_id"])
    return None


def _bearer(headers: Headers) -> str | None:
    """The credential from `Authorization` or an `awp.bearer.<token>` subprotocol (AWP-SEC-005)."""
    auth = headers.get("Authorization") or ""
    if auth.startswith("Bearer "):
        return auth.removeprefix("Bearer ")
    for v in headers.get_all("Sec-WebSocket-Protocol"):
        for p in v.split(","):
            if p.strip().startswith(BEARER_PREFIX):
                return p.strip().removeprefix(BEARER_PREFIX)
    return None


class _Outbox:
    """Ordered sends, where a pending latest-wins frame is replaced by a newer one on its
    channel instead of queueing behind it (AWP-TRN-009). Bounded: a peer that stops reading is
    disconnected rather than buffered without limit."""

    LIMIT = 10_000

    def __init__(self) -> None:
        self._items: deque[list[Item]] = deque()
        self._slots: dict[int, list[Item]] = {}
        self._ready = asyncio.Event()

    def put(self, item: Item) -> bool:
        """Queue `item`; False if the queue is full."""
        channel = _channel(item)
        if channel is not None:
            cell = self._slots.get(channel)
            if cell is not None:
                cell[0] = item
                return True
            cell = self._slots[channel] = [item]
        else:
            cell = [item]
        if len(self._items) >= self.LIMIT:
            return False
        self._items.append(cell)
        self._ready.set()
        return True

    async def get(self) -> Item:
        while not self._items:
            self._ready.clear()
            await self._ready.wait()
        cell = self._items.popleft()
        item = cell[0]
        channel = _channel(item)
        if channel is not None:
            self._slots.pop(channel, None)
        return item


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Server:
    def __init__(
        self,
        world: World,
        *,
        host: str = "127.0.0.1",
        port: int = 8710,
        token: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        allow_insecure: bool = False,
        record_dir: Path | str | None = None,
        step_ms: float = 2.0,
        stream_binding: bool = False,
    ) -> None:
        if not _is_loopback(host) and not allow_insecure:
            if token is None:
                raise ValueError("non-loopback worlds must authenticate agents (AWP-SEC-002)")
            if ssl_context is None:
                raise ValueError("connections that leave the machine must use TLS (AWP-SEC-001)")
        self.world = world
        self.host = host
        self.port = port
        self.token = token
        self.ssl_context = ssl_context
        self.step_s = step_ms / 1000
        self.recorder = TraceRecorder(record_dir) if record_dir else None
        self.stream_binding = stream_binding
        self._ids = itertools.count(1)
        self._sockets: dict[Hashable, tuple[ServerConnection, _Outbox]] = {}
        self._server: WsServer | None = None
        self._loop: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[None]] = set()

    @property
    def url(self) -> str:
        scheme = "wss" if self.ssl_context else "ws"
        return f"{scheme}://{self.host}:{self.port}"

    async def start(self) -> None:
        self._server = await serve(
            self._handle,
            self.host,
            self.port,
            ssl=self.ssl_context,
            subprotocols=[SUBPROTOCOL],
            select_subprotocol=self._select_subprotocol,
            process_request=self._authorize,
            ping_interval=None,  # AWP heartbeats govern liveness (AWP-SAF-001)
            max_size=16 * 1024 * 1024,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        if self.stream_binding:
            self.world.stream_url = self.url + STREAM_PATH
        self._loop = asyncio.create_task(self._run())
        log.info("awp-sim %s world listening on %s", self.world.config.mode, self.url)

    async def stop(self) -> None:
        if self._loop is not None:
            self._loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self.recorder is not None:
            self.recorder.close()

    async def __aenter__(self) -> Server:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def engage_estop(self) -> None:
        self._dispatch(self.world.engage_estop(time.monotonic_ns()))

    def release_estop(self) -> None:
        self._dispatch(self.world.release_estop(time.monotonic_ns()))

    # ------------------------------------------------------------ handshake

    def _authorize(self, connection: ServerConnection, request: Request) -> Response | None:
        if "token=" in urlsplit(request.path).query:
            return connection.respond(
                HTTPStatus.BAD_REQUEST, "credentials must not appear in URLs\n"
            )
        if urlsplit(request.path).path == STREAM_PATH:  # the session token is the credential
            if _bearer(request.headers) is None:
                return connection.respond(HTTPStatus.UNAUTHORIZED, "missing session token\n")
            return None
        if self.token is None or self._credential_ok(request.headers):
            return None
        return connection.respond(HTTPStatus.UNAUTHORIZED, "missing or invalid bearer token\n")

    def _credential_ok(self, headers: Headers) -> bool:
        """AWP-SEC-005: the Authorization header, or the awp.bearer.<token> subprotocol."""
        if headers.get("Authorization") == f"Bearer {self.token}":
            return True
        offered = [
            p.strip() for v in headers.get_all("Sec-WebSocket-Protocol") for p in v.split(",")
        ]
        return f"{BEARER_PREFIX}{self.token}" in offered

    def _select_subprotocol(
        self, connection: ServerConnection, offered: Sequence[Subprotocol]
    ) -> Subprotocol | None:
        return SUBPROTOCOL if SUBPROTOCOL in offered else None  # never echo a credential

    # ------------------------------------------------------------ connections

    async def _handle(self, ws: ServerConnection) -> None:
        if ws.request is not None and urlsplit(ws.request.path).path == STREAM_PATH:
            await self._handle_stream(ws)
            return
        conn = next(self._ids)
        outbox = _Outbox()
        self._sockets[conn] = (ws, outbox)
        writer = asyncio.create_task(self._write(conn, ws, outbox))
        self._dispatch(self.world.connect(conn, time.monotonic_ns()))
        try:
            async for raw in ws:
                if self.recorder is not None:
                    with contextlib.suppress(ValueError):
                        self.recorder.record(conn, self._session_id(conn), "agent", json.loads(raw))
                self._dispatch(self.world.receive_text(conn, raw, time.monotonic_ns()))
        except ConnectionClosed:
            pass
        except Exception:
            log.exception("closing connection %s after an internal error", conn)
            await ws.close(code=1011)
        finally:
            writer.cancel()
            self._sockets.pop(conn, None)
            self._dispatch(self.world.disconnect(conn, time.monotonic_ns()))
            if self.recorder is not None:
                self.recorder.forget(conn)

    async def _handle_stream(self, ws: ServerConnection) -> None:
        """A stream connection: binary frames only, one per message (AWP-TRN-003)."""
        conn = next(self._ids)
        outbox = _Outbox()
        self._sockets[conn] = (ws, outbox)
        writer = asyncio.create_task(self._write(conn, ws, outbox))
        token = _bearer(ws.request.headers) if ws.request is not None else None
        self._dispatch(self.world.attach_stream(conn, token or "", time.monotonic_ns()))
        try:
            async for raw in ws:
                if isinstance(raw, str):
                    await ws.close(code=1008, reason="AWP_MALFORMED: text on a stream connection")
                    break
                self._dispatch(self.world.receive_stream(conn, raw, time.monotonic_ns()))
        except ConnectionClosed:
            pass
        finally:
            writer.cancel()
            self._sockets.pop(conn, None)
            self._dispatch(self.world.stream_lost(conn, time.monotonic_ns()))

    async def _write(self, conn: Hashable, ws: ServerConnection, outbox: _Outbox) -> None:
        with contextlib.suppress(ConnectionClosed, asyncio.CancelledError):
            while True:
                send = await outbox.get()
                if isinstance(send, Close):  # everything queued before it has been sent
                    await ws.close(code=1008, reason=send.reason[:120])
                    return
                if isinstance(send, SendFrame):
                    await ws.send(self.world.frame_bytes(send, time.monotonic_ns()))
                    continue
                msg = send.msg
                if msg.get("method") == "obs.frame":
                    msg = self.world.frame_sent(send, time.monotonic_ns())
                if self.recorder is not None:
                    self.recorder.record(conn, send.session, "world", msg)
                await ws.send(jsonrpc.encode(msg))

    def _dispatch(self, outputs: list[Output]) -> None:
        for out in outputs:
            entry = self._sockets.get(out.conn)
            if entry is None:
                continue
            ws, outbox = entry
            if not outbox.put(out):
                log.warning("closing connection %s: send queue full", out.conn)
                self._sockets.pop(out.conn, None)
                self._close_later(ws)

    def _session_id(self, conn: Hashable) -> str | None:
        session = self.world.session_of(conn)
        return session.id if session else None

    def _close_later(self, ws: ServerConnection) -> None:
        task = asyncio.create_task(ws.close(code=1008, reason="send queue full"))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _run(self) -> None:
        while True:
            try:
                self._dispatch(self.world.advance(time.monotonic_ns()))
            except Exception:  # keep the watchdog and heartbeats running for everyone else
                log.exception("world.advance failed")
            await asyncio.sleep(self.step_s)
