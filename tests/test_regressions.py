"""Regression tests for defects found in review."""

from __future__ import annotations

import asyncio
import json

from awp.aio import AsyncClient
from awp.client import ClientConnection, FrameReceived
from awp.errors import ErrorCode

from awp_sim.config import WorldConfig
from awp_sim.server import Server, _Outbox
from awp_sim.world import Close, Send, World

from .helpers import make_net, pose, refused, statuses

FAR = pose(0.3, 0.2, 0.5)
AGENT = {"name": "r", "version": "1", "vendor": "tests"}


def streaming_agent(net, **kw):
    a = net.agent(**kw)
    a.open(mode="streaming", embodiment="arm_01", subscribe=["proprio"])
    return a


def test_requests_are_refused_while_the_session_closes():
    net = make_net()
    a = streaming_agent(net)
    a.submit("move_to_pose", FAR)
    net.advance(200)
    first = a.client.close()
    net.settle()
    assert refused(lambda: a.submit("stop", {})) == ErrorCode.SESSION_EXPIRED  # AWP-SES-011
    second = a.client.close()
    assert net.run_until(lambda: a.client.session_state == "closed", 2000)
    assert a.call(first) == {}
    assert a.call(second) == {}


def test_method_names_are_matched_exactly():
    net = make_net()
    a = net.agent()
    a.connect()
    a.call(a.client.initialize())
    assert (
        refused(lambda: a.call(a.client.request("world_reset", {}))) == ErrorCode.METHOD_NOT_FOUND
    )


def test_handler_exceptions_become_internal_errors():
    net = make_net()
    a = streaming_agent(net)

    def broken(*args):
        raise RuntimeError("boom")

    net.world._rpc_action_status = broken  # type: ignore[method-assign]
    assert refused(lambda: a.call(a.client.pull_status("x"))) == ErrorCode.INTERNAL_ERROR
    a.submit("stop", {})  # the world keeps serving


def test_resume_counts_as_agent_traffic_for_the_watchdog():
    net = make_net(watchdog_ms=400, heartbeat_interval_ms=200)
    a = streaming_agent(net, heartbeat_ms=None)
    a.drop()
    net.advance(300)
    a.connect()
    a.call(a.client.initialize())
    a.call(a.client.resume())
    net.advance(300)
    assert not next(iter(net.world.sessions.values())).safe_state


def test_lockstep_replace_preempts_only_when_it_begins_executing():
    net = make_net(mode="lockstep")
    a = net.agent(heartbeat_ms=None)
    a.open(mode="lockstep", embodiment="arm_01")
    first = a.submit("move_to_pose", FAR)
    a.call(a.client.advance(count=3))
    staged = a.submit("move_to_pose", pose(0, 0, 0.3), preempt="replace")
    assert a.client.actions[first].state == "executing"
    a.call(a.client.cancel(staged))  # no side effects before the advance (AWP-TIM-010)
    a.call(a.client.advance(count=3))  # also clears the action rate limit
    assert a.client.actions[first].state == "executing"
    replacing = a.submit("move_to_pose", pose(0, 0, 0.3), preempt="replace", action_id="a-r2")
    a.call(a.client.advance())
    assert a.client.actions[first].state == "preempted"
    assert a.client.actions[replacing].state == "executing"


def test_an_observer_closing_in_lockstep_leaves_the_holders_move_running():
    net = make_net(mode="lockstep")
    a = net.agent(heartbeat_ms=None)
    a.open(mode="lockstep", embodiment="arm_01")
    observer = net.agent("observer", heartbeat_ms=None)
    observer.open(mode="lockstep")
    move = a.submit("move_to_pose", FAR)
    a.call(a.client.advance(count=3))
    observer.call(observer.client.close())
    a.call(a.client.advance())
    assert a.client.actions[move].state == "executing"


def test_a_deadline_abort_is_bounded_by_max_abort_ms():
    net = make_net(max_duration_ms=100)
    a = streaming_agent(net)
    move = a.submit("move_to_pose", FAR)
    net.advance(50)
    net.world.arm.stuck = True
    assert net.run_until(lambda: a.client.actions[move].terminal, 3000)
    assert statuses(a, move)[-1] == ("failed", "abort_failed")


