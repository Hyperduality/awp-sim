"""Features beyond Core: task, approval, blend, transfer, sim, and a servo command channel."""

from __future__ import annotations

import json

import pytest

from awp.client import ApprovalRequested, FrameReceived, Telemetry
from awp.errors import AwpError, ErrorCode

from .helpers import events, make_net, pose, statuses

MS = 1_000_000

FAR = pose(0.4, 0.4, 0.6)
NEAR = pose(-0.3, 0.2, 0.3)
TASK = {"content": [{"type": "text", "text": "Park the arm."}]}


def refused(fn):
    with pytest.raises(AwpError) as exc:
        fn()
    return exc.value.code


def world(*features, mode="streaming", **config):
    return make_net(mode=mode, features=frozenset(features), **config)


def open_arm(net, mode="streaming", **kw):
    a = net.agent(heartbeat_ms=100)
    a.open(mode=mode, embodiment="arm_01", subscribe=["proprio", "arm_state"], **kw)
    return a


# ---------------------------------------------------------------- task


def test_task_is_set_at_open_and_replaced_whole():
    net = world("task")
    a = open_arm(net, task=TASK)
    session = next(iter(net.world.sessions.values()))
    assert session.task == TASK
    new = {"content": [{"type": "text", "text": "Now hold still."}]}
    a.call(a.client.update_task(new))
    assert session.task == new
    image = {"content": [{"type": "image", "data": "..."}]}
    assert refused(lambda: a.call(a.client.update_task(image))) == ErrorCode.PARAMS_INVALID


def test_task_update_is_unknown_without_the_capability():
    a = open_arm(make_net())
    assert refused(lambda: a.call(a.client.update_task(TASK))) == ErrorCode.METHOD_NOT_FOUND


# ---------------------------------------------------------------- approval


def approval_net(**config):
    net = world("approval", "task", **config)
    approver = net.agent("approver", approver=True, heartbeat_ms=None)
    approver.connect()
    approver.call(approver.client.initialize())
    return net, approver


def requests(approver):
    return [e.params for e in approver.of(ApprovalRequested)]


def test_an_approved_action_runs():
    net, approver = approval_net()
    a = open_arm(net, task=TASK)
    action = a.submit("park", {})
    assert a.client.actions[action].state == "pending_approval"  # AWP-LIF-002
    [req] = requests(approver)
    assert req["action_id"] == action  # AWP-APR-001
    assert req["task"] == TASK  # AWP-TSK-005
    approver.call(approver.client.respond_approval(req["approval_id"], "approve"))
    net.run_until(lambda: a.client.actions[action].terminal, 5000)
    assert [s for s, _ in statuses(a, action)][:3] == ["pending_approval", "accepted", "executing"]
    assert a.client.actions[action].state == "completed"


def test_denial_and_timeout_reject():
    net, approver = approval_net(approval_timeout_ms=300)
    a = open_arm(net)
    denied = a.submit("park", {})
    approver.call(approver.client.respond_approval(requests(approver)[0]["approval_id"], "deny"))
    assert statuses(a, denied)[-1] == ("rejected", "approval_denied")
    net.advance(60)
    timed_out = a.submit("park", {})
    net.advance(400)
    assert statuses(a, timed_out)[-1] == ("rejected", "approval_timeout")  # AWP-APR-003


def test_only_approvers_decide_and_only_once():
    net, approver = approval_net()
    a = open_arm(net)
    a.submit("park", {})
    approval_id = requests(approver)[0]["approval_id"]
    assert (
        refused(lambda: a.call(a.client.respond_approval(approval_id, "approve")))
        == ErrorCode.FORBIDDEN
    )
    approver.call(approver.client.respond_approval(approval_id, "deny"))
    again = approver.client.respond_approval(approval_id, "approve")
    assert refused(lambda: approver.call(again)) == ErrorCode.PARAMS_INVALID


def test_a_pending_approval_can_be_cancelled():
    net, _ = approval_net()
    a = open_arm(net)
    action = a.submit("park", {})
    a.call(a.client.cancel(action))
    assert statuses(a, action)[-1] == ("cancelled", "cancelled_by_agent")


def standing(req, *, lasting_ms=1000, **scope):
    """A standing approval answering `req`, until `lasting_ms` after it was requested."""
    expires = req["expires_at_ns"] - 60_000 * MS + lasting_ms * MS
    return {"scope": {"type": req["type"], **scope}, "expires_at_ns": expires}


