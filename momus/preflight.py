"""Cheap deterministic checks that run between phase 2 and phase 3.

Drops findings whose citation is structurally invalid (file missing,
line out of range) before the model audit phase. Demotes findings whose
severity exceeds the prior severity for the same finding ID without
new-evidence quoting (severity-monotonicity rule).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

SEVERITY_RANK = {"critical": 5, "high": 4, "medium": 3, "low": 2, "nit": 1}


def preflight(
    findings_doc: dict[str, Any],
    prior_findings: list[dict[str, Any]],
    repo_root: Path,
    blocking_severities: list[str],
    hunk_lines: dict[str, set[int]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Apply structural checks to ``findings_doc`` in place semantics.

    ``hunk_lines`` maps file path -> set of right-side line numbers that
    appear in any diff hunk. An empty/None mapping skips the off-hunk
    check (treated as "no diff info available", not "all lines off-hunk").

    Returns ``(updated_doc, action_log)``.
    """
    actions: list[dict[str, Any]] = []
    surviving: list[dict[str, Any]] = []
    priors_by_id = {p["id"]: p for p in prior_findings}
    hunk_lines = hunk_lines or {}

    for finding in findings_doc.get("findings", []):
        action = _check_one(
            finding, priors_by_id, repo_root, blocking_severities, hunk_lines
        )
        if action is None:
            surviving.append(finding)
        elif action["action"] == "demoted":
            surviving.append(action.pop("finding"))
            actions.append(action)
        else:
            actions.append(action)

    findings_doc = dict(findings_doc)
    findings_doc["findings"] = surviving
    findings_doc["tally"] = _recompute_tally(surviving)
    findings_doc["verdict"] = _recompute_verdict(
        surviving,
        prior_findings,
        findings_doc.get("prior_findings_status", []),
        blocking_severities,
    )
    return findings_doc, actions


def _check_one(
    finding: dict[str, Any],
    priors_by_id: dict[str, dict[str, Any]],
    repo_root: Path,
    blocking_severities: list[str],
    hunk_lines: dict[str, set[int]],
) -> dict[str, Any] | None:
    fid = finding.get("id", "<unknown>")
    rel = finding.get("file")
    line = finding.get("line")

    if not isinstance(rel, str) or not isinstance(line, int) or line < 1:
        return {"id": fid, "action": "dropped", "reason": "missing or malformed file/line"}

    # Off-hunk check: cheaper signal than touching the filesystem and gives
    # a more specific reason. Skipped entirely if no diff info was provided.
    if hunk_lines:
        if rel not in hunk_lines:
            return {"id": fid, "action": "dropped", "reason": "file not in PR diff"}
        allowed = hunk_lines[rel]
        end_line = finding.get("end_line")
        last = end_line if isinstance(end_line, int) and end_line >= line else line
        for n in range(line, last + 1):
            if n not in allowed:
                return {
                    "id": fid,
                    "action": "dropped",
                    "reason": f"line {n} not on a diff hunk",
                }

    file_path = (repo_root / rel).resolve()
    if not _is_under(file_path, repo_root) or not file_path.is_file():
        return {"id": fid, "action": "dropped", "reason": f"file not found: {rel}"}

    try:
        line_count = sum(1 for _ in file_path.open("rb"))
    except OSError as e:
        return {"id": fid, "action": "dropped", "reason": f"unreadable: {e}"}

    if line > line_count:
        return {
            "id": fid,
            "action": "dropped",
            "reason": f"line {line} > file length {line_count}",
        }

    prior = priors_by_id.get(fid)
    if prior is not None:
        cur_rank = SEVERITY_RANK.get(finding.get("severity", "medium"), 3)
        prior_rank = SEVERITY_RANK.get(prior.get("prior_severity", "medium"), 3)
        if cur_rank > prior_rank:
            old = finding.get("severity")
            new = prior["prior_severity"]
            updated = dict(finding)
            updated["severity"] = new
            updated["blocking"] = new in blocking_severities
            return {
                "id": fid,
                "action": "demoted",
                "from": old,
                "to": new,
                "reason": "severity-monotonicity (no quoted new evidence)",
                "finding": updated,
            }

    return None


def _recompute_tally(findings: list[dict[str, Any]]) -> dict[str, int]:
    tally = {"critical": 0, "high": 0, "medium": 0, "low": 0, "nit": 0}
    for f in findings:
        sev = f.get("severity", "medium")
        if sev in tally:
            tally[sev] += 1
    return tally


def _recompute_verdict(
    findings: list[dict[str, Any]],
    priors: list[dict[str, Any]],
    prior_status: list[dict[str, Any]],
    blocking: list[str],
) -> str:
    if any(f.get("severity") in blocking for f in findings):
        return "REQUEST_CHANGES"
    status_by_id = {s["id"]: s["status"] for s in prior_status}
    for prior in priors:
        if prior["status"] == "DECLINED":
            continue
        cur_status = status_by_id.get(prior["id"], "unfixed")
        if cur_status not in ("fixed", "removed"):
            return "REQUEST_CHANGES"
    return "APPROVE" if findings else "APPROVE"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
