"""Fetch prior bot review comments and their human reply chains via gh api.

Identifies "our" prior comments by an HTML marker in the comment body
(``<!-- momus:run:... -->``) so we don't accidentally pick up
comments from other bots like Dependabot or Renovate.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

BOT_MARKER_RE = re.compile(r"<!-- momus:run:[^>]+-->")
FINDING_ID_RE = re.compile(r"\*\*(BOT-[A-Za-z0-9-]+)\*\*")
FINDING_SEVERITY_RE = re.compile(
    r"\*\*Severity\*\*:\s*(critical|high|medium|low|nit)", re.IGNORECASE
)


def fetch_prior_threads(owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
    """
    Return a list of unresolved bot-finding threads with their reply chains.

    Each entry shape matches the input schema documented in
    ``prompts/phase1-plan.md``.
    """
    review_comments = _gh_api_paginated(f"/repos/{owner}/{repo}/pulls/{pr_number}/comments")
    threads = _fetch_review_threads(owner, repo, pr_number)

    bot_originals = [c for c in review_comments if BOT_MARKER_RE.search(c.get("body", ""))]
    if not bot_originals:
        return []

    threads_by_comment_id = _build_thread_index(threads)

    results: list[dict[str, Any]] = []
    for original in bot_originals:
        thread = threads_by_comment_id.get(original["id"])
        if thread is None or thread.get("isResolved"):
            continue
        finding_id = _extract_finding_id(original.get("body", ""))
        if finding_id is None:
            continue
        severity = _extract_severity(original.get("body", "")) or "medium"
        replies = [
            _format_reply(c)
            for c in thread.get("comments", [])
            if c["databaseId"] != original["id"]
        ]
        results.append(
            {
                "id": finding_id,
                "thread_id": thread["id"],
                "comment_id": original["id"],
                "file": original.get("path"),
                "line": original.get("line") or original.get("original_line"),
                "prior_severity": severity,
                "original_message": original.get("body", ""),
                "replies": replies,
            }
        )

    return results


def _gh_api_paginated(endpoint: str) -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["gh", "api", "--paginate", endpoint],
        capture_output=True,
        text=True,
        check=True,
    )
    # --paginate concatenates JSON arrays as separate top-level docs joined by
    # newlines; gh handles that into a single array if used with --paginate +
    # endpoints that return arrays. Be defensive.
    text = proc.stdout.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    # Multi-document fallback
    return [item for chunk in text.split("\n") if chunk for item in json.loads(chunk)]


def _fetch_review_threads(owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
    """Fetch reviewThreads via GraphQL, paginated."""
    threads: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        query = _GRAPHQL_THREADS
        args = ["gh", "api", "graphql"]
        args += ["-F", f"owner={owner}", "-F", f"repo={repo}", "-F", f"prNumber={pr_number}"]
        if cursor:
            args += ["-F", f"cursor={cursor}"]
        args += ["-f", f"query={query}"]
        proc = subprocess.run(args, capture_output=True, text=True, check=True)
        data = json.loads(proc.stdout)
        page = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        for node in page["nodes"]:
            # Flatten comments.nodes -> comments for downstream code.
            node["comments"] = node.get("comments", {}).get("nodes", [])
            threads.append(node)
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return threads


def _build_thread_index(threads: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Map comment.databaseId -> thread for the first comment of each thread."""
    index: dict[int, dict[str, Any]] = {}
    for thread in threads:
        comments = thread.get("comments", [])
        if not comments:
            continue
        head = comments[0]
        if "databaseId" in head:
            index[head["databaseId"]] = thread
    return index


def _extract_finding_id(body: str) -> str | None:
    m = FINDING_ID_RE.search(body)
    return m.group(1) if m else None


def _extract_severity(body: str) -> str | None:
    m = FINDING_SEVERITY_RE.search(body)
    return m.group(1).lower() if m else None


def _format_reply(comment: dict[str, Any]) -> dict[str, Any]:
    author = comment.get("author") or {}
    login = author.get("login", "")
    is_bot = login.endswith("[bot]") or BOT_MARKER_RE.search(comment.get("body", "")) is not None
    return {"author": login, "is_bot": is_bot, "body": comment.get("body", "")}


_GRAPHQL_THREADS = """
query($owner: String!, $repo: String!, $prNumber: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $prNumber) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          path
          line
          comments(first: 100) {
            nodes {
              databaseId
              body
              author { login }
            }
          }
        }
      }
    }
  }
}
"""
