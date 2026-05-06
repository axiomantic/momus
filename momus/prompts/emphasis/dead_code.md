### Dead code focus

After this PR's changes, flag code that no longer earns its place. Verify
each claim with `Grep` before raising:

- unreferenced functions, classes, or methods added or left behind by
  this diff. Grep the symbol name across the repo; if the only hits are
  the definition itself, raise it.
- unreachable branches: code after an unconditional `return`/`raise`/
  `throw`, conditions that can never be true given the surrounding
  type/value constraints, `else` after an exhaustive `if`.
- exports nobody imports: `__all__` entries, `export` statements, or
  public symbols added in this PR with no importer in the repo.
- commented-out code blocks left behind. Distinguish from explanatory
  comments; flag actual code-shaped comments.
- parameters never read inside a function body, including `**kwargs`
  passthrough that drops keys silently.
- imports added but never used in the same file (lint usually catches
  this; raise only when the linter is bypassed or absent).
