#!/usr/bin/env bash
#
# Configure Momus secrets across a fleet of repos and the axiomantic org.
#
# Sets per-repo on the 10 most-recently-updated non-fork elijahr repos:
#   MOMUS_APP_ID
#   MOMUS_APP_PRIVATE_KEY
#   LLM_API_KEY
#
# Sets at the axiomantic org level (visibility: all):
#   MOMUS_APP_ID
#   MOMUS_APP_PRIVATE_KEY
#
# The org-level call requires admin:org token scope. If your gh token
# doesn't have it, refresh with:
#   gh auth refresh -s admin:org,repo
# and re-run.
#
# Required env vars:
#   MOMUS_APP_ID                          (default: 3586842)
#   MOMUS_APP_PRIVATE_KEY_PATH            path to .pem
#   LLM_API_KEY                           value to write as LLM_API_KEY

set -euo pipefail

APP_ID="${MOMUS_APP_ID:-3586842}"
PEM_PATH="${MOMUS_APP_PRIVATE_KEY_PATH:-/Users/eek/Downloads/axiomantic-momus.2026-05-03.private-key.pem}"
LLM_KEY="${LLM_API_KEY:-}"
ELIJAHR_REPO_LIMIT="${ELIJAHR_REPO_LIMIT:-10}"

err() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
ok()  { printf '\033[32m%s\033[0m\n' "$*"; }
warn(){ printf '\033[33m%s\033[0m\n' "$*" >&2; }
info(){ printf '%s\n' "$*"; }

if [ ! -r "$PEM_PATH" ]; then
  err "PEM file not readable at $PEM_PATH"
  exit 1
fi
if ! command -v gh >/dev/null 2>&1; then
  err "gh CLI not found"
  exit 1
fi

if [ -z "$LLM_KEY" ]; then
  warn "LLM_API_KEY env var not set."
  warn "The LLM_API_KEY write step will be SKIPPED."
fi

# --- Discover elijahr repos -------------------------------------------------

info "Querying elijahr's $ELIJAHR_REPO_LIMIT most recently updated non-fork repos..."
mapfile -t REPOS < <(
  gh repo list elijahr \
    --no-archived \
    --source \
    --limit 50 \
    --json name,updatedAt,isFork \
    --jq "[.[] | select(.isFork == false)] | sort_by(.updatedAt) | reverse | .[:${ELIJAHR_REPO_LIMIT}] | .[].name"
)
if [ "${#REPOS[@]}" -eq 0 ]; then
  err "No repos returned. Check gh auth and repo:read scope."
  exit 1
fi

info "Will configure secrets on:"
for r in "${REPOS[@]}"; do info "  - elijahr/$r"; done

# --- Per-repo: elijahr ------------------------------------------------------

set_repo_secret() {
  local repo="$1" name="$2" file_or_value="$3" mode="$4"
  if [ "$mode" = "file" ]; then
    gh secret set "$name" --repo "elijahr/$repo" < "$file_or_value"
  else
    printf '%s' "$file_or_value" | gh secret set "$name" --repo "elijahr/$repo"
  fi
}

for r in "${REPOS[@]}"; do
  info "==> elijahr/$r"
  set_repo_secret "$r" MOMUS_APP_ID "$APP_ID" value
  ok   "    [elijahr/$r] MOMUS_APP_ID set"
  set_repo_secret "$r" MOMUS_APP_PRIVATE_KEY "$PEM_PATH" file
  ok   "    [elijahr/$r] MOMUS_APP_PRIVATE_KEY set"
  if [ -n "$LLM_KEY" ]; then
    set_repo_secret "$r" LLM_API_KEY "$LLM_KEY" value
    ok   "    [elijahr/$r] LLM_API_KEY set"
  fi
done

# --- Org-level: axiomantic --------------------------------------------------

info ""
info "==> axiomantic org (visibility: all)"
set_org_secret() {
  local name="$1" file_or_value="$2" mode="$3"
  if [ "$mode" = "file" ]; then
    gh secret set "$name" --org axiomantic --visibility all < "$file_or_value"
  else
    printf '%s' "$file_or_value" \
      | gh secret set "$name" --org axiomantic --visibility all
  fi
}

if gh secret list --org axiomantic >/dev/null 2>&1; then
  set_org_secret MOMUS_APP_ID "$APP_ID" value
  ok   "    [axiomantic] MOMUS_APP_ID set"
  set_org_secret MOMUS_APP_PRIVATE_KEY "$PEM_PATH" file
  ok   "    [axiomantic] MOMUS_APP_PRIVATE_KEY set"
else
  warn "Cannot access axiomantic org secrets (likely missing admin:org scope)."
  warn "Refresh scopes and re-run for the org-level part:"
  warn "  gh auth refresh -s admin:org,repo"
fi

ok ""
ok "Done."
