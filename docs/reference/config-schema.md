# Configuration schema

Every key Momus reads from `.momus.yaml`. Defaults live in `momus/config-defaults.yaml`; a target repo overrides any key by committing `.momus.yaml` at its repo root. Anything unset falls back to defaults. Unrecognized keys are an error: typos fail loudly rather than silently dropping intent.

Top-level groups: [`review`](#review), [`scope`](#scope), [`conventions`](#conventions), [`post`](#post), [`verify`](#verify), [`checks`](#checks), [`provider`](#provider).

## review {#review}

Findings cap, severity scale, calibration, and emphasis injection.

### `review.blocking_severities` {#review-blocking-severities}

- Type: `list[str]` (subset of `[critical, high, medium, low, nit]`)
- Default: `[critical, high, medium]`
- Available since 1.0

Severities that trigger `REQUEST_CHANGES`. Anything not listed is advisory and does not block merge.

See [Review philosophy / Severity and blocking](../explanation/review-philosophy.md#severity-and-blocking).

### `review.require_calibration` {#review-require-calibration}

- Type: `bool`
- Default: `true`
- Available since 1.0

Whether the model must write a one-line `calibration` justification before emitting any blocking finding. Forces "would a human block?" discipline into the visible output.

See [Review philosophy / Calibration](../explanation/review-philosophy.md#calibration).

### `review.emit_nits` {#review-emit-nits}

- Type: `bool`
- Default: `true`
- Available since 1.0

Whether the model may emit `nit`-severity findings at all. When `false`, the severity scale presented to the model omits `nit` entirely.

### `review.max_findings` {#review-max-findings}

- Type: `int`
- Default: `50`
- Available since 1.0

Hard ceiling on findings emitted per run. The model is instructed to consolidate redundant items rather than truncate; the publisher enforces the cap regardless.

### `review.noteworthy_max` {#review-noteworthy-max}

- Type: `int`
- Default: `3`
- Available since 1.0

Maximum "noteworthy" (good-work) callouts per run. Set to `0` to disable the section entirely.

### `review.run_id_scheme` {#review-run-id-scheme}

- Type: `str` (enum: `alpha` | `numeric` | `off`)
- Default: `alpha`
- Available since 1.0

Run-id scheme for finding IDs (e.g., `BOT-A1`, `BOT-A2`, ...).

- `alpha`: `A`, `B`, `C`, ... per re-review (matches the styleseat default).
- `numeric`: `1`, `2`, `3`, ... per re-review.
- `off`: bare counter, no run prefix.

The run id is computed from the highest existing finding ID across prior threads (see `momus/__main__.py` `_compute_run_id`).

### `review.repo_emphasis` {#review-repo-emphasis}

- Type: `str`
- Default: `""`
- Available since 1.0

Free-text block injected into the phase 2 prompt under "Repo-specific emphasis." Use for project-level guidance that does not belong in `docs/code-review-instructions.md` or `AGENTS.md` (e.g., "this is embedded firmware, treat any panic/unwrap as critical").

Composes additively with [`review.emphasis_modules`](#review-emphasis-modules): modules render first, then this string is appended after a blank line. Substituted into the [`<<REPO_EMPHASIS>>`](./prompt-tokens.md#token-repo-emphasis) prompt token.

### `review.emphasis_modules` {#review-emphasis-modules}

- Type: `list[str]`
- Default: `[]`
- Available since [next]

Composable emphasis-module library. Each entry is the basename of a file under `momus/prompts/emphasis/` that gets inlined into the phase 2 prompt's `<<REPO_EMPHASIS>>` block. Modules render in declared order, joined by blank lines, then [`review.repo_emphasis`](#review-repo-emphasis) is appended.

Valid values:

- `security`: OWASP-style focus (injection, traversal, secrets, authz, crypto).
- `dead_code`: unreferenced symbols, unreachable branches, dead exports.
- `quality_checklist`: no `any` types, no blanket try/except, resource leaks, etc.
- `test_quality`: green-mirage taxonomy (assertion-free, mocks-of-target, tautological, snapshot-without-comparison).

See [`reference/emphasis-modules.md`](./emphasis-modules.md) for module bodies and [`how-to/configure-emphasis.md`](../how-to/configure-emphasis.md) for picking a set.

```yaml
review:
  emphasis_modules: [security, test_quality]
  repo_emphasis: |
    Treat any panic/unwrap in firmware code as critical.
```

## scope {#scope}

Which changed files the review looks at. Where [`review`](#review) governs the findings momus emits, `scope` governs the diff it reads in the first place.

### `scope.exclude_paths` {#scope-exclude-paths}

- Type: `list[str]` (gitignore-syntax patterns)
- Default: the list below
- Available since 1.4.0

Files matching any pattern are removed from `inputs/diff.patch` and from `inputs/changed-files.txt`, and the LLM tool layer (`read_repo`, `grep_repo`, `find_repo`, `ls_repo`, `bash_ro`) refuses them.

**Setting this key REPLACES the default list. It does not extend it.** A repo that wants the defaults plus one more entry restates the whole list. This is the ordinary YAML list-override behavior and it is deliberate: an exclusion list you cannot shrink is not an exclusion list you control.

The defaults, verbatim and copy-pasteable:

```yaml
scope:
  exclude_paths:
    - dist/
    - build/
    - node_modules/
    - vendor/
    - coverage/
    - "*.min.js"
    - "*.min.css"
    - "*.map"
    - package-lock.json
    - yarn.lock
    - pnpm-lock.yaml
    - uv.lock
    - poetry.lock
    - Gemfile.lock
    - composer.lock
    - Cargo.lock
    - go.sum
```

They cover generated, minified, and vendored output whose content is decided by a source file that IS reviewed. Test snapshots and golden files are deliberately absent: they are often the most informative part of a diff.

Full gitignore semantics apply:

- `!pattern` re-includes a previously excluded path.
- A leading `/` anchors the pattern to the repo root.
- A trailing `/` matches directories only.
- `**` spans directory boundaries.
- Order matters: the last matching pattern decides.
- As in gitignore, a file under a directory this list excludes cannot be re-included by a later `!` line. Use `dist/**` rather than `dist/` when you intend to re-include something beneath it.

```yaml
scope:
  exclude_paths:
    - dist/**
    - "!dist/manifest.json"   # reviewed, even though the rest of dist/ is not
```

Negated character classes (`[!abc]`, `[^abc]`) are rejected with an error. Momus matches gitignore patterns twice, in Python for the diff and in TypeScript for the tool layer, and the two libraries read that one construct differently. A pattern the two layers disagree about would remove a file from the diff while leaving it readable, so momus refuses it instead. Positive classes and ranges (`*.[oa]`, `file[0-9].txt`) are fine.

Set to `[]` to review everything.

Note: an unrecognized key inside `scope` is currently accepted silently rather than raising, so a typo such as `exclude_path` leaves the defaults in force and reviews everything the defaults do not cover. Check `inputs/changed-files.txt` on the first run after editing this key.

### `scope.exclude_binary_files` {#scope-exclude-binary-files}

- Type: `bool`
- Default: `false`
- Available since 1.4.0

Whether to drop binary files from the review diff. Git renders a binary change as a three-line `Binary files ... differ` stanza with no hunk. The model cannot review it, any finding on it would be dropped as off-hunk, and it still counts toward the per-phase cap estimate. Detection is lexical, from the diff momus already captured: a `diff --git` stanza carrying a binary marker and no `@@` header.

Off by default, because a repo that reviews binary assets deliberately should keep seeing that they changed.

Both keys feed the [`<<SCOPE_EXCLUSIONS>>`](./prompt-tokens.md#token-scope-exclusions) prompt token.

## conventions {#conventions}

Files concatenated into `inputs/conventions.md` and presented to the model as repo conventions that override prompt defaults.

### `conventions.files` {#conventions-files}

- Type: `list[str]`
- Default: `[AGENTS.md]`
- Available since 1.0

Files concatenated into `inputs/conventions.md`. Files that do not exist are silently skipped. Repos that want richer convention loading can extend this list in `.momus.yaml`.

### `conventions.globs` {#conventions-globs}

- Type: `list[str]`
- Default: `[]`
- Available since 1.0

Glob patterns whose matches are also concatenated into `conventions.md`. None by default; opt in per repo (e.g., `["docs/code-review-*.md"]`).

## post {#post}

Verdict and approval policy at publish time.

### `post.first_review_approve_policy` {#post-first-review-approve-policy}

- Type: `str` (enum: `never` | `if_no_findings` | `if_no_blocking`)
- Default: `never`
- Available since 1.0

APPROVE policy on the very first review of a PR (no prior bot reviews).

- `never`: never APPROVE on first review (matches the styleseat default).
- `if_no_findings`: APPROVE only if findings list is empty.
- `if_no_blocking`: APPROVE if no blocking findings, even on first run.

This key feeds the [`<<APPROVE_RULE>>`](./prompt-tokens.md#token-approve-rule) and [`<<FIRST_REVIEW_APPROVE_RULE>>`](./prompt-tokens.md#token-first-review-approve-rule) tokens.

### `post.allow_human_approve_override` {#post-allow-human-approve-override}

- Type: `bool`
- Default: `false`
- Available since 1.0

Whether a user comment containing `lgtm` or `@bot approve` should override blocking findings. The styleseat default says no; the recommendation is to keep it that way.

## verify {#verify}

The phase 3 audit gate.

### `verify.enabled` {#verify-enabled}

- Type: `bool`
- Default: `true`
- Available since 1.0

When `true`, [phase 3](../explanation/four-phase-pipeline.md#phase-3-verify) audits phase 2's findings against the source before posting. The verify pass can drop or demote findings but cannot promote or add new ones. When `false`, phase 2's findings (after [preflight](../explanation/four-phase-pipeline.md#preflight-between-phase-2-and-3)) are published as-is.

## checks {#checks}

Optional GitHub Check Run alongside the Review object.

### `checks.enabled` {#checks-enabled}

- Type: `bool`
- Default: `false`
- Available since 1.0

Whether to also post a Check Run (GitHub's "Checks" UI on the PR header) alongside the Review. Useful when you want Momus to be a required check via branch protection. Requires the bot's token to have `Checks: Write` (set on the GitHub App, or available by default on `GITHUB_TOKEN`). Disabled by default; opt in once your App is configured for it.

### `checks.name` {#checks-name}

- Type: `str`
- Default: `Momus Code Review`
- Available since 1.0

Display name for the Check Run. Shows up on the PR's header checks list alongside CI ("Momus Code Review / passed"), so the qualifier disambiguates it from build/test/lint checks. Override per repo if needed.

## provider {#provider}

Per-repo overrides for the workflow's LLM env contract.

### `provider.model` {#provider-model}

- Type: `str`
- Default: `""`
- Available since 1.0

LLM model slug. Empty string means "use whatever the workflow set in `LLM_MODEL`." Override here when this repo needs a different model than the workflow default. The slug must be one your provider/`base_url` accepts.

```yaml
provider:
  model: anthropic/claude-sonnet-4-6
```

### `provider.base_url` {#provider-base-url}

- Type: `str`
- Default: `""`
- Available since 1.0

API base URL. Empty string means "use whatever the workflow set in `LLM_BASE_URL`." Override here for a different endpoint. The matching API key still comes from the `LLM_API_KEY` secret on the workflow job.

```yaml
provider:
  base_url: https://api.anthropic.com/v1
```
