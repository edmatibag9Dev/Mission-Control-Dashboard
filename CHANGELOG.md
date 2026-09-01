# Changelog

All notable changes to Mission Control Dashboard are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); dates are America/Los_Angeles.
Gitignored data/output files are never committed.

## [2026-08-31c] — Stale-snapshot guard, found by the first 20:00 sweep

The sentinel's first evening sweep ran `watch.py` against the 08:05 snapshot and got 8 false
MISSED verdicts; guard 2 (skip-if-landed) correctly blocked all 8 restarts, and the sentinel
diagnosed the cause itself and escalated. No routine was actually missed.

### Fixed
- **`watch.py` refuses a snapshot older than 3h** with an explicit refresh instruction —
  false verdicts become a loud error instead. Runners must refresh
  `runs/scheduled-tasks-snapshot.json` immediately before invoking the engine.
- **fleet-sentinel Step 3** (runtime SKILL + ORCH backup) now refreshes the snapshot via
  `list_scheduled_tasks` before running `watch.py` — same as ops-watcher Step 1.
- Regenerated tonight's poisoned artifacts from a fresh snapshot: `ops-status.json`,
  `mission-control.html`, and the dated `history/ops-status-2026-08-31.json` now carry the
  true verdicts (15 OK, 0 missed).
- OPS-RUNBOOK §3.5 documents the false-MISSED-wave signature; §3.4 records the verified
  chrome-scrape fix path (6:10 PM run succeeded with Chrome + extension open).

## [2026-08-31b] — OPS-RUNBOOK.md

### Added
- **`OPS-RUNBOOK.md`** — the operator document for the whole loop, written the day it went
  live: system diagram, the five #ops-control commands with expected latencies and the guard
  refusals, incident playbooks per failure class (auth outage → remote desktop; scrape
  failure → Chrome + extension; dead launchd job → `kick`), and a rebuild appendix capturing
  every pitfall hit during the live build (scope reinstall, private-channel scopes + invite,
  smart-quoted tokens, TCC bash-wrapper requirement, permission prestaging).
- README file table row pointing to it; AI-orchestration-layer README cross-links it.

## [2026-08-31] — Phase 4b: Slack remote control (#ops-control)

Approved by Ed 2026-08-31 after the 8/19–8/30 outage, in which he was out of office with no
way to trigger anything on this Mac. Contract: AI-orchestration-layer
`SPEC-self-healing-loop.md` Phase 4b.

### Added
- **`slack_ops_poller.py`** + `com.edmatibag.slack-ops-poller.plist` — 3-minute launchd poller
  (no Claude dependency) reading #ops-control for commands from Ed's user ID only. Strict
  closed grammar: `status`/`help` answered directly, `kick` runs `launchctl kickstart` on
  whitelisted `com.ed.*`/`com.edmatibag.*` labels, `rerun`/`ack` are queued to
  `ORCH/runs/ops-commands.jsonl` for the hourly `fleet-sentinel` scheduled task. No free-text
  execution path by design. Not armed until `ops-bot_token` + `ops-user_id` secrets exist.

### Changed
- **`fleet_watchdog.py` alert channel:** `ops-control` when its webhook secret exists,
  fallback `ai-briefing` — the flip can never silence the watchdog.
- **`SCHEDULE.md`** documents the ops-watcher/fleet-sentinel split: the watcher surfaces,
  the sentinel repairs (Class-1, capped, dedupe-guarded); neither does the other's job.

## [2026-08-30c] — Recovery detection, found by the actual recovery

Ed re-authenticated at 11:39 and `action-item-triage` ran successfully at 12:17 — the first
`Confirmed task run for:` in the log since 2026-08-19 04:10. Watching the watchdog handle that
exposed three defects, all now fixed.

### Fixed
- **The recovery line was counted as a failure.** `[oauth] clearing latched session_stale_relogin
  failures` contains the failure string, so `RE_STALE` matched the fix itself. Recovery and
  not-a-failure patterns are now tested before `RE_STALE`.
- **Recovery took ~20h to register.** Failure counts are windowed over 24h, so after a genuine fix
  the window still held the pre-fix failures and the verdict stayed `AUTH_BLOCKED`. A recovery
  marker newer than the newest failure is now decisive and overrides the counts.
- **The post-recovery message read as a second incident.** With auth fixed, 12 routines still
  carried fires missed *during* the outage, and the generic stale message announced them as
  "12 routines stale — no auth failure found". Fires due before the auth-recovery moment are now
  classified `backlog`: they never alert and clear when the routine next runs.

### Changed
- Recovery is detected **only** from `[CCDScheduledTasks] Confirmed task run for:`. `clearing
  latched ...` was tried first and rejected on evidence: the app clears the latch periodically and
  re-latches on the next failure, so it was the newest event for windows of up to **8.5 hours during
  the outage** (2026-08-26 19:38:33 → next failure 511 min later). Using it would have fired a false
  all-clear mid-outage. `Confirmed task run for:` appeared **zero** times across the 11 days.
- Recovery message now reports the **original** cause (`opened_verdict`, preserved at incident open
  rather than overwritten), lists only heartbeats *after* the recovery moment as "confirmed running",
  and states the backlog explicitly as expected rather than as a fault.

### Verified
- All six windows where a `clearing latched` line was newest still return `AUTH_BLOCKED`.
- The 2026-08-19 replay gate and the pre-incident control are unchanged.

## [2026-08-30b] — Heartbeat footers for the two routines that had none

### Added
- `rockwell-daily-capture` and `evening-digest` SKILL.md files gained attention-layer footers with
  heartbeat emission. These were the only two of 15 routines emitting no heartbeat at all, so during
  the 2026-08-19 outage their liveness could only be inferred from file mtimes — and for
  `evening-digest` that proxy is the weakest in the fleet, since other routines append to the same
  `digest.jsonl` and can make it look fresh while the job has not run in weeks.
