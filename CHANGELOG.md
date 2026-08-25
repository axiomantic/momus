# Changelog

All notable changes to momus are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.4.0] - 2026-08-25

### Added

- **Review-scope exclusions (`scope.exclude_paths`).** A list of
  gitignore-syntax patterns naming files the review ignores. Matching
  files are removed from `inputs/diff.patch` and
  `inputs/changed-files.txt`, are named as out of scope in the phase 2
  and phase 3 prompts, and are refused by the `read_repo`, `grep_repo`,
  `find_repo`, `ls_repo` and `bash_ro` tools. Full gitignore semantics:
  negation, anchoring, directory-only patterns, and `**` spans. Ships
  with a default list covering generated, minified, and vendored output
  plus common lockfiles; test snapshots and golden files are
  deliberately not excluded. Setting the key REPLACES the default list
  rather than extending it. See
  [`reference/config-schema.md`](docs/reference/config-schema.md).
- **Binary-file exclusion (`scope.exclude_binary_files`), off by
  default.** Git renders a binary change as a `Binary files ... differ`
  stanza with no hunk, which the model cannot review and on which any
  finding is dropped as off-hunk. Enabling the key drops those stanzas
  from the review diff.
- **`<<SCOPE_EXCLUSIONS>>` prompt token** carrying the scope instruction
  into phase 2 and phase 3. Empty when nothing is excluded. It names the
  configured patterns, never the paths they matched.
- **`MOMUS_EXCLUDE_PATHS`** on the pi env allowlist, carrying the
  pattern list across the Python-to-pi boundary. It is also reserved, so
  `MOMUS_PI_ENV_PASSTHROUGH` cannot forge it.
- **Shared gitignore corpus** at `tests/fixtures/gitignore-corpus.json`,
  exercised by both `tests/test_scope_exclusions.py` (Python `pathspec`)
  and `momus/extensions/readonly-tools.test.ts` (npm `ignore`). Momus
  matches gitignore patterns twice, once per language, and a
  disagreement between the two would be invisible at runtime.

### Changed

- **`preflight` now distinguishes an unavailable diff from an empty
  one.** `hunk_lines=None` means the diff could not be read and skips
  the off-hunk check, as before. `hunk_lines={}` now means the diff was
  read and nothing in it is reviewable, which is what an exclusion list
  covering every changed file produces, and every finding is dropped as
  off-diff. Previously both cases skipped the check, which would have
  let a fully excluded review publish findings on files momus refused to
  look at.
- **`pathspec` is now a runtime dependency** in `[project].dependencies`,
  and **`ignore` is now a declared npm dependency**. Both were present
  only transitively before and would not have survived the
  `pip install` / `npm ci --omit=dev` that `action.yml` runs.

### Security

- The read-only tool layer refuses excluded paths rather than merely
  omitting them from the diff. `read_repo`, `grep_repo`, `find_repo` and
  `ls_repo` return `error: ExcludedPath` for an excluded target and skip
  excluded entries while walking; `bash_ro` rejects an argv token naming
  an excluded path. Previously the diff decided what the model should
  review while `read_repo` could open anything under the cwd.
- Patterns using a negated character class (`[!abc]`, `[^abc]`) are
  rejected at config load in both languages. The two matchers read that
  construct differently, and a pattern they disagree about would remove
  a file from the diff while leaving it readable.

## [1.3.4] - 2026-08-25

### Fixed

- **Review diff now uses three-dot (merge-base) semantics.** The review
  diff and changed-files list were computed with two-dot `base..head`,
  which for a stacked PR whose base branch has advanced past the branch
  point reported the base's forward-only commits as reverse-diff
  deletions, producing false findings. They now use three-dot
  `base...head`, matching GitHub's PR "Files changed" view and excluding
  commits that only advanced the base branch.

## [1.3.3] - 2026-08-24

### Changed

- **Unified LLM provider configuration via environment variables.**
  Removed the `llm_model` action input so LLM provider settings are
  configured consistently across workflow, job, and org levels via
  `LLM_MODEL`, `LLM_BASE_URL`, and `LLM_API_KEY` (or `.momus.yaml`).
  When unset, Momus automatically falls back to OpenRouter
  (`https://openrouter.ai/api/v1`) with `z-ai/glm-5.2:free` as the
  default review model.

## [1.3.1] - 2026-08-21

### Changed

