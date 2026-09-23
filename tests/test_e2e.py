"""The client and the reference world over real WebSockets."""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus
from websockets.typing import Subprotocol

from awp.aio import AsyncClient
from awp.client import ClientConnection
from awp.errors import AwpError, ErrorCode
from awp_sim.config import WorldConfig
from awp_sim.demo import run_demo
from awp_sim.server import Server
from awp_sim.world import World

from .helpers import pose

AGENT = {"name": "e2e", "version": "0.1.0", "vendor": "tests"}


def client(server: Server, **kw) -> AsyncClient:
    return AsyncClient(ClientConnection(AGENT, ["proprio/json"]), server.url, **kw)


@pytest.fixture
async def streaming():
    config = WorldConfig(watchdog_ms=400, heartbeat_interval_ms=200, reconnect_window_ms=5000)
    async with Server(World(config), port=0) as server:
        yield server


async def test_demo_runs_against_both_time_models():
    for mode in ("streaming", "lockstep"):
        async with Server(World(WorldConfig(mode=mode)), port=0) as server:
            metrics = await run_demo(server.url, echo=False)
            assert metrics["admission_p95_ms"] < 250


async def test_move_cancel_and_refusal(streaming):
    async with client(streaming) as c:
        await c.initialize()
        await c.open_session("streaming", embodiment="arm_01", subscribe=["proprio"])
        await c.wait_for(lambda e: "proprio" in c.latest)
        record = await c.submit(
            "move_to_pose", pose(0.3, 0.2, 0.5), basis=c.latest["proprio"].frame, valid_for_ms=300
        )
        await asyncio.sleep(0.3)
        await c.cancel(record.action_id)
        final = await c.wait_terminal(record.action_id, 5)
        assert final.state == "cancelled"
        with pytest.raises(AwpError) as exc:
            await c.submit("move_to_pose", pose(0.9, 0, 0.4))
        assert exc.value.code == ErrorCode.ENVELOPE_EXCEEDED
        await c.close_session()


async def test_reconnect_resumes_and_replays(streaming):
    async with client(streaming) as c:
        await c.initialize()
        await c.open_session("streaming", embodiment="arm_01")
        record = await c.submit("move_to_pose", pose(0.3, 0.2, 0.5))
        seen = c.conn.last_status_seq
        ready = await c.reconnect()
        assert ready["replay_to_status_seq"] >= seen
        assert c.conn.session_state == "active"
        final = await c.wait_terminal(record.action_id, 5)
        assert final.state == "completed"  # a brief outage inside watchdog_ms does not stop motion


async def test_heartbeat_holds_off_the_watchdog(streaming):
    async with client(streaming) as c:
        await c.initialize()
        await c.open_session("streaming", embodiment="arm_01")
        await asyncio.sleep(1.0)  # two and a half watchdog periods
        server_session = next(iter(streaming.world.sessions.values()))
        assert not server_session.safe_state


async def test_authentication():
    async with Server(World(), port=0, token="t0ken-abcdefghijk") as server:
        with pytest.raises(InvalidStatus) as exc:
            await connect(server.url)
        assert exc.value.response.status_code == 401
        with pytest.raises(InvalidStatus) as exc:
            await connect(server.url + "/?token=t0ken-abcdefghijk")
        assert exc.value.response.status_code == 400
        offered = [Subprotocol("awp"), Subprotocol("awp.bearer.t0ken-abcdefghijk")]
        ws = await connect(server.url, subprotocols=offered)
        assert ws.subprotocol == "awp"  # the credential is never echoed
        await ws.close()
        async with client(server, token="t0ken-abcdefghijk") as c:
            assert (await c.initialize())["world"]["name"] == "awp-sim"


def test_non_loopback_requires_token_and_tls():
    with pytest.raises(ValueError, match="authenticate"):
        Server(World(), host="0.0.0.0")
    with pytest.raises(ValueError, match="TLS"):
        Server(World(), host="0.0.0.0", token="x" * 20)
    Server(World(), host="0.0.0.0", allow_insecure=True)


async def test_server_records_session_traces(tmp_path):
    async with Server(World(), port=0, record_dir=tmp_path) as server, client(server) as c:
        await c.initialize()
        await c.open_session("streaming", embodiment="arm_01", subscribe=["proprio"])
        await c.wait_for(lambda e: "proprio" in c.latest)
        await c.close_session()
    (trace,) = tmp_path.glob("*.jsonl")
    lines = [json.loads(line) for line in trace.read_text().splitlines()]
    assert lines[0]["msg"]["method"] == "initialize"
    assert any(line["msg"].get("method") == "obs.frame" for line in lines)
    assert lines[-1]["from"] == "world"
