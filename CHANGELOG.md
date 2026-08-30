# Changelog

All notable changes to Mission Control Dashboard are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); dates are America/Los_Angeles.
Gitignored data/output files are never committed.

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
