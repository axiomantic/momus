"""Unit tests for the heuristic progress tracker."""

from __future__ import annotations

import pytest

from momus.progress import (
    ProgressThrottle,
    ProgressTracker,
    estimate_phase_caps,
)


def test_three_phase_weights_renormalize_to_one() -> None:
    t = ProgressTracker(
        phases_to_run=["phase1", "phase2", "phase3"],
        caps={"phase1": 5, "phase2": 20, "phase3": 5},
    )
    assert sum(t.weight.values()) == pytest.approx(1.0)
    # Phase 2 should dominate.
    assert t.weight["phase2"] > t.weight["phase3"] > t.weight["phase1"]


def test_skipping_phase1_renormalizes_remaining_phases() -> None:
    t = ProgressTracker(
        phases_to_run=["phase2", "phase3"],
        caps={"phase2": 20, "phase3": 5},
    )
    assert sum(t.weight.values()) == pytest.approx(1.0)
    # Phase 2 alone should now occupy ~75/95 of total.
    assert t.weight["phase2"] == pytest.approx(0.75 / 0.95, rel=1e-6)
    assert t.weight["phase3"] == pytest.approx(0.20 / 0.95, rel=1e-6)


def test_fraction_zero_before_anything_starts() -> None:
    t = ProgressTracker(
        phases_to_run=["phase2"],
        caps={"phase2": 10},
    )
    assert t.fraction() == 0.0
    assert t.percent() == 0


def test_active_phase_capped_below_100() -> None:
    t = ProgressTracker(
        phases_to_run=["phase2"],
        caps={"phase2": 10},
    )
    t.start("phase2")
    for _ in range(50):  # way more ticks than the cap
        t.tick()
    # Active phase must NEVER reach 100% — cap is 0.95 within-phase.
    assert t.fraction() == pytest.approx(0.95)
    assert t.percent() == 95


def test_finishing_phase_jumps_to_phase_end_boundary() -> None:
    t = ProgressTracker(
        phases_to_run=["phase2", "phase3"],
        caps={"phase2": 10, "phase3": 5},
    )
    t.start("phase2")
    t.finish("phase2")
    # Phase 2 done means we're at the start of phase 3 (~79%).
    assert t.fraction() == pytest.approx(0.75 / 0.95)
    assert t.active is None


def test_full_pipeline_reaches_100_percent_only_after_all_phases_finish() -> None:
    t = ProgressTracker(
        phases_to_run=["phase1", "phase2", "phase3"],
        caps={"phase1": 2, "phase2": 10, "phase3": 4},
    )
    for phase in ("phase1", "phase2", "phase3"):
        t.start(phase)
        t.finish(phase)
    assert t.percent() == 100


def test_within_phase_progress_advances_total() -> None:
    t = ProgressTracker(
        phases_to_run=["phase2"],
        caps={"phase2": 10},
    )
    t.start("phase2")
    pct_before = t.percent()
    t.tick()
    t.tick()
    pct_after = t.percent()
    assert pct_after > pct_before


def test_bar_rendering_at_known_fractions() -> None:
    t = ProgressTracker(
        phases_to_run=["phase2"],
        caps={"phase2": 100},
    )
    # 0% → all empty
    bar0 = t.bar(width=10)
    assert bar0 == "░" * 10

    # 50% → half filled. Use finish on a half-cap to land exactly there.
    t2 = ProgressTracker(
        phases_to_run=["phase2", "phase3"],
        caps={"phase2": 1, "phase3": 1},
    )
    t2.start("phase2")
    t2.finish("phase2")
    # phase 2 = 75/95 ≈ 78.9% — not exactly 50%. Pick fixed weights for the
    # 50% test instead by going via fraction directly.
    bar = t2.bar(width=10)
    # phase 2 done means fraction ≈ 0.789, so 8 filled cells.
    assert bar.count("█") == 8
    assert bar.count("░") == 2

    # 100%
    t3 = ProgressTracker(
        phases_to_run=["phase2"],
        caps={"phase2": 1},
    )
    t3.start("phase2")
    t3.finish("phase2")
    assert t3.bar(width=10) == "█" * 10


def test_estimate_phase_caps_floors_protect_tiny_prs() -> None:
    caps = estimate_phase_caps(
        n_prior_threads=0,
        n_touched_files=1,
        n_findings_estimate=0,
    )
    # Floors must apply — never 0 or 1.
    assert caps["phase1"] >= 2
    assert caps["phase2"] >= 8
    assert caps["phase3"] >= 4


def test_estimate_phase_caps_scales_with_pr_size() -> None:
    small = estimate_phase_caps(
        n_prior_threads=0, n_touched_files=2, n_findings_estimate=2
    )
    big = estimate_phase_caps(
        n_prior_threads=0, n_touched_files=20, n_findings_estimate=10
    )
    assert big["phase2"] > small["phase2"]
    assert big["phase3"] > small["phase3"]


def test_throttle_first_post_always_allowed() -> None:
    th = ProgressThrottle(min_seconds=15.0, min_pct_delta=2)
    assert th.should_post(now_monotonic=0.0, pct=5) is True


def test_throttle_blocks_until_both_conditions_met() -> None:
    th = ProgressThrottle(min_seconds=15.0, min_pct_delta=2)
    th.should_post(now_monotonic=0.0, pct=5)
    # Same instant, +1 pct: blocked.
    assert th.should_post(now_monotonic=0.5, pct=6) is False
    # 20s later but only +1 pct: blocked.
    assert th.should_post(now_monotonic=20.0, pct=6) is False
    # 5s later but +5 pct: blocked (time gate not met).
    assert th.should_post(now_monotonic=5.0, pct=10) is False
    # 20s later AND +5 pct: allowed.
    assert th.should_post(now_monotonic=20.0, pct=10) is True


def test_throttle_force_always_allowed() -> None:
    th = ProgressThrottle(min_seconds=15.0, min_pct_delta=2)
    th.should_post(now_monotonic=0.0, pct=5)
    # Even right after first post, force=True wins.
    assert th.should_post(now_monotonic=0.1, pct=5, force=True) is True


def test_unknown_phase_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        ProgressTracker(phases_to_run=["phase_does_not_exist"], caps={})


def test_empty_phases_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        ProgressTracker(phases_to_run=[], caps={})
