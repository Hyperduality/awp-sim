from __future__ import annotations

import json

import pytest

from awp import jsonrpc
from awp.client import ErrorResponse, FrameReceived, ReplayCompleted, Telemetry
from awp.errors import AwpError, ErrorCode
from awp_sim.world import Close, Send

from .helpers import assert_wire_valid, events, make_net, pose, states, statuses

FAR = pose(0.3, 0.2, 0.5)


def error_code(fn):
    with pytest.raises(AwpError) as exc:
        fn()
    return exc.value.code


# ---------------------------------------------------------------- lockstep


def test_lockstep_core_flow():
    net = make_net(mode="lockstep")
    a = net.agent(heartbeat_ms=None)
    ready = a.open(mode="lockstep", embodiment="arm_01", subscribe=["proprio", "arm_state"])
    assert ready["tick"] == 0
    first = a.of(FrameReceived)
    assert {f.channel for f in first} == {"proprio", "arm_state"}
    assert all(f.frame.tick == 0 for f in first)
    action = a.submit("move_to_pose", FAR)
    assert a.client.actions[action].state == "accepted"
    ticks = 0
    while not a.client.actions[action].terminal:
        a.call(a.client.advance())
        ticks += 1
        assert ticks < 200
    assert states(a, action) == ["accepted", "executing", "completed"]
    frames_per_tick = [f.frame.tick for f in a.of(FrameReceived) if f.channel == "proprio"]
    assert frames_per_tick == list(range(ticks + 1))
    a.call(a.client.close())
    assert a.client.session_state == "closed"
    assert_wire_valid(a)


def test_lockstep_advances_only_on_tick_and_rejects_stale_expected_tick():
    net = make_net(mode="lockstep")
    a = net.agent(heartbeat_ms=None)
    a.open(mode="lockstep", embodiment="arm_01", subscribe=["proprio"])
    action = a.submit("move_to_pose", FAR)
    net.advance(500)
    assert a.client.actions[action].state == "accepted"
    assert a.call(a.client.advance(count=3)) == {"tick": 3}
    stale = a.client.request("world.tick", {"expected_tick": 2})
    assert error_code(lambda: a.call(stale)) == ErrorCode.TICK_MISMATCH


def test_lockstep_observer_cannot_tick():
    net = make_net(mode="lockstep")
    obs = net.agent("observer", heartbeat_ms=None)
    obs.open(mode="lockstep", subscribe=["arm_state"])
    assert error_code(lambda: obs.call(obs.client.advance())) == ErrorCode.TICK_NOT_AUTHORIZED


# ---------------------------------------------------------------- streaming


def open_streaming(net, **kw):
    a = net.agent(**kw)
    a.open(mode="streaming", embodiment="arm_01", subscribe=["proprio", "arm_state"])
    return a


def latest(a, channel="proprio"):
    return [f.frame for f in a.of(FrameReceived) if f.channel == channel][-1]


def test_streaming_move_with_basis_progress_and_telemetry():
    net = make_net()
    a = open_streaming(net)
    net.advance(50)
    action = a.submit("move_to_pose", FAR, basis=latest(a), valid_for_ms=200, deadline_ms=5000)
    assert net.run_until(lambda: a.client.actions[action].terminal, 5000)
    assert states(a, action) == ["accepted", "executing", "completed"]
    assert len(statuses(a, action)) > 3  # progress updates at ≥ 1 Hz
    frames = [f.frame for f in a.of(FrameReceived)]
    assert all(f.ts_send_ns is not None and f.ts_send_ns >= f.ts_mono_ns for f in frames)
    net.advance(1000)
    telemetry = [t.params for t in a.of(Telemetry)]
    assert any("admission_latency_ns" in t and "observation_to_action_ns" in t for t in telemetry)
    a.client.report()
    net.settle()
    assert_wire_valid(a)


