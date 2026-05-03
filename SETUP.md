# Setting up Momus on your repository

Momus reviews your pull requests with an LLM, posts inline comments and an
APPROVE / REQUEST_CHANGES / COMMENT verdict, and remembers prior findings
across re-reviews.

You need three things:

1. An **LLM provider key** (e.g. OpenRouter) stored as a repo or org secret.
2. (Recommended) A **GitHub App** in your org so the bot can post real
   APPROVE reviews, not just COMMENT.
3. A short **workflow file** in your repo that calls the reusable Momus
   workflow.

Without the App, Momus still works — but any APPROVE verdict is downgraded
to COMMENT because GitHub rejects approvals from the default
`GITHUB_TOKEN` (which is the `github-actions[bot]` user). With the App,
your bot can actually approve clean PRs.

---

## 1. Add the LLM provider secret

Go to **Settings → Secrets and variables → Actions** in your repo (or org)
and add:

| Secret | Value |
|---|---|
| `OPENROUTER_KEY` | Your OpenRouter API key (or any OpenAI-compatible provider key — the variable is named `OPENROUTER_KEY` for legacy reasons but accepts any provider) |

You can override the default model and base URL per repo via the workflow's
`with: model:` / `with: base_url:` inputs (see step 3).

---

## 2. (Recommended) Create your own GitHub App

Each user creates their **own** GitHub App. Sharing one App's private key
across organizations would let any holder of the key act as that App
everywhere — that is the central reason Apps are not redistributable.
The setup takes ~3 minutes.

### 2a. Create the App

Suggested name: `<your-org>-momus` (e.g. `acme-momus`). The App's display
name becomes its `[bot]` user (e.g. `acme-momus[bot]`), which is what
people will see attached to PR reviews and what they'll type when they
@-mention the bot.

Go to **Your org → Settings → Developer settings → GitHub Apps → New
GitHub App** and configure:

- **Name**: `<your-org>-momus`
- **Homepage URL**: `https://github.com/axiomantic/momus`
- **Webhook**: **disable** (uncheck "Active") — Momus does not consume
  webhook events; it runs from your repo's Actions
- **Permissions** (Repository):
  - Contents: **Read**
  - Issues: **Read & write**
  - Pull requests: **Read & write**
  - Actions: **Read**
  - Checks: **Write** (only if you plan to enable Check Runs — see below)
  - Metadata: **Read** (default)
- **Where can this App be installed?**: Only this account (or Any account,
  if you want to share it publicly)
- Click **Create GitHub App**

### 2b. Generate a private key

On your new App's page → **Private keys** → **Generate a private key**.
A `.pem` file downloads. Treat it like a password.

### 2c. Note the App ID

Top of the App settings page. You'll need it for the secret.

### 2d. Install the App

On your new App's page → **Install App** → choose your account → pick the
repos you want Momus to review. You can change this anytime.

### 2e. Add the App secrets to your repo (or org)

Add two more secrets:

| Secret | Value |
|---|---|
| `MOMUS_APP_ID` | The numeric App ID from step 2c |
| `MOMUS_APP_PRIVATE_KEY` | The full contents of the `.pem` file from step 2b, including `-----BEGIN/END-----` lines |

These are **optional**. If you skip them, Momus falls back to the default
`GITHUB_TOKEN` and downgrades APPROVE → COMMENT.

---

## 3. Add the workflow

Create `.github/workflows/momus.yml` in your repo with the following
content. Replace `<your-org>-momus` with your App's actual slug (the
lowercased, hyphenated form of its display name).

```yaml
name: Momus

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]
  issue_comment:
    types: [created]
  workflow_dispatch:
    inputs:
      pr_number:
        description: PR number to review
        required: true
        type: string

jobs:
  call:
    permissions:
      contents: read
      pull-requests: write
      issues: write
    uses: axiomantic/.github/.github/workflows/momus.yml@devel
    with:
      pr_number: ${{ github.event.pull_request.number || github.event.issue.number || github.event.inputs.pr_number }}
      event_name: ${{ github.event_name }}
      # Customize the slash command and @-mention used to trigger a re-review.
      trigger_command: /ai-review
      trigger_mention: "@<your-org>-momus[bot]"
    secrets:
      OPENROUTER_KEY: ${{ secrets.OPENROUTER_KEY }}
      # Optional. Omit these to fall back to GITHUB_TOKEN (no APPROVE).
      MOMUS_APP_ID: ${{ secrets.MOMUS_APP_ID }}
      MOMUS_APP_PRIVATE_KEY: ${{ secrets.MOMUS_APP_PRIVATE_KEY }}
```

That's it. Open a PR and Momus will post a review.

---

## Triggering a re-review

You configured two triggers in step 3:

- **Slash command**: comment `/ai-review` (or whatever you set
  `trigger_command` to) anywhere on the PR.
- **@-mention**: tag the bot, e.g. `@acme-momus[bot] please re-review`.

Either triggers a fresh review against the latest commit. Prior bot
findings are classified (fixed / declined / still-relevant) before the
new review runs, so the bot doesn't repeat itself.

---

## Customizing per repo

Drop a `.momus.yaml` at your repo root to override defaults. See
[`config-defaults.yaml`](momus/config-defaults.yaml) for every knob.
Common overrides:

```yaml
review:
  blocking_severities: [critical, high]
  emit_nits: false
  noteworthy_max: 5
verify:
  enabled: true
checks:
  enabled: true   # post a Check Run alongside the Review (see below)
  name: Momus Code Review
```

## Check Runs (optional)

Setting `checks.enabled: true` makes Momus also post a Check Run that
appears on the PR header alongside CI checks. Verdict mapping:

| Bot result | Check conclusion |
|---|---|
| APPROVE, no blocking findings | success |
| COMMENT, no blocking findings | neutral |
| Any blocking finding | failure |
| REQUEST_CHANGES verdict | failure |

You can then use the Check in branch protection ("Require status checks
to pass before merging" → pick `Momus`) so PRs with blocking findings
can't merge. Requires `Checks: Write` on the App, which you set in
step 2a above. Without that permission the post will fail (logged to
stderr; Momus continues — Check Runs are best-effort).

---

## Concurrency and cost

- One review at a time per PR. New runs while an old one is in flight
  are skipped (they would just re-do work against the same head).
- The bot uses `tool_execution_end` events for live progress, so the
  status comment updates in real time.
- A typical PR review costs ~30k–80k input tokens + ~3k output tokens.
  For a 200-line diff on DeepSeek V4 Pro, that's well under a dollar.
