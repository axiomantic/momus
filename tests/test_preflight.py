"""Unit tests for momus.preflight, including off-hunk filtering."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from momus.preflight import preflight


def _doc(findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "verdict": "APPROVE",
        "summary": "",
        "tally": {"critical": 0, "high": 0, "medium": 0, "low": 0, "nit": 0},
        "findings": findings,
        "noteworthy": [],
        "prior_findings_status": [],
    }


def _finding(
    fid: str = "BOT-A1",
    file: str = "src/foo.py",
    line: int = 5,
    severity: str = "low",
    end_line: int | None = None,
) -> dict[str, Any]:
    f: dict[str, Any] = {
        "id": fid,
        "file": file,
        "line": line,
        "severity": severity,
        "blocking": False,
        "title": "x",
        "rationale": "y",
    }
    if end_line is not None:
        f["end_line"] = end_line
    return f


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    # 20-line file so line numbers up to 20 are valid file-existence-wise.
    (src / "foo.py").write_text("\n".join(f"line{i}" for i in range(1, 21)) + "\n")
    return tmp_path


def test_finding_file_not_in_pr_diff_is_dropped(repo: Path):
    """A finding citing a file not present in the diff hunk map is dropped
    with reason 'file not in PR diff', and the off-hunk check fires before
    the file-existence check (so the dropped reason is the diff one even
    when the file exists on disk)."""
    findings = [_finding(file="src/foo.py", line=5)]
    doc = _doc(findings)
    hunk_lines: dict[str, set[int]] = {"src/other.py": {1, 2, 3}}

    updated, actions = preflight(
        doc,
        prior_findings=[],
        repo_root=repo,
        blocking_severities=["critical", "high"],
        hunk_lines=hunk_lines,
    )

    assert updated["findings"] == []
    assert actions == [{"id": "BOT-A1", "action": "dropped", "reason": "file not in PR diff"}]


def test_finding_line_not_on_hunk_is_dropped(repo: Path):
    findings = [_finding(file="src/foo.py", line=7)]
    doc = _doc(findings)
    hunk_lines = {"src/foo.py": {1, 2, 3, 4, 5}}

    updated, actions = preflight(
        doc,
        prior_findings=[],
        repo_root=repo,
        blocking_severities=["critical", "high"],
        hunk_lines=hunk_lines,
    )

    assert updated["findings"] == []
    assert actions == [{"id": "BOT-A1", "action": "dropped", "reason": "line 7 not on a diff hunk"}]


def test_finding_in_hunk_line_survives(repo: Path):
    findings = [_finding(file="src/foo.py", line=3)]
    doc = _doc(findings)
    hunk_lines = {"src/foo.py": {1, 2, 3, 4, 5}}

    updated, actions = preflight(
        doc,
        prior_findings=[],
        repo_root=repo,
        blocking_severities=["critical", "high"],
        hunk_lines=hunk_lines,
    )

    assert updated["findings"] == findings
    assert actions == []


def test_empty_hunk_lines_skips_off_hunk_check_and_uses_file_check(repo: Path):
    """When hunk_lines is empty (no diff info), the off-hunk check is skipped:
    a finding pointing at a missing file falls through to the file-existence
    check and is dropped with the file-not-found reason, NOT 'file not in PR
    diff'."""
    findings = [_finding(file="src/missing.py", line=2)]
    doc = _doc(findings)

    updated, actions = preflight(
        doc,
        prior_findings=[],
        repo_root=repo,
        blocking_severities=["critical", "high"],
        hunk_lines={},
    )

    assert updated["findings"] == []
    assert actions == [
        {"id": "BOT-A1", "action": "dropped", "reason": "file not found: src/missing.py"}
    ]


def test_empty_hunk_lines_existing_file_in_range_survives(repo: Path):
    """With no hunk info, the off-hunk check is skipped entirely; an in-range
    finding on an existing file survives."""
    findings = [_finding(file="src/foo.py", line=10)]
    doc = _doc(findings)

    updated, actions = preflight(
        doc,
        prior_findings=[],
        repo_root=repo,
        blocking_severities=["critical", "high"],
        hunk_lines={},
    )

    assert updated["findings"] == findings
    assert actions == []


def test_end_line_straddling_hunk_boundary_drops_finding(repo: Path):
    """If any line in [line, end_line] is not in the hunk set, the finding
    is unreviewable and must be dropped."""
    findings = [_finding(file="src/foo.py", line=3, end_line=6)]
    doc = _doc(findings)
    # line 5 is missing from the hunk set
    hunk_lines = {"src/foo.py": {3, 4, 6}}

    updated, actions = preflight(
        doc,
        prior_findings=[],
        repo_root=repo,
        blocking_severities=["critical", "high"],
        hunk_lines=hunk_lines,
    )

    assert updated["findings"] == []
    assert actions == [{"id": "BOT-A1", "action": "dropped", "reason": "line 5 not on a diff hunk"}]


def test_end_line_fully_inside_hunk_survives(repo: Path):
    findings = [_finding(file="src/foo.py", line=3, end_line=5)]
    doc = _doc(findings)
    hunk_lines = {"src/foo.py": {3, 4, 5, 6}}

    updated, actions = preflight(
        doc,
        prior_findings=[],
        repo_root=repo,
        blocking_severities=["critical", "high"],
        hunk_lines=hunk_lines,
    )

    assert updated["findings"] == findings
    assert actions == []


def test_existing_malformed_finding_still_dropped(repo: Path):
    """Pre-existing structural check still fires when off-hunk check is skipped."""
    findings = [_finding(file="src/foo.py", line=0)]  # line < 1
    doc = _doc(findings)

    updated, actions = preflight(
        doc,
        prior_findings=[],
        repo_root=repo,
        blocking_severities=["critical", "high"],
        hunk_lines={},
    )

    assert updated["findings"] == []
    assert actions == [
        {"id": "BOT-A1", "action": "dropped", "reason": "missing or malformed file/line"}
    ]


def test_existing_line_past_eof_still_dropped(repo: Path):
    findings = [_finding(file="src/foo.py", line=999)]
    doc = _doc(findings)
    hunk_lines = {"src/foo.py": {999}}  # off-hunk would pass; file check should still fail

    updated, actions = preflight(
        doc,
        prior_findings=[],
        repo_root=repo,
        blocking_severities=["critical", "high"],
        hunk_lines=hunk_lines,
    )

    assert updated["findings"] == []
    assert actions == [{"id": "BOT-A1", "action": "dropped", "reason": "line 999 > file length 20"}]


def test_severity_demotion_still_works_with_hunk_lines(repo: Path):
    """Severity-monotonicity rule is unaffected by the off-hunk check."""
    findings = [_finding(file="src/foo.py", line=3, severity="critical")]
    doc = _doc(findings)
    priors = [{"id": "BOT-A1", "prior_severity": "low", "status": "unfixed"}]
    hunk_lines = {"src/foo.py": {1, 2, 3, 4, 5}}

    updated, actions = preflight(
        doc,
        prior_findings=priors,
        repo_root=repo,
        blocking_severities=["critical", "high"],
        hunk_lines=hunk_lines,
    )

    assert len(updated["findings"]) == 1
    assert updated["findings"][0]["severity"] == "low"
    assert updated["findings"][0]["blocking"] is False
    assert actions == [
        {
            "id": "BOT-A1",
            "action": "demoted",
            "from": "critical",
            "to": "low",
            "reason": "severity-monotonicity (no quoted new evidence)",
        }
    ]
