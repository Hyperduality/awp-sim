"""Evidence for the requirement-matrix rows the conformance suite marks `manual`.

conformance/README.md cites each test here. AWP-DAT-008 is tests/test_frames.py.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import pytest

from awp.client import FrameReceived
from awp.errors import AwpError
from awp_sim.config import FEATURES, WorldConfig
from awp_sim.server import _Outbox
from awp_sim.world import Send

from .helpers import assert_wire_valid, events, make_net, pose, statuses

# ------------------------------------------------------------ AWP-DAT-002, AWP-TRN-009


def frame(channel: int, seq: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "obs.frame", "params": {"channel_id": channel, "seq": seq}}


def test_a_stalled_receiver_holds_one_frame_per_latest_wins_channel():
    """Nothing drains the send queue while frames are produced: a stalled receiver."""
    box = _Outbox()
    for seq in range(1, 1001):
        box.put(Send(1, frame(1, seq), latest_wins=True))  # proprio: latest-wins
        if seq % 100 == 0:
            box.put(Send(1, frame(2, seq // 100)))  # arm_state: reliable
    pending = [asyncio.run(box.get()) for _ in range(len(box._items))]
    sent = [item.msg["params"] for item in pending if isinstance(item, Send)]
    latest = [p["seq"] for p in sent if p["channel_id"] == 1]
    reliable = [p["seq"] for p in sent if p["channel_id"] == 2]
    assert len(sent) == len(pending)
    assert latest == [1000]  # every older frame was replaced, not queued
    assert reliable == list(range(1, 11))  # nothing reliable dropped, in order


# ------------------------------------------------------------ AWP-DAT-001, AWP-DAT-009 (agent side)


@pytest.mark.parametrize("mode", ["lockstep", "streaming"])
def test_the_receiver_counts_seq_gaps_as_loss_but_not_the_gap_before_a_resync(mode):
    net = make_net(mode=mode)
    a = net.agent(heartbeat_ms=None if mode == "lockstep" else 500)
    a.open(mode=mode, embodiment="arm_01", subscribe=["proprio", "arm_state"])
    ready = a.client.ready
    assert ready is not None
    before = a.client.delivery()
    ids = {g["channel"]: g["channel_id"] for g in ready["granted"]["channels"]}
    last = {
        cid: max(f.frame.seq for f in a.of(FrameReceived) if f.frame.channel_id == cid)
        for cid in ids.values()
    }

    def deliver(cid: int, seq: int, *, resync: bool = False) -> None:
        params = {
            "channel_id": cid,
            "seq": seq,
            "ts_mono_ns": 0,
            "flags": 0x09 if resync else 0x01,
            "payload_b64": "e30=",
            **({"tick": a.client.tick} if mode == "lockstep" else {"ts_send_ns": 0}),
        }
        events = a.client.receive({"jsonrpc": "2.0", "method": "obs.frame", "params": params})
        assert [type(e) for e in events] == [FrameReceived]

    proprio, arm_state = ids["proprio"], ids["arm_state"]
    for n in (1, 2, 5):  # seqs 3 and 4 are lost
        deliver(proprio, last[proprio] + n)
    deliver(arm_state, last[arm_state] + 1)
    deliver(arm_state, last[arm_state] + 6, resync=True)  # a discontinuity the sender knew of
    after = a.client.delivery()
    counts = {n: (after[n][0] - before[n][0], after[n][1] - before[n][1]) for n in ids}
    assert counts == {"proprio": (3, 2), "arm_state": (2, 0)}
    if mode == "streaming":  # and so the receiver report states (AWP-OBS-007)
        report = a.client.report()["params"]["channels"]
        assert report[str(proprio)]["gaps"] == 2
        assert report[str(arm_state)]["gaps"] == 0


# ------------------------------------------------------------ AWP-ENV-003


def inside(net, config: WorldConfig) -> bool:
    (lo, hi), arm = config.aabb_m, net.world.arm
    within = all(lo[i] - 1e-9 <= arm.position[i] <= hi[i] + 1e-9 for i in range(3))
    return within and arm.speed <= config.max_velocity_mps + 1e-9


@pytest.mark.parametrize("mode", ["lockstep", "streaming"])
def test_a_disturbance_during_execution_fails_the_action(mode):
    net = make_net(mode=mode)
    a = net.agent(heartbeat_ms=None if mode == "lockstep" else 500)
    a.open(mode=mode, embodiment="arm_01")
    action = a.submit("move_to_pose", pose(0.4, 0.4, 0.6))

    def step():
        if mode == "lockstep":
            a.call(a.client.advance())
        else:
            net.advance(20)

    step()
    step()
    assert a.client.actions[action].state == "executing"
    net.world.disturb((0.0, 0.0, 1.0))  # pushed above the envelope
    for _ in range(100):
        if a.client.actions[action].terminal:
            break
        step()
    assert "envelope_violation" in events(a)
    assert statuses(a, action)[-1] == ("failed", "envelope")
    assert_wire_valid(a)


@pytest.mark.parametrize("mode", ["lockstep", "streaming"])
def test_commanded_motion_never_leaves_the_envelope(mode):
    """Seeded random moves, replacements, queues, stops, and cancels, checked after every
    advance: without a disturbance the arm stays within the envelope."""
    config = WorldConfig(mode=mode)
    (lo, hi), v_max = config.aabb_m, config.max_velocity_mps
    net = make_net(mode=mode, watchdog_ms=config.reconnect_window_ms)
    a = net.agent(heartbeat_ms=None if mode == "lockstep" else 500)
    a.open(mode=mode, embodiment="arm_01")
    rng = random.Random(7)
    open_ids: list[str] = []
    for _ in range(400):
        choice = rng.random()
        try:
            if choice < 0.6:
                target = [rng.uniform(lo[i], hi[i]) for i in range(3)]
                params = pose(*target)
                if rng.random() < 0.3:
                    params["max_velocity_mps"] = rng.uniform(0.01, v_max)
                preempt = rng.choice(["replace", "queue"])
                open_ids.append(a.submit("move_to_pose", params, preempt=preempt))
            elif choice < 0.75:
                a.submit("stop", {})
            elif open_ids:
                a.call(a.client.cancel(rng.choice(open_ids)))
        except AwpError:
            pass  # refused (rate, queue full, already terminal): nothing moved
        for _ in range(rng.randint(1, 60)):
            if mode == "lockstep":
                a.call(a.client.advance())
            else:
                net.advance(20)
            assert inside(net, config)
    assert "envelope_violation" not in events(a)


def test_servo_motion_never_leaves_the_envelope():
    """Setpoints toward the walls, and stops during them."""
    config = WorldConfig(mode="streaming", features=frozenset({"servo"}))
    net = make_net(mode="streaming", features=config.features)
    a = net.agent(heartbeat_ms=100)
    a.open(mode="streaming", embodiment="arm_01")
    rng = random.Random(7)
    for _ in range(60):
        action = a.submit("servo", {})
        direction = [rng.uniform(-1, 1) for _ in range(3)]
        norm = sum(x * x for x in direction) ** 0.5
        v = [x / norm * config.max_velocity_mps * rng.uniform(0.5, 1.0) for x in direction]
        for _ in range(rng.randint(10, 150)):
            a.client.command("servo_arm", {"v_mps": v})
            net.advance(10)
            assert inside(net, config)
        a.call(a.client.cancel(action))
        while not a.client.actions[action].terminal:
            net.advance(10)
            assert inside(net, config)
    assert "envelope_violation" not in events(a)


# ------------------------------------------------------------ AWP-UNI-001

SI_SUFFIXES = ("_m", "_rad", "_s", "_ms", "_ns", "_mps", "_radps", "_n", "_nm", "_kg", "_hz")
# Reviewed: names awp-sim defines that carry no physical quantity.
NOT_PHYSICAL = {"pose", "frame", "q", "phase", "action_id"}


def names(schema: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(schema, dict):
        out |= set(schema.get("properties", {}))
        out |= set(schema.get("fields", []))
        for v in schema.values():
            out |= names(v)
    elif isinstance(schema, list):
        for v in schema:
            out |= names(v)
    return out


def test_world_defined_fields_carry_si_suffixes():
    """Every field awp-sim defines — action params, channel schemas, and the payloads it sends —
    either names an SI unit by its suffix or carries no physical quantity."""
    defined: set[str] = set()
    for mode in ("lockstep", "streaming"):
        features = {f for f in FEATURES if f != ("servo" if mode == "lockstep" else "sim")}
        manifest = WorldConfig(mode=mode, features=frozenset(features)).manifest()
        for decl in manifest["action_schemas"]:
            defined |= names(decl["params_schema"])
        defined |= names(manifest.get("$defs", {}))
        for ch in manifest["observation_channels"] + manifest.get("command_channels", []):
            defined |= names(ch.get("schema", {}))
    net = make_net(mode="streaming")
    a = net.agent()
    a.open(mode="streaming", embodiment="arm_01", subscribe=["proprio", "arm_state"])
    a.submit("move_to_pose", pose(0.3, 0.2, 0.5))
    net.advance(300)
    for f in a.of(FrameReceived):
        defined |= set(json.loads(f.frame.payload))
    physical = defined - NOT_PHYSICAL
    assert physical, "nothing reviewed"
    assert {n for n in physical if not n.endswith(SI_SUFFIXES)} == set()
