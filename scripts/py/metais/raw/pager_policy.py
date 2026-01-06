from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from metais.common.json_utils import load_json_file


@dataclass(frozen=True, slots=True)
class PagerPolicy:
    initial_limit: int = 2000
    min_limit: int = 1
    max_limit: int = 50_000

    # target-controller knobs
    target_seconds: float = 35.0
    tolerance_seconds: float = 0.0   # 0 = always adjust
    gain: float = 1.0                # 1.0 = full correction, 0.5 = sqrt correction

    # clamp per-step multiplicative change
    min_step_factor: float = 0.5     # smallest multiplier per update
    max_step_factor: float = 2.0     # largest multiplier per update

    timeout_factor: float = 0.5
    quantize_step: int = 1


def load_pager_policy(path: Path) -> PagerPolicy:
    path = Path(path)
    if not path.exists():
        return PagerPolicy()

    j = load_json_file(path)
    if not isinstance(j, dict):
        return PagerPolicy()

    def geti(k: str, default: int) -> int:
        v = j.get(k)
        return int(v) if isinstance(v, int) else default

    def getf(k: str, default: float) -> float:
        v = j.get(k)
        return float(v) if isinstance(v, (int, float)) else default

    pol = PagerPolicy(
        initial_limit=geti("initial_limit", PagerPolicy.initial_limit),
        min_limit=geti("min_limit", PagerPolicy.min_limit),
        max_limit=geti("max_limit", PagerPolicy.max_limit),

        target_seconds=getf("target_seconds", PagerPolicy.target_seconds),
        tolerance_seconds=getf("tolerance_seconds", PagerPolicy.tolerance_seconds),
        gain=getf("gain", PagerPolicy.gain),

        min_step_factor=getf("min_step_factor", PagerPolicy.min_step_factor),
        max_step_factor=getf("max_step_factor", PagerPolicy.max_step_factor),

        timeout_factor=getf("timeout_factor", PagerPolicy.timeout_factor),
        quantize_step=max(1, geti("quantize_step", PagerPolicy.quantize_step)),
    )

    # sanity
    min_l = max(1, pol.min_limit)
    max_l = max(min_l, pol.max_limit)
    init_l = max(min_l, min(max_l, pol.initial_limit))

    target = max(0.001, pol.target_seconds)
    tol = max(0.0, pol.tolerance_seconds)

    gain = pol.gain
    if not (0.0 < gain <= 1.0):
        gain = 1.0

    lo = pol.min_step_factor
    hi = pol.max_step_factor
    # require 0 < lo <= 1 <= hi
    lo = min(1.0, max(0.001, lo))
    hi = max(1.0, hi)

    timeout = min(1.0, max(0.001, pol.timeout_factor))

    return PagerPolicy(
        initial_limit=init_l,
        min_limit=min_l,
        max_limit=max_l,
        target_seconds=target,
        tolerance_seconds=tol,
        gain=gain,
        min_step_factor=lo,
        max_step_factor=hi,
        timeout_factor=timeout,
        quantize_step=pol.quantize_step,
    )