def test_admission_refusals():
    net = make_net()
    a = open_streaming(net)
    net.advance(100)

    def refused(*args, **kw):
        return error_code(lambda: a.submit(*args, **kw))

    assert refused("move_to_pose", {"pose": {"frame": "base"}}) == ErrorCode.PARAMS_INVALID
    assert refused("move_to_pose", pose(0.9, 0, 0.4)) == ErrorCode.ENVELOPE_EXCEEDED
    too_fast = {**FAR, "max_velocity_mps": 2.0}
    assert refused("move_to_pose", too_fast) == ErrorCode.ENVELOPE_EXCEEDED
    assert refused("move_to_pose", FAR, preempt="blend") == ErrorCode.PARAMS_INVALID
    net.advance(600)
    old_basis = next(f.frame for f in a.of(FrameReceived) if f.channel == "proprio")
    assert refused("move_to_pose", FAR, basis=old_basis) == ErrorCode.STALE_INTENT
    assert refused("move_to_pose", FAR, valid_until_ns=1) == ErrorCode.STALE_INTENT
    a.submit("move_to_pose", FAR)
    assert refused("move_to_pose", FAR) == ErrorCode.ENVELOPE_EXCEEDED  # rate limit
    err = next(
        e
        for e in a.of(ErrorResponse)
        if e.error.code == ErrorCode.ENVELOPE_EXCEEDED and e.error.retryable
    )
    assert err.error.data["retry_after_ms"] > 0
    net.advance(60)
    assert refused("move_to_pose", FAR, preempt="reject") == ErrorCode.BUSY
    for _ in range(4):
        net.advance(60)
        a.submit("move_to_pose", FAR, preempt="queue")
    net.advance(60)
    assert refused("move_to_pose", FAR, preempt="queue") == ErrorCode.QUEUE_FULL
    assert_wire_valid(a)


def test_observer_cannot_act_and_ungranted_types_are_forbidden():
    net = make_net()
    obs = net.agent("observer")
    obs.open(mode="streaming", subscribe=["arm_state"])
    assert error_code(lambda: obs.submit("stop", {})) == ErrorCode.FORBIDDEN
    b = net.agent("b")
    b.open(mode="streaming", embodiment="arm_01", action_types=["stop"])
    assert error_code(lambda: b.submit("move_to_pose", FAR)) == ErrorCode.FORBIDDEN
    c = net.agent("c")
    assert error_code(lambda: c.open(mode="streaming", embodiment="arm_01")) == (
        ErrorCode.EMBODIMENT_UNAVAILABLE
    )
    d = net.agent("d")
    assert error_code(lambda: d.open(mode="lockstep")) == ErrorCode.TIME_MODEL_UNSUPPORTED


def test_idempotent_resubmission_and_conflict():
    net = make_net()
    a = open_streaming(net)
    action = a.submit("move_to_pose", FAR)
    net.advance(100)
    seq = a.client.last_status_seq
    result = a.call(a.client.resubmit(action))
    assert result["state"] == "executing"
    assert result["status_seq"] == a.client.actions[action].status_seq
    net.settle()
    assert a.client.last_status_seq == seq  # no new status_seq consumed
    conflict = a.client.request(
        "action.submit", {"action_id": action, "type": "stop", "params": {}}
    )
    assert error_code(lambda: a.call(conflict)) == ErrorCode.ACTION_ID_CONFLICT
    assert net.run_until(lambda: a.client.actions[action].terminal, 5000)
    assert a.client.actions[action].state == "completed"


def test_failed_admission_creates_no_action():
    net = make_net()
    a = open_streaming(net)
    net.advance(10)
    bad = {"action_id": "a-1", "type": "move_to_pose", "params": pose(0.9, 0, 0.4)}
    assert error_code(lambda: a.call(a.client.request("action.submit", bad))) == (
        ErrorCode.ENVELOPE_EXCEEDED
    )
    assert a.submit("move_to_pose", FAR, action_id="a-1") == "a-1"  # AWP-ACT-010


def test_replace_preempts_and_queue_promotes():
    net = make_net()
    a = open_streaming(net)
    first = a.submit("move_to_pose", FAR)
    net.advance(100)
    queued = a.submit("move_to_pose", pose(-0.2, 0.1, 0.3), preempt="queue")
    net.advance(60)
    replacing = a.submit("move_to_pose", pose(0.1, -0.2, 0.3), preempt="replace")
    assert states(a, first) == ["accepted", "executing", "preempted"]
    assert statuses(a, queued)[-1] == ("cancelled", "superseded")
    net.advance(60)
    after = a.submit("move_to_pose", pose(0.0, 0.0, 0.5), preempt="queue")
    assert net.run_until(lambda: a.client.actions[after].terminal, 8000)
    assert states(a, replacing)[-1] == "completed"
    assert states(a, after) == ["queued", "accepted", "executing", "completed"]
    assert_wire_valid(a)


