"""Scripted scenarios: normal and failure behavior of the reference world, recorded as traces.

Each scenario runs real clients against the world on a virtual clock and returns its checks,
metrics (in virtual milliseconds), and one wire trace per agent in the spec's JSON Lines format.
Validate the traces with the spec's checker (`scripts/check_traces.py`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from awp.client import (
    ActionUpdated,
    ErrorResponse,
    FrameReceived,
    ReplayCompleted,
    Telemetry,
    WorldEvent,
)
from awp.errors import AwpError, ErrorCode

from .config import WorldConfig
from .loopback import MS, Loopback, LoopbackAgent
from .world import World

FAR = {"pose": {"frame": "base", "p_m": [0.3, 0.2, 0.5], "q": [0, 0, 0, 1]}}
NEAR = {"pose": {"frame": "base", "p_m": [0.0, 0.1, 0.35], "q": [0, 0, 0, 1]}}
OUTSIDE = {"pose": {"frame": "base", "p_m": [0.9, 0.0, 0.4], "q": [0, 0, 0, 1]}}


@dataclass
class ScenarioResult:
    name: str
    description: str
    checks: list[tuple[str, bool]] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    traces: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(ok for _, ok in self.checks)

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "checks": [{"check": c, "ok": ok} for c, ok in self.checks],
            "metrics": self.metrics,
            "error": self.error,
        }


class _Run:
    def __init__(self, result: ScenarioResult, **config: Any) -> None:
        self.result = result
        self.net = Loopback(World(WorldConfig(**config)))

    def agent(self, name: str = "agent", **kw: Any) -> LoopbackAgent:
        return self.net.agent(name, **kw)

    def check(self, label: str, ok: bool) -> None:
        self.result.checks.append((label, bool(ok)))

    def metric(self, name: str, value: float) -> None:
        self.result.metrics[name] = round(value, 3)

    def refused(self, fn: Callable[[], Any]) -> int | None:
        try:
            fn()
        except AwpError as err:
            return err.code
        return None


def states(a: LoopbackAgent, action_id: str) -> list[str]:
    out: list[str] = []
    for e in a.of(ActionUpdated):
        if e.action.action_id == action_id and (not out or out[-1] != e.status["state"]):
            out.append(e.status["state"])
    return out


def last_status(a: LoopbackAgent, action_id: str) -> dict[str, Any]:
    return a.client.actions[action_id].status


def events(a: LoopbackAgent) -> list[WorldEvent]:
    return a.of(WorldEvent)


def open_streaming(run: _Run, **kw: Any) -> LoopbackAgent:
    a = run.agent(**kw)
    a.open(mode="streaming", embodiment="arm_01", subscribe=["proprio", "arm_state"])
    return a


SCENARIOS: dict[str, tuple[str, Callable[[_Run], None]]] = {}


def scenario(
    name: str, description: str
) -> Callable[[Callable[[_Run], None]], Callable[[_Run], None]]:
    def register(fn: Callable[[_Run], None]) -> Callable[[_Run], None]:
        SCENARIOS[name] = (description, fn)
        return fn

    return register


def run(name: str) -> ScenarioResult:
    description, fn = SCENARIOS[name]
    result = ScenarioResult(name, description)
    runner = _Run(result, **_CONFIGS.get(name, {}))
    try:
        fn(runner)
    except Exception as exc:  # a scenario that raises has failed; keep the partial trace
        result.error = f"{type(exc).__name__}: {exc}"
    for a in runner.net.agents:
        result.traces[a.name] = a.trace_json()
    return result


def run_all(names: list[str] | None = None) -> list[ScenarioResult]:
    return [run(n) for n in names or list(SCENARIOS)]


def write(results: list[ScenarioResult], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for r in results:
        for agent, lines in r.traces.items():
            path = out / f"{r.name}--{agent}.jsonl"
            path.write_text(
                "".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines)
            )
    report = {"passed": all(r.passed for r in results), "scenarios": [r.summary() for r in results]}
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")


_FAST_LIVENESS = {"watchdog_ms": 400, "heartbeat_interval_ms": 200, "reconnect_window_ms": 3000}
_CONFIGS: dict[str, dict[str, Any]] = {
    "core-lockstep": {"mode": "lockstep"},
    "disconnect-and-resume": _FAST_LIVENESS,
    "quiet-agent": _FAST_LIVENESS,
    "window-expiry": {**_FAST_LIVENESS, "reconnect_window_ms": 1000},
}


# ============================================================================ scenarios


@scenario("core-lockstep", "Discovery, session, staged action advanced by explicit ticks, close.")
def _core_lockstep(r: _Run) -> None:
    a = r.agent(heartbeat_ms=None)
    ready = a.open(mode="lockstep", embodiment="arm_01", subscribe=["proprio", "arm_state"])
    r.check("session.ready carries tick 0", ready.get("tick") == 0)
    r.check(
        "initial observation per channel",
        {f.channel for f in a.of(FrameReceived)} == {"proprio", "arm_state"},
    )
    action = a.submit("move_to_pose", NEAR)
    r.net.advance(1000)
    r.check("no progress without a tick", a.client.actions[action].state == "accepted")
    ticks = 0
    while not a.client.actions[action].terminal and ticks < 500:
        a.call(a.client.advance(count=5))
        ticks += 5
    r.check(
        "completed across advances", states(a, action) == ["accepted", "executing", "completed"]
    )
    per_tick = [f.frame.tick for f in a.of(FrameReceived) if f.channel == "proprio"]
    r.check("exactly one frame per advance", per_tick == list(range(len(per_tick))))
    r.metric("ticks_to_complete", ticks)
    a.call(a.client.close())
    r.check("closed", a.client.session_state == "closed")


@scenario("core-streaming", "Clock sync, basis and validity, progress, telemetry, report, close.")
def _core_streaming(r: _Run) -> None:
    a = open_streaming(r)
    r.net.advance(50)
    basis = [f.frame for f in a.of(FrameReceived) if f.channel == "proprio"][-1]
    action = a.submit("move_to_pose", FAR, basis=basis, valid_for_ms=200, deadline_ms=6000)
    r.check("admitted", a.client.actions[action].state in ("accepted", "executing"))
    r.net.run_until(lambda: a.client.actions[action].terminal, 5000)
    r.check("completed", states(a, action) == ["accepted", "executing", "completed"])
    r.net.advance(1100)
    a.client.report()
    r.net.settle()
    telemetry = [e.params for e in a.of(Telemetry)]
    r.check("telemetry at least once per second", len(telemetry) >= 2)
    o2a = [
        t["observation_to_action_ns"]["p50"] for t in telemetry if "observation_to_action_ns" in t
    ]
    r.check("observation-to-action reported", bool(o2a))
    r.check("clock offset estimated", a.client.clock.samples > 0)
    rid = a.client.close()
    r.net.advance(10)
    r.check("closed", a.call(rid) == {} and a.client.session_state == "closed")


@scenario(
    "duplicate-and-conflict",
    "Identical resubmissions never execute twice; changed content is refused.",
)
def _duplicate(r: _Run) -> None:
    a = open_streaming(r)
    action = a.submit("move_to_pose", FAR)
    r.net.advance(100)
    before = a.client.last_status_seq
    first = a.call(a.client.resubmit(action))
    second = a.call(a.client.resubmit(action))
    r.check(
        "resubmission reports the current state", first["state"] == second["state"] == "executing"
    )
    r.check("no status_seq consumed", a.client.last_status_seq == before)
    changed = {"action_id": action, "type": "move_to_pose", "params": NEAR}
    code = r.refused(lambda: a.call(a.client.request("action.submit", changed)))
    r.check("changed content fails AWP_ACTION_ID_CONFLICT", code == ErrorCode.ACTION_ID_CONFLICT)
    r.net.run_until(lambda: a.client.actions[action].terminal, 5000)
    r.check("one execution", states(a, action) == ["accepted", "executing", "completed"])
    session = next(iter(r.net.world.sessions.values()))
    r.check("world holds one action", len(session.actions) == 1)


@scenario(
    "lost-acknowledgement",
    "An admission result is lost; resume replays it and a retry is idempotent.",
)
def _lost_ack(r: _Run) -> None:
    a = open_streaming(r)
    a.lose = lambda m: "result" in m and m["result"].get("state") == "accepted"
    a.client.submit("move_to_pose", FAR, action_id="a-lost")
    r.net.settle()
    r.check("admission never reached the agent", a.client.actions["a-lost"].state == "submitted")
    r.net.advance(150)
    a.connect()
    a.call(a.client.initialize())
    a.call(a.client.resume())
    r.check("replay completed", bool(a.of(ReplayCompleted)))
    r.metric("replayed_notifications", sum(1 for e in a.events if getattr(e, "replayed", False)))
    replaced = [e for e in a.events if getattr(e, "reason", None) == "connection_replaced"]
    r.check("half-open connection replaced (AWP-SES-010)", bool(replaced))
    retry = a.call(a.client.resubmit("a-lost"))
    r.check("retry reports the running action", retry["state"] == "executing")
    r.net.run_until(lambda: a.client.actions["a-lost"].terminal, 5000)
    r.check("executed once", states(a, "a-lost") == ["accepted", "executing", "completed"])


@scenario(
    "cancel-during-motion",
    "Cancelling an executing move completes its safe abort within max_abort_ms.",
)
def _cancel(r: _Run) -> None:
    a = open_streaming(r)
    action = a.submit("move_to_pose", FAR)
    r.net.advance(400)
    a.call(a.client.cancel(action))
    started = r.net.now
    r.net.run_until(lambda: a.client.actions[action].terminal, 3000)
    status = last_status(a, action)
    r.check("cancelling then cancelled", states(a, action)[-2:] == ["cancelling", "cancelled"])
    r.check("partial progress reported", 0 < status.get("aborted_at_progress", 0) < 1)
    abort_ms = (r.net.now - started) / MS
    r.metric("abort_ms", abort_ms)
    r.check("abort within max_abort_ms", abort_ms <= r.net.world.config.max_abort_ms)


@scenario("estop-during-abort", "An e-stop outranks an abort in progress and cancels queued work.")
def _estop(r: _Run) -> None:
    a = open_streaming(r)
    action = a.submit("move_to_pose", FAR)
    r.net.advance(60)
    queued = a.submit("move_to_pose", NEAR, preempt="queue")
    r.net.advance(300)
    a.call(a.client.cancel(action))
    r.net.advance(20)
    r.net.deliver(r.net.world.engage_estop(r.net.now))
    r.net.settle()
    r.check(
        "cancelling → failed(e_stop)",
        (last_status(a, action)["state"], last_status(a, action).get("reason"))
        == ("failed", "e_stop"),
    )
    r.check("queued → cancelled(e_stop)", last_status(a, queued).get("reason") == "e_stop")
    r.net.advance(60)
    code = r.refused(lambda: a.submit("stop", {}, action_id="a-stop"))
    r.check("admission suspended", code == ErrorCode.ESTOP_ACTIVE)
    r.net.deliver(r.net.world.release_estop(r.net.now))
    r.net.settle()
    a.submit("stop", {}, action_id="a-stop")
    r.check(
        "same id admitted after release (AWP-ACT-010)",
        a.client.actions["a-stop"].state in ("executing", "completed"),
    )


@scenario("limit-exceeded", "A target outside the spatial envelope is rejected and nothing moves.")
def _limit(r: _Run) -> None:
    a = open_streaming(r)
    start = r.net.world.arm.position
    code = r.refused(lambda: a.submit("move_to_pose", OUTSIDE))
    r.check("rejected AWP_ENVELOPE_EXCEEDED", code == ErrorCode.ENVELOPE_EXCEEDED)
    r.net.advance(300)
    r.check("arm did not move", r.net.world.arm.position == start)
    r.check("no action created", not next(iter(r.net.world.sessions.values())).actions)


@scenario(
    "replace-race", "A replacing submission preempts the running move and supersedes the queue."
)
def _replace(r: _Run) -> None:
    a = open_streaming(r)
    first = a.submit("move_to_pose", FAR)
    r.net.advance(100)
    queued = a.submit("move_to_pose", NEAR, preempt="queue")
    r.net.advance(60)
    new = a.submit(
        "move_to_pose", {"pose": {**NEAR["pose"], "p_m": [-0.2, 0.0, 0.3]}}, preempt="replace"
    )
    r.check("running move preempted", states(a, first)[-1] == "preempted")
    r.check("queued move superseded", last_status(a, queued).get("reason") == "superseded")
    r.net.run_until(lambda: a.client.actions[new].terminal, 5000)
    r.check("replacement completes", states(a, new)[-1] == "completed")


@scenario(
    "disconnect-and-resume",
    "The connection drops mid-move: safe state at watchdog_ms, full replay on resume.",
)
def _disconnect(r: _Run) -> None:
    a = open_streaming(r)
    action = a.submit("move_to_pose", FAR)
    r.net.advance(100)
    last_sent = r.net.now
    a.drop()
    r.net.run_until(
        lambda: r.net.world.arm.at_rest and not next(iter(r.net.world.sessions.values())).running,
        3000,
    )
    r.net.advance(300)
    a.connect()
    a.call(a.client.initialize())
    ready = a.call(a.client.resume())
    r.check("embodiment reported in safe state", ready.get("safe_state") is True)
    entered = [e for e in events(a) if e.event == "safe_state_entered"]
    r.check("safe_state_entered replayed", bool(entered) and entered[0].replayed)
    session = next(iter(r.net.world.sessions.values()))
    if entered:
        dt = (entered[0].params["ts_mono_ns"] - (last_sent - session.origin_ns)) / MS
        r.metric("time_to_safe_state_ms", dt)
        r.check("within watchdog_ms + 100 ms", dt <= r.net.world.config.watchdog_ms + 100)
    r.check(
        "action failed(connection_lost)", last_status(a, action).get("reason") == "connection_lost"
    )
    r.net.advance(50)
    resync = [f for f in a.of(FrameReceived) if f.channel == "arm_state" and f.frame.resync]
    r.check("reliable channel resyncs", len(resync) == 1)
    again = a.submit("move_to_pose", NEAR)
    r.check("new intent exits safe state", events(a)[-1].event == "safe_state_exited")
    r.net.run_until(lambda: a.client.actions[again].terminal, 5000)


@scenario(
    "quiet-agent", "A stalled agent on a live connection: pongs do not hold off the watchdog."
)
def _quiet(r: _Run) -> None:
    a = open_streaming(r, heartbeat_ms=None)
    action = a.submit("move_to_pose", FAR)
    t0 = r.net.now
    r.net.run_until(lambda: any(e.event == "safe_state_entered" for e in events(a)), 2000)
    dt = (r.net.now - t0) / MS
    r.metric("time_to_safe_state_ms", dt)
    watchdog = r.net.world.config.watchdog_ms
    r.check("safe state at watchdog_ms", watchdog <= dt <= watchdog + 100)
    r.check("session still active", a.client.session_state == "active")
    r.check(
        "action failed(connection_lost)", last_status(a, action).get("reason") == "connection_lost"
    )


@scenario(
    "stale-observation", "An intent built on an old observation is refused before anything moves."
)
def _stale(r: _Run) -> None:
    a = open_streaming(r)
    r.net.advance(50)
    old = next(f.frame for f in a.of(FrameReceived) if f.channel == "proprio")
    r.net.advance(r.net.world.config.max_basis_age_ms + 50)
    code = r.refused(lambda: a.submit("move_to_pose", FAR, basis=old))
    r.check("stale basis → AWP_STALE_INTENT", code == ErrorCode.STALE_INTENT)
    code = r.refused(lambda: a.submit("move_to_pose", FAR, valid_until_ns=1))
    r.check("expired validity → AWP_STALE_INTENT", code == ErrorCode.STALE_INTENT)
    fresh = [f.frame for f in a.of(FrameReceived) if f.channel == "proprio"][-1]
    a.submit("move_to_pose", FAR, basis=fresh)
    report = a.client.report()["params"]
    r.net.settle()
    staleness = report["channels"].get(str(a.client.channels["proprio"]), {}).get("staleness_ns")
    r.check("staleness visible to the agent", staleness is not None)
    r.check("errors were retryable", all(e.error.retryable for e in a.of(ErrorResponse)))


@scenario(
    "window-expiry",
    "An agent that never returns: safe state, then the session closes and its token expires.",
)
def _window(r: _Run) -> None:
    a = open_streaming(r)
    token = a.client.session_token
    a.submit("move_to_pose", FAR)
    a.abandon()
    r.net.advance(r.net.world.config.reconnect_window_ms + 1000)
    r.check("session closed", not r.net.world.sessions)
    r.check("arm at rest", r.net.world.arm.at_rest)
    b = r.agent("returning")
    b.connect()
    b.call(b.client.initialize())
    code = r.refused(
        lambda: b.call(
            b.client.request("session.resume", {"session_token": token, "last_status_seq": 0})
        )
    )
    r.check("resume fails AWP_SESSION_EXPIRED", code == ErrorCode.SESSION_EXPIRED)
