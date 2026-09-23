"""Reference world configuration and the manifest it declares."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from . import __version__

Mode = Literal["lockstep", "streaming"]

EMBODIMENT = "arm_01"
HOME: tuple[float, float, float] = (0.0, 0.0, 0.4)


@dataclass(frozen=True, slots=True)
class WorldConfig:
    mode: Mode = "streaming"
    heartbeat_interval_ms: int = 5000
    reconnect_window_ms: int = 30000
    watchdog_ms: int = 2000
    max_basis_age_ms: int = 500
    max_abort_ms: int = 1500
    max_duration_ms: int = 10000
    tick_ms: int = 20
    proprio_hz: float = 100.0
    state_hz: float = 10.0
    telemetry_interval_ms: int = 1000
    progress_interval_ms: int = 200
    aabb_m: tuple[tuple[float, float, float], tuple[float, float, float]] = (
        (-0.5, -0.5, 0.0),
        (0.5, 0.5, 0.8),
    )
    max_velocity_mps: float = 0.5
    max_action_rate_hz: float = 20.0
    accel_mps2: float = 2.0
    extensions: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.watchdog_ms > self.reconnect_window_ms:
            raise ValueError("watchdog_ms must be ≤ reconnect_window_ms (AWP-SAF-003)")
        if self.heartbeat_interval_ms < 100:
            raise ValueError("heartbeat_interval_ms must be ≥ 100")

    @property
    def envelope(self) -> dict[str, Any]:
        return {
            "embodiment": EMBODIMENT,
            "spatial": {"frame": "base", "aabb_m": [list(self.aabb_m[0]), list(self.aabb_m[1])]},
            "max_velocity_mps": self.max_velocity_mps,
            "max_action_rate_hz": self.max_action_rate_hz,
            "enforcement": "command_check",
            "on_violation": "reject",
        }

    def manifest(self) -> dict[str, Any]:
        lockstep = self.mode == "lockstep"
        safety: dict[str, Any] = {"envelopes": [self.envelope]}
        if not lockstep:
            safety["safe_state"] = {"behavior": "safe_stop", "watchdog_ms": self.watchdog_ms}
            safety["max_basis_age_ms"] = self.max_basis_age_ms
        manifest: dict[str, Any] = {
            "protocol_version": "0.1",
            "world": {"name": "awp-sim", "version": __version__, "vendor": "hyperduality"},
            "time_models": [self.mode],
            "capabilities": {},
            "initial_states": ["home"],
            "embodiments": [
                {
                    "id": EMBODIMENT,
                    "kind": "manipulator",
                    "action_types": ["move_to_pose", "stop"],
                    "channels": ["proprio", "arm_state"],
                }
            ],
            "observation_channels": [
                {
                    "id": "proprio",
                    "modality": "proprio/json",
                    "rate_hz": None if lockstep else self.proprio_hz,
                    "loss_class": "latest-wins",
                    "stale_after_ms": max(1, round(5000 / self.proprio_hz)),
                    "schema": {"fields": ["p_m", "v_mps"], "frame": "base"},
                },
                {
                    "id": "arm_state",
                    "modality": "text/event+json",
                    "rate_hz": None if lockstep else self.state_hz,
                    "loss_class": "reliable",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "phase": {"enum": ["idle", "moving", "stopping"]},
                            "target_m": {"type": ["array", "null"]},
                            "action_id": {"type": ["string", "null"]},
                        },
                        "required": ["phase", "target_m", "action_id"],
                    },
                },
            ],
            "action_schemas": [
                {
                    "type": "move_to_pose",
                    "params_schema": {"$ref": "#/$defs/move_to_pose_params"},
                    "duration": "extended",
                    "preemption": ["replace", "queue", "reject"],
                    "concurrency_group": "arm_motion",
                    "max_queue": 4,
                    "max_abort_ms": self.max_abort_ms,
                    "max_duration_ms": self.max_duration_ms,
                    "description": "Move the end effector in a straight line to a position.",
                },
                {
                    "type": "stop",
                    "params_schema": {"type": "object", "additionalProperties": False},
                    "duration": "instant",
                    "preemption": "replace",
                    "concurrency_group": "arm_motion",
                    "description": "Decelerate to rest; replaces any motion.",
                },
            ],
            "safety_policy": safety,
            "$defs": {
                "move_to_pose_params": {
                    "type": "object",
                    "properties": {
                        "pose": {
                            "$ref": "https://agentworldprotocol.com/schemas/v0.1/common.schema.json#/$defs/pose"
                        },
                        "max_velocity_mps": {"type": "number", "exclusiveMinimum": 0},
                    },
                    "required": ["pose"],
                    "additionalProperties": False,
                }
            },
        }
        if lockstep:
            manifest["tick_policy"] = "on_tick"
            manifest["tick_authority"] = "any_session"
        if self.extensions:
            manifest["extensions"] = self.extensions
        return manifest


FRAME_TREE: dict[str, Any] = {
    "frames": [
        {"id": "world", "parent": None},
        {"id": "base", "parent": "world", "transform": {"p_m": [0, 0, 0], "q": [0, 0, 0, 1]}},
    ]
}