def test_stop_replaces_motion():
    net = make_net()
    a = open_streaming(net)
    move = a.submit("move_to_pose", FAR)
    net.advance(100)
    stop = a.submit("stop", {})
    assert states(a, move)[-1] == "preempted"
    assert states(a, stop) == ["accepted", "executing", "completed"]
    net.advance(500)
    assert net.world.arm.at_rest


def test_cancel_executing_and_queued():
    net = make_net()
    a = open_streaming(net)
    move = a.submit("move_to_pose", FAR)
    net.advance(60)
    queued = a.submit("move_to_pose", pose(0, 0, 0.3), preempt="queue")
    a.call(a.client.cancel(queued))
    assert statuses(a, queued)[-1] == ("cancelled", "cancelled_by_agent")
    net.advance(200)
    a.call(a.client.cancel(move))
    assert a.client.actions[move].state == "cancelling"
    assert net.run_until(lambda: a.client.actions[move].terminal, 1500)
    final = a.client.actions[move].status
    assert final["state"] == "cancelled"
    assert 0 < final["aborted_at_progress"] < 1
    unknown = a.client.request("action.cancel", {"action_id": "nope"})
    assert error_code(lambda: a.call(unknown)) == ErrorCode.ACTION_UNKNOWN
    assert_wire_valid(a)


def test_abort_failure_ends_failed_and_enters_safe_state():
    net = make_net()
    a = open_streaming(net)
    move = a.submit("move_to_pose", FAR)
    net.advance(200)
    net.world.arm.stuck = True
    a.call(a.client.cancel(move))
    assert net.run_until(lambda: a.client.actions[move].terminal, 3000)
    assert statuses(a, move)[-1] == ("failed", "abort_failed")
    assert "safe_state_entered" in events(a)


def test_deadline_from_max_duration():
    net = make_net(max_duration_ms=300)
    a = open_streaming(net)
    move = a.submit("move_to_pose", FAR)
    assert net.run_until(lambda: a.client.actions[move].terminal, 2000)
    assert statuses(a, move)[-1] == ("failed", "deadline_exceeded")


# ---------------------------------------------------------------- liveness


def test_watchdog_trips_on_a_quiet_agent_with_a_live_connection():
    net = make_net(watchdog_ms=300, heartbeat_interval_ms=100)
    a = open_streaming(net, heartbeat_ms=None)
    move = a.submit("move_to_pose", FAR)
    t0 = net.now
    assert net.run_until(lambda: "safe_state_entered" in events(a), 1000)
    assert 300 <= (net.now - t0) / 1e6 <= 301
    assert statuses(a, move)[-1] == ("failed", "connection_lost")
    assert a.client.session_state == "active"  # pongs kept the connection alive
    net.advance(200)
    a.submit("move_to_pose", pose(0, 0, 0.5))
    assert events(a)[-1] == "safe_state_exited"


def test_disconnect_watchdog_resume_and_replay():
    net = make_net(watchdog_ms=300, heartbeat_interval_ms=100)
    a = open_streaming(net)
    move = a.submit("move_to_pose", FAR)
    net.advance(50)
    a.drop()
    net.advance(600)
    a.connect()
    a.call(a.client.initialize())
    ready = a.call(a.client.resume())
    assert ready["safe_state"] is True
    replayed = [e for e in a.events if getattr(e, "replayed", False)]
    replayed_states = [e.status["state"] for e in replayed if hasattr(e, "status")]
    assert replayed_states[-1] == "failed"  # progress sent while away is replayed too
    assert a.of(ReplayCompleted)
    net.advance(50)
    arm_state = [f.frame for f in a.of(FrameReceived) if f.channel == "arm_state"]
    resumed = [f for f in arm_state if f.resync]
    assert len(resumed) == 1  # the reliable channel resyncs once (AWP-TRN-008)
    assert resumed[0].keyframe
    assert statuses(a, move)[-1] == ("failed", "connection_lost")
    assert_wire_valid(a)


def test_half_open_resume_replaces_connection_and_replays_lost_admission():
    net = make_net()
    a = open_streaming(net)
    a.lose = lambda m: "result" in m and m["result"].get("state") == "accepted"
    a.client.submit("move_to_pose", FAR, action_id="a-8")
    net.settle()
    assert a.conn is None
    assert a.client.actions["a-8"].state == "submitted"
    net.advance(200)
    a.connect()
    a.call(a.client.initialize())
    a.call(a.client.resume())
    assert "connection_replaced" in [e.reason for e in a.events if hasattr(e, "reason")]
    result = a.call(a.client.resubmit("a-8"))
    assert result["state"] == "executing"
    assert a.client.actions["a-8"].state == "executing"
    assert_wire_valid(a)


