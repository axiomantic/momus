"""Unit tests for review-scope exclusions (scope.exclude_paths / exclude_binary_files).

Three layers are covered here:

* the shared gitignore corpus, which the TypeScript suite asserts against
  the vendored matcher in ``readonly-tools.ts`` and this suite asserts
  against ``pathspec``;
* the patch/changed-files filter, including the stanza shapes a naive
  splitter mishandles (rename, mode-only change, deletion, binary);
* config loading, where a malformed value must fail loudly.

The filter is a data transformation, so these are plain unit tests with
no mocking layer.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from momus.config import load_config
from momus.diff_filter import DiffFilter, unsupported_pattern
from momus.prep import prep_inputs
from momus.render import render_phase_prompt
from tripwire import M

CORPUS_PATH = Path(__file__).parent / "fixtures" / "gitignore-corpus.json"


def _corpus() -> dict[str, Any]:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def _corpus_cases() -> list[dict[str, Any]]:
    return list(_corpus()["cases"])


def _case_id(case: dict[str, Any]) -> str:
    return case["name"]


# ---------------------------------------------------------------------------
# Shared corpus: the Python matcher must agree with the TypeScript one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _corpus_cases(), ids=_case_id)
def test_python_matcher_agrees_with_shared_corpus(case: dict[str, Any]) -> None:
    filt = DiffFilter(patterns=tuple(case["patterns"]))
    assert filt.excludes(case["path"]) is case["excluded"]


@pytest.mark.parametrize("pattern", _corpus()["unsupported_patterns"]["patterns"])
def test_corpus_unsupported_patterns_are_rejected(pattern: str) -> None:
    assert unsupported_pattern(pattern) is True


@pytest.mark.parametrize("pattern", _corpus()["unsupported_patterns"]["accepted_counterexamples"])
def test_corpus_accepted_counterexamples_are_not_rejected(pattern: str) -> None:
    assert unsupported_pattern(pattern) is False


def test_corpus_is_not_empty_and_covers_both_verdicts():
    """A corpus that drifted to all-true or all-false would pass every
    matcher, including a matcher that ignores its input entirely.
    """
    cases = _corpus_cases()
    assert len(cases) >= 30
    verdicts = {c["excluded"] for c in cases}
    assert verdicts == {True, False}


# ---------------------------------------------------------------------------
# Patch filtering
# ---------------------------------------------------------------------------

_ORDINARY = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 keep
+added
"""

_EXCLUDED = """diff --git a/dist/bundle.js b/dist/bundle.js
index 3333333..4444444 100644
--- a/dist/bundle.js
+++ b/dist/bundle.js
@@ -1 +1 @@
-old
+new
"""

_RENAME = """diff --git a/dist/old.js b/src/new.js
similarity index 100%
rename from dist/old.js
rename to src/new.js
"""

_RENAME_INTO_DIST = """diff --git a/src/old.js b/dist/new.js
similarity index 100%
rename from src/old.js
rename to dist/new.js
"""

_MODE_ONLY = """diff --git a/scripts/run.sh b/scripts/run.sh
old mode 100644
new mode 100755
"""

_DELETION = """diff --git a/dist/gone.js b/dist/gone.js
deleted file mode 100644
index 5555555..0000000
--- a/dist/gone.js
+++ /dev/null
@@ -1 +0,0 @@
-was here
"""

_BINARY = """diff --git a/assets/logo.png b/assets/logo.png
index 6666666..7777777 100644
Binary files a/assets/logo.png and b/assets/logo.png differ
"""


def test_filter_drops_excluded_stanza_and_keeps_the_rest():
    filt = DiffFilter(patterns=("dist/",))
    patch, dropped = filt.filter_patch(_ORDINARY + _EXCLUDED)
    assert "src/app.py" in patch
    assert "dist/bundle.js" not in patch
    assert dropped == frozenset({"dist/bundle.js"})


