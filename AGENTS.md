# AGENTS.md — guide for AI agents working in this repo

This file is the canonical entry point for any AI agent (Claude Code, Cowork, Codex, etc.) asked to
**use, reference, extend, or rebuild** this project. Read it before acting.

## What this repo is

Mission Control Dashboard: the health-monitoring layer for Ed's automated fleet — every Cowork/Claude
scheduled routine, the personal launchd script jobs, and the local/remote servers — rendered as one
brand-styled HTML dashboard and swept every morning by the `ops-watcher` scheduled task. Extracted
2026-07-28 from the AI-orchestration-layer repo (its BUILD-PLAN Phase 4 "attention layer").

Design in one line: **deterministic evidence beats notification-watching — health is computed from
cron-vs-lastRun math, self-reported heartbeats, job success stamps, and server probes, never from
noticing an absent Slack ping.**

## File map

| Path | Committed? | Purpose |
|---|---|---|
| `AGENTS.md` | yes | This guide. |
| `README.md` | yes | Human quickstart. |
| `llms.txt` | yes | Machine-readable index. |
| `watch.py` | yes | The whole engine: routine health calc, launchd checks, server probes, dashboard render. |
| `fleet_watchdog.py` | yes | **Out-of-band** liveness monitor run by launchd, NOT by Claude. Judges routine health from work artifacts + the Claude app log; never from `lastRunAt`. See `WATCHDOG.md`. |
| `WATCHDOG.md` | yes | Why the fleet watchdog exists (the 2026-08-19 outage), how it detects, and how to operate/test it. |
| `runs/watchdog-state.json` | **no (gitignored)** | Fleet-watchdog dedup state + awake-tick ledger. |
| `SCHEDULE.md` | yes | How the daily ops-watcher sweep is scheduled + prerequisites. |
| `CONTRIBUTING.md` | yes | Commit format + README standards (canonical copy). |
| `CHANGELOG.md` | yes | Dated change log. |
| `samples/` | yes | Scrubbed samples of the gitignored runtime data so the repo previews. |
| `mission-control.html` | **no (gitignored)** | Generated dashboard — rebuilt by every run; never hand-edit. |
| `runs/` | **no (gitignored)** | Runtime data: task snapshot, heartbeats, ops-status, dated history archives. |

External dependencies (read, never owned here):
- `~/Documents/Claude/Projects/AI-orchestration-layer/ESCALATION-POLICY.md` — the four decision lanes
  the watcher routes by.
- `~/Documents/Claude/Projects/AI-orchestration-layer/runs/digest.jsonl` — the Lane-2 queue.
  **Owned by the escalation policy / evening-digest task; this repo only reads it** (the watcher agent
  may append rows per the policy, never edit or deliver).
- launchd evidence files (`~/Library/Logs/tokenburn/last-success`, `~/Open-Brain/.digest.log`,
  earnings screener `_launchd_scan.log`) and server ports/URLs configured at the top of `watch.py`.

## The data contract

Three JSONL/JSON shapes every consumer depends on:

**`runs/heartbeat.jsonl`** — appended by every routine's attention-layer footer at end of run:
```json
{"task": "daily-ai-morning-briefing", "ts": "2026-07-28T08:43:00-07:00", "status": "ok", "note": "edition #42 published"}
```
`status` ∈ `ok | partial | failed`. A `failed`/`partial` heartbeat covering the last expected fire
**overrides** an OK computed from `lastRunAt`.

**`runs/scheduled-tasks-snapshot.json`** — verbatim `list_scheduled_tasks` output written by the
watcher agent before each engine run (array of `{taskId, description, schedule, cronExpression?,
fireAt?, enabled, nextRunAt?, lastRunAt?, jitterSeconds}`).

