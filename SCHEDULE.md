# SCHEDULE.md — how the daily sweep runs

## Schedule

- **Task:** `ops-watcher` — a Claude scheduled task at `~/.claude/scheduled-tasks/ops-watcher/SKILL.md`
  (the shared registry; visible in the Cowork "Scheduled" sidebar).
- **Cadence:** daily, cron `0 8 * * *` local time (fires ~8:04 AM with jitter).
- **What a run does:** snapshot `list_scheduled_tasks` → `python3 watch.py` → investigate any
  MISSED/FAILED/PARTIAL/flags via session transcripts + Slack → route per
  AI-orchestration-layer/ESCALATION-POLICY.md (urgent → one webhook message to #ops-control,
  DM fallback until that webhook exists; noteworthy → Lane-2 digest rows; healthy → dashboard
  update only). It surfaces, never repairs — restarts belong to `fleet-sentinel`.
- **Task:** `fleet-sentinel` — hourly companion, cron `12 6-21 * * *` (Phase 4b, 2026-08-31).
  Every run drains `ORCH/runs/ops-commands.jsonl` (Ed's `rerun`/`ack` commands, queued by
  `slack_ops_poller.py` from #ops-control); at the 9 AM and 8 PM windows it also runs
  `watch.py` and auto-restarts Class-1 failures. Guards: skip if today's work already landed,
  max 2 restarts/routine/day, Class-1 only, briefing never after noon, 3-in-7-days tripwire.
  Ledger: `ORCH/runs/repair.jsonl` (append-only).

## Runtime prerequisites

- The Claude desktop app must be open at fire time (tasks run at next launch otherwise — the
  dashboard's >26h staleness banner is the tell).
- Tool permissions prestaged on the task (one-time "Run now") so headless runs never stall on
  approval prompts.
- Python 3.9+ on PATH (`python3`); no packages required.
- For the remote probe: this Mac and `eds-mac-studio.local` on the same LAN.

## How to edit

- **Schedule/cadence:** update the `ops-watcher` task via the scheduled-tasks tools (a Lane-3
  standing-configuration change — Ed approves).
- **Coverage (groups, launchd jobs, servers):** constants at the top of `watch.py` — no task change
  needed; the next sweep picks it up.

## Notifications

Healthy days are silent by design: the dashboard refresh and a one-line run report are the entire
output. Slack DMs happen only for severity-gate issues; noteworthy items ride the separate
`evening-digest` task (~7:12 PM) per the escalation policy.
