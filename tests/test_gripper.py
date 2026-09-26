"""The gripper: a second embodiment, bound with the arm or shared, under the barrier in lockstep."""

from __future__ import annotations

import json

import pytest
from awp.client import FrameReceived, TickCompleted, WorldEvent
from awp.errors import AwpError, ErrorCode

from awp_sim.config import WorldConfig

from .helpers import assert_wire_valid, events, make_net, pose, statuses

FAR = pose(0.4, 0.4, 0.6)
CLOSED = {"width_m": 0.0}
HALF = {"width_m": 0.04}
BOTH = ["arm_01", "gripper_01"]


def refused(fn):
    with pytest.raises(AwpError) as exc:
        fn()
    return exc.value.code


def world(*features, mode="streaming", **config):
    return make_net(mode=mode, features=frozenset({"gripper", *features}), **config)


def agent(net, name="agent", mode="streaming", heartbeat_ms=100, **kw):
    a = net.agent(name, heartbeat_ms=heartbeat_ms)
    a.open(mode=mode, **kw)
    return a


def gripper_frames(a):
    return [
        json.loads(e.frame.payload) for e in a.of(FrameReceived) if e.channel == "gripper_state"
    ]


def pending(a, rid):
    """Request `rid` has had no response yet."""
    try:
        a.call(rid)
    except TimeoutError:
        return True
    return False


# ---------------------------------------------------------------- manifest


@pytest.mark.parametrize("mode", ["streaming", "lockstep"])
def test_the_gripper_joins_the_arm_in_one_multi_bind_group(mode):
    m = WorldConfig(mode=mode, features=frozenset({"gripper"})).manifest()
    arm, gripper = m["embodiments"]
    assert arm["multi_bind_group"] == gripper["multi_bind_group"]  # AWP-MAN-007
    assert gripper["shared_control"] is True
    assert gripper["arbitration"].startswith("first-come")  # AWP-MA-003
    assert "shared_control" not in arm
    assert m.get("tick_authority") == ("barrier" if mode == "lockstep" else None)  # AWP-MA-005


def test_without_the_gripper_the_arm_has_no_group():
    arm = WorldConfig().manifest()["embodiments"][0]
    assert "multi_bind_group" not in arm
    net = make_net()
    a = net.agent()
    a.connect()
    a.call(a.client.initialize())
    rid = a.client.open_session("streaming", embodiments=BOTH)
    assert refused(lambda: a.call(rid)) == ErrorCode.EMBODIMENT_UNAVAILABLE


# ---------------------------------------------------------------- multi-bind


def test_one_session_binds_both_and_names_the_embodiment_of_each_action():
    net = world()
    a = agent(net, embodiments=BOTH, subscribe=["proprio", "arm_state", "gripper_state"])
    granted = a.client.ready["granted"]
    assert set(granted["action_types"]) == {"move_to_pose", "stop", "gripper_move"}
    assert {g["channel"] for g in granted["channels"]} == {"proprio", "arm_state", "gripper_state"}
    # AWP-EMB-005: every submission carries embodiment_id
    assert refused(lambda: a.submit("gripper_move", CLOSED)) == ErrorCode.INVALID_PARAMS
    wrong = lambda: a.submit("gripper_move", CLOSED, embodiment_id="arm_01")  # noqa: E731
    assert refused(wrong) == ErrorCode.FORBIDDEN
    move = a.submit("move_to_pose", FAR, embodiment_id="arm_01")
    grip = a.submit("gripper_move", CLOSED, embodiment_id="gripper_01")
    net.advance(200)
    # different concurrency groups run side by side (AWP-PRE-006)
    assert a.client.actions[move].state == a.client.actions[grip].state == "executing"
    assert net.run_until(lambda: a.client.actions[grip].terminal, 5000)
    assert a.client.actions[grip].state == "completed"
    net.advance(200)
    assert gripper_frames(a)[-1]["width_m"] == 0.0
    assert net.world.gripper.width_m == 0.0
    assert_wire_valid(a)


def test_binding_embodiments_outside_one_group_is_refused():
    net = world()
    a = net.agent()
    a.connect()
    a.call(a.client.initialize())
    rid = a.client.open_session("streaming", embodiments=["arm_01", "x-nobody"])
    assert refused(lambda: a.call(rid)) == ErrorCode.EMBODIMENT_UNAVAILABLE
    assert net.world.sessions == {}


