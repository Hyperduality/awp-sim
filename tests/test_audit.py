from __future__ import annotations

import json
from typing import Any

from awp_sim.audit import AuditLog, redact, verify_chain
from awp_sim.config import WorldConfig
from awp_sim.loopback import Loopback
from awp_sim.world import World

from .helpers import FIXED_WALL, pose


def test_session_audit_log_is_redacted_hashed_and_chained(tmp_path):
    audit = AuditLog(tmp_path)
    net = Loopback(World(WorldConfig(), audit=audit, wall_clock=lambda: FIXED_WALL))
    a = net.agent()
    ready = a.open(mode="streaming", embodiment="arm_01", subscribe=["proprio"])
    a.submit("move_to_pose", pose(0.2, 0.1, 0.4))
    net.advance(100)
    closing = a.client.close()
    net.advance(600)  # the close waits for the safe abort of the executing move
    assert a.call(closing) == {}

    (path,) = tmp_path.glob("*.jsonl")
    lines = path.read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert records[0]["class"] == "audit_record"
    assert records[0]["body"]["manifest"]["world"]["name"] == "awp-sim"
    assert verify_chain(lines)
    assert ready["session_token"] not in path.read_text()
    kinds = {r["kind"] for r in records}
    assert kinds == {"header", "message", "frame"}
    frame = next(r for r in records if r["kind"] == "frame")
    assert "payload_b64" not in frame["body"]
    assert len(frame["body"]["payload_sha256"]) == 64
    methods = [r["body"].get("method") for r in records if r["kind"] == "message"]
    assert "initialize" in methods
    assert "action.submit" in methods


def test_tampering_breaks_the_chain(tmp_path):
    log = AuditLog(tmp_path)
    log.open("s", {"manifest": {}})
    log.record("s", 1, "agent", {"jsonrpc": "2.0", "method": "ping", "id": 1, "params": {}})
    log.record("s", 2, "world", {"jsonrpc": "2.0", "id": 1, "result": {}})
    log.close("s")
    lines = (tmp_path / "s.jsonl").read_text().splitlines()
    lines[1] = lines[1].replace('"ts_mono_ns":1', '"ts_mono_ns":9')
    assert not verify_chain(lines)


def test_redact_paths_and_credentials():
    msg: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"session_token": "st_x", "task": {"content": "hi"}},
    }
    out = redact(msg, ["/task/content"])
    assert out["result"]["session_token"].startswith("[redacted:sha256:")
    assert out["result"]["task"]["content"].startswith("[redacted:sha256:")
    assert msg["result"]["session_token"] == "st_x"
