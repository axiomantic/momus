"""Deterministic publisher: render review markdown and submit via gh api."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import Config

SEVERITY_ORDER = ["critical", "high", "medium", "low", "nit"]
SEVERITY_LABELS = {
    "critical": "Critical",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "nit": "Nit",
}


class PublishError(RuntimeError):
    pass


def publish(
    findings_doc: dict[str, Any],
    priors: list[dict[str, Any]],
    pr_meta: dict[str, Any],
    config: Config,
    run_url: str,
) -> None:
    owner = pr_meta["owner"]
    repo = pr_meta["repo"]
    pr_number = pr_meta["pr_number"]
    head_sha = pr_meta["head_sha"]
    run_id = pr_meta.get("run_id", "A")

    body = render_review_body(findings_doc, run_url, run_id, config)
    inline_comments = build_inline_comments(findings_doc.get("findings", []), run_url, run_id)
    event = findings_doc.get("verdict", "COMMENT")

    # GitHub rejects APPROVE under two conditions: (1) self-approval (token
    # user == PR author), (2) bot tokens (e.g. github-actions[bot]) cannot
    # approve unless the org explicitly allows it. Detect both proactively
    # and downgrade to COMMENT, annotating the body so a human reader sees
    # what happened. If we can't determine the token user, attempt as-is and
    # surface any real error.
    if event == "APPROVE":
        reason = _approve_downgrade_reason(pr_meta.get("author"))
        if reason is None and _get_token_user_info() is None:
            print(
                "momus: warning: could not determine GH token user; "
                "skipping APPROVE downgrade check.",
                file=sys.stderr,
            )
        elif reason is not None:
            event = "COMMENT"
            body = (
                f"_Note: verdict was APPROVE but downgraded to COMMENT "
                f"because {reason}._\n\n"
            ) + body

    _submit_review(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        body=body,
        inline_comments=inline_comments,
        event=event,
    )

    _post_thread_replies(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        priors=priors,
        prior_status=findings_doc.get("prior_findings_status", []),
        head_sha=head_sha,
        run_url=run_url,
    )


def render_review_body(
    findings_doc: dict[str, Any],
    run_url: str,
    run_id: str,
    config: Config,
) -> str:
    summary = findings_doc.get("summary", "").strip() or "(no summary)"
    tally = findings_doc.get("tally", {})
    findings = findings_doc.get("findings", [])
    noteworthy = findings_doc.get("noteworthy", []) or []
    verdict = findings_doc.get("verdict", "COMMENT")

    parts: list[str] = [summary, "", _tally_line(tally)]

    by_severity = _group_by_severity(findings)
    for sev in SEVERITY_ORDER:
        bucket = by_severity.get(sev, [])
        if not bucket:
            continue
        is_blocking = sev in config.review.blocking_severities
        suffix = " (blocking)" if is_blocking else ""
        parts.append("")
        parts.append(f"### {SEVERITY_LABELS[sev]}{suffix}")
        for f in bucket:
            parts.append(_finding_one_liner(f))

    if noteworthy and config.review.noteworthy_max > 0:
        parts.append("")
        parts.append("### Noteworthy")
        for n in noteworthy[: config.review.noteworthy_max]:
            parts.append(f"- {n}")

    parts.append("")
    parts.append(f"**Verdict:** {verdict}.")
    parts.append("")
    parts.append(_commands_footer())
    parts.append("")
    parts.append(f"<!-- momus:run:{run_id} -->")
    parts.append(f"<!-- run: {run_url} -->")
    return "\n".join(parts)


def _tally_line(tally: dict[str, int]) -> str:
    pieces: list[str] = []
    for sev in SEVERITY_ORDER:
        n = tally.get(sev, 0)
        if n:
            pieces.append(f"{n} {SEVERITY_LABELS[sev]}{'s' if n != 1 else ''}")
    if not pieces:
        return "**No findings.**"
    return "**Severity tally:** " + ", ".join(pieces) + "."


def _group_by_severity(findings: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        out.setdefault(f.get("severity", "medium"), []).append(f)
    return out


def _finding_one_liner(f: dict[str, Any]) -> str:
    fid = f.get("id", "BOT-?")
    file = f.get("file", "?")
    line = f.get("line", "?")
    title = (f.get("title") or f.get("message", "")).strip().splitlines()[0]
    return f"- **{fid}** (`{file}:{line}`): {title}"


def _commands_footer() -> str:
    return (
        "<details><summary>Commands</summary>\n\n"
        "- Comment `/ai-review` to request a re-review of the latest changes.\n"
        "- Reply to a finding with `won't fix`, `by design`, or `not a bug` to decline it.\n"
        "- Reply with `instead, ...` to propose an alternative fix.\n\n"
        "</details>"
    )


def build_inline_comments(
    findings: list[dict[str, Any]],
    run_url: str,
    run_id: str,
) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for f in findings:
        body = _finding_inline_body(f, run_url, run_id)
        comment: dict[str, Any] = {
            "path": f["file"],
            "line": f["line"],
            "side": f.get("side", "RIGHT"),
            "body": body,
        }
        end_line = f.get("end_line")
        if isinstance(end_line, int) and end_line > f["line"]:
            comment["start_line"] = f["line"]
            comment["start_side"] = comment["side"]
            comment["line"] = end_line
        comments.append(comment)
    return comments


def _finding_inline_body(f: dict[str, Any], run_url: str, run_id: str) -> str:
    fid = f.get("id", "BOT-?")
    sev = f.get("severity", "medium")
    cat = f.get("category", "quality")
    title = (f.get("title") or "").strip()
    message = f.get("message", "").strip()
    suggestion = f.get("suggestion")

    parts = [f"**{fid}** — {SEVERITY_LABELS.get(sev, sev)} ({cat})"]
    if title:
        parts.append(title)
    parts.append(message)
    if isinstance(suggestion, str) and suggestion.strip():
        parts.append("")
        parts.append("```suggestion")
        parts.append(suggestion.rstrip("\n"))
        parts.append("```")
    parts.append("")
    parts.append(f"<!-- momus:run:{run_id} -->")
    parts.append(f"<!-- run: {run_url} -->")
    return "\n".join(parts)


def _submit_review(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    body: str,
    inline_comments: list[dict[str, Any]],
    event: str,
) -> None:
    payload = {
        "commit_id": head_sha,
        "body": body,
        "event": event,
        "comments": inline_comments,
    }
    try:
        _gh_api(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            payload,
        )
        return
    except _GhApiError as exc:
        if exc.status != 422 or not inline_comments:
            raise
        # Self-approval 422 must NOT be retried as body-only — the body-only
        # request still carries event=APPROVE and would hit the same 422.
        # Belt-and-suspenders: publish() already downgrades, but if we land
        # here for any reason, surface the error instead of looping.
        if _is_self_approval_error(str(exc)):
            raise
        # Otherwise: 422 typically means at least one comment cited a line
        # not on a diff hunk. Strip inline comments and retry body-only.
        body_only = body + "\n\n_Note: inline comments were demoted to body because some line citations were not on diff hunks._"
        retry_payload = {
            "commit_id": head_sha,
            "body": body_only,
            "event": event,
            "comments": [],
        }
        _gh_api(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            retry_payload,
        )


def _post_thread_replies(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    priors: list[dict[str, Any]],
    prior_status: list[dict[str, Any]],
    head_sha: str,
    run_url: str,
) -> None:
    status_by_id = {s["id"]: s["status"] for s in prior_status}
    for prior in priors:
        status = status_by_id.get(prior["id"])
        if status not in ("fixed", "removed"):
            continue
        comment_id = prior.get("comment_id")
        thread_id = prior.get("thread_id")
        if not comment_id:
            continue
        verb = "Fixed" if status == "fixed" else "Removed"
        reply_body = (
            f"{verb} in `{head_sha[:7]}`. Resolving.\n\n"
            f"<!-- run: {run_url} -->"
        )
        try:
            _gh_api(
                "POST",
                f"/repos/{owner}/{repo}/pulls/{pr_number}/comments/{comment_id}/replies",
                {"body": reply_body},
            )
        except _GhApiError:
            # Reply posting is best-effort; never fail the publisher on this.
            pass
        if thread_id:
            try:
                _resolve_thread(thread_id)
            except _GhApiError:
                pass


def _resolve_thread(thread_id: str) -> None:
    query = (
        "mutation($id: ID!) {"
        "  resolveReviewThread(input: {threadId: $id}) {"
        "    thread { id isResolved }"
        "  }"
        "}"
    )
    args = ["gh", "api", "graphql", "-F", f"id={thread_id}", "-f", f"query={query}"]
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise _GhApiError(proc.returncode, proc.stderr)


def _get_token_login() -> str | None:
    """Return the GH token's user login, or None if it can't be determined."""
    info = _get_token_user_info()
    if info is None:
        return None
    login = info.get("login", "")
    return login or None


def _get_token_user_info() -> dict[str, Any] | None:
    """Return the GH token user object (login + type), or None on failure."""
    try:
        proc = subprocess.run(
            ["gh", "api", "/user"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _approve_downgrade_reason(pr_author: str | None) -> str | None:
    """Return a one-line reason if APPROVE should be downgraded; else None."""
    info = _get_token_user_info()
    if info is None:
        return None
    login = info.get("login", "")
    user_type = info.get("type", "")
    if user_type == "Bot":
        return f"the token user `{login}` is a Bot and cannot approve PRs"
    if isinstance(pr_author, str) and login.lower() == pr_author.lower():
        return "the bot account is the PR author"
    return None


def _is_self_approval_error(message: str) -> bool:
    lower = message.lower()
    if "can not approve your own pull request" in lower:
        return True
    if "cannot approve your own pull request" in lower:
        return True
    # Liberal fallback for slight wording drift from GitHub.
    return "approve" in lower and "own" in lower


class _GhApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _gh_api(method: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    args = ["gh", "api", "-X", method, endpoint, "--input", "-"]
    proc = subprocess.run(
        args,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        status = _extract_status(proc.stderr)
        raise _GhApiError(status, proc.stderr.strip() or proc.stdout.strip())
    if not proc.stdout.strip():
        return {}
    return json.loads(proc.stdout)


def _extract_status(stderr: str) -> int:
    # gh api prints "HTTP 422" or similar in stderr on errors.
    import re

    m = re.search(r"HTTP\s+(\d+)", stderr)
    return int(m.group(1)) if m else 0
