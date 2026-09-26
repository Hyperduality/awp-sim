"""Reference world configuration and the manifest it declares."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from . import __version__

Mode = Literal["lockstep", "streaming"]

ARM = "arm_01"
GRIPPER = "gripper_01"
MULTI_BIND_GROUP = "cell"
HOME: tuple[float, float, float] = (0.0, 0.0, 0.4)
PARK: tuple[float, float, float] = (0.0, 0.0, 0.25)
SERVO_CHANNEL = "servo_arm"
ARBITRATION = (
    "first-come: while one session's gripper_move is pending or executing, "
    "the others' are refused with AWP_BUSY"
)

# Beyond Core, each off by default: task (AWP-TSK), approval of `park` (AWP-APR), blend preemption
# (AWP-PRE-004), embodiment transfer (AWP-EMB-003), sim-profile seeding, snapshots, and replay
# (AWP-REP, lockstep), a servo command channel (AWP-CMD, streaming), and a shared gripper, bound
# alone or with the arm (AWP-EMB-005, AWP-MA-003), under the barrier in lockstep.
FEATURES = frozenset({"task", "approval", "blend", "transfer", "sim", "servo", "gripper"})


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
    features: frozenset[str] = frozenset()
    approval_timeout_ms: int = 60000
    servo_watchdog_ms: int = 200
    servo_hz: float = 200.0
    gripper_speed_mps: float = 0.05
    gripper_max_width_m: float = 0.08
    extensions: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.watchdog_ms > self.reconnect_window_ms:
            raise ValueError("watchdog_ms must be ≤ reconnect_window_ms (AWP-SAF-003)")
        if self.heartbeat_interval_ms < 100:
            raise ValueError("heartbeat_interval_ms must be ≥ 100")
        unknown = set(self.features) - FEATURES
        if unknown:
            raise ValueError(f"unknown features {sorted(unknown)}; known: {sorted(FEATURES)}")
        if "servo" in self.features and self.mode != "streaming":
            raise ValueError("command channels are streaming-only (AWP-CMD-001)")
        if "sim" in self.features and self.mode != "lockstep":
            raise ValueError("the sim feature needs lockstep: its determinism is lockstep's")

    @property
    def envelope(self) -> dict[str, Any]:
        return {
            "embodiment": ARM,
            "spatial": {"frame": "base", "aabb_m": [list(self.aabb_m[0]), list(self.aabb_m[1])]},
            "max_velocity_mps": self.max_velocity_mps,
            "max_action_rate_hz": self.max_action_rate_hz,
            "enforcement": "command_check",
            "on_violation": "reject",
        }

    def has(self, feature: str) -> bool:
        return feature in self.features

    def manifest(self) -> dict[str, Any]:
        lockstep = self.mode == "lockstep"
        safety: dict[str, Any] = {"envelopes": [self.envelope]}
        if self.has("approval"):
            safety["approval_timeout_ms"] = self.approval_timeout_ms
            safety["standing_approvals"] = True
        capabilities: dict[str, Any] = {}
        if self.has("task"):
            capabilities["task"] = True
        if self.has("sim"):
            capabilities.update(seed=True, snapshot=True, replay=True)
        if self.has("servo"):
            capabilities["command_channels"] = True
        action_types = ["move_to_pose", "stop"]
        if self.has("approval"):
            action_types.append("park")
        if self.has("servo"):
            action_types.append("servo")
        channels = ["proprio", "arm_state"] + ([SERVO_CHANNEL] if self.has("servo") else [])
        move_policies = ["replace", "queue", "reject"] + (["blend"] if self.has("blend") else [])
        if not lockstep:
            safety["safe_state"] = {"behavior": "safe_stop", "watchdog_ms": self.watchdog_ms}
            safety["max_basis_age_ms"] = self.max_basis_age_ms
        manifest: dict[str, Any] = {
            "protocol_version": "0.1",
            "world": {"name": "awp-sim", "version": __version__, "vendor": "hyperduality"},
            "time_models": [self.mode],
            "capabilities": capabilities,
            "initial_states": ["home"],
            "embodiments": [
                {
                    "id": ARM,
                    "kind": "manipulator",
                    "action_types": action_types,
                    "channels": channels,
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
                            "phase": {"enum": ["idle", "moving", "stopping", "servo"]},
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
                    "preemption": move_policies,
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
        if self.has("approval"):
            manifest["action_schemas"].append(
                {
                    "type": "park",
                    "params_schema": {"type": "object", "additionalProperties": False},
                    "duration": "extended",
                    "preemption": "queue",
                    "concurrency_group": "arm_motion",
                    "max_queue": 4,
                    "requires_approval": True,
                    "max_abort_ms": self.max_abort_ms,
                    "max_duration_ms": self.max_duration_ms,
                    "description": "Move to the park pose; needs an approver's decision.",
                }
            )
        if self.has("servo"):
            manifest["command_channels"] = [
                {
                    "id": SERVO_CHANNEL,
                    "modality": "servo/json",
                    "rate_hz": self.servo_hz,
                    "loss_class": "latest-wins",
                    "schema": {"fields": ["v_mps"], "frame": "base"},
                }
            ]
            manifest["action_schemas"].append(
                {
                    "type": "servo",
                    "params_schema": {"type": "object", "additionalProperties": False},
                    "duration": "streaming",
                    "command_channel": SERVO_CHANNEL,
                    "watchdog_ms": self.servo_watchdog_ms,
                    "preemption": ["replace", "reject"],
                    "concurrency_group": "arm_motion",
                    "max_abort_ms": self.max_abort_ms,
                    "description": "Follow end-effector velocity setpoints on servo_arm.",
                }
            )
        if self.has("gripper"):
            manifest["embodiments"][0]["multi_bind_group"] = MULTI_BIND_GROUP
            manifest["embodiments"].append(
                {
                    "id": GRIPPER,
                    "kind": "gripper",
                    "shared_control": True,
                    "arbitration": ARBITRATION,
                    "multi_bind_group": MULTI_BIND_GROUP,
                    "action_types": ["gripper_move"],
                    "channels": ["gripper_state"],
                }
            )
            manifest["observation_channels"].append(
                {
                    "id": "gripper_state",
                    "modality": "text/event+json",
                    "rate_hz": None if lockstep else self.state_hz,
                    "loss_class": "reliable",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "phase": {"enum": ["idle", "moving"]},
                            "width_m": {"type": "number"},
                            "action_id": {"type": ["string", "null"]},
                        },
                        "required": ["phase", "width_m", "action_id"],
                    },
                }
            )
            manifest["action_schemas"].append(
                {
                    "type": "gripper_move",
                    "params_schema": {
                        "type": "object",
                        "properties": {
                            "width_m": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": self.gripper_max_width_m,
                            }
                        },
                        "required": ["width_m"],
                        "additionalProperties": False,
                    },
                    "duration": "extended",
                    "preemption": ["replace", "queue", "reject"],
                    "concurrency_group": "gripper",
                    "max_queue": 4,
                    "max_abort_ms": self.max_abort_ms,
                    "max_duration_ms": self.max_duration_ms,
                    "description": "Open or close the fingers to a width.",
                }
            )
        if lockstep:
            manifest["tick_policy"] = "on_tick"
            # Several lockstep sessions can be bound at once with the gripper (AWP-MA-005).
            manifest["tick_authority"] = "barrier" if self.has("gripper") else "any_session"
        if self.extensions:
            manifest["extensions"] = self.extensions
        return manifest


FRAME_TREE: dict[str, Any] = {
    "frames": [
        {"id": "world", "parent": None},
        {"id": "base", "parent": "world", "transform": {"p_m": [0, 0, 0], "q": [0, 0, 0, 1]}},
    ]
}
