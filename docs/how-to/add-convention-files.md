# Feed repo conventions into the review

You want Momus to honor the same conventions documents your humans read: `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, an internal style guide, whatever you have. Momus concatenates the files you list into `inputs/conventions.md` and presents them to the model as repo conventions that override prompt defaults.

The default already loads `AGENTS.md` if present. Anything else, you add explicitly.

## Add specific files

```yaml
conventions:
  files:
    - AGENTS.md
    - CLAUDE.md
    - .cursorrules
    - docs/style-guide.md
```

Files that do not exist are silently skipped, so it is safe to list optional files.

## Add files by glob

For a pattern (e.g., all code-review notes under `docs/`):

```yaml
conventions:
  globs:
    - docs/code-review-*.md
    - docs/conventions/*.md
```

Globs are evaluated relative to the repo root. They run in addition to `conventions.files`, not instead of it; both lists concatenate into the same `inputs/conventions.md`.

## Combined example

```yaml
conventions:
  files:
    - AGENTS.md
    - CLAUDE.md
  globs:
    - docs/code-review-*.md
```

## Untrusted-input note

Convention files are loaded from the PR head, which means a PR can modify `AGENTS.md` on the same PR Momus is reviewing. The threat model treats convention text as **partly attacker-controlled**: the model is told to weigh conventions as guidance, not as instructions, and phase 3's verify pass treats injected text in conventions as a drop-and-log signal. See [Threat model / untrusted inputs](../explanation/threat-model.md#untrusted-inputs).

This is intentional. If you only loaded conventions from `main`, a PR that legitimately introduces a new convention could not benefit from it during review. The trade is: trust the file, but trust phase 3 to catch anything weaponized.

## See also

- [Reference: `conventions.files`](../reference/config-schema.md#conventions-files)
- [Reference: `conventions.globs`](../reference/config-schema.md#conventions-globs)
- [Explanation: untrusted inputs](../explanation/threat-model.md#untrusted-inputs)
- [How-to: configure emphasis](./configure-emphasis.md) (for guidance that does not belong in `AGENTS.md`)
