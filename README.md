# Momus

[![CI](https://github.com/axiomantic/momus/actions/workflows/ci.yml/badge.svg)](https://github.com/axiomantic/momus/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/axiomantic/momus?color=blue)](https://github.com/axiomantic/momus/releases)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Thorough, low-noise AI pull-request review as a GitHub Action.

Momus is designed to catch genuine bugs, regressions, and design issues while filtering out the noise and hallucinations typical of single-shot LLM reviewers. It is provider-agnostic and works with OpenRouter, DeepSeek, Anthropic Claude, OpenAI, Google Gemini, Amazon Bedrock, or any OpenAI-compatible API.

---

## How it works

Momus structures review as a four-phase pipeline:

```
+--------------------------------------------------------------------+
|  1. Plan (LLM)     Classify prior thread feedback & build plan     |
+--------------------------------------------------------------------+
                                  |
+--------------------------------------------------------------------+
|  2. Review (LLM)   Inspect diff & repo using sandboxed tools       |
+--------------------------------------------------------------------+
                                  |
+--------------------------------------------------------------------+
|  3. Verify (LLM)   Audit candidate findings; prune false alarms    |
+--------------------------------------------------------------------+
                                  |
+--------------------------------------------------------------------+
|  4. Post (Python)  Publish single GitHub Review with inline diffs  |
+--------------------------------------------------------------------+
```

1. **Plan** (LLM): On re-reviews, Momus reads unresolved review comments, classifies developer responses, and prepares a focused review plan. (Skipped on initial review).
2. **Review** (LLM): Explores the diff and repository using sandboxed read-only tools (`read_repo`, `grep_repo`, `find_repo`, `ls_repo`, `bash_ro`) to find functional defects, edge cases, and security issues.
3. **Verify** (LLM): A dedicated verification pass audits every candidate finding from Phase 2 against the codebase. It drops hallucinations, demotes over-inflated severities, and consolidates duplicate items. Phase 3 cannot invent new findings.
4. **Post** (Python): A deterministic publisher renders verified findings into a single structured GitHub Review (APPROVE, REQUEST_CHANGES, or COMMENT) with inline code comments and PR status checks.

---

## Quick start

Add `.github/workflows/momus.yml` to your repository:

```yaml
name: Momus Code Review

on:
  pull_request:
    types: [opened, reopened]
  workflow_dispatch:
    inputs:
      pr_number:
        description: "PR number to review"
        required: true

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: axiomantic/momus@v1
        with:
          pr_number: ${{ github.event.pull_request.number || github.event.inputs.pr_number }}
          event: ${{ github.event_name }}
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
          LLM_BASE_URL: https://openrouter.ai/api/v1
          LLM_MODEL: deepseek/deepseek-v4-flash
```

For complete setup instructions (including GitHub App token configuration so APPROVE reviews post with full approval authority), see [SETUP.md](SETUP.md) and the [Quickstart Tutorial](https://axiomantic.github.io/momus/latest/tutorial/first-review/).

---

## Sandboxed tool harness

Momus executes LLM phases via `@mariozechner/pi-coding-agent` with custom sandboxed tool containment (`momus/extensions/readonly-tools.ts`):

- `read_repo`, `grep_repo`, `find_repo`, `ls_repo`: path-checked, worktree-contained file inspection tools.
- `bash_ro`: sandboxed shell with allowlisted binaries (`git`, `cat`, `head`, `tail`, `wc`, `find`, `rg`, `ls`). Rejects shell metacharacters and enforces worktree-contained paths.
- `write_output`: restricted strictly to writing outputs inside `.momus/outputs/` with realpath containment.

Standard unrestricted tools (`write`, `edit`, interactive `bash`) are excluded to prevent prompt-injection escapes.

---

## Environment scoping

The bot executes the LLM runtime in a process with a default-deny environment allowlist: only a minimal set of variables (`HOME`, `PATH`, `TMPDIR`, `LANG`, `LC_*`, `NODE_OPTIONS`, `NODE_PATH`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`) is forwarded. All other runner variables, including `GITHUB_TOKEN` and repository secrets, are scrubbed before LLM phases begin.

If your setup requires passing custom environment variables into the runtime, specify `MOMUS_PI_ENV_PASSTHROUGH=NAME1,NAME2` (comma-separated list of names) on the workflow job. This is an explicit opt-in escape hatch; ensure you do not forward sensitive credentials.

---

## Configuration

Customize Momus by committing a `.momus.yaml` file to your repository root. Full configuration options are documented in [`momus/config-defaults.yaml`](momus/config-defaults.yaml):

```yaml
review:
  emit_nits: false               # Omit minor style nits
  max_findings: 20               # Cap total findings per review
  emphasis_modules:              # Composable emphasis packs
    - security
    - quality_checklist

verify:
  enabled: true                  # Keep the two-pass verification safety net

provider:
  model: deepseek/deepseek-v4-flash
  base_url: https://openrouter.ai/api/v1
```

---

## Provider configuration

Configure the LLM connection on your GitHub Actions workflow job:

- `LLM_API_KEY`: API key secret for your provider.
- `LLM_BASE_URL`: Endpoint base URL (e.g. `https://openrouter.ai/api/v1` or `https://api.deepseek.com/v1`).
- `LLM_MODEL`: Model identifier slug (e.g. `deepseek/deepseek-v4-flash`, `openai/gpt-4o-mini`, `anthropic/claude-sonnet-4-6`).

---

## License

This project is licensed under the [MIT License](LICENSE).
