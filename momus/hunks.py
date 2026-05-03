"""Parse unified diffs to learn which RIGHT-side line numbers are reviewable.

GitHub rejects inline review comments whose line is not on a diff hunk.
``parse_unified_diff`` reads the ``inputs/diff.patch`` produced by ``prep.py``
and returns, per file, the set of post-image line numbers that the API will
accept as inline anchors.
"""

from __future__ import annotations

import re

# Capture only the +c[,d] portion; we don't need the left-side range.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_unified_diff(patch_text: str) -> dict[str, set[int]]:
    """
    Return a mapping ``{file_path: {right_side_line_numbers}}``.

    Lines that appear as added (``+``) or context (`` ``) on the RIGHT side
    are included. Removed lines (``-``) are LEFT-only and skipped. Files
    deleted in the diff (``+++ /dev/null``) are omitted entirely. Renames
    are keyed by the new path (the ``+++ b/<new>`` value).
    """
    result: dict[str, set[int]] = {}
    cur_path: str | None = None
    right_line = 0
    in_hunk = False

    for raw in patch_text.splitlines():
        if raw.startswith("diff --git "):
            # New file section; reset state until +++ / @@ are seen.
            cur_path = None
            in_hunk = False
            continue

        if raw.startswith("+++ "):
            # "+++ b/<path>" or "+++ /dev/null"
            target = raw[4:].strip()
            if target == "/dev/null":
                cur_path = None
            else:
                # Strip leading "b/" if present (standard git diff prefix).
                cur_path = target[2:] if target.startswith("b/") else target
                result.setdefault(cur_path, set())
            in_hunk = False
            continue

        if raw.startswith("--- "):
            # Left-side header; nothing to do, but make sure we are not
            # mid-hunk for the next iteration.
            in_hunk = False
            continue

        if raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            if m and cur_path is not None:
                right_line = int(m.group(1))
                in_hunk = True
            else:
                in_hunk = False
            continue

        if not in_hunk or cur_path is None:
            continue

        # The "\ No newline at end of file" marker does not consume a
        # right-side line number on either side.
        if raw.startswith("\\"):
            continue

        if raw.startswith("+"):
            result[cur_path].add(right_line)
            right_line += 1
        elif raw.startswith(" "):
            result[cur_path].add(right_line)
            right_line += 1
        elif raw.startswith("-"):
            # Left-only; do not advance right counter.
            continue
        else:
            # Unknown line shape inside a hunk; bail out of hunk mode to
            # avoid silently miscounting.
            in_hunk = False

    return result
