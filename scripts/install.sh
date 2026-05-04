#!/usr/bin/env bash
#
# Single-command Momus setup. Installs a `.github/workflows/momus.yml`
# stub into one or more repos and configures the secrets the bot needs:
#
#   LLM_API_KEY            — the LLM provider API key
#   MOMUS_APP_ID           — GitHub App ID (so APPROVE verdicts post)
#   MOMUS_APP_PRIVATE_KEY  — PEM contents of the App's private key
#
# Two scopes:
#
#   Per-repo (default)   — secrets set on each target repo. Works for user
#                          repos, and for org repos where you have repo
#                          admin but not org admin.
#   Org-level (--org-secrets)
#                        — secrets set once at the org level (visibility:
#                          all by default). Requires the gh token to have
#                          the `admin:org` scope.
#
# In both modes the workflow file is written per repo via an SSH clone
# (avoids the `workflow` token scope that the GitHub Contents API requires).
#
# Usage:
#   scripts/install.sh \
#     --app-id 1234567 \
#     --pem ~/Downloads/my-app.private-key.pem \
#     --llm-key-file ~/keys/openrouter \
#     --reusable-owner <owner-of-dot-github-with-momus.yml> \
#     [--reusable-ref devel] \
#     [--trigger-command /ai-review] \
#     [--trigger-mention '@<your-app>[bot]'] \
#     [--org-secrets <orgname> [--org-visibility all|private|selected]] \
#     owner/repo [owner/repo ...]
#
# All flags accept either `--name=value` or `--name value`. `--llm-key`
# and `--llm-key-file` are mutually exclusive (one is required).

set -uo pipefail

# ----- helpers ----------------------------------------------------------------

err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
info() { printf '%s\n' "$*"; }

usage() {
  # Print the leading `# ...` comment block (the usage docs above).
  awk '
    NR == 1 { next }                         # skip shebang
    /^# ----- / { exit }                     # stop at first section divider
    /^#$/ { print ""; next }
    /^# / { sub(/^# /, ""); print; next }
  ' "$0"
  exit "${1:-2}"
}

# ----- arg parsing ------------------------------------------------------------

APP_ID=""
PEM_PATH=""
LLM_KEY=""
LLM_KEY_FILE=""
REUSABLE_OWNER=""
REUSABLE_REF="devel"
TRIGGER_COMMAND="/ai-review"
TRIGGER_MENTION=""
ORG_SECRETS=""
ORG_VISIBILITY="all"
TARGETS=()

# Bash arg-loop that accepts both `--name value` and `--name=value` forms.
while [ $# -gt 0 ]; do
  case "$1" in
    --app-id)            APP_ID="$2"; shift 2 ;;
    --app-id=*)          APP_ID="${1#*=}"; shift ;;
    --pem)               PEM_PATH="$2"; shift 2 ;;
    --pem=*)             PEM_PATH="${1#*=}"; shift ;;
    --llm-key)           LLM_KEY="$2"; shift 2 ;;
    --llm-key=*)         LLM_KEY="${1#*=}"; shift ;;
    --llm-key-file)      LLM_KEY_FILE="$2"; shift 2 ;;
    --llm-key-file=*)    LLM_KEY_FILE="${1#*=}"; shift ;;
    --reusable-owner)    REUSABLE_OWNER="$2"; shift 2 ;;
    --reusable-owner=*)  REUSABLE_OWNER="${1#*=}"; shift ;;
    --reusable-ref)      REUSABLE_REF="$2"; shift 2 ;;
    --reusable-ref=*)    REUSABLE_REF="${1#*=}"; shift ;;
    --trigger-command)   TRIGGER_COMMAND="$2"; shift 2 ;;
    --trigger-command=*) TRIGGER_COMMAND="${1#*=}"; shift ;;
    --trigger-mention)   TRIGGER_MENTION="$2"; shift 2 ;;
    --trigger-mention=*) TRIGGER_MENTION="${1#*=}"; shift ;;
    --org-secrets)       ORG_SECRETS="$2"; shift 2 ;;
    --org-secrets=*)     ORG_SECRETS="${1#*=}"; shift ;;
    --org-visibility)    ORG_VISIBILITY="$2"; shift 2 ;;
    --org-visibility=*)  ORG_VISIBILITY="${1#*=}"; shift ;;
    -h|--help)           usage 0 ;;
    --) shift; break ;;
    -*) err "unknown flag: $1"; usage 2 ;;
    *)  TARGETS+=("$1"); shift ;;
  esac
