"""Unit tests for momus.hunks unified-diff parsing."""

from __future__ import annotations

from textwrap import dedent

from momus.hunks import parse_unified_diff


def test_empty_patch_returns_empty_dict():
    assert parse_unified_diff("") == {}


def test_single_file_single_hunk_collects_added_and_context_lines():
    """Right-side line numbers from a single hunk: context (' ') and added ('+')
    are collected, removed ('-') are skipped."""
    patch = dedent(
        """\
        diff --git a/foo.py b/foo.py
        index 1111111..2222222 100644
        --- a/foo.py
        +++ b/foo.py
        @@ -10,4 +10,5 @@
         line10
        -old11
        +new11
        +new12
         line13
         line14
        """
    )
    # Right-side trace starts at line 10:
    #   ' line10'  -> 10
    #   '-old11'   -> skip
    #   '+new11'   -> 11
    #   '+new12'   -> 12
    #   ' line13'  -> 13
    #   ' line14'  -> 14
    assert parse_unified_diff(patch) == {"foo.py": {10, 11, 12, 13, 14}}


def test_added_file_uses_dev_null_left_and_counts_from_plus_start():
    patch = dedent(
        """\
        diff --git a/new.py b/new.py
        new file mode 100644
        index 0000000..3333333
        --- /dev/null
        +++ b/new.py
        @@ -0,0 +1,3 @@
        +alpha
        +beta
        +gamma
        """
    )
    assert parse_unified_diff(patch) == {"new.py": {1, 2, 3}}


def test_deleted_file_is_omitted_from_result():
    patch = dedent(
        """\
        diff --git a/gone.py b/gone.py
        deleted file mode 100644
        index 4444444..0000000
        --- a/gone.py
        +++ /dev/null
        @@ -1,2 +0,0 @@
        -bye1
        -bye2
        """
    )
    assert parse_unified_diff(patch) == {}


def test_multiple_hunks_in_one_file_union_line_ranges():
    patch = dedent(
        """\
        diff --git a/multi.py b/multi.py
        index 1111111..2222222 100644
        --- a/multi.py
        +++ b/multi.py
        @@ -1,2 +1,2 @@
         keep1
        +add2
        @@ -50,2 +51,3 @@
         keep51
        +add52
         keep53
        """
    )
    # Hunk 1 starts at right=1: ' keep1'->1, '+add2'->2
    # Hunk 2 starts at right=51: ' keep51'->51, '+add52'->52, ' keep53'->53
    assert parse_unified_diff(patch) == {"multi.py": {1, 2, 51, 52, 53}}


def test_renamed_file_uses_new_path_from_plus_header():
    patch = dedent(
        """\
        diff --git a/old_name.py b/new_name.py
        similarity index 90%
        rename from old_name.py
        rename to new_name.py
        index 5555555..6666666 100644
        --- a/old_name.py
        +++ b/new_name.py
        @@ -3,2 +3,3 @@
         ctx3
        +added4
         ctx5
        """
    )
    # right starts at 3: ' ctx3'->3, '+added4'->4, ' ctx5'->5
    assert parse_unified_diff(patch) == {"new_name.py": {3, 4, 5}}


def test_multiple_files_in_one_patch():
    patch = dedent(
        """\
        diff --git a/a.py b/a.py
        index 1111111..2222222 100644
        --- a/a.py
        +++ b/a.py
        @@ -1,1 +1,2 @@
         a1
        +a2
        diff --git a/b.py b/b.py
        index 3333333..4444444 100644
        --- a/b.py
        +++ b/b.py
        @@ -10,1 +10,2 @@
         b10
        +b11
        """
    )
    assert parse_unified_diff(patch) == {
        "a.py": {1, 2},
        "b.py": {10, 11},
    }


def test_no_newline_at_eof_marker_is_ignored():
    """Lines starting with '\\ No newline at end of file' must be skipped without
    advancing the right-side counter."""
    patch = dedent(
        """\
        diff --git a/eof.py b/eof.py
        index 1111111..2222222 100644
        --- a/eof.py
        +++ b/eof.py
        @@ -1,1 +1,2 @@
         line1
        +line2
        \\ No newline at end of file
        """
    )
    assert parse_unified_diff(patch) == {"eof.py": {1, 2}}