**`runs/ops-status.json`** — the engine's output: `{generated_at, tasks[], script_jobs[], servers[],
digest, summary}`. A dated copy lands in `runs/history/ops-status-<date>.json` per run (history-strip
data).

Invariants an agent must preserve:
1. `heartbeat.jsonl` and `digest.jsonl` are **append-only** — never rewrite, reorder, or delete rows.
2. Heartbeat **absence is neutral** — never treat a missing heartbeat as failure.
3. Routine statuses come only from `watch.py`'s computation — agents never hand-assign health.
4. `mission-control.html` is generated — edits go in `render_html()`, never the artifact.
5. The tokenburn pair's health keys off the **last-success stamp**, never watchdog log mtime
   (the watchdog is silent by design when healthy).
6. Remote-server `unreachable` is amber and never escalates alone; reachable-but-stale content is red.

## How it works (pipeline — native Mac runtime)

1. The `ops-watcher` scheduled task (daily ~8:04 AM, `~/.claude/scheduled-tasks/ops-watcher/SKILL.md`)
   snapshots `list_scheduled_tasks` into `runs/`.
2. `python3 watch.py` computes, in local time: per-routine health (cron prev-fire vs `lastRunAt`,
   jitter + 45-min grace; heartbeat override; not-yet-run and on-demand handling), launchd job health
   (stamp/evidence-file freshness), server health (local port connect, remote HTTP HEAD with
   Last-Modified freshness), and digest-queue aging — then writes `ops-status.json` + dated archive
   and renders `mission-control.html` (8 purpose-group cards, script-jobs table, servers strip,
   attention list, >26h staleness banner).
3. The watcher agent routes the printed summary per the escalation policy: urgent → one Slack DM to
   Ed; noteworthy → Lane-2 digest rows; healthy → dashboard only.

## How to extend

- **New routine group / regroup:** edit `GROUPS` in `watch.py`.
- **New launchd job:** add to `SCRIPT_JOBS_STATIC` (evidence file + `max_age_h`); jobs with bespoke
  logic (like the tokenburn stamp pair) get a block in `check_script_jobs()`.
- **New server:** add to `SERVERS` (`kind: "port"` local, `kind: "http"` with `max_age_h` + `remote`).
- **New status:** add to `BADGE` and, if it affects escalation, to `BAD_*` tuples.
- **Layout:** all HTML/CSS lives in `render_html()`; brand tokens are inlined per Ed's brand guide.

> ⛔ `watch.py` must stay dependency-free (stdlib only) and must never hang: every network probe
> carries a timeout, and the script always exits 0 — the watcher agent interprets the summary.

## Privacy — hard rules

- Never commit `runs/` contents or `mission-control.html` — they embed real schedules, run times, and
  finding texts. Samples in `samples/` are the committed stand-ins.
- No emails, Slack IDs, tokens, or keys in committed files. Home paths and LAN hostnames
  (`eds-mac-studio.local`) are acceptable per the repo standard.
- Digest item texts pass through the dashboard — they may reference other projects; that is why the
  rendered HTML stays gitignored.

## Verification gates

1. `python3 watch.py` exits 0 and prints ROUTINES / SCRIPT-JOBS / SERVERS / DIGEST / ESCALATE lines.
2. `runs/ops-status.json` parses and `summary` counts match the printed lines.
3. `mission-control.html` opens with no missing sections (groups, script jobs, servers, digest, retired).
4. A synthetic failed heartbeat for a routine that ran flips it to FAILED and sets
   ESCALATE-CANDIDATE: YES (then remove the synthetic row).
5. `git status --short` shows no `runs/` or `mission-control.html` entries staged.
6. Scrub grep (`grep -rniE "@gmail|xoxb-|\bsk-[a-z0-9]{8,}|api[_-]key\s*[:=]" --exclude-dir=.git .`)
   matches nothing committed.

## ⚠ `lastRunAt` IS NOT A LIVENESS SIGNAL

Learned the hard way on **2026-08-19**, when a stale Claude OAuth session
(`session_stale_relogin`) killed all 15 scheduled routines for 11 days without a single alert.

`lastRunAt` from `list_scheduled_tasks` is stamped when a dispatch is **cleared as stale**, not
only when a run succeeds. During the outage every task reported a recent `lastRunAt` and
`enabled: true` while none had executed in days — `daily-ai-morning-briefing`'s
`lastRunAt: 2026-08-30T15:00:48Z` was *exactly* its "Cleared stale pending dispatch" moment.

**Any tooling that treats `lastRunAt` as proof of life will report a dead fleet as healthy.**
The real signals are `runs/heartbeat.jsonl` (routines self-report at end of run), each routine's
own output artifacts, and `~/Library/Logs/fleet-watchdog/last-ok`.

`watch.py` still reads `lastRunAt` for scheduling arithmetic, which is fine — but it cross-checks
heartbeats, and `fleet_watchdog.py` never reads it at all.


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