done
# Anything after `--` is also a target.
TARGETS+=("$@")

# ----- validation -------------------------------------------------------------

[ -z "$APP_ID" ]            && { err "missing --app-id"; usage 2; }
[ -z "$PEM_PATH" ]          && { err "missing --pem"; usage 2; }
[ -z "$REUSABLE_OWNER" ]    && { err "missing --reusable-owner"; usage 2; }
[ -n "$LLM_KEY" ] && [ -n "$LLM_KEY_FILE" ] && { err "--llm-key and --llm-key-file are mutually exclusive"; exit 2; }
[ -z "$LLM_KEY" ] && [ -z "$LLM_KEY_FILE" ] && { err "missing --llm-key or --llm-key-file"; usage 2; }
[ "${#TARGETS[@]}" -eq 0 ] && { err "no target repos specified"; usage 2; }

[ -r "$PEM_PATH" ] || { err "PEM file not readable: $PEM_PATH"; exit 2; }
if [ -n "$LLM_KEY_FILE" ]; then
  [ -r "$LLM_KEY_FILE" ] || { err "LLM key file not readable: $LLM_KEY_FILE"; exit 2; }
  LLM_KEY=$(<"$LLM_KEY_FILE")
fi
[ -n "$LLM_KEY" ] || { err "LLM key is empty"; exit 2; }

case "$ORG_VISIBILITY" in
  all|private|selected) ;;
  *) err "--org-visibility must be one of: all, private, selected"; exit 2 ;;
esac

if ! command -v gh >/dev/null 2>&1; then
  err "gh CLI not found. Install: https://cli.github.com/"; exit 2
fi

if [ -n "$ORG_SECRETS" ]; then
  scopes=$(gh auth status 2>&1 | sed -n 's/.*Token scopes: //p' | head -1)
  case "$scopes" in
    *"'admin:org'"*) ;;
    *)
      err "--org-secrets requires the 'admin:org' token scope. Refresh:"
      err "  gh auth refresh -s admin:org"
      exit 2
      ;;
  esac
fi

# ----- workflow stub generator ------------------------------------------------

write_momus_yml() {
  local path="$1"
  mkdir -p "$(dirname "$path")"
  {
    cat <<EOF
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
    uses: ${REUSABLE_OWNER}/.github/.github/workflows/momus.yml@${REUSABLE_REF}
    with:
      pr_number: \${{ github.event.pull_request.number || github.event.issue.number || github.event.inputs.pr_number }}
      event_name: \${{ github.event_name }}
      trigger_command: ${TRIGGER_COMMAND}
EOF
    if [ -n "$TRIGGER_MENTION" ]; then
      printf '      trigger_mention: "%s"\n' "$TRIGGER_MENTION"
    fi
    cat <<EOF
    secrets:
      LLM_API_KEY: \${{ secrets.LLM_API_KEY }}
      MOMUS_APP_ID: \${{ secrets.MOMUS_APP_ID }}
      MOMUS_APP_PRIVATE_KEY: \${{ secrets.MOMUS_APP_PRIVATE_KEY }}
EOF
  } > "$path"
}

# ----- secret writers ---------------------------------------------------------

set_repo_secret_value() {
  local repo="$1" name="$2" value="$3"
  printf '%s' "$value" | gh secret set "$name" --repo "$repo" --body -
}

set_repo_secret_file() {
  local repo="$1" name="$2" path="$3"
  gh secret set "$name" --repo "$repo" < "$path"
}

set_org_secret_value() {
  local org="$1" name="$2" value="$3"
  printf '%s' "$value" | gh secret set "$name" --org "$org" --visibility "$ORG_VISIBILITY" --body -
}

