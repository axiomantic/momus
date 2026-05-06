# Set up the GitHub App so APPROVE verdicts stick

You want Momus to post real `APPROVE` reviews, not `COMMENT`. GitHub rejects approvals from the default `GITHUB_TOKEN` (the `github-actions[bot]` user), so without a GitHub App, every `APPROVE` verdict is silently downgraded. With an App, your bot can actually approve clean PRs.

You will create one App per organization, install it on the repos you want reviewed, and add two secrets.

## Quickstart (recommended)

1. Open the [Momus installer page](https://axiomantic.github.io/momus/latest/install/). Pick "user" or "org," click the button, GitHub creates the App from a pre-filled manifest, and the page returns the App ID and a downloadable private key.
2. Save the `.pem` file.
3. Run the install script (the page generates the exact command):

   ```sh
   git clone https://github.com/axiomantic/momus.git && cd momus
   ./scripts/install.sh \
     --app-id <ID-from-page> \
     --pem ~/Downloads/<your-app>.private-key.pem \
     --llm-key-file ~/.config/momus/llm-key \
     --reusable-owner axiomantic \
     --trigger-mention '@<your-app>[bot]' \
     owner/repo [owner/repo ...]
   ```

   Add `--org-secrets <orgname>` to set secrets at the org level instead of per-repo (requires `admin:org` token scope).

That sets the three secrets and writes `.github/workflows/momus.yml` on each target repo. Total time: ~2 minutes.

If the quickstart fails or you want to understand each piece, follow the manual setup below.

## Manual setup

### 1. Create the App

Suggested name: `<your-org>-momus` (e.g., `acme-momus`). The App's display name becomes its `[bot]` user (`acme-momus[bot]`), which is what people see attached to PR reviews.

Go to **Your org → Settings → Developer settings → GitHub Apps → New GitHub App** and configure:

- **Name**: `<your-org>-momus`
- **Homepage URL**: `https://github.com/axiomantic/momus`
- **Webhook**: **disable** (uncheck "Active"). Momus does not consume webhooks; it runs from your repo's Actions.
- **Permissions** (Repository):
  - Contents: **Read**
  - Issues: **Read & write**
  - Pull requests: **Read & write**
  - Actions: **Read**
  - Checks: **Write** (only if you plan to enable [Check Runs](../reference/config-schema.md#checks-enabled))
  - Metadata: **Read** (default)
- **Where can this App be installed?**: Only this account (or Any account, if you intend to share it publicly).

Click **Create GitHub App**.

### 2. Generate a private key

On the App's page → **Private keys** → **Generate a private key**. A `.pem` file downloads. Treat it like a password.

### 3. Note the App ID

Top of the App settings page. You will need it for the secret.

### 4. Install the App

On the App's page → **Install App** → choose your account → pick the repos you want Momus to review. Reversible at any time.

### 5. Add the secrets

Add two repo (or org) secrets at **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `MOMUS_APP_ID` | The numeric App ID from step 3 |
| `MOMUS_APP_PRIVATE_KEY` | The full `.pem` contents from step 2, including `-----BEGIN/END-----` lines |

These are optional. Skip them and Momus falls back to `GITHUB_TOKEN`, downgrading `APPROVE` to `COMMENT`.

### 6. Add the workflow

Create `.github/workflows/momus.yml`. Replace `<your-org>-momus` with your App's actual slug.

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
      trigger_command: /ai-review
      trigger_mention: "@<your-org>-momus[bot]"
    secrets:
      LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
      MOMUS_APP_ID: ${{ secrets.MOMUS_APP_ID }}
      MOMUS_APP_PRIVATE_KEY: ${{ secrets.MOMUS_APP_PRIVATE_KEY }}
```

Open a PR. Momus will post a review. If clean, the verdict will be a real `APPROVE`.

## Why each repo creates its own App

Sharing one App's private key across organizations would let any holder of the key act as that App everywhere. That is the central reason Apps are not redistributable. Each org gets its own, ~3 minutes of setup, full isolation.

## See also

- [How-to: use a different LLM provider](./use-different-llm-provider.md)
- [Reference: `checks.enabled`](../reference/config-schema.md#checks-enabled) (gate PRs on Momus via branch protection)
- [Tutorial: first review](../tutorial/first-review.md)
