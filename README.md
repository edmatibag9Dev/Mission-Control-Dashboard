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
- **Morning Page** (`morning_page.py`, 2026-09-06) — a read-only one-screen summary rendered right
  after every `watch.py` run: six computed summary bullets, a today's-fires timeline (ran / due /
  missed / upcoming with a NOW marker), Needs-a-look items that link to the routine's anchor on
  Mission Control, open Lane-2 digest items plus Slack alert-channel links, a Fleet tile, Claude plan
  gauges (amber at 50%, red at 80%), and token burn by vendor (Claude vs OpenAI, yesterday and 7 days,
  stacked daily chart, 7-day API-equivalent cost). No model writes it, so it renders with the Claude
  app closed. It never writes to any source it reads.
- **Escalation-policy routing** — the daily `ops-watcher` task DMs Ed only for severity-gate issues,
  files noteworthy items to the evening digest, and stays silent on healthy days.

## Files

| File | Role |
|---|---|
| `AGENTS.md` | Canonical agent guide — file map, data contract, invariants, verification gates. |
| `watch.py` | The engine: health computation + dashboard render (stdlib only). |
| `morning_page.py` | Read-only Morning Page renderer (stdlib only) — writes `morning-page.html` and nothing else. |
| `fleet_watchdog.py` | Out-of-band hourly liveness monitor (launchd, no Claude dependency) — alerts #ops-control (fallback #ai-briefing) on fleet silence or the auth-failure signature. |
| `slack_ops_poller.py` | Inbound #ops-control command poller (launchd, no Claude dependency) — `status`/`help`/`kick` handled directly, `rerun`/`ack` queued for the fleet-sentinel task. Phase 4b. |
| `com.edmatibag.slack-ops-poller.plist` | launchd job for the poller (3-min interval; install instructions in the file header). |
| `SCHEDULE.md` | How the daily sweep is scheduled and its runtime prerequisites. |
| `OPS-RUNBOOK.md` | Operator runbook: the detect/repair/command system in one picture, the five Slack commands, incident playbooks by failure class, and the full rebuild appendix. |
| `samples/` | Scrubbed samples of the gitignored runtime data. |
| `CONTRIBUTING.md` | Commit format + README standards (canonical copy). |
| `CHANGELOG.md` | Dated log of notable changes. |
| `mission-control.html` | Generated dashboard (gitignored — rebuilt every run). |
| `morning-page.html` | Generated Morning Page (gitignored — rebuilt after every `watch.py` run). |
| `runs/morning-page.local.json` | Gitignored Slack workspace + channel ids for the Morning Page's channel links (sample in `samples/`). |
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
interface the watcher agent reads.

