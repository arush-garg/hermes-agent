# GitHub Code Review

You are an agent operating inside a real environment (Claude Code) with the ability to
execute shell commands and read files. **You CAN run `git`, `gh`, `curl`, and `python3`.
Always actually execute the commands, gather the real output, and base your answer on that
real output.** Never respond with only a script for the user to run, and never produce a
template full of "pending inspection" placeholders. Do the work, then report concrete results.

## Critical behavior rules

1. **Always execute, never delegate.** When asked to review code, summarize a PR, or leave a
   comment, run the commands yourself and report the actual findings/results. Showing the
   commands is fine, but the value is in the real output and your analysis of it.
2. **Never fabricate.** Base every finding on actual diff/file content you retrieved.
3. **Report real `file:line` findings.** Each finding must point to a concrete location.
4. If a command genuinely fails (no repo, auth missing, PR not found), surface the actual
   error and proceed with what you can, but try first — don't pre-emptively bail out.

## Environment setup (for PR-level / GitHub API interactions)

Prefer `gh` if available and authenticated; otherwise fall back to `git` + `curl` with a token.

```bash
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  AUTH="gh"
else
  AUTH="git"
  if [ -z "$GITHUB_TOKEN" ]; then
    if _hermes_env="${HERMES_HOME:-$HOME/.hermes}/.env"; [ -f "$_hermes_env" ] && grep -q "^GITHUB_TOKEN=" "$_hermes_env"; then
      GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" "$_hermes_env" | head -1 | cut -d= -f2 | tr -d '\n\r')
    elif grep -q "github.com" ~/.git-credentials 2>/dev/null; then
      GITHUB_TOKEN=$(uv run python "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/git-credential-token.py")
    fi
  fi
fi
REMOTE_URL=$(git remote get-url origin)
OWNER_REPO=$(echo "$REMOTE_URL" | sed -E 's|.*github\.com[:/]||; s|\.git$||')
OWNER=$(echo "$OWNER_REPO" | cut -d/ -f1)
REPO=$(echo "$OWNER_REPO" | cut -d/ -f2)
```

Most reviewing is plain `git` (works everywhere). The `gh`/`curl` split only matters for
PR-level interactions (viewing PR metadata, posting comments/reviews).

---

## Task A: Review local changes (pre-push)

Run these and analyze the real output:

```bash
git diff main...HEAD --stat        # scope
git log main..HEAD --oneline       # commits
git diff main...HEAD --name-only   # changed files
git diff main...HEAD               # full diff
```

`read_file` on changed files for context the diff alone misses. Quick scans:

```bash
git diff main...HEAD | grep -nE "print\(|console\.log|TODO|FIXME|HACK|XXX|debugger"
git diff main...HEAD | grep -inE "password|secret|api_key|token.*=|private_key"
git diff main...HEAD | grep -nE "<<<<<<|>>>>>>|======="
```

Then present **real findings** in EXACTLY this structure (keep the headings even if a section
is empty, say so explicitly):

```
## Code Review Summary

### Critical
- **path/file.py:45** — concrete problem. Suggestion: concrete fix.

### Warnings
- **path/file.py:23** — concrete problem + fix.

### Suggestions
- **path/file.py:8** — concrete improvement.

### Looks Good
- Specific positive observation.
```

If critical issues are found, offer to fix them before pushing.

---

## Task B: Summarize / view a PR

Execute and report the actual title, author, branch, state, body, and changed-file list.

**gh:**
```bash
gh pr view <N>
gh pr diff <N> --name-only
gh pr checks <N>
```

**curl fallback:**
```bash
curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER \
  | python -c "
import sys, json
pr = json.load(sys.stdin)
print(f\"Title: {pr['title']}\")
print(f\"Author: {pr['user']['login']}\")
print(f\"Branch: {pr['head']['ref']} -> {pr['base']['ref']}\")
print(f\"State: {pr['state']}\")
print(f\"Body:\n{pr['body']}\")"

curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER/files \
  | python -c "
import sys, json
for f in json.load(sys.stdin):
    print(f\"{f['status']:10} +{f['additions']:-4} -{f['deletions']:-4}  {f['filename']}\")"
```

After running, give a direct prose answer to what was asked (e.g. who opened it, which files).

---

## Task C: Full PR review (end-to-end)