def test_filter_keeps_a_rename_out_of_an_excluded_directory():
    """The post-image path decides. A file renamed out of `dist/` is
    reviewable at its new home even though its old path was excluded.
    """
    filt = DiffFilter(patterns=("dist/",))
    patch, _ = filt.filter_patch(_RENAME)
    assert "rename to src/new.js" in patch


def test_filter_drops_a_rename_into_an_excluded_directory():
    filt = DiffFilter(patterns=("dist/",))
    patch, _ = filt.filter_patch(_RENAME_INTO_DIST)
    assert patch == ""


def test_filter_keeps_a_mode_only_change_with_no_hunks():
    """A mode-only stanza has no `+++` line at all; the path comes from
    the `diff --git` header.
    """
    filt = DiffFilter(patterns=("dist/",))
    patch, _ = filt.filter_patch(_MODE_ONLY)
    assert "scripts/run.sh" in patch


def test_filter_drops_a_mode_only_change_on_an_excluded_path():
    filt = DiffFilter(patterns=("scripts/",))
    patch, _ = filt.filter_patch(_MODE_ONLY)
    assert patch == ""


def test_filter_matches_a_deletion_on_its_pre_image_path():
    """A deletion's `+++` is /dev/null, so the excluded path is only
    visible on the `---` line.
    """
    filt = DiffFilter(patterns=("dist/",))
    patch, _ = filt.filter_patch(_DELETION)
    assert patch == ""


def test_binary_stanza_survives_when_the_feature_is_off():
    filt = DiffFilter(patterns=("dist/",), exclude_binary=False)
    patch, dropped = filt.filter_patch(_BINARY)
    assert "assets/logo.png" in patch
    assert dropped == frozenset()


def test_binary_stanza_is_dropped_and_reported_when_the_feature_is_on():
    filt = DiffFilter(patterns=(), exclude_binary=True)
    patch, dropped = filt.filter_patch(_ORDINARY + _BINARY)
    assert "src/app.py" in patch
    assert "assets/logo.png" not in patch
    assert dropped == frozenset({"assets/logo.png"})


def test_binary_exclusion_does_not_touch_a_text_stanza():
    """The binary test keys on the absence of a hunk plus a binary
    marker. A text diff whose body happens to quote the marker still has
    hunks and must survive.
    """
    quoting = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1,2 @@\n"
        " intro\n"
        "+Binary files a/x and b/x differ\n"
    )
    filt = DiffFilter(patterns=(), exclude_binary=True)
    patch, dropped = filt.filter_patch(quoting)
    assert "README.md" in patch
    assert dropped == frozenset()


def test_changed_files_uses_the_same_verdicts_as_the_patch():
    filt = DiffFilter(patterns=("dist/",), exclude_binary=True)
    _, dropped = filt.filter_patch(_ORDINARY + _EXCLUDED + _BINARY)
    names = "src/app.py\ndist/bundle.js\nassets/logo.png\n"
    assert filt.filter_changed_files(names, dropped) == "src/app.py\n"


def test_inactive_filter_is_a_passthrough():
    filt = DiffFilter()
    patch, dropped = filt.filter_patch(_ORDINARY + _EXCLUDED)
    assert patch == _ORDINARY + _EXCLUDED
    assert dropped == frozenset()
    assert filt.filter_changed_files("dist/x.js\n", frozenset()) == "dist/x.js\n"


def test_everything_excluded_yields_an_empty_patch():
    filt = DiffFilter(patterns=("*",))
    patch, _ = filt.filter_patch(_ORDINARY + _EXCLUDED)
    assert patch == ""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_defaults_apply_when_the_key_is_absent(tmp_path: Path):
    cfg = load_config(tmp_path)
    assert "dist/" in cfg.scope.exclude_paths
    assert "package-lock.json" in cfg.scope.exclude_paths
    assert cfg.scope.exclude_binary_files is False
    # Snapshots and golden files stay reviewable.
    filt = DiffFilter(patterns=tuple(cfg.scope.exclude_paths))
    assert filt.excludes("tests/__snapshots__/a.snap") is False


