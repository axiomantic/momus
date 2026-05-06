"""Parametrized adversarial corpus runner."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

CASES_DIR = Path(__file__).parent / "cases"


def _list_cases() -> list[Path]:
    if not CASES_DIR.exists():
        return []
    return sorted(p for p in CASES_DIR.iterdir() if p.is_dir())


@pytest.mark.adversarial
@pytest.mark.parametrize("case_dir", _list_cases(), ids=lambda p: p.name)
def test_corpus_case(case_dir: Path, monkeypatch):
    if case_dir.name == "smoke":
        # Smoke uses a mocked pi (no LLM call). Marked so V4 can run it.
        monkeypatch.setenv("MOMUS_REDTEAM_MOCK_PI", "1")
    else:
        if not os.environ.get("LLM_API_KEY"):
            pytest.skip("LLM_API_KEY not set")
    from tests.adversarial.harness import run_fixture

    n_runs = (
        1
        if case_dir.name == "smoke"
        else int(os.getenv("MOMUS_REDTEAM_N_RUNS", "5"))
    )
    result = run_fixture(case_dir=case_dir, n_runs=n_runs)
    assert result.status in {"ran", "skipped_no_api_key", "errored"}
    if result.status == "ran":
        assert result.asr is not None
