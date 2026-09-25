"""The ws stream binding: frames on their own connection, in the binary envelope."""

from __future__ import annotations

import asyncio

import pytest
from awp.aio import AsyncClient
from awp.client import ClientConnection, FrameReceived, ProtocolViolation
from awp.frames import Frame
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus
from websockets.typing import Subprotocol

from awp_sim.config import WorldConfig
from awp_sim.server import Server
from awp_sim.world import World

from .helpers import events, make_net, pose

AGENT = {"name": "streams", "version": "0.1.0", "vendor": "tests"}


def streamed(**config):
    net = make_net(**config)
    net.world.stream_url = "ws://127.0.0.1:0/stream"
    a = net.agent(heartbeat_ms=100)
    mode = config.get("mode", "streaming")
    a.open(mode=mode, embodiment="arm_01", subscribe=["proprio", "arm_state"])
    return net, a


def inline_frames(a) -> int:
    return sum(1 for line in a.trace if line.msg.get("method") == "obs.frame")


def test_session_ready_offers_ws_before_inline():
    _, a = streamed()
    assert [e["binding"] for e in a.client.stream_endpoints] == ["ws", "inline"]  # AWP-TRN-004


def test_channels_move_to_the_stream_connection_with_a_resync_keyframe():
    net, a = streamed()
    before = inline_frames(a)
    a.attach_stream()
    net.advance(300)
    assert inline_frames(a) == before  # nothing inline once the channel moved (AWP-TRN-012)
    frames = [e for e in a.of(FrameReceived) if e.frame.ts_send_ns is not None]
    first: dict[str, Frame] = {}
    for e in a.of(FrameReceived)[before:]:
        first.setdefault(e.channel, e.frame)
    assert all(f.resync and f.keyframe for f in first.values())
    assert frames
    assert all(f.frame.ts_send_ns >= f.frame.ts_mono_ns for f in frames)
    assert not a.of(ProtocolViolation)


def test_a_lost_stream_withholds_frames_and_degrades_reliable_channels():
    net, a = streamed()
    a.attach_stream()
    net.advance(100)
    a.drop_stream()
    count = len(a.of(FrameReceived))
    net.advance(400)  # arm_state is reliable at 10 Hz: stale after 200 ms
    assert len(a.of(FrameReceived)) == count  # not inline either (AWP-TRN-010)
    assert "channel_degraded" in events(a)  # AWP-SAF-009
    a.attach_stream()
    net.advance(50)
    resumed = a.of(FrameReceived)[count:]
    assert resumed
    assert all(e.frame.resync for e in resumed[:2])
    assert not a.of(ProtocolViolation)


def test_a_stream_needs_the_session_token():
    net, _ = streamed()
    out = net.world.attach_stream(99, "st_not_a_token", net.now)
    assert [type(o).__name__ for o in out] == ["Close"]  # AWP-SEC-004


def test_suspension_closes_the_stream_and_resumption_goes_inline():
    net, a = streamed(watchdog_ms=400, heartbeat_interval_ms=200, reconnect_window_ms=3000)
    a.attach_stream()
    net.advance(50)
    a.drop()
    assert a.stream is None
    a.connect()
    a.call(a.client.initialize())
    a.call(a.client.resume())
    before = inline_frames(a)
    net.advance(200)
    assert inline_frames(a) > before  # frames inline until the agent attaches again


def test_lockstep_frames_on_the_stream_carry_their_tick():
    _, a = streamed(mode="lockstep")
    a.attach_stream()
    a.submit("move_to_pose", pose(0.3, 0.2, 0.5))
    tick = a.call(a.client.advance(3))["tick"]
    ticks = [e.frame.tick for e in a.of(FrameReceived) if e.channel == "proprio"]
    assert ticks[-3:] == [tick - 2, tick - 1, tick]  # AWP-OBS-002, AWP-TIM-003


@pytest.fixture
async def server():
    config = WorldConfig(watchdog_ms=1000, heartbeat_interval_ms=300, reconnect_window_ms=5000)
    async with Server(World(config), port=0, stream_binding=True) as s:
        yield s


async def test_async_client_uses_the_stream_connection(server):
    conn = ClientConnection(AGENT, ["proprio/json", "text/event+json"])
    async with AsyncClient(conn, server.url) as client:
        await client.initialize()
        await client.open_session("streaming", embodiment="arm_01", subscribe=["proprio"])
        await asyncio.sleep(0.4)
        session = next(iter(server.world.sessions.values()))
        assert session.stream_conn is not None
        seen = []
        async for event in client.events():
            if isinstance(event, FrameReceived):
                seen.append(event)
            if len(seen) >= 5:
                break
        record = await client.submit("move_to_pose", pose(0.1, 0.1, 0.45), basis=seen[-1].frame)
        assert (await client.wait_terminal(record.action_id)).state == "completed"
        await client.close_session()


async def test_stream_endpoint_rejects_missing_and_url_credentials(server):
    url = server.url + "/stream"
    with pytest.raises(InvalidStatus) as exc:
        async with connect(url, subprotocols=[Subprotocol("awp")]):
            pass
    assert exc.value.response.status_code == 401
    with pytest.raises(InvalidStatus) as exc:
        async with connect(url + "?token=st_x", subprotocols=[Subprotocol("awp")]):
            pass
    assert exc.value.response.status_code == 400  # AWP-SEC-006