- **Default review model switched to `deepseek/deepseek-v4-flash`.**
  Replaces `deepseek/deepseek-v4-pro` as the default model in the workflow
  and documentation examples, reducing review input/output token costs by
  roughly 68% while maintaining reasoning capabilities and limit
  compatibility.

## [1.3.0] - 2026-08-20

### Fixed

- **Phase 2 no longer dies at the output token cap.** The `byo`
  provider was registered with a hard-coded `contextWindow: 128000`
  and `maxTokens: 8192` regardless of which model `LLM_MODEL` named.
  Both were badly wrong for the model in production: pi-ai's own
  registry records 1048576 and 384000 for
  `deepseek/deepseek-v4-pro`. The output budget covers reasoning
  tokens as well as visible output, so the model spent the whole 8192
  reasoning, its message terminated with `stopReason: "length"` before
  it reached `write_output`, and pi's agent loop treated a message
  with no tool call as a finished agent and exited 0. Momus then
  reported only "did not produce expected output
  outputs/findings.json after retry", which is true of every failure
  mode and diagnoses none of them. Observed three times on
  `axiomantic/spellbook`. Both limits now come from the same registry
  lookup that already supplied pricing, so they track the model
  instead of drifting from it.
- **Thinking mode is no longer left to the provider's default.** The
  `byo` provider registration hand-set `reasoning: false` for whatever
  model `LLM_MODEL` named. In pi-ai that field is a wire-level gate,
  not a display hint: every thinking branch is guarded by it, so momus
  sent no reasoning field at all and the endpoint's default decided how
  much the review model thought. `reasoning` now comes from the same
  registry lookup that already supplied pricing and limits, together
  with the model's `thinkingLevelMap` and `compat`. All three are
  needed: pi asks for its default thinking level, "medium", and the
  registry records "medium" as unsupported for
  `deepseek/deepseek-v4-pro`, so without the level map momus would send
  the one effort value that model does not accept. With it the level
  clamps to "high". The `compat` entry supplies
  `requiresReasoningContentOnAssistantMessages`, which pi-ai cannot
  auto-detect here because the provider is registered under the name
  `byo` rather than `deepseek`, and without which DeepSeek rejects the
  next turn with error 20015 once thinking is on.
- **Pi no longer compacts against a window the model is nowhere
  near.** The same hard-coded `contextWindow: 128000` understated the
  production model's window by roughly 8x, which is why runs ended on
  `compaction_start reason=threshold`.
- **The retry is no longer a re-run under identical conditions.** When
  the first attempt was truncated, the retry prompt now tells the model
  to keep its analysis short and call `write_output` early, instead of
  appending a reminder that gives it more to reason about inside the
  same budget.
- **`rg` is installed on the runner.** `bash_ro`'s allowlist advertised
  `rg`, but the hosted images do not ship it, so the reviewer lost a
  turn to `spawn rg ENOENT` before falling back to `grep`.
- **Failed tool calls are labelled as failures.** Pi reports `isError`
  on the result object rather than on the event; momus read only the
  event, so the `spawn rg ENOENT` above was logged as `tool_result`
  rather than `tool_error`.

### Added

- **CI type-checks the TypeScript extension.** Nothing type-checked
  `momus/extensions/readonly-tools.ts`: the repo carried no
  `tsconfig.json`, and bun strips types rather than checking them, so
  `bun test` was green on code `tsc` rejects. Python had `mypy` in CI
  and TypeScript had no equivalent, while the same file gained a
  production crash fix and the model-registry lookup. A `tsconfig.json`
  at `strict` now covers `momus/extensions/**/*.ts` and the
  `typescript-tests` job runs `tsc --noEmit` before the suite. The
  check found four real type errors, among them a `bash_ro` handler
  whose unannotated `new Promise` widened its own return type to
  `unknown` and left every other branch of that tool unchecked.

- **CI runs the TypeScript extension suite.** `.github/workflows/ci.yml`
  ran ruff, mypy and pytest only, so the `bun test` assertions covering
  `momus/extensions/readonly-tools.ts` were local-only and could not
  gate a pull request. A `typescript-tests` job now installs the pinned
  npm dependencies with `npm ci` and runs `bun test` on a pinned bun,
  so a failing TypeScript assertion fails the PR.
- **`MOMUS_PI_MAX_TOKENS`**: per-message output token cap for the `byo`
  provider, for holding the registry-derived cap down. Defaults to the
  registry value, or 32768 for an unknown model.
