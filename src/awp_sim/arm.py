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

    def stop(self) -> None:
        """Decelerate to rest along the current path."""
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
        if dt_s <= 0 or self._target is None:
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
            self.speed = new_speed
            if self.speed == 0.0:
                self.phase = Phase.IDLE

    def _advance(self, distance: float) -> None:
        self._travelled += distance
        d = _direction(self._start, self._target or self._start, self._length)
        p = self.position
        self.position = (p[0] + d[0] * distance, p[1] + d[1] * distance, p[2] + d[2] * distance)


def _direction(a: Vec3, b: Vec3, length: float) -> Vec3:
    if length == 0:
        return (0.0, 0.0, 0.0)
    return ((b[0] - a[0]) / length, (b[1] - a[1]) / length, (b[2] - a[2]) / length)