def test_close_outranks_a_cancel_in_progress():
    net = make_net()
    a = streaming_agent(net)
    move = a.submit("move_to_pose", FAR)
    net.advance(200)
    a.call(a.client.cancel(move))
    a.client.close()
    assert net.run_until(lambda: a.client.actions[move].terminal, 2000)
    assert statuses(a, move)[-1] == ("cancelled", "session_closed")


def test_replacing_a_deadline_abort_reports_the_deadline():
    net = make_net(max_duration_ms=200)
    a = streaming_agent(net)
    move = a.submit("move_to_pose", FAR)
    net.advance(210)
    a.submit("stop", {})
    assert statuses(a, move)[-1] == ("failed", "deadline_exceeded")


def test_text_from_an_unknown_connection_is_ignored():
    world = World()
    assert world.receive_text("gone", "{not json", 0) == []


def test_idle_connections_without_a_session_are_closed_after_15_s():
    net = make_net(heartbeat_interval_ms=100)
    a = net.agent()
    a.connect()
    net.advance(14_000)
    assert a.conn is not None  # AWP-SES-012: at least 15 s, whatever the heartbeat interval
    net.advance(1_100)
    assert a.conn is None


def test_channels_in_undeclared_modalities_are_not_granted():
    net = make_net()
    a = net.agent(modalities=["proprio/json"])
    ready = a.open(mode="streaming", embodiment="arm_01", subscribe=["proprio", "arm_state"])
    assert [g["channel"] for g in ready["granted"]["channels"]] == ["proprio"]  # AWP-AGM-001
    assert refused(lambda: a.call(a.client.subscribe(["arm_state"]))) == ErrorCode.FORBIDDEN
    net.advance(200)
    assert {f.channel for f in a.of(FrameReceived)} == {"proprio"}


def test_outbox_is_bounded_and_coalesces_latest_wins():
    box = _Outbox()
    frame = {"jsonrpc": "2.0", "method": "obs.frame", "params": {"channel_id": 1}}
    assert box.put(Send(1, frame, latest_wins=True))
    assert box.put(Send(1, frame, latest_wins=True))
    for _ in range(box.LIMIT - 1):
        assert box.put(Send(1, {"jsonrpc": "2.0", "method": "x", "params": {}}))
    assert not box.put(Close(1, "x"))


async def test_a_reader_failure_closes_the_connection():
    config = WorldConfig(watchdog_ms=400, heartbeat_interval_ms=200)
    async with Server(World(config), port=0) as server:
        conn = ClientConnection(AGENT, ["proprio/json"])
        client = AsyncClient(conn, server.url)
        await client.connect()
        await client.initialize()
        await client.open_session("streaming", embodiment="arm_01", subscribe=["proprio"])
        conn.receive = lambda msg: (_ for _ in ()).throw(KeyError("boom"))  # type: ignore[method-assign]
        for _ in range(50):
            if not client.connected:
                break
            await asyncio.sleep(0.02)
        assert not client.connected
        await asyncio.sleep(0.6)
        session = next(iter(server.world.sessions.values()))
        assert session.safe_state  # no stray heartbeat kept the watchdog off
        await client.aclose()


async def test_recorded_traces_redact_session_tokens(tmp_path):
    async with Server(World(), port=0, record_dir=tmp_path) as server:
        conn = ClientConnection(AGENT, ["proprio/json"])
        async with AsyncClient(conn, server.url) as client:
            await client.initialize()
            ready = await client.open_session("streaming", embodiment="arm_01")
            await client.close_session()
    text = next(tmp_path.glob("*.jsonl")).read_text()
    assert ready["session_token"] not in text
    results = [json.loads(line)["msg"].get("result", {}) for line in text.splitlines()]
    tokens = [r["session_token"] for r in results if "session_token" in r]
    assert tokens
    assert tokens[0].startswith("[redacted")


def test_a_latest_wins_replacement_keeps_the_resync_flag():
    from awp.frames import Frame

    from awp_sim.world import SendFrame

    box = _Outbox()
    box.put(SendFrame("c", Frame(1, 5, 0, b"a", keyframe=True, resync=True), "s", latest_wins=True))
    box.put(SendFrame("c", Frame(1, 6, 1, b"b", keyframe=True), "s", latest_wins=True))
    item = asyncio.run(box.get())
    assert isinstance(item, SendFrame)
    assert item.frame.seq == 6
    assert item.frame.resync  # AWP-TRN-012, AWP-DAT-009
