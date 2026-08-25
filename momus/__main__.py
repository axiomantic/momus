"""Orchestrator entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import string
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .checks import post_check_run
from .config import load_config
from .fetch_priors import fetch_prior_threads
from .findings_schema import FindingsDoc
from .hunks import parse_unified_diff
from .invoke_pi import PhaseUsage, invoke_pi_phase_with_retry, summarize_usage
from .preflight import preflight
from .prep import prep_inputs
from .progress import ProgressThrottle, ProgressTracker, estimate_phase_caps
from .publish import publish
from .status import post_status

DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_LLM_MODEL = "z-ai/glm-5.2:free"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    run_url = os.environ.get("GITHUB_RUN_URL", _guess_run_url())
    status_kwargs = {
        "owner": args.owner,
        "repo": args.repo,
        "pr_number": args.pr_number,
        "run_url": run_url,
    }
    post_status(state="starting", detail="setting up", **status_kwargs)
    try:
        return _run(args, run_url, status_kwargs)
    except Exception as exc:
        # Truncate so the comment doesn't blow out with a megabyte traceback.
        msg = str(exc).splitlines()[0] if str(exc).strip() else type(exc).__name__
        if len(msg) > 240:
            msg = msg[:237] + "..."
        post_status(state="failed", detail=msg, **status_kwargs)
        raise


def _run(
    args: argparse.Namespace,
    run_url: str,
    status_kwargs: dict[str, Any],
) -> int:
    repo_root = Path(args.repo_root).resolve()
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    # Pi runs with cwd=repo_root so its built-in file tools (read, ls,
    # grep, find) and bash_ro resolve relative paths against the repo
    # checkout. work_dir holds inputs/outputs and MUST live inside
    # repo_root so the model can reference them via a relative path that
    # is well-defined from CWD. Reject configurations that put work_dir
    # outside repo_root rather than silently re-introducing the
    # path-resolution split that this entrypoint was rewritten to fix.
    try:
        work_dir_rel = work_dir.relative_to(repo_root)
    except ValueError as exc:
        raise SystemExit(
            f"--work-dir ({work_dir}) must be inside --repo-root "
            f"({repo_root}); pi runs from repo_root and references "
            "inputs/outputs via a relative path."
        ) from exc

    config = load_config(repo_root)

    # Per-repo provider overrides take precedence over the workflow's
    # LLM_MODEL / LLM_BASE_URL env vars. If neither is set, fall back to
    # the default OpenRouter provider + glm-5.2:free model.
    if config.provider.model:
        os.environ["LLM_MODEL"] = config.provider.model
    elif not os.environ.get("LLM_MODEL"):
        os.environ["LLM_MODEL"] = DEFAULT_LLM_MODEL

    if config.provider.base_url:
        os.environ["LLM_BASE_URL"] = config.provider.base_url
    elif not os.environ.get("LLM_BASE_URL"):
        os.environ["LLM_BASE_URL"] = DEFAULT_LLM_BASE_URL

    pr_meta = _build_pr_meta(args)

    # Phase 0a: prior-thread fetch (only if there might be priors).
    is_re_review = args.event in ("issue_comment", "workflow_dispatch") or args.force_re_review
    prior_threads: list[dict[str, Any]] = []
    if is_re_review:
        prior_threads = fetch_prior_threads(args.owner, args.repo, args.pr_number)

    pr_meta["run_id"] = _compute_run_id(prior_threads, config.review.run_id_scheme)

    inputs_dir = prep_inputs(repo_root, work_dir, work_dir_rel, pr_meta, config)
    (inputs_dir / "prior-threads.json").write_text(json.dumps(prior_threads, indent=2))

    # Parse the diff so preflight can drop findings whose line citations
    # are not on a hunk (GitHub rejects such inline comments with 422).
    # None (diff unreadable) and {} (diff read, nothing reviewable in it)
    # mean different things to preflight; see its docstring.
    diff_path = inputs_dir / "diff.patch"
    hunk_lines: dict[str, set[int]] | None
    if diff_path.exists():
        hunk_lines = parse_unified_diff(diff_path.read_text())
        if not hunk_lines:
            _log("warning: review diff is empty; every changed file is out of review scope")
    else:
        _log(f"warning: {diff_path} not found; skipping off-hunk preflight check")
        hunk_lines = None

    outputs_dir = work_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    # Build the heuristic progress tracker. Phases included depend on whether
    # there are priors (phase 1) and whether verify is enabled (phase 3).
    phases_to_run: list[str] = []
    if prior_threads:
        phases_to_run.append("phase1")
    phases_to_run.append("phase2")
    if config.verify.enabled:
        phases_to_run.append("phase3")
    caps = estimate_phase_caps(
        n_prior_threads=len(prior_threads),
        n_touched_files=len(hunk_lines or {}),
    )
    tracker = ProgressTracker(phases_to_run=phases_to_run, caps=caps)
    throttle = ProgressThrottle(min_seconds=15.0, min_pct_delta=2)

    def _post_progress(detail: str, *, force: bool = False) -> None:
        pct = tracker.percent()
        if not throttle.should_post(time.monotonic(), pct, force=force):
            return
        post_status(
            state="running",
            detail=detail,
            progress_bar=tracker.bar(width=20),
            percent=pct,
            **status_kwargs,
        )

    # Per-phase usage totals are accumulated below and rendered in the
    # publish footer so the reviewer can see how much each PR review cost.
    phase_usages: list[tuple[str, PhaseUsage]] = []

    # Phase 1 — classify priors (skip if no prior threads)
    if prior_threads:
        _log(f"Phase 1: classifying {len(prior_threads)} prior threads")
        tracker.start("phase1")
        phase1_detail = (
            f"phase 1/{len(phases_to_run)} — classifying {len(prior_threads)} prior threads"
        )
        _post_progress(phase1_detail, force=True)

        def _on_phase1_tool_complete() -> None:
            tracker.tick()
            _post_progress(phase1_detail)

        events = invoke_pi_phase_with_retry(
            "phase1",
            work_dir,
            repo_root,
            on_tool_complete=_on_phase1_tool_complete,
        )
        phase_usages.append(("phase1", summarize_usage(events)))
        tracker.finish("phase1")
        prior_findings = _read_outputs_json(outputs_dir / "prior-findings.json", default=[])
    else:
        prior_findings = []
    (inputs_dir / "prior-findings.json").write_text(json.dumps(prior_findings, indent=2))

    # Phase 2 — review
    _log("Phase 2: review")
    phase2_index = phases_to_run.index("phase2") + 1
    phase2_detail = f"phase {phase2_index}/{len(phases_to_run)} — reviewing diff"
    tracker.start("phase2")
    _post_progress(phase2_detail, force=True)

    def _on_phase2_tool_complete() -> None:
        tracker.tick()
        _post_progress(phase2_detail)

    events = invoke_pi_phase_with_retry(
        "phase2",
        work_dir,
        repo_root,
        on_tool_complete=_on_phase2_tool_complete,
    )
    phase_usages.append(("phase2", summarize_usage(events)))
    tracker.finish("phase2")
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
    (outputs_dir / "preflight-log.json").write_text(json.dumps(preflight_actions, indent=2))

    # Phase 3 — verify (optional)
    if config.verify.enabled:
        _log("Phase 3: verify gate")
        phase3_detail = f"phase {len(phases_to_run)}/{len(phases_to_run)} — verifying findings"
        tracker.start("phase3")
        _post_progress(phase3_detail, force=True)

        def _on_phase3_tool_complete() -> None:
            tracker.tick()
            _post_progress(phase3_detail)

        events = invoke_pi_phase_with_retry(
            "phase3",
            work_dir,
            repo_root,
            on_tool_complete=_on_phase3_tool_complete,
        )
        phase_usages.append(("phase3", summarize_usage(events)))
        tracker.finish("phase3")

    # W5 validation gate: read + validate the FINAL findings.json against
    # FindingsDoc before posting. Malformed shapes (extra keys, wrong
    # types, oversize text) fail closed here — sys.exit(1) before any GH
    # API call.
    validated_doc = _read_findings_doc(outputs_dir / "findings.json")
    findings_doc = validated_doc.model_dump()

    # Publish.
    verdict = validated_doc.verdict
    _log(f"Publishing review (verdict={verdict})")
    # Force a near-100% post on the way in to publish.
    post_status(
        state="posting",
        detail=f"verdict {verdict}",
        progress_bar=tracker.bar(width=20),
        percent=tracker.percent(),
        **status_kwargs,
    )
    publish(
        validated_doc,
        prior_findings,
        pr_meta,
        config,
        run_url,
        phase_usages=phase_usages,
    )
    post_check_run(
        owner=pr_meta["owner"],
        repo=pr_meta["repo"],
        head_sha=pr_meta["head_sha"],
        findings_doc=findings_doc,
        blocking_severities=config.review.blocking_severities,
        config=config.checks,
        run_url=run_url,
    )
    n_findings = len(findings_doc.get("findings") or [])
    summary = f"verdict {verdict}, {n_findings} finding{'s' if n_findings != 1 else ''}"
    post_status(
        state="done",
        detail=summary,
        progress_bar="█" * 20,
        percent=100,
        **status_kwargs,
    )
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
    rest = finding_id[len("BOT-") :]
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


class FindingsValidationError(RuntimeError):
    """Raised when the LLM-emitted findings.json fails Pydantic validation.

    Caught by the orchestrator's outer Exception handler in `main()` so
    the failure surfaces as a `failed` status and PR-visible error,
    rather than a silent SystemExit that bypasses status reporting.
    """


def _read_findings_doc(path: Path) -> FindingsDoc:
    """Read and validate findings.json against the FindingsDoc schema (W5).

    Validation failures fail-closed: log structured detail to stderr and
    raise `FindingsValidationError`. The orchestrator's outer handler
    converts this into a status update + PR error comment so the run
    fails visibly instead of dying silently. The publisher is never
    called on a malformed doc; that's still the W5 contract.
    """
    if not path.exists():
        raise FileNotFoundError(f"expected output not produced: {path}")
    raw = json.loads(path.read_text())
    try:
        return FindingsDoc.model_validate(raw)
    except ValidationError as exc:
        # `errors()` is structured; render it inline so a human reading
        # the action log can pinpoint the offending field. The path
        # element of each error names the offending key (e.g.
        # `findings.0.severity`).
        lines = [f"findings.json schema validation failed: {path}"]
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            lines.append(f"  - {loc}: {err.get('msg', '')}")
        message = "\n".join(lines)
        print(message, file=sys.stderr)
        # First line of the message is what main()'s handler surfaces to
        # the PR, keeping detail in the action log only.
        raise FindingsValidationError(lines[0]) from exc


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
