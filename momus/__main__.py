"""Orchestrator entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import string
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .fetch_priors import fetch_prior_threads
from .hunks import parse_unified_diff
from .invoke_pi import invoke_pi_phase_with_retry
from .preflight import preflight
from .prep import prep_inputs
from .publish import publish


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    config = load_config(repo_root)

    pr_meta = _build_pr_meta(args)
    run_url = os.environ.get("GITHUB_RUN_URL", _guess_run_url())

    # Phase 0a: prior-thread fetch (only if there might be priors).
    is_re_review = args.event in ("issue_comment", "workflow_dispatch") or args.force_re_review
    prior_threads: list[dict[str, Any]] = []
    if is_re_review:
        prior_threads = fetch_prior_threads(args.owner, args.repo, args.pr_number)

    pr_meta["run_id"] = _compute_run_id(prior_threads, config.review.run_id_scheme)

    inputs_dir = prep_inputs(repo_root, work_dir, pr_meta, config)
    (inputs_dir / "prior-threads.json").write_text(
        json.dumps(prior_threads, indent=2)
    )

    # Parse the diff so preflight can drop findings whose line citations
    # are not on a hunk (GitHub rejects such inline comments with 422).
    diff_path = inputs_dir / "diff.patch"
    if diff_path.exists():
        hunk_lines = parse_unified_diff(diff_path.read_text())
    else:
        _log(f"warning: {diff_path} not found; skipping off-hunk preflight check")
        hunk_lines = {}

    outputs_dir = work_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1 — classify priors (skip if no prior threads)
    if prior_threads:
        _log(f"Phase 1: classifying {len(prior_threads)} prior threads")
        invoke_pi_phase_with_retry("phase1", work_dir)
        prior_findings = _read_outputs_json(outputs_dir / "prior-findings.json", default=[])
    else:
        prior_findings = []
    (inputs_dir / "prior-findings.json").write_text(json.dumps(prior_findings, indent=2))

    # Phase 2 — review
    _log("Phase 2: review")
    invoke_pi_phase_with_retry("phase2", work_dir)
    findings_doc = _read_outputs_json(outputs_dir / "findings.json")

    # Preflight — deterministic checks.
    _log("Preflight: deterministic structural checks")
    findings_doc, preflight_actions = preflight(
        findings_doc,
        prior_findings,
        repo_root,
        config.review.blocking_severities,
        hunk_lines=hunk_lines,
    )
    (outputs_dir / "findings.json").write_text(json.dumps(findings_doc, indent=2))
    (outputs_dir / "preflight-log.json").write_text(
        json.dumps(preflight_actions, indent=2)
    )

    # Phase 3 — verify (optional)
    if config.verify.enabled:
        _log("Phase 3: verify gate")
        invoke_pi_phase_with_retry("phase3", work_dir)
        findings_doc = _read_outputs_json(outputs_dir / "findings.json")

    # Publish.
    _log(f"Publishing review (verdict={findings_doc.get('verdict')})")
    publish(findings_doc, prior_findings, pr_meta, config, run_url)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="momus")
    p.add_argument("--owner", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--pr-number", required=True, type=int)
    p.add_argument("--repo-root", required=True, help="checked-out PR head")
    p.add_argument("--work-dir", default=".momus")
    p.add_argument(
        "--event",
        required=True,
        choices=["pull_request", "issue_comment", "workflow_dispatch"],
    )
    p.add_argument(
        "--force-re-review",
        action="store_true",
        help="Treat as re-review even if event is pull_request",
    )
    return p.parse_args(argv)


def _build_pr_meta(args: argparse.Namespace) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(args.pr_number),
            "--repo",
            f"{args.owner}/{args.repo}",
            "--json",
            "title,body,author,baseRefOid,headRefOid",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    return {
        "owner": args.owner,
        "repo": args.repo,
        "pr_number": args.pr_number,
        "title": data.get("title", ""),
        "body": data.get("body", ""),
        "author": (data.get("author") or {}).get("login", ""),
        "base_sha": data["baseRefOid"],
        "head_sha": data["headRefOid"],
    }


def _compute_run_id(prior_threads: list[dict[str, Any]], scheme: str) -> str:
    """
    Determine the run id (e.g. ``A``, ``B`` for alpha; ``1``, ``2`` for
    numeric) by inspecting the highest existing finding ID in priors.
    """
    if scheme == "off":
        return ""
    max_idx = 0
    for thread in prior_threads:
        fid = thread.get("id", "")
        idx = _decode_run_index(fid, scheme)
        if idx is not None and idx > max_idx:
            max_idx = idx
    next_idx = max_idx + 1
    if scheme == "alpha":
        return _alpha_label(next_idx)
    return str(next_idx)


def _decode_run_index(finding_id: str, scheme: str) -> int | None:
    if not finding_id.startswith("BOT-"):
        return None
    rest = finding_id[len("BOT-"):]
    if scheme == "alpha":
        # BOT-A1 -> A
        prefix = "".join(c for c in rest if c in string.ascii_uppercase)
        if not prefix:
            return None
        idx = 0
        for c in prefix:
            idx = idx * 26 + (ord(c) - ord("A") + 1)
        return idx
    if scheme == "numeric":
        # BOT-2-3 -> 2
        head = rest.split("-", 1)[0]
        return int(head) if head.isdigit() else None
    return None


def _alpha_label(idx: int) -> str:
    out = ""
    while idx > 0:
        idx, r = divmod(idx - 1, 26)
        out = chr(ord("A") + r) + out
    return out


def _read_outputs_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(f"expected output not produced: {path}")
    return json.loads(path.read_text())


def _guess_run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return ""


def _log(msg: str) -> None:
    print(f"[momus] {msg}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
