"""Regression tests for review-diff scope computation in prep.py.

The review diff must use three-dot / merge-base semantics
(``base...head``) so it matches GitHub's PR "Files changed" view. Two-dot
(``base..head``) reports commits that advanced the base branch after the
feature branched as reverse-diff deletions, producing false findings on
stacked PRs whose base has moved forward.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from momus.prep import _write_changed_files, _write_diff
from tripwire import M

_ALLOW_GIT = pytest.mark.allow(M(protocol="subprocess", binary="git"))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo,
    ).stdout


def _make_stacked_repo(repo: Path) -> tuple[str, str]:
    """Build a repo where base advanced past the feature branch point.

    Returns (base_sha, head_sha). The base branch gains a forward-only
    commit that the feature branch never saw. Three-dot must exclude it;
    two-dot must include it as a spurious deletion.
    """
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")

    (repo / "shared.txt").write_text("common\n")
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-q", "-m", "root")

    # Feature branch diverges here and adds its own file.
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.txt").write_text("feature work\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-q", "-m", "feature commit")
    head_sha = _git(repo, "rev-parse", "HEAD").strip()

    # Base advances forward-only, after the branch point.
    _git(repo, "checkout", "-q", "main" if _has_main(repo) else "master")
    (repo / "base_forward.txt").write_text("added on base after branch\n")
    _git(repo, "add", "base_forward.txt")
    _git(repo, "commit", "-q", "-m", "base moved forward")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()

    return base_sha, head_sha


def _has_main(repo: Path) -> bool:
    branches = _git(repo, "branch", "--format=%(refname:short)")
    return "main" in branches.split()


@_ALLOW_GIT
def test_changed_files_uses_three_dot_excludes_base_forward(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_sha, head_sha = _make_stacked_repo(repo)

    dest = tmp_path / "changed.txt"
    _write_changed_files(repo, base_sha, head_sha, dest)
    changed = set(dest.read_text().split())

    # The feature file IS in scope.
    assert "feature.txt" in changed
    # The base's forward-only file is NOT in the review scope (three-dot).
    assert "base_forward.txt" not in changed


@_ALLOW_GIT
def test_changed_files_two_dot_would_include_base_forward(tmp_path: Path) -> None:
    """Guard the regression: confirm two-dot semantics WOULD have leaked
    the base-forward file, so the three-dot assertion above is meaningful
    and not vacuously true for this fixture.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    base_sha, head_sha = _make_stacked_repo(repo)

    two_dot = subprocess.run(
        ["git", "diff", "--name-only", f"{base_sha}..{head_sha}"],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo,
    ).stdout.split()

    assert "base_forward.txt" in two_dot


@_ALLOW_GIT
def test_write_diff_excludes_base_forward(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_sha, head_sha = _make_stacked_repo(repo)

    dest = tmp_path / "diff.patch"
    _write_diff(repo, base_sha, head_sha, dest)
    patch = dest.read_text()

    assert "feature.txt" in patch
    assert "base_forward.txt" not in patch
