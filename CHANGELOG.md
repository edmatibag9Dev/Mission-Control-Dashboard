# Changelog

All notable changes to Mission Control Dashboard are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); dates are America/Los_Angeles.
Gitignored data/output files are never committed.

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
