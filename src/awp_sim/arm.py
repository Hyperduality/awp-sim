"""A kinematic end effector: straight-line moves with bounded speed and acceleration.

The model is deliberately trivial. It exists to make timing real (moves take time, aborts
decelerate) so the protocol's execution, interruption, and freshness semantics are exercised.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

Vec3 = tuple[float, float, float]


class Phase(StrEnum):
    IDLE = "idle"
    MOVING = "moving"
    STOPPING = "stopping"
    SERVO = "servo"  # following velocity setpoints from a command channel


@dataclass(slots=True)
class Arm:
    position: Vec3 = (0.0, 0.0, 0.4)
    accel_mps2: float = 2.0
    phase: Phase = Phase.IDLE
    speed: float = 0.0
    stuck: bool = False  # fault injection: aborts never finish

    _start: Vec3 = (0.0, 0.0, 0.0)
    _target: Vec3 | None = None
    _length: float = 0.0
    _travelled: float = 0.0
    _v_max: float = 0.0
    _servo_target: Vec3 = (0.0, 0.0, 0.0)
    _servo_velocity: Vec3 = (0.0, 0.0, 0.0)
    bounds: tuple[Vec3, Vec3] | None = None

    @property
    def target(self) -> Vec3 | None:
        return self._target

    @property
    def progress(self) -> float:
        if self._length == 0:
            return 1.0 if self._target is not None else 0.0
        return min(1.0, self._travelled / self._length)

    @property
    def at_rest(self) -> bool:
        return self.speed == 0.0 and self.phase is not Phase.MOVING

    @property
    def velocity(self) -> Vec3:
        if self.phase is Phase.SERVO:
            return self._servo_velocity
        if self._target is None or self._length == 0:
            return (0.0, 0.0, 0.0)
        d = _direction(self._start, self._target, self._length)
        return (d[0] * self.speed, d[1] * self.speed, d[2] * self.speed)

    def move_to(self, target: Vec3, v_max: float) -> None:
        self._start = self.position
        self._target = target
        self._length = math.dist(self.position, target)
        self._travelled = 0.0
        self._v_max = v_max
        self.phase = Phase.MOVING if self._length > 0 else Phase.IDLE

    def servo(self) -> None:
        """Follow velocity setpoints (`command`) until stopped."""
        self._servo_velocity = self.velocity if self.phase is not Phase.IDLE else (0.0, 0.0, 0.0)
        self._servo_target = (0.0, 0.0, 0.0)
        self.speed = math.hypot(*self._servo_velocity)
        self._target = None
        self.phase = Phase.SERVO

    def command(self, v: Vec3) -> None:
        self._servo_target = v

    def stop(self) -> None:
        """Decelerate to rest along the current path."""
        if self.phase is Phase.SERVO:
            v = self._servo_velocity
            speed = math.hypot(*v)
            self._start = self.position
            if speed == 0:
                self.phase = Phase.IDLE
                return
            ahead = (v[0] / speed, v[1] / speed, v[2] / speed)
            self._length = self._room(ahead)
            self._target = tuple(
                p + d * self._length for p, d in zip(self.position, ahead, strict=True)
            )  # type: ignore[assignment]
            self._travelled = 0.0
            self.speed = speed
            self.phase = Phase.STOPPING
            return
        if self.phase is Phase.MOVING or self.speed > 0:
            self.phase = Phase.STOPPING

    def halt(self) -> None:
        """Stop instantly (e-stop, reset, or a stop that no simulated time will follow)."""
        self.speed = 0.0
        self.phase = Phase.IDLE

    def reset(self, position: Vec3) -> None:
        self.halt()
        self.position = position
        self._target = None
        self._length = self._travelled = 0.0

    def step(self, dt_s: float) -> None:
        if dt_s <= 0:
            return
        if self.phase is Phase.SERVO:
            self._step_servo(dt_s)
            return
        if self._target is None:
            return
        if self.phase is Phase.MOVING:
            remaining = self._length - self._travelled
            allowed = min(self._v_max, math.sqrt(2 * self.accel_mps2 * max(remaining, 0.0)))
            self.speed = min(self.speed + self.accel_mps2 * dt_s, allowed)
            self._advance(min(self.speed * dt_s, remaining))
            if self._length - self._travelled <= 1e-9:
                self.speed = 0.0
                self.phase = Phase.IDLE
        elif self.phase is Phase.STOPPING and not self.stuck:
            new_speed = max(0.0, self.speed - self.accel_mps2 * dt_s)
            self._advance(min((self.speed + new_speed) / 2 * dt_s, self._length - self._travelled))
            self.speed = new_speed if self._length - self._travelled > 1e-9 else 0.0
            if self.speed == 0.0:
                self.phase = Phase.IDLE

    def _step_servo(self, dt_s: float) -> None:
        v, goal = self._servo_velocity, self._servo_target
        dv = tuple(g - c for g, c in zip(goal, v, strict=True))
        mag = math.hypot(*dv)
        limit = self.accel_mps2 * dt_s
        if mag > limit:
            dv = tuple(d * limit / mag for d in dv)
        v = (v[0] + dv[0], v[1] + dv[1], v[2] + dv[2])
        p = tuple(pos + vel * dt_s for pos, vel in zip(self.position, v, strict=True))
        if self.bounds is not None:
            lo, hi = self.bounds
            clamped = tuple(min(max(x, a), b) for x, a, b in zip(p, lo, hi, strict=True))
            v = tuple(0.0 if c != x else vel for c, x, vel in zip(clamped, p, v, strict=True))  # type: ignore[assignment]
            p = clamped
        self.position = (p[0], p[1], p[2])
        self._servo_velocity = (v[0], v[1], v[2])
        self.speed = math.hypot(*v)

    def _room(self, d: Vec3) -> float:
        """How far the arm can travel along the unit vector `d` and stay within `bounds`."""
        if self.bounds is None:
            return 1.0
        room = 1.0
        for p, di, lo, hi in zip(self.position, d, *self.bounds, strict=True):
            if di > 0:
                room = min(room, (hi - p) / di)
            elif di < 0:
                room = min(room, (lo - p) / di)
        return max(room, 0.0)

    def _advance(self, distance: float) -> None:
        self._travelled += distance
        d = _direction(self._start, self._target or self._start, self._length)
        p = self.position
        self.position = (p[0] + d[0] * distance, p[1] + d[1] * distance, p[2] + d[2] * distance)


def _direction(a: Vec3, b: Vec3, length: float) -> Vec3:
    if length == 0:
        return (0.0, 0.0, 0.0)
    return ((b[0] - a[0]) / length, (b[1] - a[1]) / length, (b[2] - a[2]) / length)
