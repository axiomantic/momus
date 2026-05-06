# Configure review emphasis

You want Momus to focus on a particular class of issues for your repo: security, dead code, code quality, test quality, or some custom guidance. Two knobs cover this: [`review.emphasis_modules`](../reference/config-schema.md#review-emphasis-modules) (built-in modules) and [`review.repo_emphasis`](../reference/config-schema.md#review-repo-emphasis) (free-form text). They compose additively.

Available since 1.2 (the next release).

## The four built-in modules

| Module | Focus |
|---|---|
| [`security`](../reference/emphasis-modules.md#security) | OWASP-style: injection, traversal, secrets, authz, crypto |
| [`dead_code`](../reference/emphasis-modules.md#dead-code) | Unreferenced symbols, unreachable branches, dead exports |
| [`quality_checklist`](../reference/emphasis-modules.md#quality-checklist) | No `any` types, no blanket try/except, resource leaks, etc. |
| [`test_quality`](../reference/emphasis-modules.md#test-quality) | Green-mirage taxonomy: assertion-free, mocks-of-target, tautological, snapshot-without-comparison |

Bodies are inlined into the phase 2 prompt under "Repo-specific emphasis." See [`reference/emphasis-modules.md`](../reference/emphasis-modules.md) for the full text of each.

## Enable one module

```yaml
review:
  emphasis_modules: [security]
```

## Enable several

```yaml
review:
  emphasis_modules: [security, test_quality]
```

Modules render in the order you list them. The order matters because the prompt sees them top-to-bottom; put the module you care about most first.

## Enable all four

```yaml
review:
  emphasis_modules:
    - security
    - dead_code
    - quality_checklist
    - test_quality
```

This is the maximalist setup. Useful when you trust your model and want broad coverage; expect more findings per PR.

## Add a custom emphasis

`review.repo_emphasis` is a free-form string. It appends after any modules you enabled, joined by a blank line. Use it for guidance that is too project-specific to belong in a built-in module.

```yaml
review:
  repo_emphasis: |
    Treat any panic/unwrap in firmware code as critical. Production binaries
    cannot recover from panics; flag them even when wrapped in `Result`.
```

## Combine modules and custom emphasis

```yaml
review:
  emphasis_modules: [security, quality_checklist]
  repo_emphasis: |
    This is an embedded firmware repo. Any `unsafe` block must be paired
    with a SAFETY comment naming the invariants that hold.
```

Render order: `security` body, blank line, `quality_checklist` body, blank line, your `repo_emphasis` text. The whole block becomes the [`<<REPO_EMPHASIS>>`](../reference/prompt-tokens.md#token-repo-emphasis) substitution in the phase 2 prompt.

## See also

- [Reference: emphasis modules](../reference/emphasis-modules.md)
- [Reference: `review.emphasis_modules`](../reference/config-schema.md#review-emphasis-modules)
- [Reference: `review.repo_emphasis`](../reference/config-schema.md#review-repo-emphasis)
- [Reference: `<<REPO_EMPHASIS>>` token](../reference/prompt-tokens.md#token-repo-emphasis)
- [How-to: add convention files](./add-convention-files.md) (for guidance that belongs in `AGENTS.md` instead)
