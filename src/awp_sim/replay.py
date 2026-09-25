"""Replay a replay bundle and compare what the world does with what it recorded (AWP-REP-003).

The world is rebuilt from the bundle's configuration and initial state; the session is opened as
recorded, and the agent's calls that change action state are fed in their recorded order.
Compared: every frame's payload hash per channel, and the ordered `(state, reason)` sequence of
every action. Timestamps, sequence numbers, heartbeats, reports, and telemetry are not.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awp.client import ActionUpdated, FrameReceived

from .loopback import Loopback
from .world import World, decode_config, decode_state

# What changes action state: the agent's calls, in their recorded order.
REPLAYED = (
    "action.submit",
    "action.cancel",
    "world.tick",
    "world.reset",
    "world.restore",
    "session.close",
)


@dataclass
class Outcome:
    frames: int
    transitions: int
    difference: str | None

    @property
    def reproduced(self) -> bool:
        return self.difference is None


def _recorded(
    records: list[dict[str, Any]],
) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, Any]]]]:
    channel_of: dict[int, str] = {}
    frames: dict[str, list[str]] = {}
    states: dict[str, list[tuple[str, Any]]] = {}
    for r in records:
        body = r["body"]
        if r["kind"] == "frame" and body.get("method") == "obs.frame":
            name = channel_of.get(body["channel_id"], str(body["channel_id"]))
            frames.setdefault(name, []).append(body["payload_sha256"])
        elif r["kind"] == "message":
            result = body.get("result") or {}
            for g in (
                (result.get("granted") or {}).get("channels", [])
                if isinstance(result.get("granted"), dict)
                else []
            ):
                channel_of[g["channel_id"]] = g["channel"]
            params = body.get("params") or {}
            status = params if body.get("method") == "action.status" else None
            if (
                status is None
                and "action_id" in result
                and "state" in result
                and "status_seq" in result
            ):
                status = result
            if status is not None and r["direction"] == "world":
                seq = states.setdefault(status["action_id"], [])
                entry = (status["state"], status.get("reason"))
                if not seq or seq[-1] != entry:
                    seq.append(entry)
    return frames, states


def replay(path: Path | str) -> Outcome:
    records = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    header = records[0]
    if header.get("class") != "replay_bundle":
        raise ValueError(f"{path} is a {header.get('class')}, not a replay_bundle (AWP-AUD-007)")
    body = header["body"]
    world = World(decode_config(body["config"]))
    world.load_state(decode_state(body["initial_state"]))
    net = Loopback(world)
    agent = net.agent("replay", heartbeat_ms=None)
    agent.connect()
    agent.call(agent.client.initialize())
    opened = next(r["body"]["params"] for r in records if r["body"].get("method") == "session.open")
    agent.call(agent.client.request("session.open", opened))
    for r in records:
        msg = r["body"]
        if r["direction"] == "agent" and msg.get("method") in REPLAYED and "id" in msg:
            agent.client.request(msg["method"], msg.get("params") or {})
            net.settle()
    want_frames, want_states = _recorded(records)
    got_frames: dict[str, list[str]] = {}
    for e in agent.of(FrameReceived):
        got_frames.setdefault(e.channel, []).append(hashlib.sha256(e.frame.payload).hexdigest())
    got_states: dict[str, list[tuple[str, Any]]] = {}
    for e in agent.of(ActionUpdated):
        seq = got_states.setdefault(e.action.action_id, [])
        entry = (e.status["state"], e.status.get("reason"))
        if not seq or seq[-1] != entry:
            seq.append(entry)
    difference = None
    for name, hashes in want_frames.items():
        if got_frames.get(name, []) != hashes:
            got = got_frames.get(name, [])
            at = next(
                (i for i, (a, b) in enumerate(zip(hashes, got, strict=False)) if a != b),
                min(len(hashes), len(got)),
            )
            difference = (
                f"channel {name}: frame {at} differs ({len(hashes)} recorded, {len(got)} replayed)"
            )
            break
    if difference is None and got_states != want_states:
        bad = next(
            k
            for k in sorted(set(want_states) | set(got_states))
            if want_states.get(k) != got_states.get(k)
        )
        difference = (
            f"action {bad}: recorded {want_states.get(bad)}, replayed {got_states.get(bad)}"
        )
    return Outcome(
        sum(len(v) for v in want_frames.values()),
        sum(len(v) for v in want_states.values()),
        difference,
    )
