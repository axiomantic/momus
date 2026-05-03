"""
Sticky status comment for live PR-review progress.

Posts ONE comment per PR (identified by an HTML marker) and edits it
in place on each phase transition. On exception, the orchestrator's
top-level handler updates it to a failure note. Posting is best-effort:
network failures here MUST NOT abort the review run itself.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

# Marker placed in the comment body so we can find it on subsequent runs.
# Per-PR singleton: scope by PR number is implicit since the listing
# endpoint is already PR-scoped.
STATUS_MARKER = "<!-- momus:status -->"

# Animated indicator shown for in-progress states. Pointing at axiomantic/momus
# raw assets so updating the GIF is a one-line change in that repo.
STATUS_GIF_URL = (
    "https://raw.githubusercontent.com/axiomantic/momus/main/assets/working.gif"
)


def post_status(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    state: str,
    detail: str = "",
    run_url: str = "",
    progress_bar: str = "",
    percent: int | None = None,
) -> None:
    """
    Upsert the sticky status comment.

    ``state`` is one of: starting, running, posting, done, failed.
    ``detail`` is a short human phrase appended to the headline.
    ``run_url`` (optional) is appended as a link.
    ``progress_bar`` is the unicode bar string; when given, it is rendered
    on its own line with ``percent`` (e.g. ``████░░░ 47%``).

    Failures are swallowed and reported to stderr. The review run takes
    priority over status visibility.
    """
    try:
        body = _render_body(
            state=state,
            detail=detail,
            run_url=run_url,
            progress_bar=progress_bar,
            percent=percent,
        )
        comment_id = _find_existing_comment(owner, repo, pr_number)
        if comment_id is None:
            _create_comment(owner, repo, pr_number, body)
        else:
            _update_comment(owner, repo, comment_id, body)
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        print(f"[momus.status] post failed: {exc}", file=sys.stderr, flush=True)


def _render_body(
    *,
    state: str,
    detail: str,
    run_url: str,
    progress_bar: str = "",
    percent: int | None = None,
) -> str:
    icon = _state_icon(state)
    headline = _state_headline(state)
    line = f"{icon} **{headline}**"
    if detail:
        line += f" — {detail}"
    parts: list[str] = [line]
    if progress_bar:
        # Wrap in backticks so the box-drawing chars render with monospace
        # spacing in GitHub markdown (otherwise proportional font breaks
        # the alignment between filled and empty cells).
        if percent is not None:
            parts.append(f"`{progress_bar}` **{percent}%**")
        else:
            parts.append(f"`{progress_bar}`")
    if state in ("starting", "running", "posting"):
        parts.append(f'<img src="{STATUS_GIF_URL}" alt="Momus working" width="120">')
    if run_url:
        parts.append(f"[run log]({run_url})")
    parts.append(STATUS_MARKER)
    return "\n\n".join(parts)


def _state_icon(state: str) -> str:
    return {
        "starting": "🤖",
        "running": "🔍",
        "posting": "📝",
        "done": "✅",
        "failed": "❌",
    }.get(state, "•")


def _state_headline(state: str) -> str:
    return {
        "starting": "Momus review starting",
        "running": "Momus review running",
        "posting": "Momus posting review",
        "done": "Momus review posted",
        "failed": "Momus review failed",
    }.get(state, state)


def _find_existing_comment(owner: str, repo: str, pr_number: int) -> int | None:
    """Find a comment containing STATUS_MARKER. Returns its id or None."""
    proc = subprocess.run(
        [
            "gh", "api",
            "--paginate",
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
        ],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    # --paginate concatenates JSON arrays directly, e.g., "[{...}][{...}]".
    # Walk the buffer, parsing arrays one at a time.
    items: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    buf = proc.stdout.strip()
    i = 0
    while i < len(buf):
        while i < len(buf) and buf[i].isspace():
            i += 1
        if i >= len(buf):
            break
        obj, end = decoder.raw_decode(buf, i)
        if isinstance(obj, list):
            items.extend(obj)
        i = end
    for item in items:
        if STATUS_MARKER in (item.get("body") or ""):
            return int(item["id"])
    return None


def _create_comment(owner: str, repo: str, pr_number: int, body: str) -> None:
    payload = json.dumps({"body": body})
    proc = subprocess.run(
        [
            "gh", "api",
            "-X", "POST",
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            "--input", "-",
        ],
        input=payload, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"create comment failed: {proc.stderr.strip()}")


def _update_comment(owner: str, repo: str, comment_id: int, body: str) -> None:
    payload = json.dumps({"body": body})
    proc = subprocess.run(
        [
            "gh", "api",
            "-X", "PATCH",
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
            "--input", "-",
        ],
        input=payload, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"update comment failed: {proc.stderr.strip()}")
