"""
Heuristic progress tracking for the multi-phase review pipeline.

The bot has three phases with very different wall-clock costs:
phase 1 (classify priors), phase 2 (review), phase 3 (verify). We can't
know the true completion percentage in advance, but we can interpolate
using:

  1. Coarse phase weights derived from typical observed durations.
  2. Within-phase ticks counted from pi's `tool_execution_end` events,
     against an estimated cap derived from PR shape (touched files,
     prior thread count, finding count).

Within-phase fraction is capped at 0.95 while the phase is active so
the bar never claims 100% before the phase actually exits.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Typical fraction of total wall-clock spent in each phase. Calibrated
# against observed runs (phase 2 dominates at ~75-80%, phase 3 a steady
# 15-20%, phase 1 very small when there are priors). When a phase is
# skipped, weights are renormalized over the phases that DO run.
TYPICAL_WEIGHTS: dict[str, float] = {
    "phase1": 0.05,
    "phase2": 0.75,
    "phase3": 0.20,
}


def estimate_phase_caps(
    *,
    n_prior_threads: int,
    n_touched_files: int,
    n_findings_estimate: int = 5,
) -> dict[str, int]:
    """
    Estimate the number of ``tool_execution_end`` events each phase will
    emit. Used as the denominator for within-phase progress.

    Conservative floors prevent tiny PRs from racing to 100% on the
    first tool call.
    """
    return {
        "phase1": max(2, n_prior_threads),
        # 2x per file accounts for re-reads + a handful of greps/finds +
        # the final write_output call.
        "phase2": max(8, n_touched_files * 2 + 5),
        # Phase 3 audits each finding plus a few exploratory reads.
        "phase3": max(4, n_findings_estimate + 3),
    }


@dataclass
class ProgressTracker:
    phases_to_run: list[str]
    caps: dict[str, int]
    weight: dict[str, float] = field(default_factory=dict)
    base: dict[str, float] = field(default_factory=dict)
    ticks: dict[str, int] = field(default_factory=dict)
    active: str | None = None

    def __post_init__(self) -> None:
        if not self.phases_to_run:
            raise ValueError("phases_to_run must be non-empty")
        for p in self.phases_to_run:
            if p not in TYPICAL_WEIGHTS:
                raise ValueError(f"unknown phase: {p}")
        total = sum(TYPICAL_WEIGHTS[p] for p in self.phases_to_run)
        acc = 0.0
        for p in self.phases_to_run:
            self.weight[p] = TYPICAL_WEIGHTS[p] / total
            self.base[p] = acc
            acc += self.weight[p]
            self.ticks[p] = 0

    def start(self, phase: str) -> None:
        if phase not in self.phases_to_run:
            raise ValueError(f"phase {phase!r} not in phases_to_run")
        self.active = phase

    def tick(self) -> None:
        if self.active is None:
            return
        self.ticks[self.active] += 1

    def finish(self, phase: str) -> None:
        """Mark a phase as complete (forces within-phase fraction to 1.0)."""
        cap = max(self.caps.get(phase, 1), 1)
        self.ticks[phase] = cap
        if self.active == phase:
            self.active = None

    def fraction(self) -> float:
        """Total completion fraction in [0.0, 1.0]."""
        total = 0.0
        for p in self.phases_to_run:
            cap = max(self.caps.get(p, 1), 1)
            ticks = self.ticks[p]
            ceiling = 0.95 if p == self.active else 1.0
            within = min(ticks / cap, ceiling)
            total += self.weight[p] * within
        return min(total, 1.0)

    def percent(self) -> int:
        return round(self.fraction() * 100)

    def bar(self, width: int = 20) -> str:
        """Render a unicode progress bar of the given character width."""
        if width <= 0:
            return ""
        filled = round(self.fraction() * width)
        filled = max(0, min(filled, width))
        return "█" * filled + "░" * (width - filled)


@dataclass
class _ThrottleState:
    last_post_monotonic: float = -1.0
    last_pct: int = -1


class ProgressThrottle:
    """
    Decide whether enough has changed to be worth reposting the status
    comment. Repost when EITHER the elapsed time exceeds ``min_seconds``
    AND the percent has advanced by ``min_pct_delta``, OR when forced.
    """

    def __init__(self, min_seconds: float = 15.0, min_pct_delta: int = 2) -> None:
        self.min_seconds = min_seconds
        self.min_pct_delta = min_pct_delta
        self._state = _ThrottleState()

    def should_post(self, now_monotonic: float, pct: int, force: bool = False) -> bool:
        if force:
            self._state.last_post_monotonic = now_monotonic
            self._state.last_pct = pct
            return True
        if self._state.last_post_monotonic < 0:
            self._state.last_post_monotonic = now_monotonic
            self._state.last_pct = pct
            return True
        elapsed = now_monotonic - self._state.last_post_monotonic
        delta = pct - self._state.last_pct
        if elapsed >= self.min_seconds and delta >= self.min_pct_delta:
            self._state.last_post_monotonic = now_monotonic
            self._state.last_pct = pct
            return True
        return False
