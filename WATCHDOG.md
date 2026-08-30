# Fleet Watchdog

Out-of-band liveness monitor for the 15 enabled Claude scheduled routines.
Runs on launchd. **Requires no Claude session and no Claude auth**, by design.

## Why it exists

On **2026-08-19 07:08:16** the Claude Desktop app logged:

```
oauth authorize rejected with session_stale_relogin;
sessionKey is valid but too old for the requested scope expansion
```

From that moment every unattended session failed to start
(`Cannot start session ...: Sign in again to continue`), and each dispatch was discarded
~13 minutes later as stale. **All 15 scheduled routines died at once.** Eleven days passed
before anyone noticed. Across the app logs for that period: **95 dispatches, 92 stale-clears,
0 successful scheduled sessions.**

Three independent reasons it stayed invisible — the watchdog is built to defeat all three:

| # | Why it hid | How the watchdog defeats it |
|---|---|---|
| 1 | **Interactive sessions kept working.** Only *unattended* sessions need the elevated scope, so nothing looked broken in normal use. | Judges liveness from routine *work artifacts*, not from whether Claude feels usable. |
| 2 | **`lastRunAt` lies.** It is stamped by the "Cleared stale pending dispatch" **timeout**, so every task showed a recent `lastRunAt` and `enabled: true` while dead. | **Never reads `lastRunAt`, `nextRunAt`, or `scheduled-tasks-snapshot.json`** — not even as a tiebreaker. |
| 3 | **The watchdog shared the failure domain.** `ops-watcher` is itself a scheduled task, so it died in the very failure it exists to detect. | Runs on **launchd**, not on Claude. Plus a reciprocal check (below) so each layer watches the other. |

## Detection — three independent layers

1. **Per-routine staleness.** For each routine, compute the previous due fire from its cron,
   add a grace period, and compare against work artifacts: a `heartbeat.jsonl` row, else the
   routine's own output-file mtime.
2. **Fleet aggregate.** 15 routines appending to one file makes total silence a near-perfect
   single-bit signal that does not depend on the expectation table being correct.
3. **Auth cause.** Greps `~/Library/Logs/Claude/main*.log` for `session_stale_relogin`,
   `Cannot start session`, and `Cleared stale pending dispatch for:`. This is what lets the
   alert say *"sign in again"* instead of *"things are quiet."*
   `LOG_UNREADABLE` never renders as `AUTH_OK`.

## False-alarm suppression

Every hourly tick is recorded, and the tick ledger doubles as evidence of **when the Mac was
actually awake**. A routine due while the machine was off accrues no ticks, so nothing fires
until it has genuinely been up past the deadline. Sleep handling is mechanism, not special-casing.
Weekday-only and weekly jobs are handled by the ported cron logic, not by exceptions.

A **cold-start guard** (`MIN_TICKS_FOR_STALE = 3`) means a fresh or reset state never emits a
staleness verdict — with no tick history there is no evidence the Mac was awake, so
"did not run" and "machine was off" are indistinguishable. Without this, first install alerted
on 10 routines.

## Alert fatigue

Dedup follows the same first-time-only discipline as `ai-briefing`'s `flags.json` and
`source-health.json`. Dedup key is `(kind, auth_verdict, stale_set)` so a *changed* cause
re-alerts rather than being swallowed. A **shrinking** stale set never alerts. Hard floor of
6h between sends; reminders every ~72 ticks (3 days).

Measured on the real Aug 19–30 window: **7 sends across 271 ticks** (initial + 3 escalations
6h apart as more routines fell over, then 3 reminders 3 days apart) instead of 264 hourly repeats.

## Delivery

Via `~/.claude/lib/slack_alert.py`, channel key `ai-briefing`, prefixed `*[fleet-watchdog]*`.

**`slack_alert.py` always exits 0**, so the watchdog requires the literal `alert-sent` prefix on
stdout. Treating exit 0 as delivered would let a revoked webhook silently eat the one first-time
alert while dedup suppressed forever — reproducing the original incident with extra steps.
On failure: retry next tick (cooldown does not apply), `osascript` fallback, and a
`DELIVERY-FAILING` marker after 3 consecutive failures.

## ⚠ TCC — the plist MUST invoke `/bin/bash`, not `/usr/bin/python3`

macOS attaches Full Disk Access to the **responsible process**. `/bin/bash` holds the grant and
children inherit it; `/usr/bin/python3` as `ProgramArguments[0]` is itself responsible, has no
grant, and dies with:

```
can't open file '.../fleet_watchdog.py': [Errno 1] Operation not permitted
```

Verified empirically 2026-08-30 with a launchd probe: under `/bin/bash` both `~/Documents` and
`~/Library/Logs/Claude` are readable; invoked directly, neither is. This is the same constraint
that forces the `ai-briefing` site to mirror into `/Users/Shared`.

## Interpreter

`/usr/bin/python3` is **3.9.6**, whose `fromisoformat` is strict and cannot parse 72 of the 223
real `heartbeat.jsonl` rows (e.g. `2026-07-28T18:17:09-0700`). `fleet_watchdog.py` carries its
own tolerant `parse_ts` — do not "simplify" it. The same bug was found and fixed in `watch.py`,
which had been surviving only because Ed's interactive PATH resolves to a newer python.

## Coverage gaps (known, not hidden)

- `rockwell-daily-capture` and `evening-digest` emit **no heartbeat footer at all**; both use
  file-mtime proxies. Adding the standard footer to their SKILL.md files is the real fix.
- `ops-watcher`'s own heartbeat is unreliable (5 rows, last 2026-08-16), hence its `ops-status.json`
  mtime as a secondary source.
- **Mac powered off for days:** neither layer runs. No local design covers this; a Mac Studio–side
  check over the LAN would.

## Who watches the watchman

`ops-watcher` step 2 reads `~/Library/Logs/fleet-watchdog/last-ok` and flags if it is older than
3 hours. launchd watches Claude; Claude watches launchd. **Neither layer can hide its own death** —
which is precisely what went wrong on 2026-08-19.

## Operate

```bash
# install / reload
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.edmatibag.fleet-watchdog.plist
launchctl kickstart -k gui/$(id -u)/com.edmatibag.fleet-watchdog
launchctl print gui/$(id -u)/com.edmatibag.fleet-watchdog | grep -E 'state|last exit'

# inspect
tail -20 ~/Library/Logs/fleet-watchdog/watchdog.log

# test without waiting for an outage
/usr/bin/python3 fleet_watchdog.py --dry-run --state /tmp/s.json --now 2026-08-19T20:00
/usr/bin/python3 fleet_watchdog.py --test-alert     # sends one real Slack message

# uninstall
launchctl bootout gui/$(id -u)/com.edmatibag.fleet-watchdog
```

## Acceptance gate — replay the real incident

`main1.log` (carrying the 07:08:16 origin) and the frozen `heartbeat.jsonl` are still on disk
unmodified, so the incident replays against real data with nothing fabricated:

```bash
# control: pre-incident, must be CLEAN
/usr/bin/python3 fleet_watchdog.py --dry-run --state /tmp/s.json --now 2026-08-18T09:00
# gate: must open an incident, report AUTH_BLOCKED, and name 2026-08-19 07:08:16
/usr/bin/python3 fleet_watchdog.py --dry-run --state /tmp/s.json --now 2026-08-19T20:00
```

**If the gate does not fire, the watchdog is not finished.** Both currently pass.