def test_heartbeat_loss_then_window_expiry_closes_the_session():
    net = make_net(watchdog_ms=300, heartbeat_interval_ms=100, reconnect_window_ms=1000)
    a = open_streaming(net)
    token = a.client.session_token
    a.abandon()
    net.advance(350)
    assert not net.world.sessions[a.client.ready["session_id"]].conn
    net.advance(1100)
    assert net.world.sessions == {}
    b = net.agent("b")
    b.connect()
    b.call(b.client.initialize())
    expired = b.client.request("session.resume", {"session_token": token, "last_status_seq": 0})
    assert error_code(lambda: b.call(expired)) == ErrorCode.SESSION_EXPIRED
    unknown = b.client.request(
        "session.resume", {"session_token": "st_" + "x" * 20, "last_status_seq": 0}
    )
    assert error_code(lambda: b.call(unknown)) == ErrorCode.SESSION_UNKNOWN


def test_acknowledged_notifications_are_released():
    net = make_net()
    a = open_streaming(net)
    a.submit("move_to_pose", FAR)
    net.advance(600)
    session = net.world.sessions[a.client.ready["session_id"]]
    assert session.acked > 0
    assert min(session.log, default=session.acked + 1) > session.acked


# ---------------------------------------------------------------- safety and admin


def test_estop_terminates_everything_and_suspends_admission():
    net = make_net()
    a = open_streaming(net)
    move = a.submit("move_to_pose", FAR)
    net.advance(60)
    queued = a.submit("move_to_pose", pose(0, 0, 0.3), preempt="queue")
    net.deliver(net.world.engage_estop(net.now))
    net.settle()
    assert statuses(a, move)[-1] == ("failed", "e_stop")
    assert statuses(a, queued)[-1] == ("cancelled", "e_stop")
    net.advance(60)
    assert error_code(lambda: a.submit("stop", {})) == ErrorCode.ESTOP_ACTIVE
    net.deliver(net.world.release_estop(net.now))
    net.settle()
    assert events(a)[-2:] == ["e_stop_engaged", "e_stop_released"][-2:]
    a.submit("stop", {}, action_id="a-retry")


def test_lockstep_resumption_resyncs_every_per_tick_channel_at_the_tick():
    net = make_net(mode="lockstep")
    a = net.agent(heartbeat_ms=None)
    a.open(mode="lockstep", embodiment="arm_01", subscribe=["proprio", "arm_state"])
    a.call(a.client.advance(3))
    a.drop()
    a.connect()
    a.call(a.client.initialize())
    before = len(a.of(FrameReceived))
    ready = a.call(a.client.resume())
    after = [f.frame for f in a.of(FrameReceived)[before:]]
    assert {(f.channel_id, f.tick, f.resync, f.keyframe) for f in after} == {
        (g["channel_id"], ready["tick"], True, True) for g in ready["granted"]["channels"]
    }  # AWP-TIM-009, AWP-TRN-008
    assert a.client.holds_tick(ready["tick"])


def test_reset_sends_the_fresh_frames_before_its_result():
    net = make_net(mode="lockstep")
    a = net.agent(heartbeat_ms=None)
    a.open(mode="lockstep", embodiment="arm_01", subscribe=["proprio"], admin=["reset"])
    a.call(a.client.advance(5))
    rid = a.client.request("world.reset", {})
    a.call(rid)
    order = [e for e in a.events if isinstance(e, FrameReceived) or getattr(e, "id", None) == rid]
    frame, result = order[-2:]
    assert isinstance(frame, FrameReceived)  # the fresh frame precedes the result (AWP-PRM-006)
    assert frame.frame.tick == 0
    assert not isinstance(result, FrameReceived)


def test_reset_requires_grant_and_cancels_actions():
    net = make_net()
    a = net.agent()
    ready = a.open(mode="streaming", embodiment="arm_01", admin=["reset", "snapshot"])
    assert ready["granted"]["admin"] == ["reset"]
    move = a.submit("move_to_pose", FAR)
    net.advance(100)
    a.call(a.client.request("world.reset", {}))
    assert statuses(a, move)[-2:] == [("cancelling", "world_reset"), ("cancelled", "world_reset")]
    assert "world_resetting" in events(a)
    obs = net.agent("observer")
    assert obs.open(mode="streaming", admin=["reset"])["granted"]["admin"] == []
    assert (
        error_code(lambda: obs.call(obs.client.request("world.reset", {}))) == ErrorCode.FORBIDDEN
    )