set_org_secret_file() {
  local org="$1" name="$2" path="$3"
  gh secret set "$name" --org "$org" --visibility "$ORG_VISIBILITY" < "$path"
}

# ----- main flow --------------------------------------------------------------

info "Mode: $([ -n "$ORG_SECRETS" ] && echo "org-level secrets ($ORG_SECRETS, visibility=$ORG_VISIBILITY)" || echo "per-repo secrets")"
info "Reusable: ${REUSABLE_OWNER}/.github/.github/workflows/momus.yml@${REUSABLE_REF}"
info "Targets (${#TARGETS[@]}):"
for t in "${TARGETS[@]}"; do info "  - $t"; done
info ""

# Step 1: secrets

if [ -n "$ORG_SECRETS" ]; then
  info "==> setting org-level secrets on $ORG_SECRETS"
  if set_org_secret_value "$ORG_SECRETS" MOMUS_APP_ID    "$APP_ID"  >/dev/null 2>&1; then
    ok "  MOMUS_APP_ID set"
  else
    err "  failed to set MOMUS_APP_ID at org level"; exit 1
  fi
  if set_org_secret_file  "$ORG_SECRETS" MOMUS_APP_PRIVATE_KEY "$PEM_PATH" >/dev/null 2>&1; then
    ok "  MOMUS_APP_PRIVATE_KEY set"
  else
    err "  failed to set MOMUS_APP_PRIVATE_KEY at org level"; exit 1
  fi
  if set_org_secret_value "$ORG_SECRETS" LLM_API_KEY     "$LLM_KEY" >/dev/null 2>&1; then
    ok "  LLM_API_KEY set"
  else
    err "  failed to set LLM_API_KEY at org level"; exit 1
  fi
else
  info "==> setting per-repo secrets"
  for repo in "${TARGETS[@]}"; do
    info "  $repo"
    set_repo_secret_value "$repo" MOMUS_APP_ID            "$APP_ID"  >/dev/null \
      && ok "    MOMUS_APP_ID set"            || { err "    MOMUS_APP_ID failed"; continue; }
    set_repo_secret_file  "$repo" MOMUS_APP_PRIVATE_KEY   "$PEM_PATH" >/dev/null \
      && ok "    MOMUS_APP_PRIVATE_KEY set"   || { err "    MOMUS_APP_PRIVATE_KEY failed"; continue; }
    set_repo_secret_value "$repo" LLM_API_KEY             "$LLM_KEY" >/dev/null \
      && ok "    LLM_API_KEY set"             || { err "    LLM_API_KEY failed"; continue; }
  done
fi

# Step 2: workflow file (per repo, via SSH clone)

WORKDIR=$(mktemp -d -t momus-install.XXXXXX)
trap 'rm -rf "$WORKDIR"' EXIT

info ""
info "==> writing .github/workflows/momus.yml on each target"
for repo in "${TARGETS[@]}"; do
  info "  $repo"
  reponame="${repo#*/}"
  dir="$WORKDIR/$reponame"
  if ! git clone --depth=1 "git@github.com:${repo}.git" "$dir" 2>&1 | tail -1; then
    err "    clone failed"; continue
  fi
  pushd "$dir" >/dev/null
  write_momus_yml .github/workflows/momus.yml
  git add .github/workflows/momus.yml
  if git diff --cached --quiet; then
    info "    workflow already up to date"
    popd >/dev/null
    continue
  fi
  git_args=()
  [ -n "${GIT_AUTHOR_NAME:-}" ]  && git_args+=(-c "user.name=${GIT_AUTHOR_NAME}")
  [ -n "${GIT_AUTHOR_EMAIL:-}" ] && git_args+=(-c "user.email=${GIT_AUTHOR_EMAIL}")
  git "${git_args[@]}" commit -q -m "Install Momus review workflow"
  if push_out=$(git push origin HEAD 2>&1); then
    ok  "    pushed"
  else
    err "    push failed: $(printf '%s' "$push_out" | tail -1)"
  fi
  popd >/dev/null
done

info ""
ok "Done."
info ""
info "Next: install the GitHub App on these repos via:"
info "  https://github.com/apps/<your-app-slug>/installations/new"
