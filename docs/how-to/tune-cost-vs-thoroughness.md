# Tune cost vs thoroughness

Each Momus run costs LLM tokens. A typical PR review is ~30k–80k input + ~3k output, but a sprawling diff with deep call-graph walks can push higher. You have several levers; each trades cost for coverage. None is universally correct.

This page lays out the levers and when each is appropriate.

## Cap findings

[`review.max_findings`](../reference/config-schema.md#review-max-findings) is a hard ceiling enforced at the publisher.

```yaml
review:
  max_findings: 20
```

Lower caps push the model to consolidate redundant findings rather than emit them all. The cap does not reduce phase 2's cost (the model still walks the diff), but it does shrink the JSON output and reduces phase 3's verify load.

Use when: PRs in this repo tend to surface long lists of similar nits; you want signal density rather than volume.

## Disable nits entirely

[`review.emit_nits`](../reference/config-schema.md#review-emit-nits) controls whether the severity scale presented to the model includes `nit` at all.

```yaml
review:
  emit_nits: false
```

This is stronger than just removing `nit` from `blocking_severities`: when `emit_nits: false`, the prompt instructs the model not to emit nits in the first place. Saves tokens; eliminates the noisiest class of finding outright.

Use when: nits are not actionable in this repo; reviewers find them clutter; CI/lint already covers style.

## Phase 1 (auto-skipped on first review)

You cannot turn phase 1 off via config. It runs only on re-reviews (`event: issue_comment` or `workflow_dispatch`); first reviews of a PR have no prior threads to classify, so the orchestrator skips it. See [Reference: action inputs](../reference/action-inputs.md#event) and [Phase 1: Plan](../explanation/four-phase-pipeline.md#phase-1-plan).

If you want to avoid phase 1's cost on a re-review, the only option is not to trigger a re-review. Re-reviews on push are intentionally not wired up; they only run on manual dispatch or trigger comment.

## Disable the verify phase

[`verify.enabled`](../reference/config-schema.md#verify-enabled) gates phase 3.

```yaml
verify:
  enabled: false
```

This is the single largest cost cut on the LLM side: you eliminate one full LLM pass per review. **Read the tradeoff before flipping this.**

Phase 3 is what catches phase 2's hallucinations. Without it, every plausible-sounding-but-wrong finding phase 2 emits goes straight to GitHub. The deterministic preflight (file-exists, line-in-range, on-hunk citation) still runs and drops structural errors, but semantic errors (a function that is not actually racy, a check that is not actually missing) cannot be caught.

See [Verify cannot promote](../explanation/review-philosophy.md#verify-cannot-promote) for why phase 3 is shaped the way it is.

Use when: cost is a hard constraint and you accept the noise floor will rise. Re-enable as soon as you can.

## Choose a smaller model

The cheapest lever per quality unit. [`provider.model`](../reference/config-schema.md#provider-model) overrides the workflow default per repo.

```yaml
provider:
  model: deepseek/deepseek-v4-pro
```

A faster, cheaper model on a low-traffic repo will produce shallower findings but at a fraction of the cost. A flagship model on a high-stakes repo (security-critical, payments, infra) is the opposite trade.

Use when: you have repos with different cost profiles. Set the workflow default to the most common case; override per repo where the trade differs.

## Combining levers

A reasonable cost-conscious profile for a low-stakes repo:

```yaml
review:
  emit_nits: false
  max_findings: 15
verify:
  enabled: true   # keep the safety net
provider:
  model: deepseek/deepseek-v4-pro
```

A maximum-thoroughness profile for a security-critical repo:

```yaml
review:
  emit_nits: true
  max_findings: 50
  emphasis_modules: [security, quality_checklist]
verify:
  enabled: true
provider:
  model: anthropic/claude-sonnet-4-6
```

## See also

- [Reference: `review` config group](../reference/config-schema.md#review)
- [Reference: `verify.enabled`](../reference/config-schema.md#verify-enabled)
- [Reference: `provider.model`](../reference/config-schema.md#provider-model)
- [Explanation: review philosophy](../explanation/review-philosophy.md)
- [How-to: configure emphasis](./configure-emphasis.md)
