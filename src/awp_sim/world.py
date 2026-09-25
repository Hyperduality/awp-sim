"""The reference world engine: the world side of AWP Core as a sans-IO state machine.

Every entry point takes the current time (`now`, monotonic nanoseconds) and returns the outputs to
perform — messages to send and connections to close. Nothing here reads a clock or does IO.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import random
import secrets
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from awp import jsonrpc, schema
from awp.errors import AwpError, ErrorCode
from awp.frames import Frame, decode, encode, from_inline, to_inline
from awp.jsonrpc import Message
from awp.lifecycle import ActionState, same_submission

from .arm import Arm, Phase
from .config import EMBODIMENT, FRAME_TREE, HOME, PARK, SERVO_CHANNEL, WorldConfig
from .session import Action, ChannelGrant, Session, Telemetry

MS = 1_000_000
S = 1_000_000_000
_PREAMBLE_LIMIT = 256  # pre-session messages held for the audit log per connection
_CLOSED_TOKENS_LIMIT = 10_000

log = logging.getLogger("awp_sim")

_REFUSED_WHILE_CLOSING = {
    "obs.subscribe",
    "obs.unsubscribe",
    "action.submit",
    "action.cancel",
    "world.tick",
    "world.reset",
}
_NEEDS_SESSION = {
    "session.close",
    "obs.subscribe",
    "obs.unsubscribe",
    "action.submit",
    "action.cancel",
    "action.status",
    "world.tick",
    "world.reset",
}


@dataclass(frozen=True, slots=True)
class Send:
    conn: Hashable
    msg: Message
    session: str | None = None
    latest_wins: bool = False  # the sender may replace this frame with a newer one (AWP-TRN-009)


@dataclass(frozen=True, slots=True)
class Close:
    conn: Hashable
    reason: str


@dataclass(frozen=True, slots=True)
class SendFrame:
    """A binary frame for a stream connection (AWP-TRN-003)."""

    conn: Hashable
    frame: Frame
    session: str
    latest_wins: bool = False


Output = Send | SendFrame | Close


class AuditSink(Protocol):
    def open(self, session_id: str, header: dict[str, Any]) -> None: ...
    def record(self, session_id: str, ts_mono_ns: int, direction: str, msg: Message) -> None: ...
    def close(self, session_id: str) -> None: ...


@dataclass(slots=True)
class _Conn:
    id: Hashable
    last_rx_ns: int
    last_ping_ns: int
    initialized: bool = False
    approver: bool = False  # authorized by the deployment to decide approvals (AWP-APR-005)
    consumes: frozenset[str] = frozenset()
    max_obs_rate_hz: float | None = None
    session: Session | None = None
    next_id: int = 1
    preamble: list[tuple[str, Message]] = field(default_factory=list)


class World:
    def __init__(
        self,
        config: WorldConfig | None = None,
        *,
        audit: AuditSink | None = None,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config or WorldConfig()
        self.manifest = self.config.manifest()
        schema.check("world-manifest", self.manifest, sender=True)
        self._params = schema.ParamsValidator(self.manifest)
        self._decls = {d["type"]: d for d in self.manifest["action_schemas"]}
        self._channels = {c["id"]: c for c in self.manifest["observation_channels"]}
        self._audit = audit
        self._wall_clock = wall_clock

        self.arm = Arm(position=HOME, accel_mps2=self.config.accel_mps2, bounds=self.config.aabb_m)
        self.tick = 0  # the world's tick: reset and restore move it
        self.advances = 0  # every advance ever made: the lockstep session clock (AWP-TIM-013)
        self.rng = random.Random(0)  # simulated sensor noise, seeded (AWP-REP-001)
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._approvals: dict[str, tuple[str, str, int]] = {}  # id → (session, action, expires)
        self._transfers: dict[str, tuple[str, int]] = {}  # token → (session, expires, monotonic)
        self.estop = False
        self._violating = False  # the arm is outside its envelope (AWP-ENV-003)
        self.sessions: dict[str, Session] = {}
        self._tokens: dict[str, Session] = {}
        self._closed_tokens: OrderedDict[str, None] = OrderedDict()
        self._conns: dict[Hashable, _Conn] = {}
        self._holder: Session | None = None
        self._last_step_ns: int | None = None
        self._out: list[Output] = []
        self._started_ns: int | None = None
        self._now = 0
        self._streams: dict[Hashable, str] = {}  # stream connection → session id
        self.stream_url: str | None = None  # set by the server when it offers the ws binding

    @property
    def _handlers(self) -> dict[str, Callable[[_Conn, Any, dict[str, Any], int], None]]:
        return {
            "initialize": self._rpc_initialize,
            "world.manifest": self._rpc_world_manifest,
            "ping": self._rpc_ping,
            "session.open": self._rpc_session_open,
            "session.resume": self._rpc_session_resume,
            "session.close": self._rpc_session_close,
            "obs.subscribe": self._rpc_obs_subscribe,
            "obs.unsubscribe": self._rpc_obs_unsubscribe,
            "action.submit": self._rpc_action_submit,
            "action.cancel": self._rpc_action_cancel,
            "action.status": self._rpc_action_status,
            "world.tick": self._rpc_world_tick,
            "world.reset": self._rpc_world_reset,
            **self._feature_handlers,
        }

    @property
    def _feature_handlers(self) -> dict[str, Callable[[_Conn, Any, dict[str, Any], int], None]]:
        """Methods of advertised features; without them they are unknown (AWP-VER-007)."""
        has = self.config.has
        out: dict[str, Callable[[_Conn, Any, dict[str, Any], int], None]] = {}
        if has("task"):
            out["task.update"] = self._rpc_task_update
        if has("sim"):
            out["world.snapshot"] = self._rpc_world_snapshot
            out["world.restore"] = self._rpc_world_restore
        if has("approval"):
            out["safety.approval.respond"] = self._rpc_approval_respond
        if has("transfer"):
            out["session.transfer"] = self._rpc_session_transfer
        return out

    @property
    def lockstep(self) -> bool:
        return self.config.mode == "lockstep"

    # ================================================================ entry points

    def connect(self, conn: Hashable, now: int, *, approver: bool = False) -> list[Output]:
        self._now = now
        self._started_ns = self._started_ns if self._started_ns is not None else now
        self._conns[conn] = _Conn(conn, last_rx_ns=now, last_ping_ns=now, approver=approver)
        return self._flush()

    def receive_text(self, conn: Hashable, text: str | bytes, now: int) -> list[Output]:
        self._now = now
        c = self._conns.get(conn)
        if c is None:
            return []
        try:
            msg = jsonrpc.decode(text)
        except AwpError as err:
            c.last_rx_ns = now
            self._send(c, jsonrpc.error(None, err))
            if err.code == ErrorCode.INTEGER_RANGE:  # AWP-CTL-009 closes the session
                if c.session is not None:
                    self._close_session(c.session, now, "session_closed")
                self._out.append(Close(conn, "AWP_INTEGER_RANGE"))
            return self._flush()
        return self.receive(conn, msg, now)

    def receive(self, conn: Hashable, msg: Message, now: int) -> list[Output]:
        self._now = now
        c = self._conns.get(conn)
        if c is None:
            return []
        c.last_rx_ns = now
        s = c.session
        self._audit_in(c, msg, now)
        if s is not None and "method" in msg:
            s.last_agent_ns = now  # only messages the agent originates hold off the watchdog
        if jsonrpc.is_request(msg):
            self._on_request(c, msg, now)
        elif jsonrpc.is_notification(msg) and msg["method"] == "obs.report" and s is not None:
            pass  # accepted (AWP-OBS-007); the reference world does not adapt rates
        elif jsonrpc.is_notification(msg) and msg["method"] == "cmd.frame" and s is not None:
            with contextlib.suppress(AwpError):
                schema.check("frame-inline", msg.get("params"))
                self._on_command_frame(s, from_inline(msg["params"]), now)
        return self._flush()

    def disconnect(self, conn: Hashable, now: int) -> list[Output]:
        self._now = now
        c = self._conns.pop(conn, None)
        if c is not None and c.session is not None and c.session.conn == conn:
            self._suspend(c.session, now, "connection_lost")
        return self._flush()

    def attach_stream(self, conn: Hashable, token: str, now: int) -> list[Output]:
        """A stream connection presenting `token` (AWP-SEC-004). Its channels move to it."""
        self._now = now
        s = self._tokens.get(token)
        if s is None or s.conn is None or s.closing_reason is not None or self.stream_url is None:
            self._out.append(Close(conn, "no active session for this token"))
            return self._flush()
        if s.stream_conn is not None:
            self._out.append(Close(s.stream_conn, "replaced by a new stream connection"))
            self._streams.pop(s.stream_conn, None)
        self._streams[conn] = s.id
        s.stream_conn = conn
        s.stream_lost_ns = None
        s.stream_degraded_reported = False
        for g in s.grants.values():
            g.resync = True  # the first frame on the new connection is a resync keyframe
            self._emit_frame(s, g, now)
        return self._flush()

    def receive_stream(self, conn: Hashable, data: bytes, now: int) -> list[Output]:
        """Agent→world frames. Without command channels every frame is discarded (AWP-CMD-003)."""
        self._now = now
        try:
            frame = decode(data)
        except AwpError as err:
            self._out.append(Close(conn, err.message))
            return self._flush()
        s = self.sessions.get(self._streams.get(conn, ""))
        if s is not None:
            if self._audit is not None:
                self._audit.record(
                    s.id,
                    self._clock(s, now),
                    "agent",
                    jsonrpc.notification("cmd.frame", to_inline(frame)),
                )
            s.last_agent_ns = now
            self._on_command_frame(s, frame, now)
        return self._flush()

    def stream_lost(self, conn: Hashable, now: int) -> list[Output]:
        self._now = now
        s = self.sessions.get(self._streams.pop(conn, ""))
        if s is not None and s.stream_conn == conn:
            s.stream_conn = None
            s.stream_lost_ns = now  # its channels wait for a new stream connection (AWP-TRN-010)
        return self._flush()

    def frame_bytes(self, send: SendFrame, now: int) -> bytes:
        """Encode a frame as it is handed to the transport, stamping `ts_send_ns` (AWP-OBS-006)."""
        s = self.sessions.get(send.session)
        frame = send.frame
        if s is not None and not self.lockstep:
            ts_send = max(self._clock(s, now), frame.ts_mono_ns)
            frame = replace(frame, ts_send_ns=ts_send)
            latency = ts_send - frame.ts_mono_ns
            s.telemetry.observation.append(latency)
            s.telemetry.channels.setdefault(frame.channel_id, []).append(latency)
        return encode(frame)

    def advance(self, now: int) -> list[Output]:
        """Run timers and, in streaming, the simulation. Call often (every few milliseconds)."""
        self._now = now
        self._started_ns = self._started_ns if self._started_ns is not None else now
        if not self.lockstep:
            last = self._last_step_ns if self._last_step_ns is not None else now
            self._step_arm(now - last)
            self._last_step_ns = now
            self._monitor_envelope(now)
        for s in list(self.sessions.values()):
            if not self.lockstep:
                self._update_actions(s, now)
                self._check_deadlines(s, now)
                self._check_watchdog(s, now)
            self._check_retention(s, now)
            self._check_stream(s, now)
        if not self.lockstep:
            self._check_approvals(now)
        for s in list(self.sessions.values()):
            if s.id in self.sessions and s.conn is not None and not self.lockstep:
                self._stream_frames(s, now)
                self._send_telemetry(s, now)
        for c in list(self._conns.values()):
            self._heartbeat(c, now)
        return self._flush()

    def frame_sent(self, send: Send, now: int) -> Message:
        """Stamp `ts_send_ns` as the frame is handed to the transport (AWP-OBS-006)."""
        s = self.sessions.get(send.session or "")
        if s is None or self.lockstep:
            return send.msg
        params = dict(send.msg["params"])
        ts_send = max(self._clock(s, now), params["ts_mono_ns"])
        params["ts_send_ns"] = ts_send
        latency = ts_send - params["ts_mono_ns"]
        s.telemetry.observation.append(latency)
        s.telemetry.channels.setdefault(params["channel_id"], []).append(latency)
        return {**send.msg, "params": params}

    def engage_estop(self, now: int, source: str = "operator") -> list[Output]:
        self._now = now
        if not self.estop:
            self.estop = True
            self.arm.halt()
            for s in list(self.sessions.values()):
                self._event(s, "e_stop_engaged", now, {"embodiment": EMBODIMENT, "source": source})
                for a in list(s.actions.values()):
                    if a.state in (ActionState.EXECUTING, ActionState.CANCELLING):
                        self._finish(s, a, ActionState.FAILED, now, reason="e_stop", aborted=True)
                    elif a.state.pre_execution:
                        self._finish(s, a, ActionState.CANCELLED, now, reason="e_stop")
        return self._flush()

    def disturb(self, offset_m: tuple[float, float, float]) -> None:
        """An external disturbance displaces the arm. The world detects a resulting envelope
        violation on its next step (AWP-ENV-003)."""
        p = self.arm.position
        self.arm.position = (p[0] + offset_m[0], p[1] + offset_m[1], p[2] + offset_m[2])

    def release_estop(self, now: int) -> list[Output]:
        self._now = now
        if self.estop:
            self.estop = False
            for s in list(self.sessions.values()):
                self._event(s, "e_stop_released", now, {"embodiment": EMBODIMENT})
        return self._flush()

    def session_of(self, conn: Hashable) -> Session | None:
        c = self._conns.get(conn)
        return c.session if c else None

    # ================================================================ plumbing

    def _endpoints(self) -> list[dict[str, Any]]:
        """Stream endpoints by preference; inline is last and always offered (AWP-TRN-004)."""
        endpoints: list[dict[str, Any]] = []
        if self.stream_url is not None:
            endpoints.append({"binding": "ws", "url": self.stream_url, "max_frame_bytes": 1 << 20})
        return [*endpoints, {"binding": "inline"}]

    def _flush(self) -> list[Output]:
        out, self._out = self._out, []
        return out

    def _clock(self, s: Session, now: int) -> int:
        """The session clock (AWP-CLK-001). In lockstep it counts advances, so that reset and
        restore, which move the tick, never move it backward (AWP-TIM-013)."""
        if self.lockstep:
            return self.advances * self.config.tick_ms * MS
        return now - s.origin_ns

    def _sim_ns(self) -> int | None:
        """Simulated time, carried on frames by sim-profile worlds (AWP-CLK-003)."""
        return self.tick * self.config.tick_ms * MS if self.config.has("sim") else None

    def _send(self, c: _Conn, msg: Message, *, latest_wins: bool = False) -> None:
        s = c.session
        self._out.append(Send(c.id, msg, s.id if s else None, latest_wins))
        if s is not None:
            if self._audit is not None:
                self._audit.record(s.id, self._clock(s, self._now), "world", msg)
        elif len(c.preamble) < _PREAMBLE_LIMIT:
            c.preamble.append(("world", msg))

    def _to_session(self, s: Session, msg: Message, *, latest_wins: bool = False) -> None:
        c = self._conns.get(s.conn) if s.conn is not None else None
        if c is not None:
            self._send(c, msg, latest_wins=latest_wins)
        elif self._audit is not None and "status_seq" in msg.get("params", {}):
            self._audit.record(s.id, self._clock(s, self._now), "world", msg)

    def _audit_in(self, c: _Conn, msg: Message, now: int) -> None:
        if c.session is None:
            if len(c.preamble) < _PREAMBLE_LIMIT:
                c.preamble.append(("agent", msg))
        elif self._audit is not None:
            self._audit.record(c.session.id, self._clock(c.session, now), "agent", msg)

    def _reply(self, c: _Conn, rid: Any, result: dict[str, Any]) -> None:
        self._send(c, jsonrpc.result(rid, result))

    def _sequenced(self, s: Session, method: str, params: dict[str, Any]) -> dict[str, Any]:
        seq = s.next_seq()
        params = {**params, "status_seq": seq}
        s.retain(seq, method, params)
        self._to_session(s, jsonrpc.notification(method, params))
        return params

    def _event(
        self, s: Session, event: str, now: int, detail: dict[str, Any] | None = None
    ) -> None:
        params: dict[str, Any] = {"event": event, "ts_mono_ns": self._clock(s, now)}
        if self.lockstep:
            params["tick"] = self.tick
        if detail is not None:
            params["detail"] = detail
        self._sequenced(s, "world.event", params)

    def _session_state(self, s: Session, state: str, reason: str, now: int) -> None:
        s.state = state
        self._sequenced(
            s,
            "session.state",
            {"state": state, "ts_mono_ns": self._clock(s, now), "reason": reason},
        )

    def _status_params(self, s: Session, a: Action, now: int, **extra: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "action_id": a.action_id,
            "state": str(a.state),
            "ts_mono_ns": self._clock(s, now),
        }
        if self.lockstep:
            params["tick"] = self.tick
        params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def _transition(
        self, s: Session, a: Action, state: ActionState, now: int, **extra: Any
    ) -> None:
        a.state = state
        a.status = self._sequenced(s, "action.status", self._status_params(s, a, now, **extra))
        if state.terminal:
            a.terminal_ns = now
        self._activate(s, now)

    def _result_status(self, s: Session, a: Action, now: int, **extra: Any) -> dict[str, Any]:
        """A transition first reported in a result: consumes a status_seq and is replayable."""
        params = self._status_params(s, a, now, **extra)
        seq = s.next_seq()
        params["status_seq"] = seq
        s.retain(seq, "action.status", params)
        a.status = params
        if a.state.terminal:
            a.terminal_ns = self._now
        return params

    def _activate(self, s: Session, now: int) -> None:
        if s.state == "ready":
            self._session_state(s, "active", "first_activity", now)

    def _new_session_token(self) -> str:
        return "st_" + secrets.token_urlsafe(24)

    # ================================================================ requests

    def _on_request(self, c: _Conn, msg: Message, now: int) -> None:
        method, rid = msg["method"], msg["id"]
        params = msg.get("params") or {}
        handler = self._handlers.get(method)
        try:
            if handler is None:
                raise AwpError(ErrorCode.METHOD_NOT_FOUND, method)
            if not isinstance(params, dict):
                raise AwpError(ErrorCode.INVALID_PARAMS, "params must be an object")
            if method != "initialize" and not c.initialized:
                raise AwpError(ErrorCode.INVALID_REQUEST, "initialize first")
            if method in _NEEDS_SESSION and c.session is None:
                raise AwpError(ErrorCode.INVALID_REQUEST, "no session on this connection")
            if method in _REFUSED_WHILE_CLOSING and c.session and c.session.closing_reason:
                raise AwpError(ErrorCode.SESSION_EXPIRED, "the session is closing")  # AWP-SES-011
            params_schema = schema.schema_for(method, "params")
            if params_schema is not None:
                schema.check(params_schema, params)
            handler(c, rid, params, now)
        except AwpError as err:
            self._send(c, jsonrpc.error(rid, err))
        except Exception:
            log.exception("internal error handling %s", method)
            self._send(c, jsonrpc.error(rid, AwpError(ErrorCode.INTERNAL_ERROR)))

    def _rpc_initialize(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        if "0.1" not in p["protocol_versions"]:
            raise AwpError(ErrorCode.VERSION_UNSUPPORTED, "this world speaks 0.1")
        c.initialized = True
        c.consumes = frozenset(p["consumes_modalities"])
        c.max_obs_rate_hz = p.get("max_obs_rate_hz")
        self._reply(c, rid, self.manifest)

    def _rpc_world_manifest(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        self._reply(c, rid, self.manifest)

    def _rpc_ping(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        s = c.session
        stamp = self._clock(s, now) if s else now - (self._started_ns or now)
        if s is not None and "last_status_seq" in p:
            s.acknowledge(min(p["last_status_seq"], s.seq))
        self._reply(
            c, rid, {"origin_ns": p["origin_ns"], "receive_ns": stamp, "transmit_ns": stamp}
        )

    def _rpc_session_open(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        if c.session is not None:
            raise AwpError(ErrorCode.SESSION_EXISTS)
        if p["mode"] != self.config.mode:
            raise AwpError(ErrorCode.TIME_MODEL_UNSUPPORTED, f"this world runs {self.config.mode}")
        if "embodiments" in p:
            raise AwpError(ErrorCode.EMBODIMENT_UNAVAILABLE, "multi-bind is not offered")
        if p.get("takeover") and not self.config.has("transfer"):
            raise AwpError(ErrorCode.EMBODIMENT_UNAVAILABLE, "transfer is not offered")
        embodiment = p.get("embodiment")
        if embodiment is not None and embodiment != EMBODIMENT:
            raise AwpError(ErrorCode.EMBODIMENT_UNAVAILABLE, f"unknown embodiment {embodiment}")
        if embodiment is not None and self._holder is not None and not p.get("takeover"):
            raise AwpError(ErrorCode.EMBODIMENT_UNAVAILABLE, f"{embodiment} is bound elsewhere")
        readable = self._readable(embodiment, c.consumes)
        requested = p.get("subscribe", [])
        for sub in requested:
            if sub["channel"] not in self._channels:
                raise AwpError(ErrorCode.CHANNEL_UNKNOWN, sub["channel"])
        if "task" in p and self.config.has("task"):
            self._check_task(p["task"])
        if p.get("takeover"):
            self._take_over(p, now)

        offered = self.manifest["embodiments"][0]["action_types"] if embodiment else []
        wanted = p.get("action_types", offered)
        admin = [op for op in p.get("admin", []) if self._may_grant_admin(op, embodiment)]
        session_id = f"sess_{secrets.token_hex(6)}"
        s = Session(
            id=session_id,
            token=self._new_session_token(),
            mode=p["mode"],
            embodiment=embodiment,
            origin_ns=now,
            clock_anchor=self._wall_clock().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            conn=c.id,
            action_types=[t for t in offered if t in wanted],
            admin=admin,
            last_agent_ns=now,
            last_telemetry_ns=now,
        )
        for sub in requested:
            if sub["channel"] in readable:
                self._grant(s, sub["channel"], sub.get("rate_hz"), now, c.max_obs_rate_hz)
        if "servo" in s.action_types:  # the streaming type implies its channel (AWP-CMD-002)
            cmd = self.manifest["command_channels"][0]
            s.grants[SERVO_CHANNEL] = ChannelGrant(
                SERVO_CHANNEL, s.next_channel_id, cmd["rate_hz"], cmd["loss_class"], command=True
            )
            s.next_channel_id += 1
        if self.config.has("task"):
            s.task = p.get("task")
        if "seed" in p and self.config.has("sim"):
            self.rng.seed(
                p["seed"]
            )  # seeds the noise; the world state is as it stands (AWP-REP-001)
        self.sessions[s.id] = s
        self._tokens[s.token] = s
        if embodiment is not None:
            self._holder = s
        c.session = s
        if self._audit is not None:
            header: dict[str, Any] = {"manifest": self.manifest, "session_id": s.id}
            if self.config.has("sim"):  # what a replay bundle starts from (AWP-REP-003)
                token = "snap_" + secrets.token_urlsafe(18)
                self._snapshots[token] = self.world_state()
                header.update(
                    snapshot_token=token,
                    initial_state=encode_state(self.world_state()),
                    config=encode_config(self.config),
                )
            self._audit.open(s.id, header)
            for direction, m in c.preamble:
                self._audit.record(s.id, 0, direction, m)
        c.preamble.clear()

        ready: dict[str, Any] = {
            "session_id": s.id,
            "session_token": s.token,
            "reconnect_window_ms": self.config.reconnect_window_ms,
            "heartbeat_interval_ms": self.config.heartbeat_interval_ms,
            "granted": self._granted(s),
            "stream_endpoints": self._endpoints(),
            "frame_tree": FRAME_TREE,
            "clock_anchor": s.clock_anchor,
        }
        if self.lockstep:
            ready["tick"] = self.tick
        self._reply(c, rid, ready)
        self._session_state(s, "ready", "opened", now)
        for g in s.grants.values():
            self._emit_frame(s, g, now)  # initial observation (AWP-TIM-009, AWP-OBS-005)

    def _rpc_session_resume(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        if c.session is not None:
            raise AwpError(ErrorCode.SESSION_EXISTS)
        s = self._tokens.get(p["session_token"])
        if s is None:
            if p["session_token"] in self._closed_tokens:
                raise AwpError(ErrorCode.SESSION_EXPIRED)
            raise AwpError(ErrorCode.SESSION_UNKNOWN, "treat the session as closed (AWP-SES-008)")
        if s.closing_reason is not None:
            raise AwpError(ErrorCode.SESSION_EXPIRED, "the session is closing")
        if s.conn is not None:  # the old connection's loss is not yet detected (AWP-SES-010)
            self._out.append(Close(s.conn, "replaced by session.resume"))
            old = self._conns.pop(s.conn, None)
            if old is not None:
                old.session = None
            self._suspend(s, now, "connection_replaced")
        replay_to = s.seq
        s.conn = c.id
        s.last_agent_ns = now  # session.resume is agent traffic on the session (AWP-SAF-003)
        s.suspended_ns = None
        s.degraded_reported = False
        s.acknowledge(min(p["last_status_seq"], s.seq))
        c.session = s
        for direction, m in c.preamble:
            if self._audit is not None:
                self._audit.record(s.id, self._clock(s, now), direction, m)
        c.preamble.clear()
        readable = self._readable(s.embodiment, c.consumes)
        for name in [n for n in s.grants if n not in readable]:
            del s.grants[name]  # the new connection does not consume it (AWP-AGM-001)
        for g in s.grants.values():
            g.resync = g.loss_class == "reliable"  # AWP-TRN-008
        ready: dict[str, Any] = {
            "session_id": s.id,
            "session_token": s.token,
            "reconnect_window_ms": self.config.reconnect_window_ms,
            "replay_to_status_seq": replay_to,
            "heartbeat_interval_ms": self.config.heartbeat_interval_ms,
            "granted": self._granted(s),
            "stream_endpoints": self._endpoints(),
            "frame_tree": FRAME_TREE,
            "clock_anchor": s.clock_anchor,
            "safe_state": s.safe_state,
        }
        if self.lockstep:
            ready["tick"] = self.tick
        self._reply(c, rid, ready)
        for method, params in s.replay_after(p["last_status_seq"]):
            self._send(c, jsonrpc.notification(method, params))
        self._session_state(s, "active", "resumed", now)
        if self.lockstep:
            for g in s.grants.values():
                self._emit_frame(s, g, now)

    def _rpc_session_close(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        assert c.session is not None
        c.session.closing.append((c.id, rid))
        if c.session.closing_reason is None:
            self._close_session(c.session, now, "session_closed")

    def _rpc_obs_subscribe(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        s = c.session
        assert s is not None
        readable = self._readable(s.embodiment, c.consumes)
        for sub in p["channels"]:
            if sub["channel"] not in self._channels:
                raise AwpError(ErrorCode.CHANNEL_UNKNOWN, sub["channel"])
            if sub["channel"] not in readable:
                raise AwpError(ErrorCode.FORBIDDEN, f"{sub['channel']} is not readable")
        new = []
        for sub in p["channels"]:
            existing = s.grants.get(sub["channel"])
            if existing is None:
                new.append(
                    self._grant(s, sub["channel"], sub.get("rate_hz"), now, c.max_obs_rate_hz)
                )
            elif existing.rate_hz is not None:
                existing.rate_hz = self._rate(sub["channel"], sub.get("rate_hz"), c.max_obs_rate_hz)
        self._reply(c, rid, {"granted": [g.to_wire() for g in s.grants.values()]})
        for g in new:
            self._emit_frame(s, g, now)

    def _rpc_obs_unsubscribe(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        s = c.session
        assert s is not None
        for name in p["channels"]:
            if name not in self._channels:
                raise AwpError(ErrorCode.CHANNEL_UNKNOWN, name)
        for name in p["channels"]:
            s.grants.pop(name, None)
        self._reply(c, rid, {"granted": [g.to_wire() for g in s.grants.values()]})

    def _rpc_world_reset(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        s = c.session
        assert s is not None
        if "reset" not in s.admin:
            raise AwpError(ErrorCode.FORBIDDEN, "world.reset requires the reset admin grant")
        initial = p.get("initial_state", "home")
        if initial not in self.manifest["initial_states"]:
            raise AwpError(ErrorCode.PARAMS_INVALID, f"unknown initial state {initial}")
        if "seed" in p and not self.config.has("sim"):
            raise AwpError(ErrorCode.PARAMS_INVALID, "seed needs capabilities.seed")
        self._reset_effects(s, now, {"kind": "reset", "initial_state": initial})
        self.arm.reset(HOME)
        self.tick = 0
        if "seed" in p:
            self.rng.seed(p["seed"])
        self._after_reset(c, rid, now)

    def _reset_effects(self, s: Session, now: int, detail: dict[str, Any]) -> None:
        """AWP-PRM-006 (1)-(2): warn every session, then end its actions with world_reset."""
        for other in list(self.sessions.values()):
            self._event(other, "world_resetting", now, {"initiator": s.id, **detail})
            for a in list(other.actions.values()):
                if a.state is ActionState.EXECUTING:
                    self._transition(other, a, ActionState.CANCELLING, now, reason="world_reset")
                if a.state is ActionState.CANCELLING:
                    self._finish(
                        other, a, ActionState.CANCELLED, now, reason="world_reset", aborted=True
                    )
                elif a.state.pre_execution:
                    self._finish(other, a, ActionState.CANCELLED, now, reason="world_reset")

    def _after_reset(self, c: _Conn, rid: Any, now: int) -> None:
        """AWP-PRM-006 (4): the result, then in lockstep a fresh frame carrying the new tick."""
        self._reply(c, rid, {"tick": self.tick} if self.lockstep else {})
        if self.lockstep:
            for other in self.sessions.values():
                for g in other.grants.values():
                    self._emit_frame(other, g, now)

    # ================================================================ snapshots (sim)

    def world_state(self) -> dict[str, Any]:
        """Everything restore brings back: the arm, the tick, and the noise generator."""
        return {"arm": copy.deepcopy(self.arm), "tick": self.tick, "rng": self.rng.getstate()}

    def load_state(self, state: dict[str, Any]) -> None:
        self.arm = copy.deepcopy(state["arm"])
        self.tick = state["tick"]
        self.rng.setstate(state["rng"])

    def _rpc_world_snapshot(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        s = c.session
        assert s is not None
        if "snapshot" not in s.admin:
            raise AwpError(ErrorCode.FORBIDDEN, "world.snapshot requires the snapshot admin grant")
        token = "snap_" + secrets.token_urlsafe(18)
        self._snapshots[token] = (
            self.world_state()
        )  # valid for the life of the process (AWP-REP-002)
        self._reply(c, rid, {"snapshot_token": token})

    def _rpc_world_restore(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        s = c.session
        assert s is not None
        if "restore" not in s.admin:
            raise AwpError(ErrorCode.FORBIDDEN, "world.restore requires the restore admin grant")
        state = self._snapshots.get(p["snapshot_token"])
        if state is None:
            raise AwpError(ErrorCode.PARAMS_INVALID, "unknown snapshot_token")
        self._reset_effects(s, now, {"kind": "restore"})
        self.load_state(state)
        self._after_reset(c, rid, now)

    # ================================================================ task

    def _check_task(self, task: dict[str, Any]) -> None:
        """AWP-TSK-002: text blocks; this world declares no modality for any other kind."""
        kinds = {b["type"] for b in task["content"]}
        if kinds - {"text"}:
            raise AwpError(
                ErrorCode.PARAMS_INVALID, f"unsupported task blocks {sorted(kinds - {'text'})}"
            )

    def _rpc_task_update(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        s = c.session
        assert s is not None
        self._check_task(p["task"])
        s.task = p["task"]  # whole, no diffing; running actions are unaffected (AWP-TSK-003)
        self._reply(c, rid, {})

    # ================================================================ actions

    def _rpc_action_submit(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        s = c.session
        assert s is not None
        received = self._clock(s, now)
        known = s.actions.get(p["action_id"])
        if known is not None:
            if not same_submission(known.content, p):
                raise AwpError(ErrorCode.ACTION_ID_CONFLICT, f"{p['action_id']} has other content")
            result = {
                k: v
                for k, v in known.status.items()
                if k in ("action_id", "state", "status_seq", "ts_mono_ns", "reason")
            }
            self._reply(c, rid, {**result, "received_ts_mono_ns": known.received_ns})
            return
        decl = self._admit(s, p, received, now)
        a = Action(p["action_id"], p, decl, received_ns=received)
        if decl["type"] == "move_to_pose":
            pose = p["params"]["pose"]
            a.target = (pose["p_m"][0], pose["p_m"][1], pose["p_m"][2])
            a.v_max = p["params"].get("max_velocity_mps", self.config.max_velocity_mps)
        elif decl["type"] == "park":
            a.target, a.v_max = PARK, self.config.max_velocity_mps
        bound = [v for v in (p.get("deadline_ms"), decl.get("max_duration_ms")) if v is not None]
        if bound and not self.lockstep:
            a.deadline_ns = received + min(bound) * MS  # AWP-ACT-004, AWP-ACT-008
        preempt = p.get("preempt") or self._policies(decl)[0]
        busy = self._busy(s, a.group)
        s.actions[a.action_id] = a
        s.last_admitted_ns = received
        a.replaces = preempt in ("replace", "blend")
        a.blends = preempt == "blend"
        if a.replaces:
            self._supersede_pending(s, a.group, now)
        if decl.get("requires_approval"):
            state = ActionState.PENDING_APPROVAL  # admitted now, decided later (AWP-APR-001)
        elif preempt == "queue" and busy:
            state = ActionState.QUEUED
        else:
            state = ActionState.ACCEPTED
        a.state = state
        result = self._result_status(s, a, now)
        admission = self._clock(s, now) - received
        s.telemetry.admission.append(admission)
        if "basis_ts_mono_ns" in p:
            s.telemetry.observation_to_action.append(max(0, received - p["basis_ts_mono_ns"]))
        self._reply(
            c,
            rid,
            {
                "action_id": a.action_id,
                "state": str(state),
                "status_seq": result["status_seq"],
                "received_ts_mono_ns": received,
                "ts_mono_ns": result["ts_mono_ns"],
            },
        )
        self._activate(s, now)
        if state is ActionState.PENDING_APPROVAL:
            self._request_approval(s, a, now)
        elif state is ActionState.QUEUED:
            s.queue(a.group).append(a)
        elif self.lockstep:
            s.staged.append(a)  # AWP-TIM-010
        else:
            self._execute(s, a, now)

    def _admit(self, s: Session, p: dict[str, Any], received: int, now: int) -> dict[str, Any]:
        """Admission checks, in the order a first-time submission is refused."""
        if s.embodiment is None:
            raise AwpError(ErrorCode.FORBIDDEN, "observer sessions cannot act (AWP-EMB-004)")
        if p["type"] not in s.action_types:
            raise AwpError(ErrorCode.FORBIDDEN, f"{p['type']} is not granted")
        if p.get("embodiment_id", s.embodiment) != s.embodiment:
            raise AwpError(ErrorCode.FORBIDDEN, f"{p['embodiment_id']} is not bound")
        if self.estop:
            raise AwpError(ErrorCode.ESTOP_ACTIVE)
        interval = S / self.config.max_action_rate_hz
        clock = self._clock(s, now)
        if s.last_admitted_ns is not None and clock - s.last_admitted_ns < interval:
            wait = (s.last_admitted_ns + interval - clock) / MS
            raise AwpError(
                ErrorCode.ENVELOPE_EXCEEDED,
                "max_action_rate_hz",
                retryable=True,
                retry_after_ms=max(1, round(wait)),
            )
        decl: dict[str, Any] = self._decls[p["type"]]
        problems = self._params.errors(p["type"], p["params"])
        if problems:
            raise AwpError(ErrorCode.PARAMS_INVALID, "; ".join(problems[:3]))
        if p.get("preempt", self._policies(decl)[0]) not in self._policies(decl):
            raise AwpError(
                ErrorCode.PARAMS_INVALID, f"preempt must be one of {self._policies(decl)}"
            )
        if p["type"] == "move_to_pose":
            self._check_envelope(p["params"])
        self._check_intent(p, received)
        policy = p.get("preempt", self._policies(decl)[0])
        group = decl.get("concurrency_group", f"_{p['type']}")
        if policy == "reject" and self._busy(s, group):
            raise AwpError(ErrorCode.BUSY, f"{group} is busy")
        if policy == "queue" and self._busy(s, group) and len(s.queue(group)) >= decl["max_queue"]:
            raise AwpError(ErrorCode.QUEUE_FULL)
        return decl

    def _check_envelope(self, params: dict[str, Any]) -> None:
        pose = params["pose"]
        if pose["frame"] != "base":
            raise AwpError(ErrorCode.PARAMS_INVALID, "pose.frame must be base")
        lo, hi = self.config.aabb_m
        if not all(lo[i] <= pose["p_m"][i] <= hi[i] for i in range(3)):
            raise AwpError(ErrorCode.ENVELOPE_EXCEEDED, "target outside the spatial envelope")
        if params.get("max_velocity_mps", 0) > self.config.max_velocity_mps:
            raise AwpError(ErrorCode.ENVELOPE_EXCEEDED, "max_velocity_mps above the envelope")

    def _check_intent(self, p: dict[str, Any], now_session: int) -> None:
        """AWP-SAF-013; binding in streaming, advisory (and ignored) in lockstep."""
        if self.lockstep:
            return
        basis = p.get("basis_ts_mono_ns")
        if basis is not None and now_session - basis > self.config.max_basis_age_ms * MS:
            raise AwpError(ErrorCode.STALE_INTENT, "basis older than max_basis_age_ms")
        if p.get("valid_until_ns") is not None and p["valid_until_ns"] <= now_session:
            raise AwpError(ErrorCode.STALE_INTENT, "valid_until_ns has passed")

    def _still_valid(self, s: Session, a: Action, now: int) -> str | None:
        """Re-check a queued action as it becomes accepted; returns a rejection reason."""
        clock = self._clock(s, now)
        if a.deadline_ns is not None and clock > a.deadline_ns:
            return "deadline_exceeded"
        try:
            self._check_intent(a.content, clock)
        except AwpError:
            return "stale_intent"
        return None

    def _policies(self, decl: dict[str, Any]) -> list[str]:
        pre = decl["preemption"]
        return [pre] if isinstance(pre, str) else list(pre)

    def _busy(self, s: Session, group: str) -> bool:
        return group in s.running or bool(s.queue(group)) or any(a.group == group for a in s.staged)

    def _supersede_pending(self, s: Session, group: str, now: int) -> None:
        pending = [
            a
            for a in s.actions.values()
            if a.state is ActionState.PENDING_APPROVAL and a.group == group
        ]
        for a in [*s.queue(group), *[a for a in s.staged if a.group == group], *pending]:
            self._finish(s, a, ActionState.CANCELLED, now, reason="superseded")

    def _preempt_running(self, s: Session, group: str, now: int, *, blended: bool = False) -> None:
        """A replacing action takes over its group when it begins executing (AWP-PRE-003); a
        blending one merges into the motion under way, so the arm does not stop (AWP-PRE-004)."""
        running = s.running.get(group)
        if running is None:
            return
        if running.state is ActionState.EXECUTING and running.failing_with is None:
            self._finish(s, running, ActionState.PREEMPTED, now, blended=blended or None)
        else:  # an abort already under way ends as it would have (AWP-LIF-008)
            state = ActionState.FAILED if running.failing_with else ActionState.CANCELLED
            reason = running.failing_with or running.cancel_reason
            self._finish(s, running, state, now, reason=reason, aborted=True)

    def _execute(self, s: Session, a: Action, now: int) -> None:
        if a.replaces:
            self._preempt_running(s, a.group, now, blended=a.blends)
        s.running[a.group] = a
        a.last_progress_ns = now
        a.last_frame_ns = now
        exiting_safe_state = s.safe_state
        if a.target is not None:
            self.arm.move_to(a.target, a.v_max)
        elif a.decl["duration"] == "streaming":
            self.arm.servo()  # setpoints arrive on the command channel (AWP-CMD-003)
            a.stream = {"frames_applied": 0, "last_seq": 0, "clamped_count": 0}
        else:
            self.arm.stop()
        if exiting_safe_state:
            s.safe_state = False
            self._event(s, "safe_state_exited", now, {"embodiment": EMBODIMENT})  # AWP-SAF-008
        self._transition(s, a, ActionState.EXECUTING, now, progress=0.0)
        if a.decl["duration"] == "instant":
            self._finish(s, a, ActionState.COMPLETED, now, progress=1.0)

    def _finish(
        self,
        s: Session,
        a: Action,
        state: ActionState,
        now: int,
        *,
        reason: str | None = None,
        aborted: bool = False,
        progress: float | None = None,
        blended: bool | None = None,
    ) -> None:
        was_executing = a.state in (ActionState.EXECUTING, ActionState.CANCELLING)
        if a.approval_id is not None:
            self._approvals.pop(a.approval_id, None)
        self._transition(
            s,
            a,
            state,
            now,
            reason=reason,
            progress=progress,
            blended=blended,
            aborted_at_progress=round(self.arm.progress, 4) if aborted else None,
            stream=a.stream,
        )
        if s.running.get(a.group) is a:
            del s.running[a.group]
        queue = s.queue(a.group)
        if a in queue:
            queue.remove(a)
        if a in s.staged:
            s.staged.remove(a)
        if was_executing and self.arm.phase in (Phase.MOVING, Phase.SERVO) and not blended:
            self.arm.stop()

    def _rpc_action_cancel(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        s = c.session
        assert s is not None
        a = s.actions.get(p["action_id"])
        if a is None:
            raise AwpError(ErrorCode.ACTION_UNKNOWN, p["action_id"])
        if a.state.pre_execution:
            self._remove_pending(s, a)
            a.state = ActionState.CANCELLED
            res = self._result_status(s, a, now, reason="cancelled_by_agent")
        elif a.state is ActionState.EXECUTING:
            a.state = ActionState.CANCELLING
            a.cancel_reason = "cancelled_by_agent"
            a.cancel_started_ns = now
            self.arm.stop()
            res = self._result_status(s, a, now, reason="cancelled_by_agent")
        else:
            res = a.status  # cancelling or terminal: nothing changes
        self._reply(
            c, rid, {k: res[k] for k in ("action_id", "state", "status_seq", "reason") if k in res}
        )
        self._activate(s, now)

    def _remove_pending(self, s: Session, a: Action) -> None:
        queue = s.queue(a.group)
        if a in queue:
            queue.remove(a)
        if a in s.staged:
            s.staged.remove(a)

    def _rpc_action_status(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        s = c.session
        assert s is not None
        a = s.actions.get(p["action_id"])
        if a is None:
            raise AwpError(ErrorCode.ACTION_UNKNOWN, p["action_id"])
        self._reply(c, rid, a.status)

    def _promote(self, s: Session, group: str, now: int) -> None:
        """Move the queue head into execution once its group frees (AWP-PRE-002)."""
        queue = s.queue(group)
        while queue and group not in s.running:
            a = queue.popleft()
            reason = self._still_valid(s, a, now)
            if reason is not None:
                self._transition(s, a, ActionState.REJECTED, now, reason=reason)
                continue
            self._transition(s, a, ActionState.ACCEPTED, now)
            if self.lockstep:
                s.staged.append(a)
                return
            self._execute(s, a, now)

    # ================================================================ approval

    def _request_approval(self, s: Session, a: Action, now: int) -> None:
        """AWP-APR-001: to every approver connection; logged in the requester's audit record."""
        approval_id = "ap_" + secrets.token_hex(6)
        expires = self._clock(s, now) + self.config.approval_timeout_ms * MS
        a.approval_id = approval_id
        self._approvals[approval_id] = (s.id, a.action_id, expires)
        params: dict[str, Any] = {
            "approval_id": approval_id,
            "action_id": a.action_id,
            "type": a.type,
            "params": a.content["params"],
            "requester": {"session": s.id, "embodiment": s.embodiment},
            "expires_at_ns": expires,
        }
        if s.task is not None:
            params["task"] = s.task  # AWP-TSK-005
        msg = jsonrpc.notification("safety.approval_requested", params)
        if self._audit is not None:
            self._audit.record(s.id, self._clock(s, now), "world", msg)
        for c in self._conns.values():
            if c.approver and c.initialized:
                self._out.append(Send(c.id, msg))

    def _rpc_approval_respond(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        if not c.approver:
            raise AwpError(ErrorCode.FORBIDDEN, "this connection may not decide approvals")
        entry = self._approvals.get(p["approval_id"])
        if entry is None:
            raise AwpError(ErrorCode.PARAMS_INVALID, "unknown or already decided approval_id")
        session_id, action_id, _ = entry
        s = self.sessions[session_id]
        a = s.actions[action_id]
        if self._audit is not None:
            self._audit.record(
                s.id,
                self._clock(s, now),
                "approver",
                jsonrpc.request(rid, "safety.approval.respond", p),
            )
        self._reply(c, rid, {})
        if p["decision"] == "deny":
            self._finish(s, a, ActionState.REJECTED, now, reason="approval_denied")  # AWP-APR-002
            return
        del self._approvals[p["approval_id"]]
        a.approval_id = None
        reason = self._still_valid(s, a, now)
        if reason is not None:
            self._transition(s, a, ActionState.REJECTED, now, reason=reason)
        elif self._busy(s, a.group):
            self._transition(s, a, ActionState.QUEUED, now)
            s.queue(a.group).append(a)
        else:
            self._transition(s, a, ActionState.ACCEPTED, now)
            if self.lockstep:
                s.staged.append(a)
            else:
                self._execute(s, a, now)

    def _check_approvals(self, now: int) -> None:
        """AWP-APR-003: no decision by expires_at_ns (the session clock) is a rejection."""
        for approval_id, (session_id, action_id, expires) in list(self._approvals.items()):
            s = self.sessions.get(session_id)
            if s is None or self._clock(s, now) <= expires:
                continue
            a = s.actions.get(action_id)
            if a is not None and a.state is ActionState.PENDING_APPROVAL:
                self._finish(s, a, ActionState.REJECTED, now, reason="approval_timeout")
            self._approvals.pop(approval_id, None)

    # ================================================================ transfer

    def _rpc_session_transfer(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        s = c.session
        assert s is not None
        if s.embodiment is None:
            raise AwpError(ErrorCode.FORBIDDEN, "only the holder of an embodiment can transfer it")
        ttl = p.get("expires_in_ms", 30000)
        token = "tt_" + secrets.token_urlsafe(18)
        self._transfers[token] = (s.id, now + ttl * MS)
        self._reply(c, rid, {"transfer_token": token, "expires_in_ms": ttl})

    def _take_over(self, p: dict[str, Any], now: int) -> None:
        """AWP-EMB-003: a single-use token from the holder moves the embodiment to this open."""
        entry = self._transfers.pop(p.get("transfer_token", ""), None)
        holder = self.sessions.get(entry[0]) if entry else None
        if (
            entry is None
            or now > entry[1]
            or holder is None
            or holder.embodiment != p.get("embodiment")
        ):
            raise AwpError(ErrorCode.EMBODIMENT_UNAVAILABLE, "invalid or expired transfer_token")
        for a in list(holder.actions.values()):
            if a.state is ActionState.EXECUTING:
                self._finish(holder, a, ActionState.PREEMPTED, now, reason="transferred")
            elif a.state is ActionState.CANCELLING:  # the abort ends as it would have (AWP-LIF-008)
                state = ActionState.FAILED if a.failing_with else ActionState.CANCELLED
                self._finish(
                    holder, a, state, now, reason=a.failing_with or a.cancel_reason, aborted=True
                )
            elif a.state.pre_execution:
                self._finish(holder, a, ActionState.CANCELLED, now, reason="transferred")
        detail = {"embodiment": holder.embodiment}
        holder.embodiment = None  # it continues as an observer session
        self._holder = None
        self._event(holder, "embodiment_transferred", now, detail)

    # ================================================================ execution

    def _step_arm(self, dt_ns: int) -> None:
        while dt_ns > 0:
            step = min(dt_ns, 5 * MS)
            self.arm.step(step / S)
            dt_ns -= step

    def _update_actions(self, s: Session, now: int) -> None:
        for a in list(s.running.values()):
            aborting = a.state is ActionState.CANCELLING or a.failing_with is not None
            if aborting and self.arm.at_rest:
                if a.state is ActionState.CANCELLING:
                    self._finish(
                        s, a, ActionState.CANCELLED, now, reason=a.cancel_reason, aborted=True
                    )
                else:
                    self._finish(s, a, ActionState.FAILED, now, reason=a.failing_with, aborted=True)
            elif aborting and self._abort_overdue(a, now):  # AWP-LIF-010
                self._finish(s, a, ActionState.FAILED, now, reason="abort_failed", aborted=True)
                self._enter_safe_state(s, now)
            elif a.state is ActionState.EXECUTING and a.decl["duration"] == "streaming":
                if now - a.last_frame_ns > a.decl["watchdog_ms"] * MS:  # AWP-CMD-005
                    a.failing_with = "watchdog"
                    a.cancel_started_ns = now
                    self.arm.stop()
                elif self._progress_due(a, now):
                    a.last_progress_ns = now
                    self._transition(s, a, ActionState.EXECUTING, now, stream=dict(a.stream or {}))
            elif a.state is ActionState.EXECUTING and not aborting:
                if self.arm.phase is Phase.IDLE and self.arm.at_rest:
                    self._finish(s, a, ActionState.COMPLETED, now, progress=1.0)
                elif self._progress_due(a, now):
                    a.last_progress_ns = now
                    self._transition(
                        s, a, ActionState.EXECUTING, now, progress=round(self.arm.progress, 4)
                    )
        for group in [g for g, q in s.queues.items() if q and g not in s.running]:
            self._promote(s, group, now)
        if s.closing_reason is not None and not s.running:
            self._finalize_close(s, now)

    def _abort_overdue(self, a: Action, now: int) -> bool:
        limit = a.decl.get("max_abort_ms")
        return (
            not self.lockstep
            and limit is not None
            and a.cancel_started_ns is not None
            and now - a.cancel_started_ns > limit * MS
        )

    def _progress_due(self, a: Action, now: int) -> bool:
        if self.lockstep:
            return bool(a.status.get("tick") != self.tick)  # once per advance (AWP-LIF-003)
        return now - a.last_progress_ns >= self.config.progress_interval_ms * MS

    def _monitor_envelope(self, now: int) -> None:
        """AWP-ENV-003: leaving the envelope during execution fails the action (reason
        `envelope`, after its safe abort) and emits `envelope_violation`."""
        lo, hi = self.config.aabb_m
        p = self.arm.position
        inside = all(lo[i] - 1e-9 <= p[i] <= hi[i] + 1e-9 for i in range(3))
        violating = not inside or self.arm.speed > self.config.max_velocity_mps + 1e-9
        if violating and not self._violating:
            self.arm.stop()
            for s in list(self.sessions.values()):
                self._event(s, "envelope_violation", now, {"embodiment": EMBODIMENT})
                for a in s.running.values():
                    if a.state is ActionState.EXECUTING and a.failing_with is None:
                        a.failing_with = "envelope"
                        a.cancel_started_ns = now
        self._violating = violating

    def _check_deadlines(self, s: Session, now: int) -> None:
        clock = self._clock(s, now)
        for a in list(s.actions.values()):
            if a.deadline_ns is None or clock <= a.deadline_ns or a.state.terminal:
                continue
            if a.state.pre_execution:
                self._remove_pending(s, a)
                self._transition(s, a, ActionState.REJECTED, now, reason="deadline_exceeded")
            elif a.state is ActionState.EXECUTING and a.failing_with is None:
                a.failing_with = "deadline_exceeded"  # fails once the safe abort completes
                a.cancel_started_ns = now
                self.arm.stop()

    def _check_watchdog(self, s: Session, now: int) -> None:
        if s.embodiment is None or s.safe_state or s.state == "closed":
            return
        if now - s.last_agent_ns > self.config.watchdog_ms * MS:
            self._enter_safe_state(s, now)

    def _enter_safe_state(self, s: Session, now: int) -> None:
        """AWP-SAF-004: behavior first, then terminations, then the event."""
        self.arm.stop()
        if self.arm.stuck:
            self.arm.halt()
        s.safe_state = True
        for a in list(s.actions.values()):
            if a.state in (ActionState.EXECUTING, ActionState.CANCELLING):
                self._finish(s, a, ActionState.FAILED, now, reason="connection_lost", aborted=True)
            elif a.state.pre_execution:
                self._finish(s, a, ActionState.CANCELLED, now, reason="safe_state")
        self._event(
            s, "safe_state_entered", now, {"behavior": "safe_stop", "embodiment": EMBODIMENT}
        )

    # ================================================================ lockstep

    def _rpc_world_tick(self, c: _Conn, rid: Any, p: dict[str, Any], now: int) -> None:
        s = c.session
        assert s is not None
        if not self.lockstep:
            raise AwpError(ErrorCode.TIME_MODEL_UNSUPPORTED, "world.tick is lockstep-only")
        if s.embodiment is None:
            raise AwpError(ErrorCode.TICK_NOT_AUTHORIZED, "observer sessions cannot tick")
        if p["expected_tick"] != self.tick:
            raise AwpError(
                ErrorCode.TICK_MISMATCH,
                f"expected_tick {p['expected_tick']}, current {self.tick}",
                tick=self.tick,
            )
        count = p.get("count", 1)
        if not 1 <= count <= 10_000:
            raise AwpError(ErrorCode.INVALID_PARAMS, "count must be 1..10000")
        for _ in range(count):
            self._advance_tick(now)
        self._reply(c, rid, {"tick": self.tick})

    def _advance_tick(self, now: int) -> None:
        """One advance: statuses and events, then a frame per per-tick channel (AWP-TIM-003)."""
        self.tick += 1
        self.advances += 1
        self._check_approvals(now)
        for s in list(self.sessions.values()):
            for a in list(s.staged):
                s.staged.remove(a)
                if a.state is ActionState.ACCEPTED:
                    self._execute(s, a, now)
        self.arm.step(self.config.tick_ms / 1000)
        self._monitor_envelope(now)
        for s in list(self.sessions.values()):
            self._update_actions(s, now)
        for s in list(self.sessions.values()):
            if s.conn is not None:
                for g in s.grants.values():
                    self._emit_frame(s, g, now)

    # ================================================================ frames and telemetry

    def _readable(self, embodiment: str | None, consumes: frozenset[str]) -> set[str]:
        """Channels the session may read: its embodiment's (every channel for an observer), and
        only in modalities the agent declared (AWP-AGM-001)."""
        names = (
            set(self._channels)
            if embodiment is None
            else set(self.manifest["embodiments"][0]["channels"])
        )
        return {
            n for n in names if n in self._channels and self._channels[n]["modality"] in consumes
        }

    def _rate(
        self, channel: str, requested: float | None, cap: float | None = None
    ) -> float | None:
        """The granted rate: never above the declared one, the request, or the agent's
        max_obs_rate_hz (AWP-NEG-003, AWP-AGM-002)."""
        declared: float | None = self._channels[channel]["rate_hz"]
        if declared is None:
            return None
        bounds = [declared, *(float(v) for v in (requested, cap) if v is not None)]
        return min(bounds)

    def _grant(
        self, s: Session, channel: str, rate: float | None, now: int, cap: float | None = None
    ) -> ChannelGrant:
        g = ChannelGrant(
            channel,
            s.next_channel_id,
            self._rate(channel, rate, cap),
            self._channels[channel]["loss_class"],
            next_due_ns=now,
        )
        s.next_channel_id += 1
        s.grants[channel] = g
        return g

    def _granted(self, s: Session) -> dict[str, Any]:
        return {
            "channels": [g.to_wire() for g in s.grants.values()],
            "action_types": s.action_types,
            "admin": s.admin,
            "envelopes": [self.config.envelope] if s.embodiment else [],
        }

    def _may_grant_admin(self, op: str, embodiment: str | None) -> bool:
        if op == "tick":
            return self.lockstep and embodiment is not None
        if op in ("reset", "restore"):  # at most one session at a time (AWP-PRM-005)
            if op == "restore" and not self.config.has("sim"):
                return False
            return not any(op in other.admin for other in self.sessions.values())
        return op == "snapshot" and self.config.has("sim")

    def _payload(self, channel: str) -> bytes:
        arm = self.arm
        if channel == "proprio":
            noise = self.config.has("sim")
            body: dict[str, Any] = {
                "p_m": [
                    round(v + (self.rng.gauss(0, 1e-4) if noise else 0), 6) for v in arm.position
                ],
                "v_mps": [round(v, 6) for v in arm.velocity],
            }
        else:
            holder = self._holder
            running = holder.running.get("arm_motion") if holder else None
            body = {
                "phase": str(arm.phase),
                "target_m": list(arm.target)
                if arm.target and arm.phase is not Phase.IDLE
                else None,
                "action_id": running.action_id if running else None,
            }
        return json.dumps(body, separators=(",", ":")).encode()

    def _emit_frame(self, s: Session, g: ChannelGrant, now: int) -> None:
        if g.command:
            return  # agent→world
        if s.conn is None or (s.stream_conn is None and s.stream_lost_ns is not None):
            return  # no control connection, or the channel waits for its stream (AWP-TRN-010)
        g.seq += 1
        frame = Frame(
            channel_id=g.channel_id,
            seq=g.seq,
            ts_mono_ns=self._clock(s, now),
            payload=self._payload(g.name),
            keyframe=True,
            resync=g.resync,
            tick=self.tick if self.lockstep else None,
            ts_sim_ns=self._sim_ns() if self.lockstep else None,
        )
        g.resync = False
        streaming_lw = not self.lockstep and g.loss_class == "latest-wins"
        inline = jsonrpc.notification("obs.frame", to_inline(frame))
        if s.stream_conn is not None:
            self._out.append(SendFrame(s.stream_conn, frame, s.id, streaming_lw))
            if self._audit is not None:
                self._audit.record(s.id, frame.ts_mono_ns, "world", inline)
        else:
            self._to_session(s, inline, latest_wins=streaming_lw)
        self._activate(s, now)

    # ================================================================ command channels

    def _on_command_frame(self, s: Session, frame: Frame, now: int) -> None:
        """A setpoint: applied only while its servo action executes (AWP-CMD-003), in seq order
        (AWP-CMD-007), and envelope-checked before actuation (AWP-CMD-006)."""
        grant = s.grants.get(SERVO_CHANNEL)
        a = s.running.get("arm_motion")
        if grant is None or frame.channel_id != grant.channel_id or a is None:
            return
        if a.decl["duration"] != "streaming" or a.state is not ActionState.EXECUTING:
            return
        if a.failing_with is not None or frame.seq <= a.command_seq:
            return
        try:
            v = json.loads(frame.payload)["v_mps"]
            velocity = (float(v[0]), float(v[1]), float(v[2]))
        except (ValueError, KeyError, TypeError, IndexError):
            return
        assert a.stream is not None
        a.command_seq = frame.seq
        a.last_frame_ns = now
        s.telemetry.command.append(max(0, self._clock(s, now) - frame.ts_mono_ns))
        if sum(x * x for x in velocity) ** 0.5 > self.config.max_velocity_mps:
            a.stream["clamped_count"] += 1  # on_violation: reject drops the frame
            return
        self.arm.command(velocity)
        a.stream["frames_applied"] += 1
        a.stream["last_seq"] = frame.seq

    def _stream_frames(self, s: Session, now: int) -> None:
        for g in s.grants.values():
            if g.command or g.rate_hz is None or now < g.next_due_ns:
                continue
            period = round(S / g.rate_hz)
            g.next_due_ns = max(g.next_due_ns + period, now - period)
            self._emit_frame(s, g, now)

    def _send_telemetry(self, s: Session, now: int) -> None:
        interval = self.config.telemetry_interval_ms * MS
        if now - s.last_telemetry_ns < interval:
            return
        window = (now - s.last_telemetry_ns) // MS
        s.last_telemetry_ns = now
        self._to_session(s, jsonrpc.notification("session.telemetry", s.telemetry.snapshot(window)))
        s.telemetry = Telemetry()

    # ================================================================ liveness and retention

    def _heartbeat(self, c: _Conn, now: int) -> None:
        interval = self.config.heartbeat_interval_ms * MS
        if c.session is None:
            if now - c.last_rx_ns > max(15 * S, 3 * interval):  # AWP-SES-012
                self._out.append(Close(c.id, "idle"))
                self._conns.pop(c.id, None)
            return
        if now - c.last_rx_ns > 3 * interval:  # AWP-SAF-002
            self._out.append(Close(c.id, "heartbeat lost"))
            self._conns.pop(c.id, None)
            if c.session.conn == c.id:
                self._suspend(c.session, now, "connection_lost")
            return
        if now - c.last_ping_ns >= interval:
            c.last_ping_ns = now
            rid = f"w{c.next_id}"
            c.next_id += 1
            self._send(c, jsonrpc.request(rid, "ping", {"origin_ns": self._clock(c.session, now)}))

    def _end_stream(self, s: Session, reason: str) -> None:
        if s.stream_conn is not None:
            self._out.append(Close(s.stream_conn, reason))
            self._streams.pop(s.stream_conn, None)
        s.stream_conn = None
        s.stream_lost_ns = None  # frames go inline until the agent attaches again (AWP-TRN-008)

    def _check_stream(self, s: Session, now: int) -> None:
        """A reliable channel whose stream connection is gone is degraded (AWP-SAF-009)."""
        if s.stream_lost_ns is None or s.stream_degraded_reported:
            return
        for g in s.grants.values():
            stale = self._channels[g.name].get("stale_after_ms")
            limit = (stale or 2000 / g.rate_hz) * MS if g.rate_hz else None
            if g.loss_class == "reliable" and limit and now - s.stream_lost_ns > limit:
                s.stream_degraded_reported = True
                self._event(s, "channel_degraded", now, {"channel": g.name})

    def _suspend(self, s: Session, now: int, reason: str) -> None:
        self._end_stream(s, "session suspended")
        s.conn = None
        s.suspended_ns = now
        if s.state != "closed":
            self._session_state(s, "suspended", reason, now)

    def _check_retention(self, s: Session, now: int) -> None:
        window = self.config.reconnect_window_ms * MS
        expired = [
            a.action_id
            for a in s.actions.values()
            if a.terminal_ns is not None and now - a.terminal_ns > window
        ]
        for action_id in expired:
            del s.actions[
                action_id
            ]  # retained for the window after the terminal transition (AWP-ACT-006)
        if s.suspended_ns is None:
            return
        away = now - s.suspended_ns
        if not s.degraded_reported:
            for g in s.grants.values():
                stale = self._channels[g.name].get("stale_after_ms")
                if (
                    g.loss_class == "reliable"
                    and g.rate_hz
                    and away > (stale or 2000 / g.rate_hz) * MS
                ):
                    s.degraded_reported = True
                    self._event(s, "channel_degraded", now, {"channel": g.name})  # AWP-SAF-009
        if away > self.config.reconnect_window_ms * MS:
            if s.embodiment is not None and not s.safe_state and not self.lockstep:
                self._enter_safe_state(s, now)  # AWP-SES-005
            self._close_session(s, now, "window_expired")

    # ================================================================ closing

    def _close_session(self, s: Session, now: int, reason: str) -> None:
        """AWP-SES-006: pre-execution cancelled; executing aborted through cancelling."""
        s.closing_reason = reason
        for a in s.pre_execution():
            self._finish(s, a, ActionState.CANCELLED, now, reason="session_closed")
        for a in list(s.running.values()):
            a.cancel_reason = "session_closed"  # close outranks a cancel in progress (AWP-LIF-008)
            if a.state is ActionState.EXECUTING and a.failing_with is None:
                a.cancel_started_ns = now
                self._transition(s, a, ActionState.CANCELLING, now, reason="session_closed")
                self.arm.stop()
        if self.lockstep or s.conn is None:
            self.arm.halt()  # no simulated time will pass for the abort to run in
            for a in list(s.running.values()):
                state = ActionState.FAILED if a.failing_with else ActionState.CANCELLED
                why = a.failing_with or a.cancel_reason
                self._finish(s, a, state, now, reason=why, aborted=True)
        if not s.running:
            self._finalize_close(s, now)

    def _finalize_close(self, s: Session, now: int) -> None:
        self._session_state(s, "closed", s.closing_reason or "session_closed", now)
        self._end_stream(s, "session closed")
        for conn, rid in s.closing:
            if conn in self._conns:
                self._send(self._conns[conn], jsonrpc.result(rid, {}))
        s.closing.clear()
        if self._holder is s:
            self._holder = None
        self.sessions.pop(s.id, None)
        self._tokens.pop(s.token, None)
        self._closed_tokens[s.token] = None
        while len(self._closed_tokens) > _CLOSED_TOKENS_LIMIT:
            self._closed_tokens.popitem(last=False)
        if s.conn is not None and s.conn in self._conns:
            self._conns[s.conn].session = None
        s.conn = None
        if self._audit is not None:
            self._audit.close(s.id)


def encode_state(state: dict[str, Any]) -> dict[str, Any]:
    """`World.world_state()` as JSON, for replay bundles."""
    arm = {f.name: getattr(state["arm"], f.name) for f in fields(Arm)}
    version, internal, gauss = state["rng"]
    return {
        "arm": json.loads(json.dumps(arm)),
        "tick": state["tick"],
        "rng": [version, list(internal), gauss],
    }


def decode_state(data: dict[str, Any]) -> dict[str, Any]:
    def tuples(v: Any) -> Any:
        return tuple(tuples(x) for x in v) if isinstance(v, list) else v

    arm = {k: tuples(v) for k, v in data["arm"].items()}
    arm["phase"] = Phase(arm["phase"])
    version, internal, gauss = data["rng"]
    return {"arm": Arm(**arm), "tick": data["tick"], "rng": (version, tuple(internal), gauss)}


def encode_config(config: WorldConfig) -> dict[str, Any]:
    out = asdict(config)
    out["features"] = sorted(config.features)
    encoded: dict[str, Any] = json.loads(json.dumps(out))
    return encoded


def decode_config(data: dict[str, Any]) -> WorldConfig:
    def tuples(v: Any) -> Any:
        return tuple(tuples(x) for x in v) if isinstance(v, list) else v

    return WorldConfig(
        **{**data, "features": frozenset(data["features"]), "aabb_m": tuples(data["aabb_m"])}
    )
