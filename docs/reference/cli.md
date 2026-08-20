# CLI

The `momus` entry point is the orchestrator. The GitHub Action invokes it from inside the action checkout (see [`reference/action-inputs.md`](./action-inputs.md)); local invocation is supported for development and reproducing a failed run from artifacts.

Available since 1.0.

## `momus`

Run the four-phase pipeline against a checked-out PR head. Reads `.momus.yaml` from `--repo-root`, fetches PR metadata via `gh pr view`, runs phases 1-3 (LLM) and phase 4 (pure-Python publish), and posts a GitHub Review.

### Usage

```text
momus \
  --owner OWNER \
  --repo REPO \
  --pr-number N \
  --repo-root PATH \
  --event {pull_request|issue_comment|workflow_dispatch} \
  [--work-dir DIR] \
  [--force-re-review]
```

### Flags

- `--owner` (required): GitHub repo owner (organization or user).
- `--repo` (required): GitHub repo name (without owner).
- `--pr-number` (required, int): pull request number to review.
- `--repo-root` (required): path to the checked-out PR head. Pi runs with this as cwd.
- `--event` (required, enum: `pull_request` | `issue_comment` | `workflow_dispatch`): triggering event name. `pull_request` is treated as a first review (phase 1 skipped); the others are re-reviews and trigger prior-thread fetch.
- `--work-dir` (default: `.momus`): working directory for `inputs/` and `outputs/`. Must be inside `--repo-root`; pi addresses these via a relative path. A `--work-dir` outside `--repo-root` is rejected with `SystemExit` at startup.
- `--force-re-review` (flag): treat the run as a re-review even when `--event=pull_request`. Useful for re-running phase 1 against an existing PR locally.

### Environment

Required:

- `GITHUB_TOKEN`: posts the Review, Check Run, and status comments. Needs `pull_request: write` and (for [`checks.enabled`](./config-schema.md#checks-enabled)) `checks: write`.
- `LLM_API_KEY`: provider key for pi.
- `LLM_BASE_URL`: provider endpoint. Per-repo override: [`provider.base_url`](./config-schema.md#provider-base-url).
- `LLM_MODEL`: model slug. Per-repo override: [`provider.model`](./config-schema.md#provider-model).

Optional:

- `GITHUB_RUN_URL`: linked into status comments and the review footer. Auto-composed from `GITHUB_SERVER_URL` / `GITHUB_REPOSITORY` / `GITHUB_RUN_ID` when unset.
- `MOMUS_PI_ENV_PASSTHROUGH=NAME1,NAME2`: comma-separated env-var allowlist passed through to pi. Default-deny otherwise. Names must match `^[A-Z][A-Z0-9_]*$`. See `momus/invoke_pi.py` and the README opt-in escape hatch warning.
- `MOMUS_TOOLCALL_LOG`: when set, the readonly-tools extension emits one JSON record per tool invocation. Used by the adversarial corpus harness. Available since 1.1.0.
- `MOMUS_PI_MAX_TOKENS`: per-message output token cap for the `byo` provider. Defaults to the cap pi-ai's bundled model registry records for `LLM_MODEL`, or `32768` for a model the registry does not know. Set this only to hold the cap *down*. The budget covers reasoning tokens as well as visible output, so a cap sized only for the answer starves the `write_output` tool call and the phase ends with `stopReason=length` having written nothing. Must be a positive integer; an unparseable value fails at extension load rather than falling back to the default.
- `MOMUS_LOG_SNIPPET_CHARS`: width of the per-event summaries momus writes to stderr. Default `300`. Raise it when reproducing a phase failure whose cause is inside a clipped reasoning block.

`MOMUS_WORK_DIR`, `MOMUS_PI_ENV_PASSTHROUGH`, and `MOMUS_TOOLCALL_LOG` are reserved and never forwarded to pi via the passthrough mechanism.

### Exit codes

- `0`: review posted (verdict `APPROVE`, `REQUEST_CHANGES`, or `COMMENT`).
- non-zero: any failure. The first line of the exception message is posted as a `failed` status comment on the PR; the full traceback stays in the action log.

### Output artifacts

Written under `--work-dir`:

- `inputs/diff.patch`, `inputs/pr-meta.json`, `inputs/conventions.md`, `inputs/prior-threads.json`, `inputs/prior-findings.json`.
- `outputs/findings.json` (final, schema-validated), `outputs/preflight-log.json`, `outputs/audit-log.json` (when phase 3 runs).
- `outputs/<phase>-attempt<N>-last-message.txt`: written only when a phase ends without its expected output. Carries that attempt's final assistant message in full, with its stop reason, turn count, and `write_output` call count. It is uploaded with the rest of `outputs/` even though the run failed, so analysis the model completed before it died is recoverable without mining the stderr log, whose per-event summaries are clipped to `MOMUS_LOG_SNIPPET_CHARS`.

The action uploads `inputs/` and `outputs/` as a workflow artifact (`momus-<pr>-<run_id>`, 7-day retention).

### Example

```bash
momus \
  --owner elijahr \
  --repo lockfreequeues \
  --pr-number 42 \
  --repo-root "$PWD" \
  --event workflow_dispatch \
  --work-dir .momus
```