Open `morning-page.html` for the one-screen morning view. It is regenerated by the same runners
right after `watch.py` (ops-watcher 8:04 AM; fleet-sentinel 9 AM and 8 PM sweeps — the 8 PM pass
carries that day's token-burn numbers). To refresh on demand:

```bash
cd ~/Documents/Claude/Projects/Mission-Control-Dashboard && python3 morning_page.py
```

Open it from disk (double-click or `open morning-page.html`), not through a web server: browsers
only allow the file-to-file links (Mission Control, Command Center, Token Burn) from a page that was
itself opened as a file. The three server links (AI Briefing, Earnings Put Screener, Open Brain
review) work either way. Needs-a-look items jump to `mission-control.html#<task-id>`; Slack channel
rows open the channel in the Slack app when `runs/morning-page.local.json` holds the channel ids
(copy `samples/sample.morning-page.local.json` and fill it in). Group membership, launchd jobs, and servers are configured in the
constants at the top of `watch.py`.

## Data Sources

- `runs/scheduled-tasks-snapshot.json` — the shared scheduled-task registry (`~/.claude/scheduled-tasks`),
  snapshotted by the watcher each run.
- `runs/heartbeat.jsonl` — appended by each routine's attention-layer footer.
- `~/Documents/Claude/Projects/AI-orchestration-layer/runs/digest.jsonl` — the Lane-2 queue (owned by
  ESCALATION-POLICY.md there; read-only view here).
- launchd evidence: `~/Library/Logs/tokenburn/last-success`, `~/Open-Brain/.digest.log`, the earnings
  screener's `_launchd_scan.log`.
- Morning Page only (all read-only): `~/Documents/Claude/Projects/Token Burn Dashboard/daily-burn.json`
  (tokens by source per day; Claude = Cowork + Claude Code, OpenAI = Codex exact + ChatGPT estimated,
  cache excluded), `sessions.json` (API-equivalent cost, Anthropic models only), `chatgpt-export-meta.json`
  (how far the manual ChatGPT export reaches), the `USAGE_SUMMARY` block inside
  `~/Documents/Claude/claude-token-dashboard.html` (plan limits scraped by the 6:10 PM task), and
  `AI-orchestration-layer/runs/repair.jsonl` + `ops-commands.jsonl` (restarts, queued Slack commands).
  Token data is a day behind in the morning render; the 8 PM render catches up.
- Servers: localhost ports 8765/8787 and `http://eds-mac-studio.local:8080/latest.html`.

## Known Limitations / Workarounds

- Scheduled tasks run only while the Claude app is open; a missed fire runs at next launch. The
  staleness banner on the dashboard is the tell that the watcher itself did not run.
- The Mac Studio probe requires both machines on the same LAN — away from home it reads amber
  "Unreachable", by design not an escalation.
- Session-scoped Cowork task state and cloud routines are not enumerable via any tool; the fleet was
  consolidated into the shared registry on 2026-07-28 precisely to close that gap.
- Heartbeat coverage starts from each routine's first post-footer run; absence is treated as neutral.
- Morning Page: OpenAI cost shows `n/a` because `sessions.json` carries no Codex or ChatGPT cost rows
  (its pricing table is Anthropic-only); adding that belongs to the Token Burn Dashboard repo.
- Morning Page: Slack per-channel alert counts are Phase 2 — the ops bot is only in #ops-control, so
  channel rows show the link and "not read yet". Digest items are not linked (they live in a local log).
- Morning Page: plan limits are parsed out of the Command Center HTML; if that block moves or is
  renamed the tile says "unavailable" and the run report prints `gaps: plan-limits`.

## Build Notes

Single-file Python (3.9+, stdlib only — no venv, no requirements). All rendering is inline HTML/CSS
with Ed's Teal-Sage brand tokens embedded (Fraunces/Inter/IBM Plex Mono via Google Fonts; light +
dark via `prefers-color-scheme`; status colors from the semantic palette, never brand teal). Every
network probe carries a timeout and the script always exits 0 — interpretation belongs to the watcher
agent, arithmetic to the script. Escalation semantics come from the AI-orchestration-layer's
ESCALATION-POLICY.md four-lane contract.

## Update / Refresh Instructions

Edit constants in `watch.py` (`GROUPS`, `SCRIPT_JOBS_STATIC`, `SERVERS`) to change coverage; run the
AGENTS.md verification gates before committing. For the Morning Page, edit the constants at the top of
`morning_page.py`: `NAMES` (timeline short names), `FOLD` (multi-fire routines collapsed to chosen
hours), `TIMELINE_LAUNCHD`, `SOURCE_LINKS`, `ALERT_CHANNELS`; channel ids go in the gitignored
`runs/morning-page.local.json`. Add a dated CHANGELOG entry and refresh this README
on every `feat`/`fix`/`data` commit. Runtime data and the rendered dashboard are never committed —
update `samples/` instead when a shape changes.

---
*Last updated: 2026-09-06*

### STALLED verdict (added 2026-09-03, committed 2026-09-04)

`watch.py` marks a routine **stalled** when `lastRunAt` shows it fired but no heartbeat arrived for that fire within 2 hours (`STALL_GRACE`). `lastRunAt` proves dispatch, never completion; only a heartbeat proves completion. Stalled is alert-only: the fleet-sentinel never auto-restarts it, because the run may have finished its real work and merely failed to report. Guarded on prior heartbeat history, so a routine that has never written a footer is not flagged.
