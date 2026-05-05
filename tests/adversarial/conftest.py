"""Conftest for the adversarial corpus harness.

Emits ``tests/adversarial/.last-run.json`` after the adversarial test
session, summarising per-fixture status and ASR. The file is git-ignored
(see ``.gitignore``) and serves as the V4 verification artifact source.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

_LAST_RUN_PATH = Path(__file__).parent / ".last-run.json"

# Per-fixture entries collected during the session. Each is the dict
# produced by ``FixtureResult.to_summary_dict``. Populated by harness
# code via ``record_fixture_result``.
_FIXTURE_RESULTS: list[dict[str, Any]] = []


def record_fixture_result(entry: dict[str, Any]) -> None:
    """Append a fixture summary dict for inclusion in ``.last-run.json``.

    Called from harness ``run_fixture`` after each fixture completes.
    """
    _FIXTURE_RESULTS.append(entry)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Write ``.last-run.json`` if any adversarial fixtures ran this session."""
    if not _FIXTURE_RESULTS:
        return
    summary = {
        "n_fixtures": len(_FIXTURE_RESULTS),
        "n_ran": sum(1 for f in _FIXTURE_RESULTS if f.get("status") == "ran"),
        "n_skipped": sum(
            1 for f in _FIXTURE_RESULTS if f.get("status") == "skipped_no_api_key"
        ),
        "n_errored": sum(
            1 for f in _FIXTURE_RESULTS if f.get("status") == "errored"
        ),
    }
    payload = {"fixtures": _FIXTURE_RESULTS, "summary": summary}
    _LAST_RUN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
