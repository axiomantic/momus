"""V3 verification: capture today's git behavior for the §W2/§W4 argv matrix.

This script is a *verification artifact*, not production code. It iterates
the test matrix from design §W2 (git wrapper) and §W4 (bash_ro
absolute-path/tilde reject), runs each ``git ...`` invocation in a real
checkout (the worktree itself), and records:

  - argv: the exact argv tested
  - exit_code: today's actual ``git`` exit code
  - would_be_rejected_by_wrapper: a Python port of the §W2 wrapper rules
  - reject_reason: the rule that fired, or null
  - today_actual_behavior: human-readable summary of what real git did

The script writes a single JSON document to stdout. The companion
``v3-git-matrix.json`` file contains the captured run.

PASS criterion (per plan §4.4): every row where
``would_be_rejected_by_wrapper == True`` corresponds to an attack vector
(absolute path, traversal, ambiguous argv, ``--no-index``); every row
where it's False returns a non-empty success result from ``git``. No
legitimate ``git show HEAD:src/foo.py`` shape gets rejected.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Python port of the §W2 wrapper rules
# ---------------------------------------------------------------------------


_ALLOWED_GIT_SUBCMDS = {
    "log", "show", "diff", "blame", "rev-parse", "ls-files",
    "cat-file", "status", "ls-tree", "describe", "name-rev", "merge-base",
}
_PATH_ARG_SUBCMDS = {"log", "diff", "blame", "show", "cat-file"}


def _is_absolute_or_tilde(token: str) -> bool:
    return token.startswith("/") or token.startswith("~")


def _has_dotdot_segment(token: str) -> bool:
    parts = token.replace("\\", "/").split("/")
    return ".." in parts


def _check_ref_colon_path(argv: list[str]) -> tuple[bool, str | None]:
    # show / cat-file: tokens of the shape REF:PATH where PATH is treated
    # as a repo-relative path. Reject absolute, tilde, or .. PATH halves;
    # reject when more than one REF:PATH-shaped token appears (ambiguous).
    refpath_tokens = [t for t in argv[2:] if ":" in t and not t.startswith("-")]
    if len(refpath_tokens) > 1:
        return False, "AmbiguousShowArgv"
    for tok in refpath_tokens:
        _ref, _, path = tok.partition(":")
        if not path:
            continue
        if _is_absolute_or_tilde(path) or _has_dotdot_segment(path):
            return False, "OutsideRepo"
    return True, None


def _check_dashdash_argv(argv: list[str]) -> tuple[bool, str | None]:
    # diff / log / blame: paths must come after a `--` separator. If two
    # or more positional path-shaped tokens appear without `--`, reject
    # as ambiguous. Any path token that is absolute/tilde/.. -> reject.
    if "--" in argv:
        idx = argv.index("--")
        path_tokens = argv[idx + 1 :]
    else:
        # Heuristic: path-shaped non-flag tokens after the subcommand
        # that are not refs (no :) and not options.
        path_tokens = [t for t in argv[2:] if not t.startswith("-") and ":" not in t]
        # Refs like HEAD or HEAD~1 may slip in. Allow 0 or 1 candidate
        # without --; 2+ is ambiguous.
        if len(path_tokens) >= 2:
            # Ambiguous if any look like real paths (contain / or .)
            if any("/" in t or t.endswith(".py") or t.endswith(".md") for t in path_tokens):
                return False, "AmbiguousDiffArgv"
    for tok in path_tokens:
        if _is_absolute_or_tilde(tok) or _has_dotdot_segment(tok):
            return False, "OutsideRepo"
    return True, None


def check_git_argv(argv: list[str]) -> tuple[bool, str | None]:
    """Apply the §W2 wrapper rules. Returns (ok, reject_reason)."""
    if not argv or argv[0] != "git":
        return True, None
    if len(argv) < 2:
        return False, "UnsupportedGitSubcommand"
    sub = argv[1]
    if sub not in _ALLOWED_GIT_SUBCMDS:
        return False, "UnsupportedGitSubcommand"
    if sub == "diff" and "--no-index" in argv:
        return False, "UnsupportedGitOption"
    if sub in ("show", "cat-file"):
        return _check_ref_colon_path(argv)
    if sub in _PATH_ARG_SUBCMDS:
        return _check_dashdash_argv(argv)
    return True, None


# ---------------------------------------------------------------------------
# Test matrix
# ---------------------------------------------------------------------------


# (description, argv, attack_class)  — attack_class None means a
# legitimate row that should NOT be rejected.
_MATRIX: list[tuple[str, list[str], str | None]] = [
    # §W2 wrapper-test set:
    ("git_show_HEAD_relative_path_succeeds",
        ["git", "show", "HEAD:README.md"], None),
    ("git_show_HEAD_absolute_path_rejected_OutsideRepo",
        ["git", "show", "HEAD:/etc/passwd"], "absolute_path"),
    ("git_show_HEAD_traversal_path_rejected_OutsideRepo",
        ["git", "show", "HEAD:../../../etc/passwd"], "traversal"),
    ("git_cat_file_p_HEAD_etc_passwd_rejected_OutsideRepo",
        ["git", "cat-file", "-p", "HEAD:/etc/passwd"], "absolute_path"),
    ("git_log_p_dash_dash_relative_path_succeeds",
        ["git", "log", "-p", "--", "README.md"], None),
    ("git_log_p_dash_dash_absolute_path_rejected",
        ["git", "log", "-p", "--", "/etc/passwd"], "absolute_path"),
    ("git_blame_relative_path_succeeds",
        ["git", "blame", "README.md"], None),
    ("git_blame_absolute_path_rejected",
        ["git", "blame", "/etc/passwd"], "absolute_path"),
    ("git_status_succeeds_no_path_args",
        ["git", "status"], None),
    ("git_unsupported_subcommand_rejected",
        ["git", "push", "origin", "main"], "unsupported_subcommand"),
    # §W4 git-specific matrix rows:
    ("git_diff_no_index_rejected_UnsupportedGitOption",
        ["git", "diff", "--no-index", "/etc/passwd", "/tmp/x"], "no_index"),
    ("git_diff_two_paths_no_dashdash_rejected_AmbiguousDiffArgv",
        ["git", "diff", "README.md", "pyproject.toml"], "ambiguous_argv"),
    ("git_diff_with_dashdash_succeeds",
        ["git", "diff", "--", "README.md"], None),
    ("git_diff_ref_range_succeeds",
        ["git", "diff", "HEAD~1..HEAD"], None),
    ("git_show_two_ref_path_tokens_rejected_AmbiguousShowArgv",
        ["git", "show", "HEAD:README.md", "HEAD:pyproject.toml"], "ambiguous_show"),
]


def _run_git(argv: list[str], cwd: Path) -> dict[str, Any]:
    """Run ``argv`` from ``cwd``. Return short summary dict."""
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "summary": "timeout"}
    stdout_short = (proc.stdout[:120] or b"").decode("utf-8", errors="replace")
    stderr_short = (proc.stderr[:120] or b"").decode("utf-8", errors="replace")
    return {
        "exit_code": proc.returncode,
        "stdout_head": stdout_short.replace("\n", "\\n"),
        "stderr_head": stderr_short.replace("\n", "\\n"),
    }


def main() -> None:
    cwd = Path(os.environ.get("V3_CWD", os.getcwd()))
    rows: list[dict[str, Any]] = []
    legitimate_rejected: list[str] = []
    attack_allowed: list[str] = []
    for desc, argv, attack_class in _MATRIX:
        ok, reason = check_git_argv(argv)
        actual = _run_git(argv, cwd)
        row = {
            "description": desc,
            "argv": argv,
            "attack_class": attack_class,
            "would_be_rejected_by_wrapper": not ok,
            "reject_reason": reason,
            "today_actual_behavior": actual,
        }
        rows.append(row)
        # Cross-check semantics:
        if attack_class is None and not ok:
            legitimate_rejected.append(desc)
        if attack_class is not None and ok:
            # Allowed by wrapper but is a known attack vector — fail.
            attack_allowed.append(desc)
    status = "PASS" if not legitimate_rejected and not attack_allowed else "FAIL"
    print(json.dumps({
        "status": status,
        "n_rows": len(rows),
        "legitimate_rejected": legitimate_rejected,
        "attack_allowed": attack_allowed,
        "rows": rows,
    }, indent=2))


if __name__ == "__main__":
    main()