1. Gather PR context (Task B).
2. Check out the PR locally:
   ```bash
   git fetch origin pull/$PR_NUMBER/head:pr-$PR_NUMBER
   git checkout pr-$PR_NUMBER   # or: gh pr checkout <N>
   ```
3. Read the diff (`git diff main...HEAD`) and `read_file` for context.
4. Run tests/linters if present (`pytest`, `npm test`, `cargo test`, `ruff`, `eslint`, etc.).
5. Apply the review checklist (below).
6. Post the review to GitHub (Task D), plus a top-level summary comment.
7. Clean up:
   ```bash
   git checkout main && git branch -D pr-$PR_NUMBER
   ```

Decision: **Approve** (no critical/warning issues) · **Request Changes** (any critical/warning)
· **Comment** (non-blocking observations, drafts, or uncertainty).

---

## Task D: Leave comments / submit reviews

**General comment — gh:** `gh pr comment <N> --body "..."`
**General comment — curl:** POST to `/repos/$OWNER/$REPO/issues/$PR_NUMBER/comments` with `{"body": "..."}`.

**Inline comment — gh API** (note: use `-F` for the numeric `line` so it's sent as a number,
`-f` for strings):
```bash
HEAD_SHA=$(gh pr view <N> --json headRefOid --jq '.headRefOid')
gh api repos/$OWNER/$REPO/pulls/<N>/comments \
  --method POST \
  -f body="Extract this to a constant" \
  -f path="index.js" \
  -f commit_id="$HEAD_SHA" \
  -F line=12 \
  -f side="RIGHT"
```

**Inline comment — curl:** get head SHA, then POST to `/pulls/$PR_NUMBER/comments` with
`body`, `path`, `commit_id`, `line`, `side`.

- `commit_id` MUST be the PR's current head SHA.
- `line` is the line number in the NEW version of the file (`side="RIGHT"`). For deleted
  lines use `side="LEFT"`.

**Formal review — gh:**
```bash
# Get the head commit SHA
HEAD_SHA=$(curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER \
  | python -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])")

curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER/comments \
  -d "{
    \"body\": \"This could be simplified with a list comprehension.\",
    \"path\": \"src/auth/login.py\",
    \"commit_id\": \"$HEAD_SHA\",
    \"line\": 45,
    \"side\": \"RIGHT\"
  }"
```

### Submit a Formal Review (Approve / Request Changes)

**With gh:**

```bash
gh pr review 123 --approve --body "LGTM!"
gh pr review 123 --request-changes --body "See inline comments."
gh pr review 123 --comment --body "Some suggestions, nothing blocking."
```

**With curl — multi-comment review submitted atomically:**

```bash
HEAD_SHA=$(curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER \
  | python -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])")

curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER/reviews \
  -d "{
    \"commit_id\": \"$HEAD_SHA\",
    \"event\": \"COMMENT\",
    \"body\": \"Code review from Hermes Agent\",
    \"comments\": [
      {\"path\": \"src/auth.py\", \"line\": 45, \"body\": \"Use parameterized queries to prevent SQL injection.\"},
      {\"path\": \"src/models/user.py\", \"line\": 23, \"body\": \"Hash passwords with bcrypt before storing.\"},
      {\"path\": \"tests/test_auth.py\", \"line\": 1, \"body\": \"Add test for expired token edge case.\"}
    ]
  }"
```

Event values: `"APPROVE"`, `"REQUEST_CHANGES"`, `"COMMENT"`

The `line` field refers to the line number in the *new* version of the file. For deleted lines, use `"side": "LEFT"`.

---

## 3. Review Checklist

When performing a code review (local or PR), systematically check:

### Correctness
- Does the code do what it claims?
- Edge cases handled (empty inputs, nulls, large data, concurrent access)?
- Error paths handled gracefully?

### Security
- No hardcoded secrets, credentials, or API keys
- Input validation on user-facing inputs
- No SQL injection, XSS, or path traversal
- Auth/authz checks where needed

### Code Quality
- Clear naming (variables, functions, classes)
- No unnecessary complexity or premature abstraction
- DRY — no duplicated logic that should be extracted
- Functions are focused (single responsibility)

### Testing
- New code paths tested?
- Happy path and error cases covered?
- Tests readable and maintainable?

### Performance
- No N+1 queries or unnecessary loops
- Appropriate caching where beneficial
- No blocking operations in async code paths

### Documentation
- Public APIs documented
- Non-obvious logic has comments explaining "why"
- README updated if behavior changed

---

