# OPS-RUNBOOK.md — operating the routine fleet from anywhere

**Audience:** Ed, on a phone, possibly mid-incident. Short sentences, exact commands, no theory.
**Contracts this implements:** `AI-orchestration-layer/SPEC-self-healing-loop.md` (Phase 4a/4b)
and `AI-orchestration-layer/ESCALATION-POLICY.md` (v1.2). This file points; the SPEC defines.
**Born:** 2026-08-31, the day after recovery from the 8/19–8/30 `session_stale_relogin` outage
(11 days, ~15 routines silent, Ed out of office with no way to trigger a fix).

---

## 1. The system in one picture

```
DETECT                      REPAIR                      COMMAND
fleet-watchdog (launchd,    fleet-sentinel (scheduled   slack_ops_poller (launchd,
hourly, no Claude dep)      task, hourly 6a–9p)         3 min, no Claude dep)
  auth signature + fleet      drains command queue        reads #ops-control
  silence → #ops-control      every hour; sweeps +        Ed-only, closed grammar
                              restarts Class-1 fails      status/help answered
ops-watcher (scheduled        at 9 AM / 8 PM              directly; kick runs now;
task, daily 8:04 AM)          guards: skip-if-landed,     rerun/ack queued for
  full health sweep +         2/day cap, noon cutoff      the sentinel
  Mission Control dashboard   (briefing), 3-in-7
  → #ops-control              tripwire
```

- Each detection layer watches the other: ops-watcher checks the watchdog's `last-ok` stamp;
  the watchdog checks routines' work artifacts. Neither can die invisibly.
- **Channel rule:** everything ops goes to **#ops-control** (private; webhook identity so it
  banners on the phone). Exception: the ai-briefing routine's own domain alerts stay in
  **#ai-briefing**.
- **Division of labor:** ops-watcher surfaces, fleet-sentinel repairs. Neither does the
  other's job. Only fleet-sentinel may restart a routine.

## 2. Operator card — the five commands

Type in #ops-control. Only Ed's Slack ID (`ops-user_id` config) is honored; everything else
in the channel is ignored as data. No free-text execution exists.

| Command | What happens | When |
|---|---|---|
| `status` | Fleet summary from the last `watch.py` run, problems itemized | Reply ≤3 min |
| `rerun <task-id>` | Queues a restart of that routine | Ack ≤3 min; executes at the next sentinel tick (hourly at ~:16, 6 AM–9 PM) |
| `ack <task-id>` | Suppresses today's auto-restart of that task | Ack ≤3 min |
| `kick <launchd-label>` | `launchctl kickstart -k` on a `com.ed.*` / `com.edmatibag.*` job | Runs immediately |
| `help` | The command menu | Reply ≤3 min |

The sentinel may **refuse** a `rerun`, by design. Refusals it will report:
- `skipped-already-landed` — today's work already exists; a rerun would duplicate it.
- `skipped-cap-reached` — 2 restarts/routine/day cap hit (the cap includes your reruns).
- `skipped-past-window` — time-boxed output (morning briefing never restarts after 12:00).
- `tripwired` — same repair ≥3× in 7 days: the routine's spec is broken; a human must look.
- User Action Required — Class-2 or credential cause; no restart can fix it (see §3.1).

## 3. Incident playbooks, by failure class

### 3.1 Fleet-wide silence / auth outage (the 8/19 class)
**Signature:** watchdog alert in #ops-control (`AUTH_BLOCKED` / stale routines), or the app
log line `session_stale_relogin`.
**What works:** ONLY an interactive re-login — Claude Desktop → sign out → sign in. No Slack
command, no agent, no script can or should do this (credentials are owner-only, SPEC
invariant 4, Lane 4).
**Away from the Mac:** remote desktop in from the phone (Screens / Jump Desktop / Tailscale +
Screen Sharing), sign in, done. Fleet resumes on its own at the next scheduled times — no
restarts needed (verified in the 8/30 recovery).
**Expected total outage with this runbook: ~1 hour** (watchdog detection latency), vs 11 days.

### 3.2 One routine failed or missed
Usually: do nothing. The 9 AM / 8 PM sweeps auto-restart Class-1 failures within the guards.
Want it sooner? `rerun <task-id>`. Check the thread reply for guard refusals.

### 3.3 launchd job dead (ingest, watchdog, poller itself)
`kick <label>` from Slack — deterministic, immediate, no Claude involved. Labels in use:
`com.edmatibag.fleet-watchdog`, `com.edmatibag.slack-ops-poller`, the `com.ed.tokenburn.*`
jobs. (If the *poller* is dead you can't kick from Slack — that one needs the Mac, or waits
for its next 3-min tick after whatever killed it clears.)

