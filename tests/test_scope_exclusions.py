"""Unit tests for review-scope exclusions (scope.exclude_paths / exclude_binary_files).

Three layers are covered here:

* the shared gitignore corpus, which the TypeScript suite asserts against
  the npm ``ignore`` package and this suite asserts against ``pathspec``;
* the patch/changed-files filter, including the stanza shapes a naive
  splitter mishandles (rename, mode-only change, deletion, binary);
* config loading, where an unsupported pattern must fail loudly.

The filter is a data transformation, so these are plain unit tests with
no mocking layer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from momus.diff_filter import DiffFilter, unsupported_pattern

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


def test_corpus_is_not_empty_and_covers_both_verdicts():
    """A corpus that drifted to all-true or all-false would pass every
    matcher, including a matcher that ignores its input entirely.
    """
    cases = _corpus_cases()
    assert len(cases) >= 30
    verdicts = {c["excluded"] for c in cases}
    assert verdicts == {True, False}


@pytest.mark.parametrize("pattern", _corpus()["unsupported_patterns"]["patterns"])
def test_corpus_unsupported_patterns_are_rejected(pattern: str) -> None:
    assert unsupported_pattern(pattern) is True


@pytest.mark.parametrize("pattern", _corpus()["unsupported_patterns"]["accepted_counterexamples"])
def test_corpus_accepted_counterexamples_are_not_rejected(pattern: str) -> None:
    assert unsupported_pattern(pattern) is False


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
