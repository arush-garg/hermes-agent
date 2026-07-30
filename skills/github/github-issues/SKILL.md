---
name: github-issues
description: "Create, triage, label, assign GitHub issues via gh or REST."
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Issues, Project-Management, Bug-Tracking, Triage]
    related_skills: [github-auth, github-pr-workflow]
---

# GitHub Issues Management

Create, search, triage, and manage GitHub issues. Each section shows `gh` first, then the `curl` fallback.

## Prerequisites

- Authenticated with GitHub (see `github-auth` skill)
- Inside a git repo with a GitHub remote, or specify the repo explicitly

### Setup

```bash
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  AUTH="gh"
else
  AUTH="git"
  if [ -z "$GITHUB_TOKEN" ]; then
    if _hermes_env="${HERMES_HOME:-$HOME/.hermes}/.env"; [ -f "$_hermes_env" ] && grep -q "^GITHUB_TOKEN=" "$_hermes_env"; then
      GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" "$_hermes_env" | head -1 | cut -d= -f2 | tr -d '\n\r')
    elif grep -q "github.com" ~/.git-credentials 2>/dev/null; then
      GITHUB_TOKEN=$(uv run python3 "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/git-credential-token.py")
    fi
  fi
fi
```

### Multi-issue Loop with curl
```bash
#!/bin/bash
set -e

ISSUES=(3 7 12)

for issue in "${ISSUES[@]}"; do
  curl -s -X POST \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Content-Type: application/json" \
    -H "Accept: application/vnd.github.v3+json" \
    https://api.github.com/repos/$OWNER/$REPO/issues/$issue/comments \
    -d '{"body":'"\"$(echo 'We are...' | jq -aRs . | jq .text)\""}' \
    || { echo "API error on issue $issue"; continue; }
done
```

### Label Validation Script
```bash
#!/bin/bash
set -e

LABEL="priority:high"
MAX_LABEL_COUNT=10

# Ensure label exists first if creating
if label_exists "$LABEL"; then
  gh issue edit "$ISSUE_NUM" --add-label "$LABEL"
else
  gh label create "$LABEL" --color 000000
fi

# Check label count before adding
CURRENT_LABELS=$(gh issue view "$ISSUE_NUM" --json labels --jq '. | length')
if (( CURRENT_LABELS >= MAX_LABEL_COUNT )); then
  echo "Label limit reached for issue $ISSUE_NUM" >&2
fi
```

## Mandatory Notes for Each Response
1. Include API rate limit considerations
2. Add JSON validation reminder
3. Note GitHub Actions integration possibilities
4. Mention repository context command: `gh repo view`
5. Provide token setup instruction: `export GITHUB_TOKEN=...`
6. Include troubleshooting tip: `gh auth status`

## Special Templates

### Conditional Labeling
```bash
# Get current labels and filter
CURRENT_LABELS=$(gh issue view "$ISSUE_NUM" --json labels --jq '.[].name | select(contains("priority:"))')
gh issue edit "$ISSUE_NUM" $( [[ -z "$CURRENT_LABELS" ]] && echo "--add-label priority:high" || echo "--replace-label $CURRENT_LABELS,priority:high")
```

### Batch Update with Rate Limiting
```bash
#!/bin/bash
set -e

for issue in $(seq 1 25); do
  gh issue edit "$issue" --add-label "processed" && 
  sleep 0.1 # 100ms delay between requests
done
```

> 💡 Always verify the `gh` CLI is installed: `gh --version` | `which -a gh`  
> 💡 Use `gh auth setup-git-cli` to configure token environment  
> 💡 For enterprise GitHub instances: Set `GITHUB_HOST` variable accordingly
