"""Narrow the review diff to the files momus is configured to review.

Two independent exclusions live here:

* ``scope.exclude_paths`` -- gitignore-syntax patterns. A matching file is
  removed from ``inputs/diff.patch`` and from ``inputs/changed-files.txt``.
* ``scope.exclude_binary_files`` -- when enabled, files git rendered as a
  ``Binary files ... differ`` stanza are removed as well.

The same :class:`DiffFilter` instance drives both output files, so the
patch and the changed-files list can never disagree about what is in
scope.

Gitignore matching is implemented twice in this repo: here with
``pathspec`` and in ``momus/extensions/readonly-tools.ts`` with the npm
``ignore`` package. ``tests/fixtures/gitignore-corpus.json`` is the
shared contract both matchers are asserted against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pathspec

# The npm `ignore` package returns the inverse of git's verdict for a
# negated character class: for `[!a]bc`, git ignores `xbc` and keeps
# `abc`, and `ignore` does the opposite. pathspec matches git. Momus
# cannot enforce a pattern its two matchers read differently, so it
# refuses the pattern instead of letting the diff layer and the tool
# layer diverge in silence. The same rule is implemented in
# readonly-tools.ts (`findUnsupportedPattern`).
_NEGATED_CHARACTER_CLASS = re.compile(r"(?<!\\)\[[!^]")

# A pattern ending in `/**` needs at least one path segment below the
# directory, so it never excludes the directory itself. git relies on
# that when deciding whether it may descend and honour a later negation;
# pathspec's `match_file` does not, so the directory verdict below skips
# these patterns explicitly.
_DIR_BLIND_SUFFIX = "/**"

_BINARY_MARKERS = ("Binary files ", "GIT binary patch")


def unsupported_pattern(pattern: str) -> bool:
    """Whether ``pattern`` uses a construct momus refuses to accept."""
    return _NEGATED_CHARACTER_CLASS.search(pattern) is not None


@dataclass(frozen=True)
class DiffFilter:
    """Decide, once per run, which changed files momus reviews."""

    patterns: tuple[str, ...] = ()
    exclude_binary: bool = False
    _spec: pathspec.PathSpec = field(init=False, repr=False, compare=False)
    _dir_specs: tuple[tuple[bool, pathspec.PathSpec], ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        cleaned = [p.strip() for p in self.patterns if p.strip() and not p.strip().startswith("#")]
        object.__setattr__(self, "_spec", pathspec.GitIgnoreSpec.from_lines(cleaned))
        dir_specs = []
        for pattern in cleaned:
            negated = pattern.startswith("!")
            body = pattern[1:] if negated else pattern
            if body.endswith(_DIR_BLIND_SUFFIX):
                continue
            dir_specs.append((negated, pathspec.GitIgnoreSpec.from_lines([body])))
        object.__setattr__(self, "_dir_specs", tuple(dir_specs))

    @property
    def active(self) -> bool:
        """Whether this filter can drop anything at all."""
        return bool(self.patterns) or self.exclude_binary

    def excludes(self, path: str) -> bool:
        """Whether the repo-relative ``path`` is out of review scope.

        A trailing slash marks ``path`` as a directory, which is what
        makes directory-only patterns (``build/``) discriminate.
        """
        segments = path.rstrip("/").split("/")
        for depth in range(1, len(segments)):
            if self._dir_excluded("/".join(segments[:depth])):
                return True
        if path.endswith("/"):
            return self._dir_excluded(path.rstrip("/"))
        return bool(self._spec.match_file(path))

    def _dir_excluded(self, directory: str) -> bool:
        """Last-match-wins verdict for a directory path.

        git decides directory by directory whether it may descend, and a
        directory it refuses to enter cannot be reopened by a later
        negation on something beneath it. ``pathspec`` has no such
        traversal step, so the verdict is assembled here: each pattern is
        tested against both the bare and the trailing-slash form of the
        directory, and the last one that matches decides.
        """
        verdict = False
        for negated, spec in self._dir_specs:
            if spec.match_file(directory) or spec.match_file(f"{directory}/"):
                verdict = not negated
        return verdict

    def filter_patch(self, patch_text: str) -> tuple[str, frozenset[str]]:
        """Drop out-of-scope stanzas from a ``git diff`` patch.

        Returns the filtered patch and the set of paths that were
        dropped, which ``changed-files.txt`` needs so the two files agree
        about binary exclusions.
        """
        if not self.active or not patch_text.strip():
            return patch_text, frozenset()

        kept: list[str] = []
        dropped: set[str] = set()
        for stanza in _split_stanzas(patch_text):
            path = _stanza_path(stanza)
            if path is not None and self._drops(path, stanza):
                dropped.add(path)
                continue
            kept.extend(stanza)

        if not kept:
            return "", frozenset(dropped)
        return "\n".join(kept) + "\n", frozenset(dropped)

    def filter_changed_files(self, names_text: str, dropped: frozenset[str]) -> str:
        """Drop out-of-scope paths from ``git diff --name-only`` output."""
        if not self.active:
            return names_text
        kept = [
            name
            for name in names_text.splitlines()
            if name and name not in dropped and not self.excludes(name)
        ]
        if not kept:
            return ""
        return "\n".join(kept) + "\n"

    def _drops(self, path: str, stanza: list[str]) -> bool:
        if self.excludes(path):
            return True
        return self.exclude_binary and _is_binary_stanza(stanza)


def _split_stanzas(patch_text: str) -> list[list[str]]:
    """Split a patch into one list of lines per ``diff --git`` stanza.

    Splitting on ``+++ b/`` would mishandle the two stanza shapes that
    carry no ``+++`` line at all: a pure rename and a mode-only change.
    """
    stanzas: list[list[str]] = []
    current: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            if current:
                stanzas.append(current)
            current = [line]
        elif current:
            current.append(line)
        else:
            # Anything before the first stanza header (nothing, for
            # `git diff`) is carried through as its own block.
            current = [line]
    if current:
        stanzas.append(current)
    return stanzas


def _stanza_path(stanza: list[str]) -> str | None:
    """The path a stanza should be matched against.

    The post-image path is what the reviewer sees, so it wins. A deletion
    has no post-image and falls back to the pre-image path. A rename or a
    mode-only change has neither ``---`` nor ``+++``, so the header is
    parsed last.
    """
    if not stanza or not stanza[0].startswith("diff --git "):
        return None

    pre: str | None = None
    for line in stanza[1:]:
        if line.startswith("@@"):
            break
        if line.startswith("rename to "):
            return line[len("rename to ") :].strip()
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                continue
            return target[2:] if target.startswith("b/") else target
        if line.startswith("--- "):
            target = line[4:].strip()
            if target != "/dev/null":
                pre = target[2:] if target.startswith("a/") else target
    if pre is not None:
        return pre
    return _path_from_header(stanza[0])


def _path_from_header(header: str) -> str | None:
    """Recover the path from ``diff --git a/<old> b/<new>``.

    Unquoted paths containing spaces make the header ambiguous in
    general. The common case is an unchanged path, where old and new are
    identical and the midpoint split is unambiguous; a rename falls back
    to the first `` b/`` separator.
    """
    rest = header[len("diff --git ") :]
    if not rest.startswith("a/"):
        return None
    # "a/<p> b/<p>" has length 2 + n + 3 + n for a path of length n.
    n, remainder = divmod(len(rest) - 5, 2)
    if remainder == 0 and n > 0 and rest[2 + n : 5 + n] == " b/":
        return rest[2 : 2 + n]
    separator = rest.find(" b/")
    if separator == -1:
        return None
    return rest[separator + 3 :]


def _is_binary_stanza(stanza: list[str]) -> bool:
    """Whether git rendered this stanza as a binary difference.

    A binary stanza carries a ``Binary files ... differ`` line (or, under
    ``--binary``, a ``GIT binary patch`` block) and never a hunk header,
    which is why it reaches ``parse_unified_diff`` as a path with an
    empty line set.
    """
    saw_marker = False
    for line in stanza[1:]:
        if line.startswith("@@"):
            return False
        if line.startswith(_BINARY_MARKERS):
            saw_marker = True
    return saw_marker