- `evening-digest`'s footer is **adapted, not boilerplate**, for two hazards specific to it:
  (a) it owns the queue it files into, and duty 5 rewrites `digest.jsonl` atomically from a copy read
  at duty 1 — so a Lane-2 row appended before that rewrite is destroyed by its own move; the footer
  requires appending only afterward. (b) duties 1 and 4 both say "stop", which would have skipped the
  heartbeat on an empty queue or a failed delivery; the footer explicitly overrides both, since an
  empty queue is a healthy run and must still heartbeat `ok` or a quiet day is indistinguishable from
  a dead routine.

### Changed
- `fleet_watchdog.py` ROUTINES now lists `HB` first for both tasks, with the mtime proxies **retained
  as fallback** — neither can emit a heartbeat until it runs again, which is blocked on the auth fix,
  and `assess()` takes the freshest source so the proxy carries them until the first real heartbeat
  lands. Dropping the proxies now would blind the watchdog during the exact outage it was built for.

### Fixed
- Both footers specify the **colon form** of the UTC offset, generated with
  `/usr/bin/python3 -c "...astimezone().replace(microsecond=0).isoformat()"`. BSD `date '+%z'` emits
  `-0700` and macOS has no `%:z`; that is the source of the format drift that made 72 of 223 existing
  heartbeat rows unparseable by strict `fromisoformat`. New writes will be canonical.

## [2026-08-30] — Fleet watchdog: out-of-band monitoring after the 11-day auth outage

### Context
On 2026-08-19 07:08:16 the Claude Desktop OAuth session went stale (`session_stale_relogin`,
"sessionKey is valid but too old for the requested scope expansion"). Every unattended session
failed to start and all 15 scheduled routines died for 11 days with zero alerts. The app latched
the failure and never retried. 95 dispatches, 92 stale-clears, 0 successful scheduled sessions.

### Added
- `fleet_watchdog.py` — out-of-band liveness monitor on launchd (`com.edmatibag.fleet-watchdog`,
  hourly). No Claude session, no Claude auth, no MCP calls. Three independent detection layers
  (per-routine staleness from work artifacts, fleet-wide heartbeat silence, and a Claude-log grep
  that names the auth cause). Awake-tick ledger suppresses false alarms while the Mac is off;
  cold-start guard prevents alerting before any awake history exists. Dedup keyed on
  `(kind, auth_verdict, stale_set)` — measured at 7 sends across 271 ticks on the real outage
  window, vs 264 hourly repeats.
- `WATCHDOG.md` — rationale, detection design, TCC constraint, operate/test instructions, and the
  replay acceptance gate.
- Reciprocal check in `ops-watcher` SKILL.md step 2: flags if the watchdog's `last-ok` stamp is
  older than 3h, so launchd watches Claude *and* Claude watches launchd.

### Fixed
- **`watch.py` `parse_iso` crashed under `/usr/bin/python3` (3.9.6)** on 72 of 223 real
  `heartbeat.jsonl` rows (`-0700`-style offsets; 3.9's `fromisoformat` is strict). It survived only
  because the interactive PATH resolves to a newer python — any launchd invocation would have died
  on the first such row. Now tolerant and non-raising.

### Documented
- **`lastRunAt` is not a liveness signal** (AGENTS.md). It is stamped by the stale-dispatch timeout,
  so a dead fleet reports recent timestamps and `enabled: true`.
- **TCC:** the plist must invoke `/bin/bash`, not `/usr/bin/python3` directly — Full Disk Access
  attaches to the responsible process, and python invoked directly cannot read `~/Documents` or
  `~/Library/Logs/Claude`. Verified with a launchd probe.

### Known gaps (recorded, not hidden)
- `rockwell-daily-capture` and `evening-digest` emit no heartbeat footer; both fall back to
  file-mtime proxies until their SKILL.md files gain one.
- A Mac powered off for days is covered by neither layer; a Mac Studio–side LAN check would.

## [2026-07-28] — Initial release: extracted from AI-orchestration-layer

### Added
- `watch.py` — the V2 engine, extracted from AI-orchestration-layer `ops/watch.py` (built 7/27–7/28
  as the BUILD-PLAN Phase 4 attention-layer pilot there): deterministic routine health (cron
  prev-fire vs lastRunAt in local time, jitter + 45-min grace), heartbeat override (failed/partial
  self-reports beat start-time OK), not-yet-run/on-demand handling, launchd script-job checks
  (tokenburn pair keyed to the `last-success` stamp; evidence-file freshness for the rest), local
  port probes + remote Mac Studio HTTP probe with Last-Modified freshness, 8 purpose-group dashboard
  cards, >26h staleness banner, and dated `runs/history/` archives.
- Standard file set per REPO-STANDARD.md: AGENTS.md (data contract + invariants + verification
  gates), README, llms.txt, SCHEDULE.md, CONTRIBUTING.md (canonical copy), samples/ of all
  gitignored runtime shapes.
- Boundary kept clean on extraction: `digest.jsonl` (the Lane-2 queue) stays owned by
  AI-orchestration-layer's ESCALATION-POLICY.md — this repo reads it cross-repo; watcher-owned data
  (snapshot, heartbeats, ops-status, history) moved here. All 13 routine footers and the ops-watcher
  task prompt repointed the same day.

### Provenance
- Prior history of the engine (pilot, heartbeat channel, registry consolidation, V2 build) lives in
  AI-orchestration-layer's CHANGELOG entries 2026-07-27 through 2026-07-28b.
