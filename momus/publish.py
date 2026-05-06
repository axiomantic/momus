"""Deterministic publisher: render review markdown and submit via gh api."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import Config
from .findings_schema import Finding, FindingsDoc
from .invoke_pi import PhaseUsage

SEVERITY_ORDER = ["critical", "high", "medium", "low", "nit"]


# ---------------------------------------------------------------------------
# W5-Redaction: scrub credential-shaped strings + off-domain images
# ---------------------------------------------------------------------------
#
# Applied to every LLM-emitted publish-bound string (summary, finding
# title/message/suggestion, noteworthy entries) inside render_review_body
# and _finding_inline_body. Centralizing redaction at construction time
# means the 422-retry branches in _submit_review automatically inherit
# redaction without re-applying it to already-redacted strings.
#
# Token patterns: high-confidence prefix + length combinations only.
# Anything looser would burn legitimate review prose (`sk_buffer`,
# `AKIATooShort`, etc.).
#
# Off-domain image stripping: defends against the CamoLeak class of
# exfiltration where an attacker-shaped finding embeds an `![](evil.com)`
# whose URL path encodes data; GitHub camo-fetches the image and the
# attacker's server logs the request. github.com and
# user-images.githubusercontent.com are the only domains we allow because
# (a) they're GitHub's own image hosts and (b) requests to them are
# already traceable in GitHub's audit log.
TOKEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bgho_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bghu_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bghs_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bghr_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{48,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]

OFF_DOMAIN_IMG_RE = re.compile(
    r"!\[[^\]]*\]\((?!https?://github\.com|https?://user-images\.githubusercontent\.com)[^)]+\)"
)


def redact_for_publish(text: str) -> tuple[str, int]:
    """Return (redacted_text, n_redactions) for a publisher-bound string.

    n_redactions counts only credential-token replacements; off-domain
    image strips are not counted (they're a separate exfiltration class
    and the caller logs them separately).
    """
    n = 0
    for pat in TOKEN_PATTERNS:
        text, k = pat.subn("[redacted]", text)
        n += k
    text = OFF_DOMAIN_IMG_RE.sub("[image stripped: off-domain]", text)
    return text, n
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
    findings_doc: FindingsDoc | dict[str, Any],
    priors: list[dict[str, Any]],
    pr_meta: dict[str, Any],
    config: Config,
    run_url: str,
    phase_usages: list[tuple[str, PhaseUsage]] | None = None,
) -> None:
    """Publish a validated FindingsDoc to GitHub.

    Post-W5: the typed shape is `FindingsDoc`. The orchestrator
    (`__main__._read_findings_doc`) validates before calling. Dict input
    is accepted for legacy test callers that build payloads inline; it
    runs through the same `FindingsDoc.model_validate` so the eventual
    publish-bound state is identical regardless of entry point.
    """
    if not isinstance(findings_doc, FindingsDoc):
        findings_doc = FindingsDoc.model_validate(findings_doc)
    owner = pr_meta["owner"]
    repo = pr_meta["repo"]
    pr_number = pr_meta["pr_number"]
    head_sha = pr_meta["head_sha"]
    run_id = pr_meta.get("run_id", "A")

    body = render_review_body(
        findings_doc, run_url, run_id, config, phase_usages=phase_usages
    )
    inline_comments = build_inline_comments(findings_doc.findings, run_url, run_id)
    event = findings_doc.verdict

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
        prior_status=[
            p.model_dump() for p in findings_doc.prior_findings_status
        ],
        head_sha=head_sha,
        run_url=run_url,
    )


def render_review_body(
    findings_doc: FindingsDoc | dict[str, Any],
    run_url: str,
    run_id: str,
    config: Config,
    phase_usages: list[tuple[str, PhaseUsage]] | None = None,
) -> str:
    """Render the markdown body. Accepts FindingsDoc (post-W5) or dict
    (legacy callers in tests). Applies redaction to every LLM-emitted
    string at construction time so the 422-retry branches in
    `_submit_review` automatically inherit redaction.
    """
    if isinstance(findings_doc, FindingsDoc):
        summary_raw = findings_doc.summary
        tally = findings_doc.tally
        findings_models = findings_doc.findings
        findings = [f.model_dump() for f in findings_models]
        noteworthy_raw = findings_doc.noteworthy or []
        verdict = findings_doc.verdict
    else:
        summary_raw = findings_doc.get("summary", "").strip() or "(no summary)"
        tally = findings_doc.get("tally", {})
        findings = findings_doc.get("findings", [])
        noteworthy_raw = findings_doc.get("noteworthy", []) or []
        verdict = findings_doc.get("verdict", "COMMENT")

    summary = summary_raw.strip() or "(no summary)"
    summary, _ = redact_for_publish(summary)
    noteworthy = [redact_for_publish(n)[0] for n in noteworthy_raw]

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
    cost_line = _cost_footer_line(phase_usages or [])
    if cost_line is not None:
        parts.append("")
        parts.append(cost_line)
    parts.append("")
    parts.append(_attribution_line())
    parts.append("")
    parts.append(f"<!-- momus:run:{run_id} -->")
    parts.append(f"<!-- run: {run_url} -->")
    return "\n".join(parts)


def _cost_footer_line(
    phase_usages: list[tuple[str, PhaseUsage]],
) -> str | None:
    """Render `Cost: $X.YZ - I in / O out tokens - model` or None.

    Returns None when there's no usage data to summarize. The cost is
    rounded to whole cents (two decimals) per spec; sub-cent runs render
    as `$0.00`. The model name comes from PhaseUsage and is the same
    across phases under normal operation, so we take it from the first
    non-empty phase.
    """
    if not phase_usages:
        return None
    total_cost = sum(u.cost_usd for _, u in phase_usages)
    total_in = sum(u.input_tokens for _, u in phase_usages)
    total_out = sum(u.output_tokens for _, u in phase_usages)
    if total_in == 0 and total_out == 0:
        return None
    model = next((u.model for _, u in phase_usages if u.model), "")
    cents = round(total_cost * 100)
    dollars = cents // 100
    rem = cents % 100
    cost_str = f"${dollars}.{rem:02d}"
    tokens_str = f"{total_in:,} in / {total_out:,} out tokens"
    if model:
        return f"_Cost: {cost_str} - {tokens_str} - {model}_"
    return f"_Cost: {cost_str} - {tokens_str}_"


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
    title_raw = (f.get("title") or f.get("message", "")).strip().splitlines()[0]
    title, _ = redact_for_publish(title_raw)
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
    findings: list[dict[str, Any]] | list[Finding],
    run_url: str,
    run_id: str,
) -> list[dict[str, Any]]:
    """Build inline-comment payloads from findings (validated models or dicts).

    Accepts both the post-W5 `list[Finding]` and the pre-W5 `list[dict]`
    so test helpers and any legacy callers that hand in dicts continue
    to work.
    """
    comments: list[dict[str, Any]] = []
    for f in findings:
        f_dict: dict[str, Any] = (
            f.model_dump() if isinstance(f, Finding) else f
        )
        body = _finding_inline_body(f_dict, run_url, run_id)
        comment: dict[str, Any] = {
            "path": f_dict["file"],
            "line": f_dict["line"],
            "side": f_dict.get("side", "RIGHT"),
            "body": body,
        }
        end_line = f_dict.get("end_line")
        if isinstance(end_line, int) and end_line > f_dict["line"]:
            comment["start_line"] = f_dict["line"]
            comment["start_side"] = comment["side"]
            comment["line"] = end_line
        comments.append(comment)
    return comments


def _finding_inline_body(f: dict[str, Any], run_url: str, run_id: str) -> str:
    fid = f.get("id", "BOT-?")
    sev = f.get("severity", "medium")
    cat = f.get("category", "quality")
    title_raw = (f.get("title") or "").strip()
    message_raw = f.get("message", "").strip()
    suggestion_raw = f.get("suggestion")
    # W5-Redaction: scrub credentials in every LLM-emitted body field at
    # construction time. Static labels (severity, category, run id, run
    # URL) are not LLM-controlled and are skipped.
    title, _ = redact_for_publish(title_raw)
    message, _ = redact_for_publish(message_raw)
    suggestion = (
        redact_for_publish(suggestion_raw)[0]
        if isinstance(suggestion_raw, str)
        else suggestion_raw
    )

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
