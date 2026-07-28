# Mission Control Dashboard

One brand-styled page — and one autonomous morning watcher — covering the run health of Ed's entire
automated fleet: every Claude/Cowork scheduled routine, the personal launchd script jobs, and the
local and remote servers.

## Overview / Purpose

By mid-2026 the fleet had grown to ~15 scheduled AI routines, four launchd script jobs, and three
always-on servers spread across two Macs and four storage locations. The only visibility was per-run
Slack notifications — which meant a *silently missed* run (the Mac asleep at fire time, a task
stalled on a permission prompt) was invisible, because the hardest thing to notice is an absent
notification. This project replaces notification-watching with deterministic evidence: schedule math,
self-reported heartbeats, job success stamps, and live server probes, all rendered on one dashboard
and swept every morning by an agent that only interrupts when something is actually wrong. It was
extracted from the AI-orchestration-layer repo, where it began as the BUILD-PLAN Phase 4 "attention
layer" pilot.

## Features

- **Deterministic routine health** — `watch.py` computes each routine's most recent expected fire
  from its cron expression (local time) and compares it to `lastRunAt` with jitter + grace; missed
  runs are caught without any notification parsing. Newly created tasks show "Not yet run", manual
  tasks show "On-demand" — no false alarms.
- **Heartbeat override** — every routine's prompt carries an attention-layer footer that appends
  `{task, ts, status, note}` to `runs/heartbeat.jsonl` at end of run; a `failed`/`partial` heartbeat
  overrides an OK computed from start-time alone, so a run that started and died still surfaces.
- **launchd script-job checks** — each job is verified against its *own* success evidence: the
  tokenburn pair against the pipeline's `last-success` stamp (its watchdog is silent by design),
  others against evidence-file freshness.
- **Server probes** — local port checks plus a remote HTTP HEAD of the Mac Studio screener with
  `Last-Modified` freshness; an asleep remote host shows amber, never a false red.
- **Grouped dashboard** — 8 purpose-group cards with health rollups, script-jobs table, servers
  strip, global attention list, Lane-2 digest queue view, and a >26h staleness banner that exposes a
  dead watcher.
- **Escalation-policy routing** — the daily `ops-watcher` task DMs Ed only for severity-gate issues,
  files noteworthy items to the evening digest, and stays silent on healthy days.

## Files

| File | Role |
|---|---|
| `AGENTS.md` | Canonical agent guide — file map, data contract, invariants, verification gates. |
| `watch.py` | The engine: health computation + dashboard render (stdlib only). |
| `SCHEDULE.md` | How the daily sweep is scheduled and its runtime prerequisites. |
| `samples/` | Scrubbed samples of the gitignored runtime data. |
| `CONTRIBUTING.md` | Commit format + README standards (canonical copy). |
| `CHANGELOG.md` | Dated log of notable changes. |
| `mission-control.html` | Generated dashboard (gitignored — rebuilt every run). |
| `runs/` | Runtime data: snapshot, heartbeats, ops-status + dated history (gitignored). |

## How to Use

Open `mission-control.html` in any browser — it regenerates every morning via the `ops-watcher`
scheduled task (~8:04 AM). To refresh on demand:

```bash
cd ~/Documents/Claude/Projects/Mission-Control-Dashboard
# 1. write a fresh list_scheduled_tasks JSON array to runs/scheduled-tasks-snapshot.json
# 2. then:
python3 watch.py
```

The printed summary (ROUTINES / SCRIPT-JOBS / SERVERS / DIGEST / ESCALATE-CANDIDATE) is the same
interface the watcher agent reads. Group membership, launchd jobs, and servers are configured in the
constants at the top of `watch.py`.

## Data Sources

- `runs/scheduled-tasks-snapshot.json` — the shared scheduled-task registry (`~/.claude/scheduled-tasks`),
  snapshotted by the watcher each run.
- `runs/heartbeat.jsonl` — appended by each routine's attention-layer footer.
- `~/Documents/Claude/Projects/AI-orchestration-layer/runs/digest.jsonl` — the Lane-2 queue (owned by
  ESCALATION-POLICY.md there; read-only view here).
- launchd evidence: `~/Library/Logs/tokenburn/last-success`, `~/Open-Brain/.digest.log`, the earnings
  screener's `_launchd_scan.log`.
- Servers: localhost ports 8765/8787 and `http://eds-mac-studio.local:8080/latest.html`.

## Known Limitations / Workarounds

- Scheduled tasks run only while the Claude app is open; a missed fire runs at next launch. The
  staleness banner on the dashboard is the tell that the watcher itself did not run.
- The Mac Studio probe requires both machines on the same LAN — away from home it reads amber
  "Unreachable", by design not an escalation.
- Session-scoped Cowork task state and cloud routines are not enumerable via any tool; the fleet was
  consolidated into the shared registry on 2026-07-28 precisely to close that gap.
- Heartbeat coverage starts from each routine's first post-footer run; absence is treated as neutral.

## Build Notes

Single-file Python (3.9+, stdlib only — no venv, no requirements). All rendering is inline HTML/CSS
with Ed's Teal-Sage brand tokens embedded (Fraunces/Inter/IBM Plex Mono via Google Fonts; light +
dark via `prefers-color-scheme`; status colors from the semantic palette, never brand teal). Every
network probe carries a timeout and the script always exits 0 — interpretation belongs to the watcher
agent, arithmetic to the script. Escalation semantics come from the AI-orchestration-layer's
ESCALATION-POLICY.md four-lane contract.

## Update / Refresh Instructions

Edit constants in `watch.py` (`GROUPS`, `SCRIPT_JOBS_STATIC`, `SERVERS`) to change coverage; run the
AGENTS.md verification gates before committing. Add a dated CHANGELOG entry and refresh this README
on every `feat`/`fix`/`data` commit. Runtime data and the rendered dashboard are never committed —
update `samples/` instead when a shape changes.

---
*Last updated: 2026-07-28*
