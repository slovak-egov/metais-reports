from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from metais.raw.pager_policy import PagerPolicy


@dataclass(frozen=True, slots=True)
class PagerDecision:
    old_limit: int
    new_limit: int
    reason: str              # "grow" / "shrink" / "hold" / "timeout"
    seconds: Optional[float] # None for timeout
    factor: Optional[float]  # multiplier applied (after clamp/gain), None for hold


class AdaptivePager:
    def __init__(self, initial_limit: int, policy: PagerPolicy):
        self.pol = policy
        self._limit = self._clamp(initial_limit)
        self._last: Optional[PagerDecision] = None

    def limit(self) -> int:
        return self._limit

    def last_decision(self) -> Optional[PagerDecision]:
        return self._last

    def on_timeout_like(self) -> None:
        old = self._limit
        if old <= self.pol.min_limit:
            self._last = PagerDecision(old, old, "timeout", None, None)
            return

        new_v = int(old * self.pol.timeout_factor)
        if new_v >= old:
            new_v = old - self.pol.quantize_step

        new = self._quantize(self._clamp(new_v), direction=-1)
        self._limit = new
        self._last = PagerDecision(old, new, "timeout", None, (new / old) if old else None)

    def on_success(self, seconds: float) -> None:
        old = self._limit

        # defensive: avoid divide-by-zero / nonsense
        if not (seconds > 0.0) or not math.isfinite(seconds):
            self._last = PagerDecision(old, old, "hold", seconds, None)
            return

        # deadband around the target
        if self.pol.tolerance_seconds > 0.0 and abs(seconds - self.pol.target_seconds) <= self.pol.tolerance_seconds:
            self._last = PagerDecision(old, old, "hold", seconds, 1.0)
            return

        raw = self.pol.target_seconds / seconds  # desired multiplicative correction
        # soften jumps if desired
        factor = raw ** self.pol.gain

        # clamp per-step factor
        factor = max(self.pol.min_step_factor, min(self.pol.max_step_factor, factor))

        # compute new limit
        new_v = int(old * factor)

        # ensure progress if rounding kept it the same but factor != 1
        if new_v == old:
            if factor > 1.0:
                new_v = old + self.pol.quantize_step
            elif factor < 1.0:
                new_v = old - self.pol.quantize_step

        direction = +1 if factor > 1.0 else (-1 if factor < 1.0 else 0)
        new = self._quantize(self._clamp(new_v), direction=direction)

        self._limit = new

        reason = "hold"
        if new > old:
            reason = "grow"
        elif new < old:
            reason = "shrink"

        self._last = PagerDecision(old, new, reason, seconds, (new / old) if old else None)

    def _clamp(self, v: int) -> int:
        return max(self.pol.min_limit, min(self.pol.max_limit, int(v)))

    def _quantize(self, v: int, *, direction: int) -> int:
        step = max(1, int(self.pol.quantize_step))
        if step == 1:
            return v

        if direction > 0:
            q = int(math.ceil(v / step) * step)
        elif direction < 0:
            q = int(math.floor(v / step) * step)
        else:
            q = int(round(v / step) * step)

        return self._clamp(q)