def test_a_standing_approval_admits_matching_submissions_until_it_expires():
    net, approver = approval_net()
    a = open_arm(net)
    first = a.submit("park", {})
    [req] = requests(approver)
    approver.call(
        approver.client.respond_approval(req["approval_id"], "approve", standing=standing(req))
    )
    net.run_until(lambda: a.client.actions[first].terminal, 5000)
    second = a.client.submit("park", {})
    result = a.call(a.client.last_id)
    assert result["state"] == "accepted"  # no pending_approval (AWP-APR-004)
    assert result["approval_id"] == req["approval_id"]
    assert len(requests(approver)) == 1
    net.run_until(lambda: a.client.actions[second].terminal, 5000)
    net.advance(1000)
    third = a.submit("park", {})
    assert a.client.actions[third].state == "pending_approval"  # the grant expired
    assert len(requests(approver)) == 2


def test_a_standing_approval_is_scoped_to_its_predicate():
    net, approver = approval_net()
    a = open_arm(net)
    first = a.submit("park", {})
    [req] = requests(approver)
    never = standing(req, lasting_ms=60_000, predicate={"not": {}})
    approver.call(approver.client.respond_approval(req["approval_id"], "approve", standing=never))
    net.run_until(lambda: a.client.actions[first].terminal, 5000)
    assert a.client.actions[a.submit("park", {})].state == "pending_approval"


def test_a_standing_approval_ends_with_its_session():
    net, approver = approval_net()
    a = open_arm(net)
    first = a.submit("park", {})
    [req] = requests(approver)
    grant = standing(req, lasting_ms=60_000)
    approver.call(approver.client.respond_approval(req["approval_id"], "approve", standing=grant))
    net.run_until(lambda: a.client.actions[first].terminal, 5000)
    a.call(a.client.close())
    b = open_arm(net)
    assert b.client.actions[b.submit("park", {})].state == "pending_approval"


@pytest.mark.parametrize(
    "change",
    [
        {"decision": "deny"},
        {"scope": {"type": "move_to_pose"}},
        {"scope": {"type": "park", "predicate": {"type": 5}}},
    ],
)
def test_an_invalid_standing_approval_changes_nothing(change):
    net, approver = approval_net()
    a = open_arm(net)
    action = a.submit("park", {})
    [req] = requests(approver)
    grant = {**standing(req), **{k: v for k, v in change.items() if k == "scope"}}
    decision = change.get("decision", "approve")
    call = approver.client.respond_approval(req["approval_id"], decision, standing=grant)
    assert refused(lambda: approver.call(call)) == ErrorCode.PARAMS_INVALID
    assert a.client.actions[action].state == "pending_approval"
    approver.call(approver.client.respond_approval(req["approval_id"], "approve"))
    assert a.client.actions[action].state != "pending_approval"


# ---------------------------------------------------------------- blend


def test_blend_preempts_without_stopping():
    net = world("blend")
    a = open_arm(net)
    first = a.submit("move_to_pose", FAR)
    net.advance(400)
    speed = net.world.arm.speed
    second = a.submit("move_to_pose", NEAR, preempt="blend")
    assert a.client.actions[first].state == "preempted"
    assert a.client.actions[first].status.get("blended") is True  # AWP-PRE-004
    assert net.world.arm.speed == speed  # the motion carried on
    net.run_until(lambda: a.client.actions[second].terminal, 5000)
    assert a.client.actions[second].state == "completed"


# ---------------------------------------------------------------- transfer


def test_transfer_moves_the_embodiment_and_leaves_an_observer():
    net = world("transfer")
    old = open_arm(net)
    running = old.submit("move_to_pose", FAR)
    token = old.call(old.client.transfer())["transfer_token"]
    new = net.agent("new", heartbeat_ms=100)
    new.open(
        mode="streaming",
        embodiment="arm_01",
        subscribe=["proprio"],
        takeover=True,
        transfer_token=token,
    )
    assert statuses(old, running)[-1] == ("preempted", "transferred")  # AWP-EMB-003
    assert "embodiment_transferred" in events(old)
    assert refused(lambda: old.submit("move_to_pose", NEAR)) == ErrorCode.FORBIDDEN
    moved = new.submit("move_to_pose", NEAR)
    net.run_until(lambda: new.client.actions[moved].terminal, 5000)
    assert new.client.actions[moved].state == "completed"


def test_a_transfer_token_is_single_use():
    net = world("transfer")
    old = open_arm(net)
    token = old.call(old.client.transfer())["transfer_token"]
    first, second = net.agent("first"), net.agent("second")
    for other in (first, second):
        other.connect()
        other.call(other.client.initialize())
    takeover = {"embodiment": "arm_01", "takeover": True, "transfer_token": token}
    first.call(first.client.open_session("streaming", **takeover))
    rid = second.client.open_session("streaming", **takeover)
    assert refused(lambda: second.call(rid)) == ErrorCode.EMBODIMENT_UNAVAILABLE


# ---------------------------------------------------------------- sim


def payloads(a, since):
    return [
        json.loads(e.frame.payload) for e in a.of(FrameReceived)[since:] if e.channel == "proprio"
    ]


