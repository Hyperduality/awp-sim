"""Per-session state: grants, status delivery, actions, and telemetry samples."""

from __future__ import annotations

from collections import deque
from collections.abc import Hashable
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from awp.lifecycle import ActionState

from .arm import Vec3


@dataclass(slots=True)
class ChannelGrant:
    name: str
    channel_id: int
    rate_hz: float | None
    loss_class: str
    seq: int = 0
    next_due_ns: int = 0
    resync: bool = False
    command: bool = False  # an agent→world command channel (AWP-CMD-002)

    def to_wire(self) -> dict[str, Any]:
        return {"channel": self.name, "rate_hz": self.rate_hz, "channel_id": self.channel_id}


@dataclass(slots=True)
class Action:
    action_id: str
    content: dict[str, Any]
    decl: dict[str, Any]
    received_ns: int
    deadline_ns: int | None = None
    target: Vec3 | None = None
    v_max: float = 0.0
    state: ActionState = ActionState.SUBMITTED
    status: dict[str, Any] = field(default_factory=dict)
    cancel_reason: str | None = None
    cancel_started_ns: int | None = None
    replaces: bool = False
    blends: bool = False
    approval_id: str | None = None
    stream: dict[str, int] | None = None  # streaming-duration progress (AWP-CMD-004)
    last_frame_ns: int = 0
    command_seq: int = 0
    failing_with: str | None = None  # terminal reason to report once the safe abort completes
    last_progress_ns: int = 0
    terminal_ns: int | None = None

    @property
    def type(self) -> str:
        return str(self.content["type"])

    @property
    def group(self) -> str:
        return str(self.decl.get("concurrency_group", f"_{self.type}"))


@dataclass(slots=True)
class Telemetry:
    observation: list[int] = field(default_factory=list)
    admission: list[int] = field(default_factory=list)
    observation_to_action: list[int] = field(default_factory=list)
    channels: dict[int, list[int]] = field(default_factory=dict)
    command: list[int] = field(default_factory=list)

    def snapshot(self, window_ms: int) -> dict[str, Any]:
        params: dict[str, Any] = {"window_ms": window_ms}
        for key, values in (
            ("observation_latency_ns", self.observation),
            ("admission_latency_ns", self.admission),
            ("observation_to_action_ns", self.observation_to_action),
            ("command_latency_ns", self.command),
        ):
            if values:
                params[key] = stats(values)
        if self.channels:
            params["channels"] = {str(c): stats(v) for c, v in self.channels.items() if v}
        return params


def stats(values: list[int]) -> dict[str, int]:
    ordered = sorted(values)
    last = len(ordered) - 1
    return {
        "count": len(ordered),
        "p50": ordered[round(0.5 * last)],
        "p95": ordered[round(0.95 * last)],
        "max": ordered[-1],
    }


@dataclass(frozen=True, slots=True)
class Standing:
    """A scoped standing approval, held by the session that requested it (AWP-APR-004)."""

    approval_id: str
    type: str
    predicate: Draft202012Validator | None
    expires_ns: int  # on the session clock

    def admits(self, type: str, params: dict[str, Any], clock_ns: int) -> bool:
        return (
            type == self.type
            and clock_ns <= self.expires_ns
            and (self.predicate is None or self.predicate.is_valid(params))
        )


@dataclass(slots=True)
class Session:
    id: str
    token: str
    mode: str
    embodiment: str | None
    origin_ns: int
    clock_anchor: str
    conn: Hashable | None
    action_types: list[str]
    admin: list[str]
    grants: dict[str, ChannelGrant] = field(default_factory=dict)
    state: str = "ready"
    seq: int = 0
    log: dict[int, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    acked: int = 0
    actions: dict[str, Action] = field(default_factory=dict)
    running: dict[str, Action] = field(default_factory=dict)
    queues: dict[str, deque[Action]] = field(default_factory=dict)
    staged: list[Action] = field(default_factory=list)
    last_agent_ns: int = 0
    last_admitted_ns: int | None = None
    suspended_ns: int | None = None
    task: dict[str, Any] | None = None
    stream_conn: Hashable | None = None
    stream_lost_ns: int | None = None
    stream_degraded_reported: bool = False
    safe_state: bool = False
    degraded_reported: bool = False
    closing: list[tuple[Hashable, Any]] = field(default_factory=list)  # close requests to answer
    closing_reason: str | None = None
    last_telemetry_ns: int = 0
    telemetry: Telemetry = field(default_factory=Telemetry)
    next_channel_id: int = 1
    standing: list[Standing] = field(default_factory=list)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def retain(self, seq: int, method: str, params: dict[str, Any]) -> None:
        """Keep a sequenced notification until the agent acknowledges it (AWP-CTL-010)."""
        self.log[seq] = (method, params)

    def acknowledge(self, seq: int) -> None:
        if seq <= self.acked:
            return
        for s in [s for s in self.log if s <= seq]:
            del self.log[s]
        self.acked = seq

    def replay_after(self, seq: int) -> list[tuple[str, dict[str, Any]]]:
        return [self.log[s] for s in sorted(self.log) if s > seq]

    def pre_execution(self) -> list[Action]:
        return [a for a in self.actions.values() if a.state.pre_execution]

    def queue(self, group: str) -> deque[Action]:
        return self.queues.setdefault(group, deque())
