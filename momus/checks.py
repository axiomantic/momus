"""
Optional Check Run posting.

When ``checks.enabled`` is true in config, Momus posts a Check Run
alongside the Review object. The Check Run surfaces on the PR header
and can be required via branch protection rules. The bot's token
needs ``Checks: Write`` — granted on the GitHub App, or default on
``GITHUB_TOKEN`` inside Actions.

Verdict → conclusion mapping is intentional:

  - APPROVE                                 → success
  - COMMENT, no blocking findings           → neutral
  - REQUEST_CHANGES OR any blocking finding → failure

The "failure" path is what makes Momus a meaningful merge gate:
branch-protection rules that require the Momus check will block
merges when the bot has blocking findings.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from .config import ChecksConfig
from .publish import redact_for_publish

SEVERITY_LABELS = {
    "critical": "Critical",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "nit": "Nit",
}


def post_check_run(
    *,
    owner: str,
    repo: str,
    head_sha: str,
    findings_doc: dict[str, Any],
    blocking_severities: list[str],
    config: ChecksConfig,
    run_url: str,
) -> None:
    """
    Post a Check Run summarizing the review. Failures are best-effort
    and logged to stderr; they do not abort the publish step.
    """
    if not config.enabled:
        return
    try:
        verdict = findings_doc.get("verdict", "COMMENT")
        findings = findings_doc.get("findings") or []
        blocking_set = set(blocking_severities or [])
        blocking_findings = [
            f for f in findings if f.get("severity") in blocking_set
        ]
        conclusion = _verdict_to_conclusion(verdict, blocking_findings)
        title = _build_title(verdict, len(findings), len(blocking_findings))
        summary = _build_summary(findings_doc, blocking_findings)

        payload: dict[str, Any] = {
            "name": config.name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title, "summary": summary},
        }
        if run_url:
            payload["details_url"] = run_url

        _gh_api(
            "POST",
            f"/repos/{owner}/{repo}/check-runs",
            payload,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        print(f"[momus.checks] post failed: {exc}", file=sys.stderr, flush=True)


def _verdict_to_conclusion(
    verdict: str,
    blocking_findings: list[dict[str, Any]],
) -> str:
    """
    Map review verdict + blocking severity presence to a Check Run
    conclusion. Order matters: any blocking finding forces failure
    regardless of the verdict string, so a model that emits APPROVE
    despite blockers still produces a failing check.
    """
    if blocking_findings:
        return "failure"
    if verdict == "APPROVE":
        return "success"
    if verdict == "REQUEST_CHANGES":
        return "failure"
    return "neutral"


def _build_title(verdict: str, n_findings: int, n_blocking: int) -> str:
    if n_blocking > 0:
        plural = "s" if n_blocking != 1 else ""
        return f"{n_blocking} blocking finding{plural}"
    if n_findings == 0:
        return "No findings"
    plural = "s" if n_findings != 1 else ""
    return f"{n_findings} non-blocking finding{plural}"


def _build_summary(
    findings_doc: dict[str, Any],
    blocking_findings: list[dict[str, Any]],
) -> str:
    # W5-Redaction: the Check Run summary is posted to GitHub like the
    # review body, so every LLM-emitted string that lands here has to
    # be scrubbed before it leaves the process.
    parts: list[str] = []
    summary_raw = (findings_doc.get("summary") or "").strip()
    if summary_raw:
        summary_text, _ = redact_for_publish(summary_raw)
        parts.append(summary_text)

    tally = findings_doc.get("tally") or {}
    tally_line = _tally_line(tally)
    if tally_line:
        parts.append("")
        parts.append(tally_line)

    if blocking_findings:
        parts.append("")
        parts.append("**Blocking findings:**")
        for f in blocking_findings:
            parts.append(_finding_one_liner(f))

    return "\n".join(parts) or "(no summary)"


def _tally_line(tally: dict[str, int]) -> str:
    pieces: list[str] = []
    for sev in ("critical", "high", "medium", "low", "nit"):
        n = tally.get(sev, 0)
        if n:
            pieces.append(f"{n} {SEVERITY_LABELS[sev]}{'s' if n != 1 else ''}")
    if not pieces:
        return ""
    return "**Severity tally:** " + ", ".join(pieces) + "."


def _finding_one_liner(f: dict[str, Any]) -> str:
    fid_raw = f.get("id", "BOT-?")
    fid, _ = redact_for_publish(fid_raw)
    file = f.get("file", "?")
    line = f.get("line", "?")
    title_raw = (f.get("title") or f.get("message", "")).strip().splitlines()[0]
    title, _ = redact_for_publish(title_raw)
    return f"- **{fid}** (`{file}:{line}`): {title}"


def _gh_api(method: str, endpoint: str, payload: dict[str, Any]) -> None:
    proc = subprocess.run(
        ["gh", "api", "-X", method, endpoint, "--input", "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"gh api {method} {endpoint} failed: {msg}")