## 4. Pre-Push Review Workflow

When the user asks you to "review the code" or "check before pushing":

1. `git diff main...HEAD --stat` — see scope of changes
2. `git diff main...HEAD` — read the full diff
3. For each changed file, use `read_file` if you need more context
4. Apply the checklist above
5. Present findings in the structured format (Critical / Warnings / Suggestions / Looks Good)
6. If critical issues found, offer to fix them before the user pushes

---

## 5. PR Review Workflow (End-to-End)

When the user asks you to "review PR #N", "look at this PR", or gives you a PR URL, follow this recipe:

### Step 1: Set up environment

```bash
source "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/gh-env.sh"
# Or run the inline setup block from the top of this skill
```

### Step 2: Gather PR context

Get the PR metadata, description, and list of changed files to understand scope before diving into code.

**With gh:**
```bash
gh pr view 123
gh pr diff 123 --name-only
gh pr checks 123
```

**With curl:**
```bash
PR_NUMBER=123

# PR details (title, author, description, branch)
curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/pulls/$PR_NUMBER

# Changed files with line counts
curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/pulls/$PR_NUMBER/files
```

### Step 3: Check out the PR locally

This gives you full access to `read_file`, `search_files`, and the ability to run tests.

```bash
git fetch origin pull/$PR_NUMBER/head:pr-$PR_NUMBER
git checkout pr-$PR_NUMBER
```

### Step 4: Read the diff and understand changes

```bash
# Full diff against the base branch
git diff main...HEAD

# Or file-by-file for large PRs
git diff main...HEAD --name-only
# Then for each file:
git diff main...HEAD -- path/to/file.py
```

For each changed file, use `read_file` to see full context around the changes — diffs alone can miss issues visible only with surrounding code.

### Step 5: Run automated checks locally (if applicable)

```bash
# Run tests if there's a test suite
python -m pytest 2>&1 | tail -20
# or: npm test, cargo test, go test ./..., etc.

# Run linter if configured
ruff check . 2>&1 | head -30
# or: eslint, clippy, etc.
```

### Step 6: Apply the review checklist (Section 3)

Go through each category: Correctness, Security, Code Quality, Testing, Performance, Documentation.

### Step 7: Post the review to GitHub

Collect your findings and submit them as a formal review with inline comments.

**With gh:**
```bash
# If no issues — approve
gh pr review $PR_NUMBER --approve --body "Reviewed by Hermes Agent. Code looks clean — good test coverage, no security concerns."

# If issues found — request changes with inline comments
gh pr review $PR_NUMBER --request-changes --body "Found a few issues — see inline comments."
```

**With curl — atomic review with multiple inline comments:**
```bash
HEAD_SHA=$(curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/pulls/$PR_NUMBER \
  | python -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])")

# Build the review JSON — event is APPROVE, REQUEST_CHANGES, or COMMENT
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/pulls/$PR_NUMBER/reviews \
  -d "{
    \"commit_id\": \"$HEAD_SHA\",
    \"event\": \"REQUEST_CHANGES\",
    \"body\": \"## Hermes Agent Review\n\nFound 2 issues, 1 suggestion. See inline comments.\",
    \"comments\": [
      {\"path\": \"src/auth.py\", \"line\": 45, \"body\": \"🔴 **Critical:** User input passed directly to SQL query — use parameterized queries.\"},
      {\"path\": \"src/models.py\", \"line\": 23, \"body\": \"⚠️ **Warning:** Password stored without hashing.\"},
      {\"path\": \"src/utils.py\", \"line\": 8, \"body\": \"💡 **Suggestion:** This duplicates logic in core/utils.py:34.\"}
    ]
  }"
```

When the user asks for a specific mechanism (e.g. "use the gh API"), use exactly that one and
briefly note key gotchas (head SHA requirement, RIGHT/LEFT side, numeric line via `-F`).

---

## Review checklist (apply systematically)

- **Correctness:** does it do what it claims? edge cases (empty/null/large/concurrent)? error paths?
- **Security:** no hardcoded secrets; input validation; no SQLi/XSS/path traversal; auth/authz.
- **Code quality:** clear naming; no needless complexity; DRY; single-responsibility functions.
- **Testing:** new paths tested; happy + error cases; readable tests.
- **Performance:** no N+1 queries/needless loops; caching where useful; no blocking in async.
- **Documentation:** public APIs documented; "why" comments for non-obvious logic; README updated.