def test_the_arm_stays_exclusive():
    net = world()
    agent(net, embodiments=BOTH)
    b = net.agent("b")
    b.connect()
    b.call(b.client.initialize())
    rid = b.client.open_session("streaming", embodiment="arm_01")
    assert refused(lambda: b.call(rid)) == ErrorCode.EMBODIMENT_UNAVAILABLE  # AWP-EMB-001
    b.call(b.client.open_session("streaming", embodiment="gripper_01"))


# ---------------------------------------------------------------- shared control


def test_the_shared_gripper_goes_to_whoever_asked_first():
    net = world()
    a = agent(net, "a", embodiment="gripper_01", subscribe=["gripper_state"])
    b = agent(net, "b", embodiment="gripper_01", subscribe=["gripper_state"])
    first = a.submit("gripper_move", CLOSED)
    assert refused(lambda: b.submit("gripper_move", HALF)) == ErrorCode.BUSY  # its arbitration
    assert net.run_until(lambda: a.client.actions[first].terminal, 5000)
    second = b.submit("gripper_move", HALF)
    assert net.run_until(lambda: b.client.actions[second].terminal, 5000)
    assert b.client.actions[second].state == "completed"
    net.advance(200)
    assert gripper_frames(a)[-1]["width_m"] == 0.04  # both see the one gripper


def test_a_quiet_sharer_does_not_stop_the_other_ones_grip():
    net = world(watchdog_ms=300, heartbeat_interval_ms=100)
    a = agent(net, "a", embodiment="gripper_01")
    quiet = agent(net, "quiet", embodiment="gripper_01", heartbeat_ms=None)
    grip = a.submit("gripper_move", CLOSED)
    assert net.run_until(lambda: "safe_state_entered" in events(quiet), 1000)
    assert a.client.actions[grip].state == "executing"
    assert net.run_until(lambda: a.client.actions[grip].terminal, 5000)
    assert a.client.actions[grip].state == "completed"


def test_safe_state_stops_every_embodiment_the_session_binds():
    net = world(watchdog_ms=300, heartbeat_interval_ms=100)
    a = agent(net, embodiments=BOTH, heartbeat_ms=None)
    move = a.submit("move_to_pose", FAR, embodiment_id="arm_01")
    grip = a.submit("gripper_move", CLOSED, embodiment_id="gripper_01")
    assert net.run_until(lambda: "safe_state_entered" in events(a), 1000)
    for action in (move, grip):
        assert statuses(a, action)[-1] == ("failed", "connection_lost")  # AWP-SAF-004
    entered = [
        e.params["detail"]["embodiment"]
        for e in a.of(WorldEvent)
        if e.event == "safe_state_entered"
    ]
    assert entered == BOTH
    net.advance(1000)
    assert 0 < net.world.gripper.width_m < 0.08
    assert net.world.gripper.at_rest
    assert net.world.arm.at_rest


def test_a_transfer_moves_one_of_the_holders_embodiments():
    net = world("transfer")
    old = agent(net, "old", embodiments=BOTH)
    grip = old.submit("gripper_move", CLOSED, embodiment_id="gripper_01")
    move = old.submit("move_to_pose", FAR, embodiment_id="arm_01")
    token = old.call(old.client.transfer())["transfer_token"]
    agent(net, "new", embodiment="arm_01", takeover=True, transfer_token=token)
    assert statuses(old, move)[-1] == ("preempted", "transferred")  # AWP-EMB-003
    assert old.client.actions[grip].state == "executing"
    detail = next(
        e.params["detail"] for e in old.of(WorldEvent) if e.event == "embodiment_transferred"
    )
    assert detail == {"embodiment": "arm_01"}
    again = lambda: old.submit("move_to_pose", FAR, embodiment_id="arm_01")  # noqa: E731
    assert refused(again) == ErrorCode.FORBIDDEN
    old.submit("gripper_move", HALF)  # one embodiment left: embodiment_id may be left out


# ---------------------------------------------------------------- the barrier