- **`MOMUS_LOG_SNIPPET_CHARS`**: width of the per-event stderr
  summaries. Default 300, the previously hard-coded value. A crash you
  cannot diagnose from its own logs is a second defect.
- **Salvage of a failed phase's final message.** When a phase ends
  without its expected output, the last assistant message of each
  attempt is written to
  `outputs/<phase>-attempt<N>-last-message.txt`. `outputs/` is uploaded
  as a run artifact even on failure, so analysis completed before the
  crash stays recoverable without hand-mining a clipped Actions log.

### Changed

- **A skipped test now says so in the CI log.** `pytest` reported the
  one opt-in integration test as a single `s` in the progress line and
  exited 0, so `tests/integration/test_pi_tool_enforcement.py::
  test_pi_rejects_disallowed_tool` skipping on every CI run for want of
  `LLM_API_KEY` was indistinguishable from it passing. The skip is
  intended and CI still does not run it, since that would need a live
  API key; what was missing was any signal that it had not run. `-ra`
  in the `addopts` of `pyproject.toml` now prints a short summary line
  for every non-passing outcome, so a green run states which tests were
  skipped and why. It sits in `addopts` rather than in the CI
  invocation so the same reason text appears locally and in every
  workflow that calls pytest.

- **The missing-output error names its cause.** The first line, which
  is what the PR rollup comment shows, now reports the stop reason and
  the remedy; the lines below it carry per-attempt turn counts,
  stop reasons, and `write_output` call counts for the Actions log.
- **`message_end` stop reasons are logged** when they are neither
  `stop` nor `toolUse`. Suppressing every `message_end` hid the one
  field that distinguishes a model that chose to stop from one that
  was cut off.

## [1.2.0] - 2026-05-06

Composable review emphasis and sharper green-mirage discipline in
the review prompts. Documentation site restructured around the
Diataxis framework.

### Added

- **Composable emphasis modules**: new `review.emphasis_modules`
  config knob accepts a list of named modules from
  `momus/prompts/emphasis/`. Ships with `security`, `dead_code`,
  `quality_checklist`, and `test_quality`. Modules compose with
  free-form repo emphasis; unknown names fail validation at
  config load.
- **Diataxis-structured docs site**: new tutorial, how-to (8
  recipes), reference (5 pages), explanation (4 pages), and
  contributing (4 pages) quadrants under `docs/`. Root
  `CONTRIBUTING.md` points to the contributing quadrant.

### Changed

- **Phase 2 review prompt** now requires a verifying observation
  for every finding and uses an expanded green-mirage taxonomy.
- **Phase 3 verify prompt** drops findings whose grounding
  evidence does not survive verification, instead of softening
  them.
- **Docs site** rewritten: `docs/index.md` is now a trust pitch
  with quadrant wayfinding; `SETUP.md` is a thin pointer to the
  docs site; `docs/usage.md` removed (content migrated).

## [1.1.0] - 2026-05-05

Injection hardening release. Closes the prompt-injection attack
surface surfaced in the design doc at
`docs/design/2026-05-05-momus-injection-hardening.md`. Single PR,
twenty-plus atomic commits, full implementation plan at
`plans/2026-05-05-momus-injection-hardening-impl.md`.

### Added

- **Contained read-only tools**: new pi extension tools
  `read_repo`, `grep_repo`, `find_repo`, `ls_repo`. Each is cwd-
  contained: paths outside the worktree are rejected before the
  filesystem is touched. Replaces pi's built-in `read`, `grep`,
  `find`, `ls`, which can address absolute paths anywhere on the
  runner.
