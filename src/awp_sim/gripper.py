"""A parallel gripper: its finger opening moves toward a target width at a fixed speed."""

from __future__ import annotations

from dataclasses import dataclass

from .arm import Phase


@dataclass(slots=True)
class Gripper:
    width_m: float = 0.08
    speed_mps: float = 0.05
    max_width_m: float = 0.08
    phase: Phase = Phase.IDLE

    _start: float = 0.08
    _target: float | None = None

    @property
    def progress(self) -> float:
        if self._target is None:
            return 0.0
        length = abs(self._target - self._start)
        return 1.0 if length == 0 else min(1.0, abs(self.width_m - self._start) / length)

    @property
    def at_rest(self) -> bool:
        return self.phase is Phase.IDLE

    def move_to(self, width_m: float) -> None:
        self._start = self.width_m
        self._target = width_m
        self.phase = Phase.MOVING if width_m != self.width_m else Phase.IDLE

    def stop(self) -> None:
        """The fingers stop where they are."""
        self.phase = Phase.IDLE

    def halt(self) -> None:
        self.phase = Phase.IDLE

    def reset(self) -> None:
        """Fully open, at rest."""
        self.halt()
        self.width_m = self._start = self.max_width_m
        self._target = None

    def step(self, dt_s: float) -> None:
        if self.phase is not Phase.MOVING or self._target is None or dt_s <= 0:
            return
        gap = self._target - self.width_m
        move = self.speed_mps * dt_s
        if abs(gap) <= move:
            self.width_m = self._target
            self.phase = Phase.IDLE
        else:
            self.width_m += move if gap > 0 else -move