def lockstep_pair(net):
    a = agent(net, "a", mode="lockstep", embodiment="arm_01", subscribe=["proprio"])
    b = agent(net, "b", mode="lockstep", embodiment="gripper_01", subscribe=["gripper_state"])
    return a, b


def test_the_world_advances_once_every_bound_session_has_ticked():
    net = world(mode="lockstep")
    a, b = lockstep_pair(net)
    observer = agent(net, "observer", mode="lockstep", subscribe=["proprio"])
    grip = b.submit("gripper_move", CLOSED)
    first = a.client.advance()
    assert pending(a, first)  # AWP-TIM-012: a call advances nothing on its own
    assert net.world.tick == 0
    assert refused(lambda: a.call(a.client.advance())) == ErrorCode.INVALID_REQUEST
    assert refused(lambda: observer.call(observer.client.advance())) == (
        ErrorCode.TICK_NOT_AUTHORIZED
    )
    assert b.call(b.client.advance()) == {"tick": 1}
    assert a.call(first) == {"tick": 1}
    assert b.client.actions[grip].state == "executing"
    for s in (a, b, observer):  # the same advance, delivered to all (AWP-TIM-003)
        assert s.client.holds_tick(1)
    assert "tick" not in a.client.ready["granted"]["admin"]
    assert_wire_valid(a)
    assert_wire_valid(b)


def test_each_call_advances_as_far_as_its_count():
    net = world(mode="lockstep")
    a, b = lockstep_pair(net)
    three = a.client.advance(3)
    assert b.call(b.client.advance()) == {"tick": 1}
    assert pending(a, three)
    assert b.call(b.client.advance(2)) == {"tick": 3}
    assert a.call(three) == {"tick": 3}
    assert [e.tick for e in a.of(TickCompleted)] == [3]


def test_a_closing_session_releases_the_barrier():
    net = world(mode="lockstep")
    a, b = lockstep_pair(net)
    rid = a.client.advance()
    assert pending(a, rid)
    b.call(b.client.close())
    assert a.call(rid) == {"tick": 1}


def test_a_reset_answers_a_waiting_tick_with_the_new_tick():
    net = world(mode="lockstep")
    a = agent(net, "a", mode="lockstep", embodiment="arm_01", admin=["reset"])
    b = agent(net, "b", mode="lockstep", embodiment="gripper_01")
    b.client.advance()
    assert a.call(a.client.advance()) == {"tick": 1}
    rid = b.client.advance()
    assert pending(b, rid)
    a.call(a.client.reset())
    with pytest.raises(AwpError) as exc:
        b.call(rid)
    assert exc.value.code == ErrorCode.TICK_MISMATCH
    assert exc.value.data["tick"] == 0 == net.world.tick


def test_a_tick_from_a_new_connection_replaces_the_lost_one():
    net = world(mode="lockstep")
    a, b = lockstep_pair(net)
    a.client.advance()
    net.settle()
    a.drop()
    a.connect()
    a.call(a.client.initialize())
    a.call(a.client.resume())
    rid = a.client.advance()
    assert pending(a, rid)
    assert b.call(b.client.advance()) == {"tick": 1}
    assert a.call(rid) == {"tick": 1}


def test_a_restore_brings_the_gripper_back():
    net = world("sim", mode="lockstep")
    a = agent(net, mode="lockstep", embodiments=BOTH, admin=["snapshot", "restore"])
    token = a.call(a.client.snapshot())["snapshot_token"]
    a.submit("gripper_move", CLOSED, embodiment_id="gripper_01")
    a.call(a.client.advance(20))
    assert net.world.gripper.width_m < 0.08
    a.call(a.client.restore(token))
    assert net.world.gripper.width_m == 0.08  # AWP-REP-002


def test_an_e_stop_is_reported_for_each_embodiment():
    net = world()
    a = agent(net, embodiment="gripper_01")
    grip = a.submit("gripper_move", CLOSED)
    net.deliver(net.world.engage_estop(net.now))
    net.settle()
    engaged = [
        e.params["detail"]["embodiment"] for e in a.of(WorldEvent) if e.event == "e_stop_engaged"
    ]
    assert engaged == BOTH
    assert statuses(a, grip)[-1] == ("failed", "e_stop")  # AWP-EVT-002