def test_setting_the_key_replaces_rather_than_merges(tmp_path: Path):
    (tmp_path / ".momus.yaml").write_text("scope:\n  exclude_paths:\n    - generated/\n")
    cfg = load_config(tmp_path)
    assert cfg.scope.exclude_paths == ["generated/"]
    filt = DiffFilter(patterns=tuple(cfg.scope.exclude_paths))
    assert filt.excludes("generated/x.py") is True
    assert filt.excludes("dist/bundle.js") is False


def test_empty_list_disables_exclusions_entirely(tmp_path: Path):
    (tmp_path / ".momus.yaml").write_text("scope:\n  exclude_paths: []\n")
    cfg = load_config(tmp_path)
    assert cfg.scope.exclude_paths == []


def test_binary_exclusion_is_opt_in(tmp_path: Path):
    (tmp_path / ".momus.yaml").write_text("scope:\n  exclude_binary_files: true\n")
    cfg = load_config(tmp_path)
    assert cfg.scope.exclude_binary_files is True
    # Replacing one key in the group leaves the other at its default.
    assert "dist/" in cfg.scope.exclude_paths


def test_negated_character_class_is_accepted(tmp_path: Path):
    """Both matchers agree with git here, so the pattern is configurable.

    The verdicts asserted below are git's, from ``git check-ignore``:
    ``[!a]bc`` ignores ``xbc`` and keeps ``abc``.
    """
    (tmp_path / ".momus.yaml").write_text('scope:\n  exclude_paths:\n    - "[!a]bc"\n')
    cfg = load_config(tmp_path)
    assert cfg.scope.exclude_paths == ["[!a]bc"]
    assert DiffFilter(patterns=("[!a]bc",)).excludes("xbc") is True
    assert DiffFilter(patterns=("[!a]bc",)).excludes("abc") is False


def test_unsupported_pattern_fails_loudly(tmp_path: Path):
    (tmp_path / ".momus.yaml").write_text('scope:\n  exclude_paths:\n    - "[[:digit:]]ile.txt"\n')
    with pytest.raises(ValueError, match="unsupported pattern"):
        load_config(tmp_path)


def test_non_list_exclude_paths_fails_loudly(tmp_path: Path):
    (tmp_path / ".momus.yaml").write_text("scope:\n  exclude_paths: dist/\n")
    with pytest.raises(ValueError, match="must be a list"):
        load_config(tmp_path)


# ---------------------------------------------------------------------------
# Prompt token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["phase2", "phase3"])
def test_scope_token_renders_the_instruction_when_active(tmp_path: Path, phase: str):
    (tmp_path / ".momus.yaml").write_text(
        "scope:\n  exclude_paths:\n    - generated/\n  exclude_binary_files: true\n"
    )
    rendered = render_phase_prompt(phase, load_config(tmp_path), "A", Path(".momus"))
    assert "## Review scope" in rendered
    assert "`generated/`" in rendered
    assert "Binary files are excluded" in rendered
    assert "<<" not in rendered


@pytest.mark.parametrize("phase", ["phase2", "phase3"])
def test_scope_token_renders_empty_when_inert(tmp_path: Path, phase: str):
    (tmp_path / ".momus.yaml").write_text(
        "scope:\n  exclude_paths: []\n  exclude_binary_files: false\n"
    )
    rendered = render_phase_prompt(phase, load_config(tmp_path), "A", Path(".momus"))
    assert "Review scope" not in rendered
    assert "<<" not in rendered


def test_phase1_prompt_has_no_scope_token(tmp_path: Path):
    """Phase 1 runs with `--tools ["write_output"]`, so it has no file
    tools to constrain and needs no scope instruction.
    """
    rendered = render_phase_prompt("phase1", load_config(tmp_path), "A", Path(".momus"))
    assert "Review scope" not in rendered