def test_close_while_executing_aborts_before_closing():
    net = make_net()
    a = open_streaming(net)
    move = a.submit("move_to_pose", FAR)
    net.advance(200)
    rid = a.client.close()
    net.settle()
    assert a.client.actions[move].state == "cancelling"
    assert net.run_until(lambda: a.client.session_state == "closed", 1500)
    assert a.call(rid) == {}
    assert statuses(a, move)[-1] == ("cancelled", "session_closed")
    assert net.world.sessions == {}


# ---------------------------------------------------------------- channels and protocol errors


def test_subscribe_and_unsubscribe():
    net = make_net()
    a = open_streaming(net)
    result = a.call(a.client.unsubscribe(["arm_state"]))
    assert [g["channel"] for g in result["granted"]] == ["proprio"]
    result = a.call(a.client.subscribe([{"channel": "arm_state", "rate_hz": 2}]))
    assert {g["channel"]: g["rate_hz"] for g in result["granted"]} == {
        "proprio": 100,
        "arm_state": 2,
    }
    unknown = a.client.subscribe(["nope"])
    assert error_code(lambda: a.call(unknown)) == ErrorCode.CHANNEL_UNKNOWN


def test_protocol_errors():
    net = make_net()
    a = net.agent()
    a.connect()
    assert (
        error_code(lambda: a.call(a.client.request("world.manifest"))) == ErrorCode.INVALID_REQUEST
    )
    a.call(a.client.initialize())
    assert (
        error_code(lambda: a.call(a.client.request("task.update", {})))
        == ErrorCode.METHOD_NOT_FOUND
    )
    a.call(a.client.open_session("streaming", embodiment="arm_01"))
    again = a.client.open_session("streaming")
    assert error_code(lambda: a.call(again)) == ErrorCode.SESSION_EXISTS
    malformed = a.client.request("action.submit", {"action_id": "x"})
    assert error_code(lambda: a.call(malformed)) == ErrorCode.MALFORMED

    (reply,) = net.world.receive_text(a.conn, "{not json", net.now)
    assert isinstance(reply, Send)
    assert reply.msg["error"]["code"] == ErrorCode.PARSE_ERROR
    huge = jsonrpc.encode(jsonrpc.request(99, "ping", {"origin_ns": 1}))
    huge = huge.replace('"origin_ns":1', '"origin_ns":' + str(2**60))
    out = net.world.receive_text(a.conn, huge, net.now)
    assert isinstance(out[-1], Close)  # AWP-CTL-009 closes the session and the connection
    assert (out[-1].code, out[-1].reason) == (1002, "AWP_INTEGER_RANGE")
    closed = [o.msg["params"] for o in out if isinstance(o, Send) and "method" in o.msg]
    assert (closed[-1]["state"], closed[-1]["reason"]) == ("closed", "protocol_error")
    assert net.world.sessions == {}


def test_version_negotiation():
    net = make_net()
    a = net.agent()
    a.connect()
    rid = a.client.request(
        "initialize",
        {
            "protocol_versions": ["9.9"],
            "agent": a.client.agent,
            "consumes_modalities": ["proprio/json"],
        },
    )
    assert error_code(lambda: a.call(rid)) == ErrorCode.VERSION_UNSUPPORTED


def test_manifest_is_valid_for_both_modes():
    from awp import schema
    from awp_sim.config import WorldConfig

    for mode in ("lockstep", "streaming"):
        manifest = WorldConfig(mode=mode).manifest()
        assert schema.errors("world-manifest", manifest, sender=True) == []
        json.dumps(manifest)


def test_terminal_actions_are_released_after_the_reconnect_window():
    net = make_net(reconnect_window_ms=1000, watchdog_ms=500)
    a = open_streaming(net)
    action = a.submit("stop", {})
    session = net.world.sessions[a.client.ready["session_id"]]
    assert action in session.actions
    net.advance(1100)
    assert action not in session.actions  # AWP-ACT-006 retention has lapsed


def test_resume_keeps_the_original_clock_anchor():
    net = make_net()
    a = open_streaming(net)
    anchor = a.client.ready["clock_anchor"]
    a.drop()
    a.connect()
    a.call(a.client.initialize())
    assert a.call(a.client.resume())["clock_anchor"] == anchor
