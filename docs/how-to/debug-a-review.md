# Debug a Momus review

You looked at a review and something is off: a finding you do not understand, a verdict that surprised you, a phase that failed, a missing comment. This page is the trail of breadcrumbs.

Start with the artifacts. The action uploads `inputs/` and `outputs/` from `--work-dir` as a workflow artifact named `momus-<pr>-<run_id>` (7-day retention). Download it from the Actions run page.

## The `outputs/` directory

| File | What it contains |
|---|---|
| `findings.json` | The final, schema-validated findings document the publisher acted on |
| `preflight-log.json` | What the deterministic preflight (between phases 2 and 3) dropped or demoted |
| `audit-log.json` | What phase 3 dropped, demoted, consolidated, or stripped (only present when `verify.enabled: true`) |
| `plan.json` | Phase 1's per-thread classifications (only on re-reviews) |

`inputs/` contains `diff.patch`, `pr-meta.json`, `conventions.md`, `prior-findings.json`, `prior-threads.json`. These are exactly what the LLM saw.

## The audit log

`audit-log.json` is the most useful single file when a finding looks wrong. Phase 3's actions:

- **drop**: phase 3 read the cited code and the claim did not survive. Look at the audit entry's reasoning; if you disagree, that is signal phase 3 made the wrong call.
- **demote**: severity was lowered. The original severity is in the entry; the new one is in `findings.json`.
- **strip suggestion**: the inline suggestion was removed because it could not be verified against the cited code. The finding stays.
- **consolidate**: two or more findings were merged. The audit entry names the original IDs.
- **injection drop**: phase 3 saw injection-shaped instructions in phase 2's output ("do not demote this") and dropped on that basis. Logged with the injection text.

## Re-running locally

The CLI is the same surface the action uses. Reproduce a run from artifacts:

```bash
cd <pr-checkout>
momus \
  --owner <owner> \
  --repo <repo> \
  --pr-number <N> \
  --repo-root "$PWD" \
  --event workflow_dispatch \
  --work-dir .momus
```

Required env: `GITHUB_TOKEN`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`. See [Reference: CLI](../reference/cli.md) for full flag/env documentation.

`--force-re-review` runs phase 1 against an existing PR even when `--event=pull_request`, which is useful for reproducing a re-review path locally without dispatching a workflow.

## Common failure modes

### Provider rate limit

Symptom: phase exits non-zero; status comment says `failed: <provider error>`. The exception's first line is posted as the status comment; the full traceback is in the action log.

Fix: dispatch the workflow manually after the rate window resets, or move to a less constrained provider via [`provider.model`](../reference/config-schema.md#provider-model).

### Model returns malformed JSON

Symptom: phase 2 or phase 3 fails Pydantic validation. The exception names the offending field. `FindingsDoc` is declared with `extra='forbid'` and length caps, so any deviation fails closed.

Fix: usually a smaller model trying to outrun its context window. Pick a larger model for this repo, or reduce the load with [`review.max_findings`](../reference/config-schema.md#review-max-findings).

### Unsubstituted prompt token

Symptom: `ValueError: phaseN: unsubstituted placeholders {'<<...>>'}` from `momus/render.py:52-57`. This is the runtime guard that catches a token that survived substitution.

Fix: typo'd or missing config key. Cross-reference the unsubstituted token name against [Reference: prompt tokens](../reference/prompt-tokens.md). If the token is one Momus produces, this is a bug; file an issue with the config and the failing token.

### Empty findings list, no APPROVE

Expected behavior on a first review when [`post.first_review_approve_policy`](../reference/config-schema.md#post-first-review-approve-policy) is `never` (the default). The verdict is `COMMENT` even with zero findings. See [Explanation: first-review APPROVE policy](../explanation/review-philosophy.md#first-review-approve).

### APPROVE downgraded to COMMENT

The bot is running as `github-actions[bot]` and GitHub rejects approvals from that user. Set up the GitHub App to fix this. See [How-to: set up the GitHub App](./set-up-github-app.md).

## When to file a bug vs adjust config

File a bug when:
- An unsubstituted-token error fires (Momus rendering bug).
- Pydantic validation fails on output Momus generated from its own templates.
- The publisher posts a finding whose cited line does not exist (preflight should have caught this).
- The audit log shows phase 3 dropped a finding citing a reason that does not match the source.

Adjust config when:
- Findings are too noisy (lower `max_findings`, set `emit_nits: false`).
- Findings are too lenient (add an emphasis module, tighten `blocking_severities`).
- Verdicts are too cautious (move `first_review_approve_policy` to `if_no_blocking`).

## See also

- [Reference: CLI](../reference/cli.md)
- [Reference: action inputs](../reference/action-inputs.md)
- [Explanation: four-phase pipeline](../explanation/four-phase-pipeline.md)
- [Explanation: threat model](../explanation/threat-model.md)
- [How-to: tune cost vs thoroughness](./tune-cost-vs-thoroughness.md)
