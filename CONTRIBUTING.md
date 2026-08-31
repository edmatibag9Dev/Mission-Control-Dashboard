# Contributing Standards
# Ed Matibag — Global GitHub Commit & README Rules
# Location: ~/Documents/Claude/CONTRIBUTING.md
# Last updated: 2026-06-02

---

## Commit Message Format

Every commit must follow this structure — no exceptions.

```
<type>(<scope>): <subject line — max 72 chars, imperative mood>

<body — minimum 3 bullets describing what changed and why>

- What was built or changed
- Why it was needed or what problem it solves
- Any known limitations, workarounds, or follow-up items
```

### Types

| Type | Use when |
|------|----------|
| `feat` | New feature or new file added |
| `fix` | Bug fix or correction |
| `data` | Data update — new scrape, refresh, or content change |
| `docs` | README or documentation only change |
| `refactor` | Code restructure with no behavior change |
| `chore` | Config, tooling, or maintenance |

### Rules

- **`feat`, `fix`, `data` commits require a body with ≥ 3 bullets.** No one-liners.
- Subject line: imperative mood ("Add month filter" not "Added month filter")
- Body bullets must be specific — not generic filler like "updated code"
- If a feat or fix touches the README, say so in the body

### Examples

**Good:**
```
feat(trip-planner): add month filter to Trip Finder and Processing Planner

- Added month selector buttons (Jun–Jan) to Trip Finder tab so Ed can browse
  trips for any month without scrolling through the full schedule
- Added month selector (Jun–Dec) to Processing Planner with return dates
  derived algorithmically from departure date + trip duration
- Processing planner now auto-expands heavy days (3+ boats) and Ed's
  personal trip (flagged with 📍) on load
- Updated README with new feature documentation
```

**Bad:**
```
update dashboard
```

```
fix stuff
```

---

## README Standards

Every `feat`, `fix`, or `data` commit must include a README create or update.

### Required Sections (all 9, minimum 400 words total)

1. **Project title + one-line description**
2. **Overview / Purpose** — what problem this solves and for whom
3. **Features** — bulleted list of what the tool does
4. **Files** — table of all files with descriptions
5. **How to Use** — step-by-step for each major feature/tab/view
6. **Data Sources** — where data comes from, with links, and freshness notes
7. **Known Limitations / Workarounds** — anything that doesn't work perfectly
8. **Build Notes** — tech stack, architecture decisions, no-dependency notes
9. **Update / Refresh Instructions** — how to get new data into the tool

### README Rules

- Never leave a stub or placeholder README
- Always read the existing README before updating — preserve sections that are still accurate
- Add a "Last updated" date at the bottom
- Link all data sources and booking URLs explicitly
- If a scraping workaround exists, document it fully so it can be repeated

---

## File Push via GitHub Contents API

When git clone is blocked (sandbox environment), use the GitHub Contents API.

### New file (does not exist in repo):
```
PUT /repos/{owner}/{repo}/contents/{path}
Body: { "message": "...", "content": "<base64>" }
```

### Update existing file (already in repo):
```
# Step 1 — GET the file to retrieve its SHA
GET /repos/{owner}/{repo}/contents/{path}
→ note the "sha" field

# Step 2 — PUT with sha included
PUT /repos/{owner}/{repo}/contents/{path}
Body: { "message": "...", "content": "<base64>", "sha": "<sha from step 1>" }
```

> **Missing sha on an update = 409 Conflict error.** Always GET first.

### Base64 encoding in bash:
```bash
CONTENT=$(base64 -w 0 "$FILE")
```

---

## Pre-Commit Checklist

Before pushing any commit:

- [ ] Commit message has type, scope, subject, and ≥ 3 body bullets
- [ ] README has been created or updated
- [ ] README has all 9 required sections
- [ ] README is ≥ 400 words
- [ ] If updating an existing file: SHA was retrieved first
- [ ] CONTRIBUTING.md and AGENTS.md are present in the repo

---

## What to Stage — Never Commit Blindly

Staging is part of the commit, not a detail beneath it. A commit records what you
staged, so an unconditional stage records whatever state the working tree happens
to be in — including damage you did not cause and did not notice.

### Rules

- **Stage named paths.** `git add <path> <path>` — only the files your change
  actually touched. You should be able to say why each one is in the commit.
- **Never `git add -A`, `git add .`, `git add --all`, or `git commit -a`** in a
  repository that already has history. Use them only to bootstrap a fresh
  `git init`, and verify the staged list before that first commit.
- **Check for deletions before every commit:**

  ```
  git diff --cached --name-status --diff-filter=D
  ```

  If that prints anything you did not deliberately delete, STOP. Unstage with
  `git reset`, find out why the file is missing, and restore it. Do not commit
  the removal.
- **A file missing from the working tree is not a change.** It is a filesystem,
  sync-client, or tooling problem. Committing its deletion converts a recoverable
  accident into recorded history and destroys the git copy that would have
  restored it.
- **Untracked is not protected.** A file that was never committed has no git copy
  at all. If a working file matters, commit it or ignore it deliberately — never
  leave it untracked by accident.

### Staging self-check

- [ ] Staged named paths only — no `-A`, no `.`, no `-a`
- [ ] `git diff --cached --name-status --diff-filter=D` shows nothing unintended
- [ ] Every staged path belongs to the change described in the commit message

### Why this rule exists

On 2026-08-30, commit `3df1d05` in the ai-briefing repo — a routine data commit —
was staged unconditionally while two files were missing from the working tree. A
two-way sync client had deleted them nine days earlier. The commit recorded both
deletions, removing the last recoverable copies from git and leaving the sync
client's quarantine folder as the only source. They were recovered, but only
because that quarantine had not yet been purged on its retention timer.

The same pattern nearly caused a data leak once before: an untracked `reports/`
folder holding local absolute paths and an email address sat in a public repo,
where any `git add .` would have swept it into a public commit.

Unconditional staging fails in both directions. It commits what should never be
published, and it deletes what should never be lost.
