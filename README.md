# momus

Thorough AI code review as a GitHub Action.

Provider-agnostic: works with any OpenAI-compatible LLM (OpenRouter, Anthropic, OpenAI, Bedrock, ...). Highly customizable.

> Momus (/ˈmoʊməs/): The ancient Greek personification of relentless scrutiny. He was ultimately banished from Mount Olympus for his pedantic criticism of trivial defects in the gods' creations rather than assessing their functional intent.

## Quick start

See **[SETUP.md](SETUP.md)** for the full installation walkthrough,
including the recommended GitHub App configuration that lets the bot post
real APPROVE reviews (without it, APPROVE downgrades to COMMENT).

Status: under construction. Pilot on `elijahr/lockfreequeues`.

## Phases

1. **Plan** — fetch prior bot reviews, classify each prior finding into
   `PENDING / DECLINED / PARTIAL_AGREEMENT / ALTERNATIVE_PROPOSED /
   ANSWERED`, produce a checklist for phase 2. Skipped on first review.
2. **Review** — read the diff, investigate context with `Read` / `Grep` /
   sandboxed read-only `Bash`, produce structured findings with severity,
   category, and inline-comment-ready suggestions.
3. **Verify** — audit phase 2's findings: drop false positives, demote
   over-severe findings, strip invalid suggestions, consolidate
   duplicates. Cannot promote or add findings.
4. **Post** — deterministic Python publisher. Renders findings into a
   single GitHub Review object (APPROVE / REQUEST_CHANGES / COMMENT)
   with all inline comments attached. Posts thread replies on prior
   items, resolves fixed-prior threads via GraphQL.

Phases 1, 2, 3 are LLM calls. Phase 4 is pure code.

## Harness

[pi](https://github.com/badlogic/pi-mono) (`@mariozechner/pi-coding-agent`)
runs each LLM phase. The pi version is pinned via `package-lock.json`;
install with `npm ci` rather than `npm install` to honor the pin.

We supply a custom extension (`extensions/readonly-tools.ts`) that
exposes:

- `bash_ro`: shell with allowlisted binaries (`git`, `cat`, `head`,
  `tail`, `wc`, `find`, `rg`, `ls`); rejects shell metacharacters and
  walks `git` argv to keep paths inside the worktree. `gh` is invoked
  from Python pre-pi (`fetch_priors.py`); the LLM phases never need
  it, so it was removed from the allowlist in v1.1.0.
- `write_output`: restricted to writing under `outputs/`, with realpath
  containment so symlink swaps cannot redirect writes.
- `read_repo` / `grep_repo` / `find_repo` / `ls_repo`: cwd-contained
  replacements for pi's built-in read/grep/find/ls. Pi's built-ins are
  excluded via the `--tools` allowlist so the LLM cannot escape the
  worktree.

Built-in `bash`, `write`, `edit` are also excluded via `--tools`.

## Environment scoping

The bot runs pi in a process with a **default-deny env allowlist**:
only a small set of variables (`HOME`, `PATH`, `TMPDIR`, `LANG`,
`LC_*`, `NODE_OPTIONS`, `NODE_PATH`, `LLM_BASE_URL`, `LLM_MODEL`,
`LLM_API_KEY`) is forwarded to the pi child. Anything else on the
runner environment — including `GITHUB_TOKEN` and `GITHUB_REPOSITORY` —
is scrubbed before the LLM phases start.

If your fork or extension needs a custom env var inside pi, set
`MOMUS_PI_ENV_PASSTHROUGH=NAME1,NAME2` on the workflow job. Each name
is added to the allowlist and forwarded as-is. **This is an opt-in
escape hatch; review what you pass through.** It bypasses the hardening
that prevents prompt-injected pi runs from reading credentials from
sibling env vars.

## Configuration

`config-defaults.yaml` documents every knob. A target repo overrides any
of these by committing `.momus.yaml` at its repo root.

## Provider config (deployment layer)

Set these env vars on the workflow job:

- `LLM_API_KEY` — API key for whichever provider
- `LLM_BASE_URL` — e.g. `https://openrouter.ai/api/v1`
- `LLM_MODEL` — model slug (e.g. `deepseek/deepseek-v4-pro`)

The runner translates these into pi's `--provider`, `--model`,
`--base-url`, `--api-key-env` arguments. The bot itself reads no
provider-specific env vars.

## Triggers

- `pull_request` opened / synchronize / reopened — full review
- `issue_comment` body starting with the configured trigger command
  (default `/ai-review`) OR containing the configured @-mention (e.g.
  `@your-org-momus[bot]`) — re-review with prior-findings continuity
- `workflow_dispatch` — manual run

Both `trigger_command` and `trigger_mention` are inputs on the reusable
workflow; see [SETUP.md](SETUP.md#3-add-the-workflow).
