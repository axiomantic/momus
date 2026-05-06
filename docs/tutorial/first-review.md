# Your first review

This walks you from an empty repo to a real momus review on a real pull
request. Plan on fifteen minutes if you already have an LLM provider
account, twenty if you need to make one.

By the end you will have:

- A workflow that runs momus on every PR.
- An API key wired in as a repo secret.
- A test PR that produced a real review object.
- A `.momus.yaml` that customizes one thing, so you have seen the shape
  of the config.

This is the tutorial. It is opinionated. Read [How-to](../how-to/configure-blocking-severities.md)
for recipes you can mix and match; read [Reference](../reference/config-schema.md)
for the full surface. Read this for a working setup you can build on.

## Prerequisites

You need three things:

1. **A GitHub repository** you control. A scratch repo is fine. Forks of
   public projects work; you do need write access to push branches and
   open PRs.
2. **An LLM provider account.** This tutorial uses
   [OpenRouter](https://openrouter.ai) because it is the lowest-friction:
   one signup, one key, access to most major models. Momus is
   provider-agnostic and works with any OpenAI-compatible endpoint
   (OpenAI, Anthropic, Bedrock, self-hosted). For the full provider
   matrix, see [How-to: Use a different LLM provider](../how-to/use-different-llm-provider.md#per-repo-override).
3. **An OpenRouter API key with credit on it.** Five dollars is plenty for
   the tutorial. Reviews typically cost a few cents each on
   `deepseek/deepseek-v4-pro`. Heavier models cost more.

That is it. You do not need to install anything locally. Momus runs
entirely inside GitHub Actions.

## Step 1: Add the workflow

Create `.github/workflows/momus.yml` in your repo with this content:

```yaml
name: Momus Code Review

on:
  pull_request:
    types: [opened, reopened]
  workflow_dispatch:
    inputs:
      pr_number:
        description: "PR number to re-review"
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
          LLM_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
          LLM_BASE_URL: https://openrouter.ai/api/v1
          LLM_MODEL: deepseek/deepseek-v4-pro
```

Commit this to your default branch. Momus only triggers on pull requests
once the workflow file is on the branch the PR is targeting.

A few notes for later, when you want to understand what you just pasted.
The `permissions` block is the minimum momus needs: read the repo,
post review comments. The `pr_number` and `event` inputs are required;
they are documented at [Reference / Action inputs](../reference/action-inputs.md).
The `LLM_*` env vars are the provider contract; the bot itself reads no
provider-specific configuration, only those three variables.

Re-reviews on push are intentionally not wired up. Each run consumes
provider quota, and rapid pushes during fix cycles will burn that quota
fast. The `workflow_dispatch` block lets you re-review manually when you
want a fresh pass.

## Step 2: Add the API key

In your GitHub repo, go to **Settings → Secrets and variables →
Actions → New repository secret**.

- Name: `OPENROUTER_API_KEY`
- Value: your OpenRouter key (starts with `sk-or-...`)

The workflow above references this secret as
`secrets.OPENROUTER_API_KEY`. The name on the left has to match the
name in the workflow file; if you name your secret something else,
update the `LLM_API_KEY` line accordingly.

GitHub does not let you read the secret back after you save it. If you
typo the value, you have to delete and recreate it.

## Step 3: Open a test PR

Make a tiny change on a side branch and open a PR. A README typo fix is
ideal: small, safe, and gives the reviewer something concrete to look
at without flooding the diff.

```sh
git checkout -b momus-test
# fix a typo in README, or add a sentence somewhere
git add README.md
git commit -m "Test momus on a trivial change"
git push -u origin momus-test
```

Open the PR in the GitHub UI. Watch the **Actions** tab — the
`Momus Code Review` workflow should start within a few seconds of the
PR opening.

A typical run takes one to three minutes. Phase 1 is skipped on a
first review (no prior threads exist), so this run is phases 2, 3, and 4.

## Step 4: Read the review

When the workflow finishes, scroll to the PR conversation. You will see
a single review object posted by your bot account. It will be one of:

- **APPROVE** — no findings, or all findings are below the blocking
  threshold, and your `post.first_review_approve_policy` allows it.
  By default, momus does not APPROVE on first review. See
  [Review philosophy / First-review APPROVE policy](../explanation/review-philosophy.md#first-review-approve).
- **REQUEST_CHANGES** — at least one finding hit a blocking severity
  (`critical`, `high`, or `medium` by default).
- **COMMENT** — there are findings, but none are blocking, or the
  policy says no APPROVE on first review.

Each finding is rendered as an inline comment on the line it cites.
Findings include severity, category, the model's hypothesis, the
grounding evidence it found, and (when applicable) a suggested fix.

**An empty findings list is a legitimate outcome.** If your test PR is a
README typo, momus may post a COMMENT verdict with zero findings and a
short summary. That is correct behavior, not a broken bot. Most LLM
review tools default to manufacturing concerns to look useful; momus
does not. See
[Review philosophy / Signal over noise](../explanation/review-philosophy.md#signal-over-noise).

If you want to see momus actually find something, push a second commit
that breaks something visible: a function with an obvious off-by-one,
a `TODO: handle the null case` next to code that does not handle the
null case, an unused import, a typo in an identifier. Then dispatch the
workflow manually (Actions → Momus Code Review → Run workflow → enter
the PR number). You will get a fresh review on the updated diff.

## Step 5: Customize one thing

The defaults are deliberately conservative. To see how customization
works, add a `.momus.yaml` at the root of your repo:

```yaml
review:
  blocking_severities: [critical, high]
```

This narrows the blocking set from the default `[critical, high, medium]`
to just `[critical, high]`. Mediums are now advisory: still posted as
comments, but they will not produce a `REQUEST_CHANGES` verdict.

Commit this and push. The next review run picks it up. There is no
restart, no cache, no separate config service — momus reads
`.momus.yaml` from the worktree at review time.

That is the shape of all momus customization: edit a YAML file, merge it,
the next review uses the new config. For more on this specific knob and
when to reach for it, see
[How-to: Configure blocking severities](../how-to/configure-blocking-severities.md).
For every key you can set, see
[Reference / Configuration schema](../reference/config-schema.md).

## Next steps

You now have a working momus install. From here:

- **Adjust review behavior.** [How-to](../how-to/configure-blocking-severities.md)
  is the recipe section. Common tasks: enabling the Check Run for branch
  protection, switching providers, adding emphasis modules for
  security-focused or test-quality-focused review, extending the env
  passthrough for custom integrations.
- **Look up specific config.** [Reference / Configuration schema](../reference/config-schema.md)
  documents every key in `.momus.yaml`. [Reference / Action inputs](../reference/action-inputs.md)
  documents the workflow inputs. [Reference / Emphasis modules](../reference/emphasis-modules.md)
  documents the prebuilt emphasis library.
- **Understand the design.** [Explanation / The four-phase pipeline](../explanation/four-phase-pipeline.md)
  is the load-bearing design page. [Explanation / Review philosophy](../explanation/review-philosophy.md)
  is why momus behaves the way it does. [Explanation / Threat model](../explanation/threat-model.md)
  is what counts as untrusted and what containment is in place.

## A note on APPROVE

Without a GitHub App, APPROVE verdicts downgrade to COMMENT — GitHub does
not let an account approve its own PRs, and the default `GITHUB_TOKEN`
acts as the repo. To get real APPROVE verdicts, set up a GitHub App and
mint a token from it for the workflow. See
[How-to: Set up the GitHub App](../how-to/set-up-github-app.md).

Most users do not need this for the tutorial. Come back to it once the
basic setup works and you want APPROVE to mean APPROVE.
