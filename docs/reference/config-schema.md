# Configuration schema

Every key Momus reads from `.momus.yaml`. Defaults live in `momus/config-defaults.yaml`; a target repo overrides any key by committing `.momus.yaml` at its repo root. Anything unset falls back to defaults. Unrecognized keys are an error: typos fail loudly rather than silently dropping intent.

Top-level groups: [`review`](#review), [`conventions`](#conventions), [`post`](#post), [`verify`](#verify), [`checks`](#checks), [`provider`](#provider).

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