### 3.4 Token dashboard fails on the live-limits scrape
**Signature:** `chrome-scrape unreachable (Chrome MCP not connected)` — seen live 2026-08-31.
The claude.ai limits scrape needs Chrome running with the Claude extension connected on the
Mac. Fix: open Chrome + extension, then `rerun claude-token-dashboard-update` (or let the
6:10 PM run / 7:26 PM sentinel retry). The burn side (ingest, reconciliation) is independent
and usually healthy — the failure alert says which side broke.

### 3.5 Sentinel or poller misbehaving
Never "fix" a watcher from inside the system — surface it. Poller log:
`~/Library/Logs/slack-ops-poller/poller.log`. Watchdog log:
`~/Library/Logs/fleet-watchdog/watchdog.log`. Sentinel history: `runs/heartbeat.jsonl`
(task `fleet-sentinel`) and `AI-orchestration-layer/runs/repair.jsonl`.

## 4. Setup / rebuild appendix (every pitfall from the 2026-08-31 build, so this takes 10 minutes)

Slack side (app `ops_control` on workspace "Ed Matibag AI"):
1. Channel **#ops-control** exists (currently private, id C0BTH036UKH).
2. Incoming webhook bound to it → save URL to `~/.config/claude-alerts/ops-control_webhook`.
3. Bot Token Scopes: `channels:read`, `channels:history`, `chat:write`, **plus
   `groups:read` + `groups:history` because the channel is private**. Pitfalls, all hit live:
   - Scopes go under **Bot** Token Scopes, not User Token Scopes.
   - Adding scopes does nothing until you click **Reinstall to Workspace** — the token only
     carries scopes baked in at install time (`missing_scope` otherwise).
   - Listing private channels without `groups:read` fails the WHOLE `conversations.list`
     call, even for a bot that's a member.
   - The bot must be **invited** to a private channel (`/invite @ops_control`) — scopes
     alone don't grant visibility.
4. Token → `~/.config/claude-alerts/ops-bot_token`. **Use printf with straight quotes and no
   trailing newline**; macOS smart-quote substitution twice wrapped a pasted token in `‘…’`
   and broke auth (the poller now strips stray quotes, but don't rely on it):
   `printf 'xoxb-…' > ~/.config/claude-alerts/ops-bot_token && chmod 600 $_`
5. Ed's Slack member ID → `~/.config/claude-alerts/ops-user_id` (currently `U0AQ3HDM8E5`).

Mac side:
6. Poller: `cp com.edmatibag.slack-ops-poller.plist ~/Library/LaunchAgents/ && launchctl
   bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.edmatibag.slack-ops-poller.plist`.
   It self-arms once the secrets exist — log shows `armed on #ops-control`. Both launchd
   jobs MUST invoke python through `/bin/bash -c` (TCC Full Disk Access attaches to the
   responsible process; bare python3 gets `Operation not permitted`).
7. Fleet-sentinel scheduled task exists (cron `12 6-21 * * *`); after any prompt change,
   click **Run now** once and approve tools — permission prestaging, per the SPEC. A run
   that stalls on an approval prompt repairs nothing.
8. Smoke test: `help` in #ops-control (≤3 min reply), then `status`.

## 5. File map

| Thing | Where |
|---|---|
| Poller + plist | this repo: `slack_ops_poller.py`, `com.edmatibag.slack-ops-poller.plist` |
| Watchdog | this repo: `fleet_watchdog.py` (design doc: `WATCHDOG.md`) |
| Health engine + dashboard | this repo: `watch.py` → `mission-control.html` |
| Sentinel / ops-watcher prompts | `~/.claude/scheduled-tasks/{fleet-sentinel,ops-watcher}/SKILL.md` (backups: `AI-orchestration-layer/scheduled-tasks/`) |
| Command queue | `AI-orchestration-layer/runs/ops-commands.jsonl` (append-only) |
| Repair ledger | `AI-orchestration-layer/runs/repair.jsonl` (append-only, Lane-4 protected) |
| Heartbeats | this repo: `runs/heartbeat.jsonl` |
| Secrets | `~/.config/claude-alerts/` (chmod 600; never committed) |
| Poller/watchdog logs | `~/Library/Logs/{slack-ops-poller,fleet-watchdog}/` |
| Contracts | `AI-orchestration-layer/SPEC-self-healing-loop.md`, `ESCALATION-POLICY.md` |

## 6. Deliberately not built (decisions, not gaps)

- **No auto-reauth, ever** — credentials are owner-only (Lane 4).
- **`fix` beyond re-running a routine's own prompt** — held for discussion (Ed, 2026-08-31);
  a `rerun` re-executes the routine verbatim, nothing else.
- **No free-text command path** — the grammar is closed; widening it is a spec change.
- **Watchers never repair watchers** — a monitoring layer that fixes itself can hide its own
  death; both layers surface each other instead.
