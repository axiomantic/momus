# Prompt tokens

Momus's phase 1 / 2 / 3 prompts are templated. Tokens of the form `<<TOKEN>>` are substituted at render time from config keys, env vars, or computed values, and the substituted prompt is what the LLM sees. Token detection regex: `r"<<([A-Z_][A-Z0-9_]*)>>"` (`momus/render.py` line 229).

Failed substitution is a hard error. If any `<<TOKEN>>` survives substitution, `_find_unsubstituted` raises `ValueError(f"{phase}: unsubstituted placeholders {leftover}. ...")` (`momus/render.py` lines 52-57). Adding a new token requires wiring it in `_substitutions`; an orphan token in a prompt file aborts the run.

Phase mapping: `phase1` → `prompts/phase1-plan.md`, `phase2` → `prompts/phase2-review.md`, `phase3` → `prompts/phase3-verify.md` (`momus/render.py` line 93).

## `<<WORK_DIR>>` {#token-work-dir}

- Substituted from: `work_dir_rel` arg (the `work_dir` path relative to the repo root).
- Used by: phase1, phase2, phase3.
- Available since 1.0.

Path prefix the model uses to reference `inputs/` and `outputs/` from inside the running pi cwd (which is the repo root).

Example expansion when `work_dir: .momus`:

```text
Read <<WORK_DIR>>/inputs/diff.patch
```

becomes:

```text
Read .momus/inputs/diff.patch
```

## `<<REPO_EMPHASIS>>` {#token-repo-emphasis}

- Substituted from: [`review.emphasis_modules`](./config-schema.md#review-emphasis-modules) + [`review.repo_emphasis`](./config-schema.md#review-repo-emphasis), composed by `_compose_repo_emphasis` (`momus/render.py` lines 204-223).
- Used by: phase2.
- Available since 1.0 (`repo_emphasis` only); modules added [next].

Concatenated module bodies followed by the free-form `repo_emphasis` string, joined by blank lines. Falls back to `(none configured for this repo)` when both are empty. See [`reference/emphasis-modules.md`](./emphasis-modules.md) for module bodies.

## `<<CALIBRATION_PROCEDURE>>` {#token-calibration-procedure}

- Substituted from: [`review.require_calibration`](./config-schema.md#review-require-calibration). Boilerplate paragraph when `true`; empty string when `false`.
- Used by: phase2.
- Available since 1.0.

The calibration step the model must run before emitting any blocking finding ("would a human reviewer genuinely block this PR over this?").

## `<<BLOCKING_SEVERITIES>>` {#token-blocking-severities}

- Substituted from: [`review.blocking_severities`](./config-schema.md#review-blocking-severities), formatted as a backticked, comma-joined list.
- Used by: phase2, phase3.
- Available since 1.0.

Example expansion with the default `[critical, high, medium]`:

```text
`critical`, `high`, `medium`
```

## `<<NIT_SCALE_LINE>>` {#token-nit-scale-line}

- Substituted from: [`review.emit_nits`](./config-schema.md#review-emit-nits). The literal severity-scale bullet for `nit` when `true`; empty string when `false`.
- Used by: phase2.
- Available since 1.0.

When `emit_nits=true` the line is:

```markdown
- **nit** (non-blocking): style, naming preference, minor readability.
```

## `<<EMIT_NITS_ANTIPATTERN>>` {#token-emit-nits-antipattern}

- Substituted from: [`review.emit_nits`](./config-schema.md#review-emit-nits). Anti-pattern bullet when `false`; empty string when `true`.
- Used by: phase2.
- Available since 1.0.

When `emit_nits=false` the line is:

```text
Do not emit `nit`-severity findings; this repo has nits disabled.
```

## `<<MAX_FINDINGS>>` {#token-max-findings}

- Substituted from: [`review.max_findings`](./config-schema.md#review-max-findings) as a string.
- Used by: phase2.
- Available since 1.0.

Inserted into the Output section as the cap the model is told to consolidate under (e.g., `50`).

## `<<ID_EXAMPLE>>` {#token-id-example}

- Substituted from: computed from [`review.run_id_scheme`](./config-schema.md#review-run-id-scheme) and the current run id (`momus/render.py` lines 145-157).
- Used by: phase2.
- Available since 1.0.

Example finding ID inserted into the JSON template the model fills in. Shapes:

- `alpha`: `BOT-A1`
- `numeric`: `BOT-1-1`
- `off`: `BOT-1`

## `<<CALIBRATION_FIELD>>` {#token-calibration-field}

- Substituted from: [`review.require_calibration`](./config-schema.md#review-require-calibration). JSON fragment for the `calibration` field when `true`; empty string when `false`.
- Used by: phase2.
- Available since 1.0.

Optional `"calibration": {...}` field appended to the example finding in the schema description so the model emits it under the right key.

## `<<NOTEWORTHY_FIELD>>` {#token-noteworthy-field}

- Substituted from: [`review.noteworthy_max`](./config-schema.md#review-noteworthy-max). JSON fragment when `> 0`; empty string when `0`.
- Used by: phase2.
- Available since 1.0.

Optional `"noteworthy": [...]` array appended to the schema example. Disabled when the cap is zero so the prompt does not solicit a section it would then drop.

## `<<APPROVE_RULE>>` {#token-approve-rule}

- Substituted from: [`post.first_review_approve_policy`](./config-schema.md#post-first-review-approve-policy). Branches on `never` vs the two opt-in policies (`momus/render.py` lines 175-185).
- Used by: phase2.
- Available since 1.0.

Inline conjunct of the `APPROVE` verdict rule the model applies when deciding the verdict.

## `<<FIRST_REVIEW_APPROVE_RULE>>` {#token-first-review-approve-rule}

- Substituted from: [`post.first_review_approve_policy`](./config-schema.md#post-first-review-approve-policy). Branches on `never` / `if_no_findings` / `if_no_blocking`.
- Used by: phase2.
- Available since 1.0.

Sentence describing first-review APPROVE behavior. Visible to the model so it does not waste tokens deliberating about a verdict the publisher would override.

## `<<ID_SCHEME>>` {#token-id-scheme}

- Substituted from: [`review.run_id_scheme`](./config-schema.md#review-run-id-scheme). Sentence built around the chosen scheme.
- Used by: phase2.
- Available since 1.0.

Tells the model how to generate finding IDs (`BOT-A1`, `BOT-A2`, ... vs `BOT-1-1`, `BOT-1-2`, ... vs bare `BOT-1`).

## `<<UNTRUSTED_PRIOR_THREADS_JSON>>` {#token-untrusted-prior-threads-json}

- Substituted from: contents of `<<WORK_DIR>>/inputs/prior-threads.json`, wrapped in `BEGIN_UNTRUSTED_PRIOR_THREADS_JSON` / `END_UNTRUSTED_PRIOR_THREADS_JSON` fences. Fence markers are UUID-suffixed if the body itself contains a fence (`momus/render.py` lines 17-19, 61-89).
- Used by: phase1.
- Available since 1.1.0.

Phase-1-only fenced data block carrying prior-thread reply text. Documented to the LLM as DATA, not instructions; reply bodies are attacker-influenced and the fenced framing is what enforces that boundary. See [Threat model](../explanation/threat-model.md).
