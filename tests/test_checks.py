"""Unit tests for the optional Check Run publisher."""

from __future__ import annotations

import json
import subprocess

import tripwire

from momus import checks as checks_mod
from momus.checks import (
    _build_summary,
    _build_title,
    _verdict_to_conclusion,
    post_check_run,
)
from momus.config import ChecksConfig


# --- conclusion mapping ------------------------------------------------------


def test_conclusion_approve_no_blockers_is_success() -> None:
    assert _verdict_to_conclusion("APPROVE", []) == "success"


def test_conclusion_request_changes_is_failure_even_without_blockers() -> None:
    # The model said REQUEST_CHANGES; honor it as a hard fail.
    assert _verdict_to_conclusion("REQUEST_CHANGES", []) == "failure"


def test_conclusion_comment_no_blockers_is_neutral() -> None:
    assert _verdict_to_conclusion("COMMENT", []) == "neutral"


def test_conclusion_any_blocking_finding_forces_failure() -> None:
    blockers = [{"severity": "critical"}]
    # Even when verdict is APPROVE, a blocking finding overrides.
    assert _verdict_to_conclusion("APPROVE", blockers) == "failure"
    assert _verdict_to_conclusion("COMMENT", blockers) == "failure"
    assert _verdict_to_conclusion("REQUEST_CHANGES", blockers) == "failure"


# --- title rendering --------------------------------------------------------


def test_title_no_findings() -> None:
    assert _build_title("APPROVE", 0, 0) == "No findings"


def test_title_non_blocking_only() -> None:
    assert _build_title("COMMENT", 3, 0) == "3 non-blocking findings"
    assert _build_title("COMMENT", 1, 0) == "1 non-blocking finding"


def test_title_blocking_dominates() -> None:
    assert _build_title("REQUEST_CHANGES", 5, 2) == "2 blocking findings"
    assert _build_title("REQUEST_CHANGES", 5, 1) == "1 blocking finding"


# --- summary rendering ------------------------------------------------------


def test_summary_includes_doc_summary_and_tally() -> None:
    doc = {
        "summary": "Adds latency thresholds.",
        "tally": {"critical": 0, "high": 1, "medium": 2, "low": 0, "nit": 0},
        "findings": [],
    }
    out = _build_summary(doc, [])
    assert "Adds latency thresholds." in out
    assert "1 High" in out
    assert "2 Mediums" in out


def test_summary_lists_blocking_findings_with_locations() -> None:
    blockers = [
        {
            "id": "BOT-A1",
            "file": "src/auth.py",
            "line": 42,
            "severity": "high",
            "title": "Missing nonce validation",
        }
    ]
    out = _build_summary({"summary": "", "findings": blockers}, blockers)
    assert "**Blocking findings:**" in out
    assert "BOT-A1" in out
    assert "src/auth.py:42" in out
    assert "Missing nonce validation" in out


def test_summary_falls_back_when_empty() -> None:
    out = _build_summary({"summary": "", "findings": []}, [])
    assert out == "(no summary)"


# --- post_check_run end-to-end (mocked subprocess) --------------------------


def _gh_proc(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=""
    )


def _captured_run_factory():
    captured: list[tuple[tuple, dict]] = []
    queue: list[subprocess.CompletedProcess] = []

    def side_effect(*args, **kwargs):
        captured.append((args, kwargs))
        if not queue:
            raise AssertionError("subprocess.run called more than queued")
        return queue.pop(0)

    return side_effect, captured, queue


def test_post_check_run_disabled_makes_no_api_call() -> None:
    """When checks.enabled is false, the function returns without any
    subprocess call. Tripwire would catch unauthorized calls anyway."""
    cfg = ChecksConfig(enabled=False, name="Momus")
    # No mock needed — there must be NO subprocess call.
    post_check_run(
        owner="o",
        repo="r",
        head_sha="abc",
        findings_doc={"verdict": "APPROVE", "findings": []},
        blocking_severities=["critical", "high"],
        config=cfg,
        run_url="",
    )


def test_post_check_run_enabled_posts_with_correct_payload_shape() -> None:
    cfg = ChecksConfig(enabled=True, name="Momus")
    findings_doc = {
        "verdict": "REQUEST_CHANGES",
        "summary": "Has issues.",
        "tally": {"critical": 0, "high": 1, "medium": 0, "low": 0, "nit": 0},
        "findings": [
            {
                "id": "BOT-A1",
                "file": "x.py",
                "line": 10,
                "severity": "high",
                "title": "bug",
            }
        ],
    }
    side_effect, captured, queue = _captured_run_factory()
    queue.append(_gh_proc(stdout="{}"))

    run_mock = tripwire.mock.object(checks_mod.subprocess, "run")
    run_mock.calls(side_effect)

    with tripwire:
        post_check_run(
            owner="o",
            repo="r",
            head_sha="deadbeef",
            findings_doc=findings_doc,
            blocking_severities=["critical", "high"],
            config=cfg,
            run_url="https://x/run/1",
        )

    assert len(captured) == 1
    for args, kwargs in captured:
        run_mock.assert_call(args=args, kwargs=kwargs)

    argv = captured[0][0][0]
    assert "POST" in argv
    assert "/repos/o/r/check-runs" in " ".join(argv)

    payload = json.loads(captured[0][1]["input"])
    assert payload["name"] == "Momus"
    assert payload["head_sha"] == "deadbeef"
    assert payload["status"] == "completed"
    # blocking finding present → failure conclusion.
    assert payload["conclusion"] == "failure"
    assert payload["details_url"] == "https://x/run/1"
    assert "1 blocking finding" in payload["output"]["title"]
    assert "BOT-A1" in payload["output"]["summary"]


def test_post_check_run_swallows_failures() -> None:
    """A failing gh call MUST NOT raise — Check Run is best-effort."""
    cfg = ChecksConfig(enabled=True, name="Momus")
    captured: list[tuple[tuple, dict]] = []

    def side_effect(*args, **kwargs):
        captured.append((args, kwargs))
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr="HTTP 403"
        )

    run_mock = tripwire.mock.object(checks_mod.subprocess, "run")
    run_mock.calls(side_effect)

    with tripwire:
        post_check_run(
            owner="o",
            repo="r",
            head_sha="abc",
            findings_doc={"verdict": "APPROVE", "findings": []},
            blocking_severities=[],
            config=cfg,
            run_url="",
        )

    assert len(captured) == 1
    for args, kwargs in captured:
        run_mock.assert_call(args=args, kwargs=kwargs)
