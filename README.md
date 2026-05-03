# momus

Multi-phase AI pull-request review as a GitHub Action.

Provider-agnostic: works with any OpenAI-compatible LLM (OpenRouter,
Anthropic, OpenAI, Bedrock, ...) configured at the deployment layer.
The bot itself never names a provider.

Status: under construction. Pilot on `elijahr/lockfreequeues` PR #25.

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
runs each LLM phase. We supply a custom extension
(`extensions/readonly-tools.ts`) that exposes:

- `bash_ro` — shell with allowlisted binaries (`git`, `gh`, `cat`,
  `head`, `tail`, `wc`, `find`, `rg`, `ls`); rejects shell metacharacters
- `write_output` — restricted to writing under `outputs/`

Plus pi's built-in `read`, `grep`, `find`, `ls` (read-only). Built-in
`bash`, `write`, `edit` are excluded via `--tools` allowlist.

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
- `issue_comment` body starting with `/ai-review` — re-review with
  prior-findings continuity
- `workflow_dispatch` — manual run