def test_scope_instruction_names_patterns_not_matched_paths(tmp_path: Path):
    """The instruction lists the configured patterns, which are already
    visible in the repo's own .momus.yaml, and never the paths they
    matched, which are unbounded and would hand the model a map of the
    hidden tree.
    """
    (tmp_path / ".momus.yaml").write_text("scope:\n  exclude_paths:\n    - dist/\n")
    rendered = render_phase_prompt("phase2", load_config(tmp_path), "A", Path(".momus"))
    assert "`dist/`" in rendered
    assert "1 configured" in rendered


# ---------------------------------------------------------------------------
# End to end through prep_inputs
# ---------------------------------------------------------------------------

_ALLOW_GIT = pytest.mark.allow(M(protocol="subprocess", binary="git"))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True, cwd=repo
    ).stdout


def _make_repo(repo: Path) -> tuple[str, str]:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    # The developer's global gitignore commonly lists `dist/`. Left in
    # place, `git add` skips the very files this fixture exists to stage
    # and every exclusion assertion below passes vacuously.
    _git(repo, "config", "core.excludesFile", "/dev/null")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()

    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('hello')\n")
    (repo / "dist").mkdir()
    (repo / "dist" / "bundle.js").write_text("var x=1\n")
    (repo / "dist" / "keep.js").write_text("var keep=1\n")
    (repo / "vendor.min.js").write_text("var v=1\n")
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "work")
    head_sha = _git(repo, "rev-parse", "HEAD").strip()
    return base_sha, head_sha


@_ALLOW_GIT
def test_prep_inputs_excludes_paths_from_both_diff_and_changed_files(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    base_sha, head_sha = _make_repo(repo)
    (repo / ".momus.yaml").write_text(
        "scope:\n  exclude_paths:\n    - dist/**\n    - '!dist/keep.js'\n    - '*.min.js'\n"
    )
    work_dir = repo / ".momus"

    inputs_dir = prep_inputs(
        repo,
        work_dir,
        Path(".momus"),
        {"base_sha": base_sha, "head_sha": head_sha, "run_id": "A"},
        load_config(repo),
    )

    patch = (inputs_dir / "diff.patch").read_text()
    changed = (inputs_dir / "changed-files.txt").read_text().split()

    # Excluded from BOTH artifacts.
    for excluded in ("dist/bundle.js", "vendor.min.js"):
        assert excluded not in patch, f"{excluded} leaked into diff.patch"
        assert excluded not in changed, f"{excluded} leaked into changed-files.txt"

    # Ordinary source survives in both.
    assert "src/app.py" in patch
    assert "src/app.py" in changed

    # The `!` re-include survives in both, which is what distinguishes a
    # real gitignore matcher from a prefix check.
    assert "dist/keep.js" in patch
    assert "dist/keep.js" in changed


@_ALLOW_GIT
def test_prep_inputs_excludes_binary_files_when_enabled(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    base_sha, head_sha = _make_repo(repo)
    (repo / ".momus.yaml").write_text("scope:\n  exclude_paths: []\n  exclude_binary_files: true\n")
    work_dir = repo / ".momus"

    inputs_dir = prep_inputs(
        repo,
        work_dir,
        Path(".momus"),
        {"base_sha": base_sha, "head_sha": head_sha, "run_id": "A"},
        load_config(repo),
    )
    patch = (inputs_dir / "diff.patch").read_text()
    changed = (inputs_dir / "changed-files.txt").read_text().split()

    assert "logo.png" not in patch
    assert "logo.png" not in changed
    assert "src/app.py" in changed


@_ALLOW_GIT
def test_prep_inputs_keeps_binary_files_by_default(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    base_sha, head_sha = _make_repo(repo)
    work_dir = repo / ".momus"

    inputs_dir = prep_inputs(
        repo,
        work_dir,
        Path(".momus"),
        {"base_sha": base_sha, "head_sha": head_sha, "run_id": "A"},
        load_config(repo),
    )
    changed = (inputs_dir / "changed-files.txt").read_text().split()
    assert "logo.png" in changed