def run(a, seed, ticks=12):
    a.call(a.client.reset(seed=seed))
    since = len(a.of(FrameReceived))
    a.submit("move_to_pose", FAR)
    for _ in range(ticks):
        a.call(a.client.advance())
    return payloads(a, since)


def test_equal_seeds_give_equal_observations():
    net = world("sim", mode="lockstep")
    a = open_arm(net, mode="lockstep", admin=["reset"])
    assert run(a, 7) == run(a, 7)  # AWP-REP-001
    assert run(a, 7) != run(a, 8)


def test_restore_returns_to_the_captured_state_and_the_clock_keeps_going():
    net = world("sim", mode="lockstep")
    a = open_arm(net, mode="lockstep", admin=["snapshot", "restore"])
    token = a.call(a.client.snapshot())["snapshot_token"]

    def from_snapshot():
        assert a.call(a.client.restore(token)) == {"tick": 0}  # AWP-REP-002
        since = len(a.of(FrameReceived))
        a.submit("move_to_pose", FAR)
        for _ in range(10):
            a.call(a.client.advance())
        return payloads(a, since)

    first = from_snapshot()
    clock = a.of(FrameReceived)[-1].frame.ts_mono_ns
    assert "world_resetting" in events(a)
    assert from_snapshot() == first
    frames = [e.frame for e in a.of(FrameReceived) if e.channel == "proprio"]
    assert frames[-1].ts_mono_ns > clock  # the session clock never goes back (AWP-TIM-013)
    assert frames[-1].ts_sim_ns == frames[-1].tick * 20_000_000  # AWP-CLK-003


# ---------------------------------------------------------------- servo


def servo_net():
    net = world("servo")
    a = net.agent(heartbeat_ms=100)
    a.open(mode="streaming", embodiment="arm_01", subscribe=["proprio"])
    return net, a


def test_setpoints_drive_the_arm_until_they_stop():
    net, a = servo_net()
    assert "servo_arm" in a.client.channels  # AWP-CMD-002
    action = a.submit("servo", {})
    start = net.world.arm.position
    for _ in range(15):
        a.client.command("servo_arm", {"v_mps": [0.2, 0.0, 0.0]})
        net.advance(20)
    assert net.world.arm.position[0] > start[0] + 0.01
    record = a.client.actions[action]
    assert record.state == "executing"
    assert record.status["stream"]["frames_applied"] > 0
    net.advance(400)  # no frames: the stream's watchdog (AWP-CMD-005)
    assert statuses(a, action)[-1] == ("failed", "watchdog")
    net.advance(1000)
    assert any("command_latency_ns" in e.params for e in a.of(Telemetry))


def test_frames_outside_the_envelope_or_the_action_are_dropped():
    net, a = servo_net()
    a.client.command("servo_arm", {"v_mps": [0.2, 0.0, 0.0]})  # before any servo action
    net.advance(50)
    assert net.world.arm.speed == 0  # AWP-CMD-003
    action = a.submit("servo", {})
    a.client.command("servo_arm", {"v_mps": [2.0, 0.0, 0.0]})  # above max_velocity_mps
    net.advance(50)
    assert net.world.arm.speed == 0  # AWP-CMD-006
    a.call(a.client.cancel(action))
    net.run_until(lambda: a.client.actions[action].terminal, 2000)
    assert statuses(a, action)[-1][0] == "cancelled"
    assert a.client.actions[action].status["stream"]["clamped_count"] == 1


def test_a_replay_bundle_reproduces_its_session(tmp_path):
    from awp_sim.audit import AuditLog
    from awp_sim.config import WorldConfig
    from awp_sim.loopback import Loopback
    from awp_sim.replay import replay
    from awp_sim.world import World

    config = WorldConfig(mode="lockstep", features=frozenset({"sim"}))
    log = AuditLog(tmp_path, bundle=True)
    net = Loopback(World(config, audit=log))
    a = open_arm(net, mode="lockstep", seed=3)
    first = a.submit("move_to_pose", FAR)
    a.call(a.client.advance(15))
    a.call(a.client.cancel(first))
    a.submit("move_to_pose", NEAR, action_id="a-second")
    for _ in range(40):
        a.call(a.client.advance())
    a.call(a.client.close())
    log.close_all()
    [bundle] = list(tmp_path.glob("*.jsonl"))
    assert json.loads(bundle.read_text().splitlines()[0])["class"] == "replay_bundle"
    outcome = replay(bundle)
    assert outcome.reproduced, outcome.difference  # AWP-REP-003
    assert outcome.frames > 80
    assert outcome.transitions >= 6
    lines = bundle.read_text().splitlines()
    tampered = [
        line.replace('"payload_sha256":"', '"payload_sha256":"0', 1)
        if '"kind":"frame"' in line and i > 10
        else line
        for i, line in enumerate(lines)
    ]
    bad = tmp_path / "tampered.jsonl"
    bad.write_text("\n".join(tampered) + "\n")
    assert not replay(bad).reproduced
