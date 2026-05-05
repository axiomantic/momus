"""Deterministic publisher: render review markdown and submit via gh api."""

from __future__ import annotations

import json
import os
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

    # Pre-emptive downgrade for cases we can decide from the token's user
    # info alone: self-approval (token user == PR author) and Bot-typed
    # tokens. The third case — the default Actions ``GITHUB_TOKEN``, an
    # installation token indistinguishable from a custom App's token at
    # this layer — falls through and is detected at submit time via
    # GitHub's 422 ("Apps cannot approve...").
    if event == "APPROVE":
        reason = _approve_downgrade_reason(pr_meta.get("author"))
        if reason is not None:
            event = "COMMENT"
            body = _prepend_downgrade_note(body, reason)

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
    parts.append(_attribution_line())
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


def _attribution_line() -> str:
    """
    Footer line crediting the project and showing what model/host produced
    the review. The model and host come from the env (set by the workflow).
    The host is rendered as the bare hostname so the line stays readable.
    """
    model = os.environ.get("LLM_MODEL", "").strip()
    host = _hostname_from_url(os.environ.get("LLM_BASE_URL", ""))
    base = "_Powered by [Momus](https://github.com/axiomantic/momus)"
    if model and host:
        return f"{base} running `{model}` via {host}._"
    if model:
        return f"{base} running `{model}`._"
    return base + "._"


def _hostname_from_url(url: str) -> str:
    """Strip scheme and path from a URL, returning just the hostname."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return parsed.hostname or ""
    except Exception:  # noqa: BLE001
        return ""


def _commands_footer() -> str:
    trigger = os.environ.get("MOMUS_TRIGGER_COMMAND", "").strip() or "/ai-review"
    mention = os.environ.get("MOMUS_TRIGGER_MENTION", "").strip()
    trigger_line = f"- Comment `{trigger}`"
    if mention:
        trigger_line += f" or mention {mention}"
    trigger_line += " to request a re-review of the latest changes."
    return (
        "<details><summary>Commands</summary>\n\n"
        f"{trigger_line}\n"
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
    endpoint = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    payload = {
        "commit_id": head_sha,
        "body": body,
        "event": event,
        "comments": inline_comments,
    }
    try:
        _gh_api("POST", endpoint, payload)
        return
    except _GhApiError as exc:
        if exc.status != 422:
            raise
        # 422 on event=APPROVE most commonly means the token is the default
        # Actions ``GITHUB_TOKEN`` (an installation token with no approval
        # rights). The pre-emptive check in publish() can't distinguish
        # this from a real App token, so we fall back here: prepend the
        # downgrade note and retry as COMMENT. Resubmits keep inline
        # comments — they were valid; only the verdict was rejected.
        if event == "APPROVE" and _is_app_cannot_approve_error(str(exc)):
            downgrade_payload = {
                "commit_id": head_sha,
                "body": _prepend_downgrade_note(body, _APP_CANNOT_APPROVE_REASON),
                "event": "COMMENT",
                "comments": inline_comments,
            }
            _gh_api("POST", endpoint, downgrade_payload)
            return
        # Self-approval 422 must NOT be retried — the retry would carry
        # the same event and hit the same 422.
        if _is_self_approval_error(str(exc)):
            raise
        if not inline_comments:
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
        _gh_api("POST", endpoint, retry_payload)


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


_APP_CANNOT_APPROVE_REASON = (
    "the default GITHUB_TOKEN cannot approve PRs "
    "(configure a GitHub App; see SETUP.md)"
)


def _prepend_downgrade_note(body: str, reason: str) -> str:
    return (
        f"_Note: verdict was APPROVE but downgraded to COMMENT because {reason}._\n\n"
    ) + body


def _approve_downgrade_reason(pr_author: str | None) -> str | None:
    """Return a one-line reason if APPROVE should be pre-emptively downgraded.

    Pre-emptive cases (decidable from the token's user info alone):
    - ``Bot``-typed tokens cannot approve.
    - The token user matching the PR author would self-approve.

    The opaque case — installation tokens, where ``gh api /user`` 4xxs and
    ``gh api /app`` requires a JWT we do not have — is not decided here.
    Those go to GitHub as ``APPROVE``; if it is the default Actions
    ``GITHUB_TOKEN``, GitHub returns a 422 and ``_submit_review`` retries
    as ``COMMENT`` with the App-cannot-approve note prepended. A custom
    App with approval rights succeeds on the first attempt.
    """
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


def _is_app_cannot_approve_error(message: str) -> bool:
    """Detect the 422 GitHub returns when an App installation token (most
    notably the default Actions ``GITHUB_TOKEN``) tries to APPROVE.

    GitHub's wording has historically included phrases like "GitHub Apps
    cannot approve their own pull request" and "must use one of the events
    `COMMENT` or `REQUEST_CHANGES`". Match liberally to absorb future
    rephrasing without needing a release.
    """
    lower = message.lower()
    if "must use one of the events" in lower:
        return True
    if "cannot approve" in lower and (
        "github app" in lower or "apps" in lower or "installation" in lower
    ):
        return True
    return False


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