- **Default-deny env allowlist**: `momus/invoke_pi.py` now scrubs the
  process environment before spawning pi. Only a fixed set of
  variables (`HOME`, `PATH`, `TMPDIR`, `LANG`, `LC_*`, `NODE_OPTIONS`,
  `NODE_PATH`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`) is forwarded.
  Anything else, including `GITHUB_TOKEN` and `GITHUB_REPOSITORY`, is
  scrubbed. The escape hatch `MOMUS_PI_ENV_PASSTHROUGH=NAME1,NAME2`
  lets users opt extra variables back in.
- **Pydantic-validated findings.json**: `momus/findings_schema.py`
  declares the wire-format with `extra='forbid'` everywhere and
  length caps on every text field. Validation happens at publish-
  payload construction time, so prompt-injected shape drift fails
  closed instead of leaking to GitHub.
- **Publish-time redaction**: `momus/publish.py` runs
  `redact_for_publish` over every finding before posting. Credential-
  shaped strings (`ghp_`, `sk-`, `AKIA...`) and off-domain image URLs
  are scrubbed. Redaction holds across the GitHub 422-retry branches.
  Redaction now also covers the `id` and `category` fields on each
  finding (both LLM-emitted and length-capped but otherwise unrestricted)
  and the top-level `summary` rendered into the optional Check Run.
- **Adversarial corpus harness**: `tests/adversarial/` carries six
  attacker-goal fixtures plus a smoke case. Run via
  `pytest -m adversarial` (opt-in by default). Weekly cron at
  `.github/workflows/redteam-corpus.yml` (Monday 06:00 UTC) executes
  the corpus against the configured LLM and uploads results.
- **Phase-1 prompt placeholder**: `<<UNTRUSTED_PRIOR_THREADS_JSON>>`
  inlines prior-threads as fenced data instead of pointing the LLM
  at a file path.
- **Per-tool-call audit log**: `MOMUS_TOOLCALL_LOG` env var, when
  set, causes the readonly-tools extension to emit one JSON record
  per tool invocation. Used by the corpus harness to assert that no
  forbidden paths or commands were touched.
- **Per-PR LLM cost in the review footer**: each review now ends
  with `Cost: $X.YZ ... I in / O out tokens ... model`. The figure
  is summed from pi-ai's per-turn `usage.cost.total` (which it
  computes from its bundled per-Mtok pricing table) and rounded to
  whole cents. The BYO provider registration now pulls the model's
  real cost from pi-ai's registry by id; previously it hardcoded
  zero so cost computation produced $0.00 even when tokens flowed.
  Suppressed entirely when no tokens are observed (e.g. a phase
  aborted before its first request).

### Changed

- **POSSIBLY BREAKING**: pi-coding-agent is pinned to v0.72.1 via
  `package-lock.json`. Use `npm ci`, not `npm install`. Floating to
  `latest` is no longer supported because pi releases occasionally
  alter the tool-event schema, and the behavioral CI test
  (`test_pi_rejects_disallowed_tool`) is calibrated against 0.72.x.
- **POSSIBLY BREAKING**: `gh` is removed from the `bash_ro`
  allowlist. The LLM phases never invoked it; `gh` runs from Python
  pre-pi (`fetch_priors.py`). If your fork relies on the LLM calling
  `gh`, restore it explicitly in your local extension.
- **POSSIBLY BREAKING**: env vars not on the W3 allowlist no longer
  reach pi. Audit any provider-specific or fork-specific variables
  and add them to `MOMUS_PI_ENV_PASSTHROUGH` if needed.
- **TRANSPARENT**: phase 1 prompt now inlines prior-threads as
  fenced data instead of as an unreadable path reference. Existing
  callers see no API change.
- **TRANSPARENT**: phase 2 example finding uses `"calibration":
  {"would_human_block": "...", "rationale": "..."}` to match the
  Pydantic schema (was a string in v1.0.0; the schema was already
  dict-typed but the example was inconsistent).
- **POSSIBLY BREAKING**: this repo's self-review workflow
  (`.github/workflows/momus.yml`) is restricted to `pull_request`
  opened/reopened plus manual `workflow_dispatch`. Previously it
  also fired on `synchronize` and `/ai-review` comments; both
  were dropped because every push during a fix cycle queued a
  fresh review and burned LLM provider quota fast. Consumers of
  `axiomantic/momus` as a shared action are unaffected: the
  trigger choice belongs to the caller workflow, see SETUP.md.

### Fixed

- Calibration field rendered the wrong type in the prompt example
  (string vs schema-required dict). Real LLM output following the
  example would have failed Pydantic validation. Discovered during
  W5 fact-check.

### Security

- Closes G3 (credential exfil via prior-thread instruction): the
  `*_repo` tools cannot read `/proc/self/environ`, the env allowlist
  scrubs unrelated variables, and `bash_ro`'s argv allowlist excludes
  shell builtins like `env`/`printenv`/`set`.
- Reduces ASR for G1, G2a, G2b, G2c, G4 via fenced-data input
  framing, the phase-3 verify pass, and schema-gated output. ASR
  numbers tracked weekly via `redteam-corpus.yml`.

## [1.0.0] - 2026-04-26

Initial release. See git log before this commit for details.
