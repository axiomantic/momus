# Usage

Momus runs as a GitHub Action that posts a single, structured PR review
through a multi-phase LLM pipeline.

For installation -- including the recommended GitHub App configuration
that lets the bot post real `APPROVE` reviews -- see
[`SETUP.md`](https://github.com/axiomantic/momus/blob/main/SETUP.md) in
the repository.

## Triggers

- `pull_request` opened / reopened: full review on first PR open.
- `workflow_dispatch`: manual re-review (provide `pr_number` input).

Re-reviews on push are intentionally NOT wired up: each run consumes LLM
provider quota, and pushing in rapid succession during fix cycles will
burn that quota fast. To re-review after pushing fixes, dispatch the
workflow manually.

## Provider configuration

Set these env vars on the workflow job:

- `LLM_API_KEY` -- API key for whichever provider.
- `LLM_BASE_URL` -- e.g. `https://openrouter.ai/api/v1`.
- `LLM_MODEL` -- model slug (e.g. `deepseek/deepseek-v4-pro`).

The runner translates these into pi's `--provider`, `--model`,
`--base-url`, `--api-key-env` arguments. The bot itself reads no
provider-specific env vars.

## Per-repo configuration

`config-defaults.yaml` documents every knob. A target repo overrides any
of these by committing `.momus.yaml` at its repo root.

## Environment scoping

The bot runs pi in a process with a default-deny env allowlist. Only a
small set of variables (`HOME`, `PATH`, `TMPDIR`, `LANG`, `LC_*`,
`NODE_OPTIONS`, `NODE_PATH`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`)
is forwarded to the pi child. Anything else on the runner environment,
including `GITHUB_TOKEN` and `GITHUB_REPOSITORY`, is scrubbed before the
LLM phases start.

If your fork or extension needs a custom env var inside pi, set
`MOMUS_PI_ENV_PASSTHROUGH=NAME1,NAME2` on the workflow job. Each name is
added to the allowlist and forwarded as-is. This is an opt-in escape
hatch; review what you pass through. It bypasses the hardening that
prevents prompt-injected pi runs from reading credentials from sibling
env vars.
