#!/usr/bin/env python3
"""Out-of-band liveness monitor for Ed's scheduled-routine fleet.

WHY THIS EXISTS
---------------
On 2026-08-19 07:08:16 the Claude Desktop app logged

    oauth authorize rejected with session_stale_relogin;
    sessionKey is valid but too old for the requested scope expansion

and from that moment EVERY scheduled routine failed to start with
"Cannot start session ...: Sign in again to continue".  Eleven days of total
automation silence followed with ZERO alerts, for three independent reasons:

  1. Interactive Claude sessions kept working -- only UNATTENDED sessions need
     the elevated scope -- so nothing looked broken in normal use.
  2. `lastRunAt` in the scheduled-tasks MCP is stamped by the "Cleared stale
     pending dispatch" TIMEOUT, not by a real run.  Every task therefore showed
     a recent lastRunAt and enabled:true while being entirely dead.
  3. ops-watcher -- the job built to catch dead routines -- is ITSELF a
     scheduled task, so it died in the same failure it exists to detect.

This watchdog defeats all three.  It runs on launchd, needs NO Claude session
and NO Claude auth, never reads lastRunAt, and judges liveness only from
WORK ARTIFACTS plus the Claude app log.  Its only network call is the Slack
webhook.

HARD RULES (carried over from ops-watcher's SKILL.md)
----------------------------------------------------
  * SURFACE, NEVER FIX.  No restarting Claude.app, no re-running routines,
    nothing that touches sign-in.  Credentials are never automated.
  * NEVER read scheduled-tasks-snapshot.json / lastRunAt / nextRunAt -- that is
    incident reason 2, and the snapshot is written by ops-watcher, i.e. inside
    the failure domain.
  * NEVER write to heartbeat.jsonl -- it would pollute the signal we read and
    create a self-satisfying liveness loop.
  * ALWAYS exit 0.  A watchdog that fails loudly still fails.

INTERPRETER: /usr/bin/python3 (3.9.6).  stdlib only, no venv, no deps.
NOTE: 3.9's datetime.fromisoformat is strict and CANNOT parse 72 of the 223
real heartbeat rows (e.g. '2026-07-28T18:17:09-0700').  watch.py's parse_iso
crashes on exactly this and only survives because Ed's interactive PATH
resolves to a newer python.  Hence parse_ts() below -- do not "simplify" it.
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
ORCHESTRATION = HOME / "Documents/Claude/Projects/AI-orchestration-layer"

HEARTBEAT = ROOT / "runs" / "heartbeat.jsonl"
STATE_FILE = ROOT / "runs" / "watchdog-state.json"
CLAUDE_LOGDIR = HOME / "Library/Logs/Claude"
LOGDIR = HOME / "Library/Logs/fleet-watchdog"
LAST_OK = LOGDIR / "last-ok"
DELIVERY_FAILING = LOGDIR / "DELIVERY-FAILING"
SLACK_ALERT = HOME / ".claude/lib/slack_alert.py"
SLACK_CHANNEL = "ai-briefing"

GRACE_DEFAULT = timedelta(minutes=120)
AWAKE_GRACE = timedelta(minutes=60)
LATE_SLACK = timedelta(seconds=120)
TICK_COVERAGE = timedelta(minutes=60)
FLEET_SILENCE_AWAKE = timedelta(hours=12)
AUTH_WINDOW = timedelta(hours=24)
EPISODE_GAP = timedelta(hours=36)
REMINDER_TICKS = 72
HARD_COOLDOWN = timedelta(hours=6)
SELF_TIMEOUT_S = 120
MAX_LOG_BYTES = 40 * 1024 * 1024
MAX_TICKS = 200
MIN_TICKS_FOR_STALE = 3   # cold-start guard: need real awake history before judging staleness
LOG_STALE_HOURS = 48

HB = "HB"          # heartbeat.jsonl row for this task
MT = "MTIME"       # file mtime, solid evidence
MW = "MTIME_WEAK"  # file mtime, but shared/ambiguous -> annotate, never alone-trigger

# (task_id, cron, grace_minutes, [(kind, path_or_None), ...])
ROUTINES = [
    ("substack-inbox-watcher",         "0 4 * * *",    120, [(HB, None)]),
    ("earnings-put-am-recheck",        "0 7 * * 1-5",  120, [(HB, None)]),
    ("daily-ai-morning-briefing",      "45 7 * * *",   180, [(HB, None)]),
    ("ops-watcher",                    "0 8 * * *",    120, [(HB, None), (MT, ROOT / "runs" / "ops-status.json")]),
    ("weekly-brain-review",            "0 8 * * 0",    240, [(HB, None)]),
    ("weekly-saltwater-fishing-report","0 9 * * 5",    240, [(HB, None)]),
    ("earnings-put-pxo-capture",       "30 11 * * 1-5",150, [(HB, None)]),
    ("earnings-put-weekly-scan",       "0 12 * * 5",   180, [(HB, None),
                                                             (MT, HOME / "Documents/Claude/earnings-put-screener/output/_launchd_scan.log")]),
    ("action-item-triage",             "15 12 * * *",  120, [(HB, None)]),
    ("longboard-daily-capture",        "0 18 * * *",   180, [(HB, None)]),
    ("claude-token-dashboard-update",  "10 18 * * *",  120, [(HB, None),
                                                             (MT, HOME / "Library/Logs/tokenburn/last-success")]),
    ("mastermind-daily-capture",       "30 18 * * 1-5",120, [(HB, None)]),
    # rockwell + evening-digest GAINED heartbeat footers 2026-08-30 (Ed approved).
    # HB is listed first but the mtime proxies are RETAINED as fallback: neither
    # task can emit a heartbeat until it actually runs again, which is blocked on
    # the auth fix. assess() takes the FRESHEST source, so the proxy carries them
    # until the first real heartbeat lands and then HB naturally wins. Dropping the
    # proxies now would blind the watchdog during exactly the outage it was built for.
    ("rockwell-daily-capture",         "45 18 * * *",  150, [(HB, None),
                                                             (MT, HOME / "Documents/Claude/Rockwell-Options-Capture/state.json")]),
    ("evening-digest",                 "5 19 * * *",   120, [(HB, None),
                                                             (MW, ORCHESTRATION / "runs" / "digest.jsonl")]),
    ("token-dashboard-sentinel",       "20 19 * * *",  120, [(HB, None)]),
]
# Deliberately excluded: freshwater-trip-log (manual), open-brain-wiki-update
# (disabled), earnings-put-t1-recheck (disabled).

LOCAL_TZ = datetime.now().astimezone().tzinfo


# ------------------------------------------------------------------ time utils

def parse_ts(s):
    """Tolerant ISO parser. Returns aware datetime in LOCAL_TZ, or None.

    Handles all four shapes present in real heartbeat.jsonl:
      2026-08-18T08:50:10-07:00   strict, fine everywhere
      2026-08-18T18:16:17-0700    3.9 raises -- normalize the offset
      2026-08-09T08:29            naive -- treat as local
      ...Z                        -> +00:00
    NEVER raises; a bad row is worth less than a crashed watchdog.
    """
    if not s or not isinstance(s, str):
        return None
    s = s.strip().replace("Z", "+00:00")
    m = re.search(r"([+-])(\d{2})(\d{2})$", s)
    if m:
        s = s[: m.start()] + "%s%s:%s" % (m.group(1), m.group(2), m.group(3))
    try:
        d = datetime.fromisoformat(s)
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=LOCAL_TZ)
    return d.astimezone(LOCAL_TZ)


def fmt(dt):
    if dt is None:
        return "never"
    return dt.strftime("%a %b %-d, %-I:%M %p")


def human_delta(td):
    secs = int(td.total_seconds())
    if secs < 0:
        secs = 0
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return "%dd %dh" % (d, h)
    if h:
        return "%dh %dm" % (h, m)
    return "%dm" % m


# ------------------------------------------------------------------ cron
# Ported verbatim from watch.py (cron_matches / prev_fire).  COPIED, not
# imported: watch.py carries module-level path+network constants and the
# parse_iso bug described in the module docstring.

def _field_matches(field, value, lo, hi):
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part == "*":
            rng = range(lo, hi + 1)
        elif "-" in part:
            a, b = part.split("-", 1)
            rng = range(int(a), int(b) + 1)
        else:
            rng = range(int(part), int(part) + 1)
        if value in rng and (value - rng.start) % step == 0:
            return True
    return False


def cron_matches(expr, dt):
    minute, hour, dom, mon, dow = expr.split()
    cron_dow = (dt.weekday() + 1) % 7  # cron: 0 = Sunday
    return (
        _field_matches(minute, dt.minute, 0, 59)
        and _field_matches(hour, dt.hour, 0, 23)
        and _field_matches(dom, dt.day, 1, 31)
        and _field_matches(mon, dt.month, 1, 12)
        and _field_matches(dow, cron_dow, 0, 6)
    )


def prev_fire(expr, now, lookback_days=9):
    dt = now.replace(second=0, microsecond=0)
    for _ in range(lookback_days * 1440):
        if cron_matches(expr, dt):
            return dt
        dt -= timedelta(minutes=1)
    return None


def count_fires(expr, since, until):
    n = 0
    dt = until.replace(second=0, microsecond=0)
    guard = 0
    while dt > since and guard < 40 * 1440:
        if cron_matches(expr, dt):
            n += 1
        dt -= timedelta(minutes=1)
        guard += 1
    return n


# ------------------------------------------------------------------ state

def load_state(path):
    try:
        with open(path) as fh:
            st = json.load(fh)
        if not isinstance(st, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        return fresh_state(), False
    except Exception:
        try:
            shutil.move(str(path), str(path) + ".corrupt-%d" % int(time.time()))
        except Exception:
            pass
        return fresh_state(), True
    st.setdefault("schema", 1)
    st.setdefault("ticks", [])
    st.setdefault("incident", {"state": "none"})
    st.setdefault("routines", {})
    return st, False


def fresh_state():
    return {
        "_spec": ("Persistent state for fleet_watchdog.py. Purpose is DEDUP: an ongoing known "
                  "outage must not re-alert every hour. Same first-time-only philosophy as "
                  "ai-briefing data/flags.json and data/source-health.json. NEVER send a message "
                  "restating an unchanged known-bad state."),
        "schema": 1,
        "ticks": [],
        "incident": {"state": "none"},
        "routines": {},
        "last_outcome": None,
        "last_tick": None,
    }


def save_state(path, st):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=2)
    os.replace(tmp, str(path))


def awake_minutes(ticks, a, b):
    """Minutes the Mac is *evidenced* to have been awake in [a,b].

    Each recorded hourly tick evidences the hour ending at it. A routine due
    while the Mac was powered off accrues no ticks, so no alert fires until the
    machine has genuinely been up. Sleep handling is mechanism, not a special case.
    """
    if b <= a:
        return 0.0
    covered = 0.0
    for t in ticks:
        lo = max(a, t - TICK_COVERAGE)
        hi = min(b, t)
        if hi > lo:
            covered += (hi - lo).total_seconds()
    return covered / 60.0


# ------------------------------------------------------------------ evidence

def read_heartbeats(path, upto=None):
    """task -> latest timestamp. Unparseable rows are skipped, never fatal.

    `upto` bounds the scan so a --now replay cannot see evidence from its own
    future. Without it the pre-incident control run reads post-incident rows and
    the whole test loses its meaning.
    """
    latest = {}
    newest = None
    try:
        fh = open(path)
    except Exception:
        return latest, None
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = parse_ts(d.get("ts"))
            if ts is None:
                continue
            if upto is not None and ts > upto:
                continue
            task = d.get("task")
            if task and (task not in latest or ts > latest[task]):
                latest[task] = ts
            if newest is None or ts > newest:
                newest = ts
    return latest, newest


def mtime_of(p):
    try:
        return datetime.fromtimestamp(Path(p).stat().st_mtime, LOCAL_TZ)
    except Exception:
        return None


# ------------------------------------------------------------------ auth log

RE_LINE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
RE_STALE = re.compile(r"session_stale_relogin")
RE_LATCH = re.compile(r"short-circuiting fresh /authorize on latched session_stale_relogin")
RE_CANNOT = re.compile(r"Cannot start session")
RE_CLEARED = re.compile(r"\[CCDScheduledTasks\] Cleared stale pending dispatch for: (\S+)")

# POSITIVE RECOVERY MARKERS. Two reasons these exist, both found 2026-08-30 when the
# real outage was fixed and the watchdog failed to notice:
#   1. "clearing latched session_stale_relogin failures" CONTAINS the failure string,
#      so RE_STALE matched the fix itself and counted it as a failure. Recovery lines
#      must be tested FIRST and excluded from the failure tally.
#   2. The failure counts are windowed over 24h, so after a genuine fix the window
#      still holds the pre-fix failures and the verdict stayed AUTH_BLOCKED for ~20
#      more hours -- delaying the recovery message by most of a day. A recovery marker
#      NEWER than the newest failure is decisive and overrides the counts.
# "Confirmed task run for:" is the strongest of these: it appears only when a scheduled
# session actually started, and appeared ZERO times during the 11-day outage.
# ONLY "Confirmed task run for:" counts as recovery. It is emitted when a scheduled
# session actually STARTED, and it appeared ZERO times inside the 11-day outage
# (last before: 2026-08-19 04:10:13; first after: 2026-08-30 12:17:21).
#
# "clearing latched ..." was tried here first and REJECTED on the evidence: the app
# clears the latch periodically and re-latches on the next failure, so it was the
# newest event for windows of up to 8.5 HOURS *during* the outage (e.g. 2026-08-26
# 19:38:33, next failure 511 min later). Treating it as recovery would have fired a
# false all-clear mid-outage and then re-alerted -- exactly the flapping the dedup
# logic exists to prevent. It is still excluded from the FAILURE tally below, since
# the line contains the failure string, but it proves nothing on its own.
RE_RECOVER = re.compile(r"\[CCDScheduledTasks\] Confirmed task run for")

# Contains "session_stale_relogin" but is NOT a failure -- must not inflate the count.
RE_NOT_FAILURE = re.compile(r"clearing latched session_stale_relogin failures")


def scan_auth(logdir, now):
    # NOTE: every timestamp test below is bounded ABOVE by `now` as well as
    # below by `cutoff`. A --now replay must never count log lines written
    # after the moment being replayed -- that bug made the pre-incident
    # control run report AUTH_BLOCKED off the post-incident lines.
    """Grep the Claude app log for the failure signatures.

    Returns a dict. verdict is one of AUTH_OK / AUTH_WARN / AUTH_BLOCKED /
    LOG_UNREADABLE. LOG_UNREADABLE must NEVER be rendered as AUTH_OK -- a TCC
    change would otherwise blind the cause layer silently.
    """
    out = {"verdict": "LOG_UNREADABLE", "n_stale": 0, "n_cannot": 0, "latched": False,
           "cleared": {}, "episode_start": None, "newest_line": None, "files_read": 0,
           "newest_failure": None, "newest_recovery": None}
    logdir = Path(logdir)
    names = ["main.log", "main1.log", "main2.log", "main3.log", "main4.log"]
    files = [logdir / n for n in names if (logdir / n).exists()]
    if not files:
        return out

    cutoff = now - AUTH_WINDOW
    budget = MAX_LOG_BYTES
    stale_times = []

    for f in files:
        try:
            size = f.stat().st_size
        except Exception:
            continue
        if budget <= 0:
            break
        budget -= size
        try:
            fh = open(f, "r", errors="replace")
        except Exception:
            continue
        out["files_read"] += 1
        with fh:
            for line in fh:
                m = RE_LINE_TS.match(line)
                if not m:
                    continue
                try:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ)
                except Exception:
                    continue
                if ts > now:
                    continue
                if out["newest_line"] is None or ts > out["newest_line"]:
                    out["newest_line"] = ts
                # Order matters: both branches below must be tested BEFORE RE_STALE,
                # because the latch-clearing line contains the failure string.
                if RE_RECOVER.search(line):
                    if out["newest_recovery"] is None or ts > out["newest_recovery"]:
                        out["newest_recovery"] = ts
                    continue
                if RE_NOT_FAILURE.search(line):
                    continue
                if RE_STALE.search(line):
                    stale_times.append(ts)
                    if out["newest_failure"] is None or ts > out["newest_failure"]:
                        out["newest_failure"] = ts
                    if ts >= cutoff:
                        out["n_stale"] += 1
                    if RE_LATCH.search(line) and ts >= cutoff:
                        out["latched"] = True
                if ts >= cutoff:
                    if RE_CANNOT.search(line):
                        out["n_cannot"] += 1
                        if out["newest_failure"] is None or ts > out["newest_failure"]:
                            out["newest_failure"] = ts
                    cm = RE_CLEARED.search(line)
                    if cm:
                        out["cleared"][cm.group(1)] = out["cleared"].get(cm.group(1), 0) + 1

    # Episode start = first timestamp of the final contiguous run of failures.
    if stale_times:
        stale_times.sort()
        start = stale_times[0]
        for a, b in zip(stale_times, stale_times[1:]):
            if b - a > EPISODE_GAP:
                start = b
        out["episode_start"] = start

    rec, fail = out["newest_recovery"], out["newest_failure"]
    recovered = rec is not None and (fail is None or rec > fail)

    if out["newest_line"] is None or (now - out["newest_line"]) > timedelta(hours=LOG_STALE_HOURS):
        out["verdict"] = "LOG_UNREADABLE"
    elif recovered:
        # A recovery marker newer than the newest failure is decisive, regardless of
        # how many failures remain inside the 24h window.
        out["verdict"] = "AUTH_OK"
        out["latched"] = False
    elif out["n_cannot"] >= 1:
        out["verdict"] = "AUTH_BLOCKED"
    elif out["n_stale"] >= 1:
        out["verdict"] = "AUTH_WARN"
    else:
        out["verdict"] = "AUTH_OK"
    return out


def claude_running():
    try:
        r = subprocess.run(["pgrep", "-f", "/Applications/Claude.app/Contents/MacOS/Claude"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return r.returncode == 0
    except Exception:
        return None


# ------------------------------------------------------------------ assess

def assess(now, ticks, hb_latest, hb_newest, auth_ok_since=None):
    # COLD START: with no tick history we have no evidence the Mac was awake, so
    # we cannot distinguish "routine did not run" from "machine was off". A fresh
    # or reset state must therefore never emit a staleness verdict -- it builds
    # the ledger first. (Without this, first install alerts on 10 routines.)
    cold = len(ticks) < MIN_TICKS_FOR_STALE
    # BACKLOG vs STALE. After an auth outage is fixed, every routine still carries the
    # fires it missed while sign-in was broken. Those are a CONSEQUENCE of the resolved
    # incident, not a new fault, and reporting them as "12 routines stale - no auth
    # failure found" reads as a second incident. A fire that was DUE BEFORE auth
    # recovered is classified "backlog": it clears on its own when the routine next
    # runs, and it never alerts. Only a fire missed AFTER recovery is a real fault.
    rows = []
    for task, cron, grace_min, evidence in ROUTINES:
        r = {"id": task, "cron": cron, "state": "ok", "missed": 0,
             "evidence": None, "weak": False, "expected": None}
        expected = prev_fire(cron, now)
        r["expected"] = expected
        if expected is None:
            rows.append(r)
            continue
        deadline = expected + timedelta(minutes=grace_min)
        if now < deadline:
            r["state"] = "pending"
            rows.append(r)
            continue
        if cold or awake_minutes(ticks, deadline, now) <= AWAKE_GRACE.total_seconds() / 60.0:
            r["state"] = "asleep"
            rows.append(r)
            continue

        best = None
        only_weak = True
        for kind, path in evidence:
            if kind == HB:
                ts = hb_latest.get(task)
                if ts is not None:
                    only_weak = False
            else:
                ts = mtime_of(path)
                if ts is not None and ts > now:
                    ts = None          # replay must not see its own future
                if ts is not None and kind == MT:
                    only_weak = False
            if ts is not None and (best is None or ts > best):
                best = ts
        r["evidence"] = best
        if best is None:
            r["state"] = "unknown"          # AGENTS.md invariant: absence != failure
        elif best >= expected - LATE_SLACK:
            r["state"] = "ok"
        else:
            r["weak"] = only_weak
            r["missed"] = count_fires(cron, best, now)
            if auth_ok_since is not None and expected < auth_ok_since:
                r["state"] = "backlog"   # missed while auth was broken; clears on next run
            else:
                r["state"] = "stale"
        rows.append(r)

    fleet_silent = False
    if auth_ok_since is not None:
        pass  # backlog silence is expected right after a fix; not a fleet-down signal
    elif hb_newest is not None and not cold:
        fleet_silent = awake_minutes(ticks, hb_newest, now) >= FLEET_SILENCE_AWAKE.total_seconds() / 60.0
    return rows, fleet_silent


# ------------------------------------------------------------------ messages

def _cooldown_expired(inc, now):
    last = parse_ts(inc.get("last_alert_at"))
    return (last is None) or ((now - last) >= HARD_COOLDOWN)


def render_auth_msg(now, auth, rows, hb_newest, hb_latest, stale, running):
    ep = auth.get("episode_start")
    cleared = auth.get("cleared") or {}
    names = sorted(cleared.keys())
    shown = ", ".join(names[:4])
    if len(names) > 4:
        shown += ", +%d more" % (len(names) - 4)
    lines = []
    lines.append("*[fleet-watchdog] Scheduled runs are blocked — Claude sign-in is stale*")
    lines.append("")
    latch = ("The app latched this at *%s* and now short-circuits every fresh `/authorize`, "
             "so it will *not* self-heal on its own." % fmt(ep)) if auth.get("latched") else \
            ("First seen *%s*." % fmt(ep))
    lines.append("*Cause:* `session_stale_relogin`. " + latch)
    lines.append("")
    lines.append("*Evidence (last 24h):* %d × `Cannot start session` · %d dispatches dropped as stale%s."
                 % (auth.get("n_cannot", 0), sum(cleared.values()), (" (%s)" % shown) if shown else ""))
    if hb_newest is not None:
        who = None
        for t, ts in hb_latest.items():
            if ts == hb_newest:
                who = t
                break
        lines.append("*Silence:* no heartbeat from any routine for *%s* — last row was %s, %s."
                     % (human_delta(now - hb_newest), who or "unknown", fmt(hb_newest)))
    if stale:
        lines.append("")
        lines.append("*Stale (%d of %d):* %s" % (len(stale), len(ROUTINES), " · ".join(sorted(stale))))
    lines.append("")
    lines.append("*Fix:* open Claude Desktop → sign out → sign in again. Only unattended sessions need "
                 "the elevated scope, so interactive chat keeps working normally and nothing else looks broken.")
    lines.append("")
    lines.append("_Note: `lastRunAt` in the Scheduled sidebar will still look recent — it is stamped by the "
                 "stale-dispatch timeout, not by a real run. Ignore it._")
    if running is False:
        lines.append("_Claude Desktop is not currently running._")
    lines.append("")
    lines.append("Detail: `~/Library/Logs/fleet-watchdog/watchdog.log`")
    return "\n".join(lines)


def render_stale_msg(now, auth, rows, stale, weak_stale, running):
    byid = dict((r["id"], r) for r in rows)
    lines = []
    lines.append("*[fleet-watchdog] %d scheduled routine%s stale — no auth failure found*"
                 % (len(stale), " is" if len(stale) == 1 else "s are"))
    lines.append("")
    if auth.get("verdict") == "LOG_UNREADABLE":
        lines.append("The Claude log could *not be read*, so the cause layer is blind — check permissions on "
                     "`~/Library/Logs/Claude/`.")
    else:
        lines.append("The Claude log shows no `session_stale_relogin` and no `Cannot start session` in the "
                     "last 24h, so this is *not* the Aug-19 failure mode."
                     + (" Claude Desktop is *not running*." if running is False else ""))
    lines.append("")
    lines.append("*Missed:*")
    for t in sorted(stale):
        r = byid[t]
        lines.append("• %s — %d fire%s missed, last evidence %s"
                     % (t, r["missed"], "" if r["missed"] == 1 else "s", fmt(r["evidence"])))
    healthy = sum(1 for r in rows if r["state"] in ("ok", "pending", "backlog", "asleep"))
    lines.append("")
    lines.append("*Healthy:* %d of %d." % (healthy, len(ROUTINES)))
    if weak_stale:
        lines.append("*Weak evidence:* %s — no heartbeat footer, status inferred from file mtime."
                     % ", ".join(sorted(weak_stale)))
    lines.append("")
    lines.append("Next: check the Scheduled sidebar and `~/Library/Logs/Claude/main.log`.")
    return "\n".join(lines)


def render_recovery_msg(now, inc, rows, hb_latest, auth_ok_since, backlog):
    opened = parse_ts(inc.get("episode_start")) or parse_ts(inc.get("opened_at"))
    cause = inc.get("opened_verdict") or inc.get("auth_verdict") or "unknown"
    lines = []
    lines.append("*[fleet-watchdog] Recovered* — scheduled runs are working again, %s." % fmt(now))
    lines.append("")
    if opened and (now - opened).total_seconds() > 0:
        lines.append("Outage ran *%s* (%s → %s), cause `%s`, %d alert%s sent."
                     % (human_delta(now - opened), fmt(opened), fmt(now), cause,
                        inc.get("alert_count", 0), "" if inc.get("alert_count", 0) == 1 else "s"))
    # Only heartbeats AFTER the recovery moment are evidence of "back" -- listing
    # pre-outage timestamps here would read as if dead routines had returned.
    if auth_ok_since is not None:
        back = sorted(((ts, t) for t, ts in hb_latest.items() if ts >= auth_ok_since), reverse=True)
        if back:
            lines.append("*Confirmed running:* " + " · ".join(
                "%s %s" % (t, ts.strftime("%-I:%M %p")) for ts, t in back[:4]))
    if backlog:
        lines.append("")
        lines.append("*%d routine%s still carry missed fires from the outage* — this is expected "
                     "backlog, not a new fault. Each clears on its own when it next runs: %s"
                     % (len(backlog), "" if len(backlog) == 1 else "s", " · ".join(backlog[:6])
                        + (" +%d more" % (len(backlog) - 6) if len(backlog) > 6 else "")))
        lines.append("_Weekly jobs (brain review, saltwater report) wait for their next weekday slot._")
    lines.append("")
    lines.append("Backfill is *not* automatic — skipped runs stay skipped.")
    return "\n".join(lines)


# ------------------------------------------------------------------ delivery

def send_slack(msg, dry_run, logf):
    """Deliver via slack_alert.py.

    CRITICAL: slack_alert.py ALWAYS exits 0 and reports outcome on stdout. If we
    treated exit 0 as delivered, a revoked webhook would silently eat the one
    first-time alert and the dedup logic would then suppress forever --
    reproducing the original incident with extra steps. So: require 'alert-sent'.
    """
    if dry_run:
        logf("DRY-RUN would send:\n" + msg)
        return True, "dry-run"
    if not SLACK_ALERT.exists():
        return False, "slack_alert.py missing"
    try:
        p = subprocess.run(["/usr/bin/python3", str(SLACK_ALERT), SLACK_CHANNEL, "-"],
                           input=msg, capture_output=True, text=True, timeout=60)
        out = (p.stdout or "").strip()
    except Exception as e:
        return False, "exec failed: %s" % e
    if out.startswith("alert-sent"):
        return True, out
    return False, out or "no output"


def notify_local(text):
    try:
        subprocess.run(["osascript", "-e",
                        'display notification %s with title "fleet-watchdog"' % json.dumps(text)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except Exception:
        pass


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="Out-of-band liveness monitor for Ed's scheduled routines.")
    ap.add_argument("--dry-run", action="store_true", help="print verdict + message; write nothing, send nothing")
    ap.add_argument("--now", help="override current time (ISO) for replay/testing")
    ap.add_argument("--state", help="state file path")
    ap.add_argument("--heartbeat", help="heartbeat.jsonl path")
    ap.add_argument("--logdir", help="Claude log directory")
    ap.add_argument("--test-alert", action="store_true", help="send one throwaway Slack message and exit")
    args = ap.parse_args()

    LOGDIR.mkdir(parents=True, exist_ok=True)

    def logf(s):
        stamp = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
        print("%s %s" % (stamp, s), flush=True)

    if args.test_alert:
        ok, detail = send_slack("*[fleet-watchdog]* test alert — delivery path is working. "
                                "No action needed.", False, logf)
        logf("test-alert ok=%s detail=%s" % (ok, detail))
        return

    now = parse_ts(args.now) if args.now else datetime.now(LOCAL_TZ)
    if now is None:
        logf("bad --now, using wall clock")
        now = datetime.now(LOCAL_TZ)

    state_path = Path(args.state) if args.state else STATE_FILE
    hb_path = Path(args.heartbeat) if args.heartbeat else HEARTBEAT
    logdir = Path(args.logdir) if args.logdir else CLAUDE_LOGDIR

    st, was_corrupt = load_state(state_path)
    if was_corrupt:
        logf("state file was corrupt; reset and moved aside")

    ticks = [t for t in (parse_ts(x) for x in st.get("ticks", [])) if t is not None]
    ticks.append(now)
    ticks = sorted(set(ticks))[-MAX_TICKS:]
    st["ticks"] = [t.isoformat() for t in ticks]

    hb_latest, hb_newest = read_heartbeats(hb_path, upto=now)

    auth = scan_auth(logdir, now)
    auth_ok_since = auth["newest_recovery"] if auth["verdict"] == "AUTH_OK" else None
    rows, fleet_silent = assess(now, ticks, hb_latest, hb_newest, auth_ok_since)
    running = claude_running()

    stale = sorted(r["id"] for r in rows if r["state"] == "stale" and not r["weak"])
    weak_stale = sorted(r["id"] for r in rows if r["state"] == "stale" and r["weak"])
    backlog = sorted(r["id"] for r in rows if r["state"] == "backlog")

    bad = bool(stale) or fleet_silent or auth["verdict"] in ("AUTH_BLOCKED", "AUTH_WARN")
    kind = "auth" if auth["verdict"] in ("AUTH_BLOCKED", "AUTH_WARN") else ("stale" if bad else "none")
    signature = json.dumps([kind, auth["verdict"], stale], sort_keys=True)

    logf("tick now=%s verdict=%s stale=%d weak=%d backlog=%d fleet_silent=%s claude_running=%s log_files=%d"
         % (now.isoformat(), auth["verdict"], len(stale), len(weak_stale), len(backlog), fleet_silent,
            running, auth["files_read"]))

    inc = st.get("incident", {"state": "none"})
    outcome = "clean"
    msg = None
    is_recovery = False

    if bad and inc.get("state") != "open":
        msg = (render_auth_msg(now, auth, rows, hb_newest, hb_latest, stale, running)
               if kind == "auth" else
               render_stale_msg(now, auth, rows, stale, weak_stale, running))
        inc = {"state": "open", "kind": kind, "auth_verdict": auth["verdict"],
               "opened_kind": kind, "opened_verdict": auth["verdict"],
               "stale_set": stale, "signature": signature,
               "opened_at": now.isoformat(),
               "episode_start": auth["episode_start"].isoformat() if auth["episode_start"] else None,
               "alert_count": 0, "consecutive_runs_in_state": 0, "pending_delivery": False,
               "first_alert_at": None, "last_alert_at": None}
    elif bad and inc.get("state") == "open":
        prev_stale = set(inc.get("stale_set") or [])
        escalated = (auth["verdict"] == "AUTH_BLOCKED" and inc.get("auth_verdict") == "AUTH_WARN")
        widened = set(stale) > prev_stale
        changed_kind = kind != inc.get("kind")
        if escalated or widened or changed_kind:
            msg = (render_auth_msg(now, auth, rows, hb_newest, hb_latest, stale, running)
                   if kind == "auth" else
                   render_stale_msg(now, auth, rows, stale, weak_stale, running))
            inc["consecutive_runs_in_state"] = 0
        elif inc.get("consecutive_runs_in_state", 0) >= REMINDER_TICKS:
            base = (render_auth_msg(now, auth, rows, hb_newest, hb_latest, stale, running)
                    if kind == "auth" else
                    render_stale_msg(now, auth, rows, stale, weak_stale, running))
            opened = parse_ts(inc.get("episode_start")) or parse_ts(inc.get("opened_at"))
            msg = base + ("\n\n_Still down — day %d._" % ((now - opened).days + 1) if opened else "\n\n_Still down._")
            inc["consecutive_runs_in_state"] = 0
        elif inc.get("pending_delivery"):
            # A genuine delivery FAILURE -- retry immediately, cooldown does not apply.
            msg = (render_auth_msg(now, auth, rows, hb_newest, hb_latest, stale, running)
                   if kind == "auth" else
                   render_stale_msg(now, auth, rows, stale, weak_stale, running))
        elif inc.get("deferred") and _cooldown_expired(inc, now):
            # An escalation held back by the cooldown -- send the CURRENT state now.
            msg = (render_auth_msg(now, auth, rows, hb_newest, hb_latest, stale, running)
                   if kind == "auth" else
                   render_stale_msg(now, auth, rows, stale, weak_stale, running))
            inc["deferred"] = False
        else:
            outcome = "suppressed"
            inc["consecutive_runs_in_state"] = inc.get("consecutive_runs_in_state", 0) + 1
        inc["kind"] = kind
        inc["auth_verdict"] = auth["verdict"]
        # A SHRINKING stale set never alerts -- partial recovery is logged only.
        inc["stale_set"] = sorted(set(stale) | prev_stale) if not widened else stale
        inc["signature"] = signature
    elif (not bad) and inc.get("state") == "open":
        msg = render_recovery_msg(now, inc, rows, hb_latest, auth_ok_since, backlog)
        is_recovery = True

    # Hard spam floor -- never applies to a recovery or a failed-delivery retry.
    if msg and not is_recovery and not inc.get("pending_delivery"):
        if not _cooldown_expired(inc, now):
            logf("within hard cooldown; deferring escalation")
            inc["deferred"] = True
            msg = None
            outcome = "suppressed"

    if msg:
        ok, detail = send_slack(msg, args.dry_run, logf)
        logf("delivery ok=%s detail=%s" % (ok, detail))
        if ok:
            outcome = "alerted"
            if not args.dry_run:
                if is_recovery:
                    inc = {"state": "none"}
                else:
                    inc["alert_count"] = inc.get("alert_count", 0) + 1
                    inc["first_alert_at"] = inc.get("first_alert_at") or now.isoformat()
                    inc["last_alert_at"] = now.isoformat()
                    inc["pending_delivery"] = False
                    inc["deferred"] = False
                try:
                    DELIVERY_FAILING.unlink()
                except Exception:
                    pass
        else:
            outcome = "delivery-failed"
            inc["pending_delivery"] = True
            inc["delivery_failures"] = inc.get("delivery_failures", 0) + 1
            notify_local("Alert delivery failed: %s" % detail)
            if inc["delivery_failures"] >= 3:
                try:
                    DELIVERY_FAILING.write_text("%s %s\n" % (now.isoformat(), detail))
                except Exception:
                    pass

    st["incident"] = inc
    st["routines"] = dict((r["id"], {"state": r["state"],
                                     "last_evidence": r["evidence"].isoformat() if r["evidence"] else None})
                          for r in rows)
    st["last_outcome"] = outcome
    st["last_tick"] = now.isoformat()

    if not args.dry_run:
        save_state(state_path, st)
        try:
            LAST_OK.write_text("%d\n" % int(time.time()))
        except Exception:
            pass

    logf("outcome=%s incident=%s" % (outcome, inc.get("state")))


def _timeout(signum, frame):
    # A wedged watchdog holding the hourly slot is worse than none: it looks installed.
    print("fleet-watchdog: self-timeout after %ds" % SELF_TIMEOUT_S, flush=True)
    os._exit(0)


if __name__ == "__main__":
    try:
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(SELF_TIMEOUT_S)
    except Exception:
        pass
    try:
        main()
    except Exception as e:
        import traceback
        print("fleet-watchdog: unhandled error: %s" % e, flush=True)
        traceback.print_exc()
    sys.exit(0)  # ALWAYS exit 0
