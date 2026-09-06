#!/usr/bin/env python3
"""watch.py — Mission Control Dashboard: deterministic run-health engine for the Ops Watcher.

Extracted 2026-07-28 from AI-orchestration-layer (Phase 4 attention layer) into its own repo.

V2 (2026-07-28, Ed's locked spec): card-per-group routine layout (8 groups), launchd
script-job checks, local + remote server probes, dashboard staleness self-check banner,
and dated ops-status archival for the future run-history strip.

Reads:
  runs/scheduled-tasks-snapshot.json  — verbatim output of list_scheduled_tasks, written by the watcher agent
  runs/digest.jsonl                   — Lane-2 queue (ESCALATION-POLICY.md Mechanics)
  runs/heartbeat.jsonl                — self-reported {task, ts, status, note} from routine footers
  launchd job evidence files + server ports/URLs (see SCRIPT_JOBS / SERVERS)

Writes:
  runs/ops-status.json                — machine-readable health snapshot
  runs/history/ops-status-<date>.json — dated archive (history strip data)
  mission-control.html                — brand-styled dashboard (gitignored, generated)

Prints a compact summary to stdout for the watcher agent. Exit 0 always (the agent
decides escalation from the summary; a crash here is itself a watcher failure).

No third-party dependencies. Cron evaluated in local time (same Mac that fires the tasks).
"""

import json
import re
import socket
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ORCHESTRATION = Path.home() / "Documents/Claude/Projects/AI-orchestration-layer"
SNAPSHOT = ROOT / "runs" / "scheduled-tasks-snapshot.json"
DIGEST = ORCHESTRATION / "runs" / "digest.jsonl"  # Lane-2 queue is owned by ESCALATION-POLICY.md, read cross-repo
HEARTBEAT = ROOT / "runs" / "heartbeat.jsonl"
STATUS_OUT = ROOT / "runs" / "ops-status.json"
HISTORY_DIR = ROOT / "runs" / "history"
HTML_OUT = ROOT / "mission-control.html"

RUN_GRACE = timedelta(minutes=45)   # allowance past fire+jitter before a run counts as missed
LATE_SLACK = timedelta(seconds=120)  # lastRunAt may precede the matched fire minute slightly
# A routine that fired but never wrote a heartbeat is STALLED — almost always
# parked on an interactive approval nobody is present to answer. Observed
# 2026-09-02: longboard/mastermind/rockwell fired 18:09-18:52, hung on a uv +
# macOS TCC prompt, and drained only when Ed reached the machine at 08:05 the
# next morning. The 20:18 sweep scored all three "ok" because lastRunAt was
# set. lastRunAt means DISPATCHED, never COMPLETED — only a heartbeat means
# completed. Grace is generous: the longest legitimate routine (the morning
# briefing) runs well under an hour.
STALL_GRACE = timedelta(hours=2)     # fired, but silent this long => stalled

# Routine groups — Ed's locked spec 2026-07-28. Unlisted taskIds land in "Other".
GROUPS = [
    ("AI Morning Briefing", ["daily-ai-morning-briefing"]),
    ("Earnings Puts", ["earnings-put-weekly-scan", "earnings-put-t1-recheck", "earnings-put-pxo-capture"]),
    ("Longboard", ["longboard-daily-capture"]),
    ("Mastermind", ["mastermind-daily-capture"]),
    ("Open Brain", ["substack-inbox-watcher", "action-item-triage", "open-brain-wiki-update", "weekly-brain-review"]),
    ("Token Dashboards", ["claude-token-dashboard-update", "token-dashboard-sentinel"]),
    ("Ops & System", ["ops-watcher", "evening-digest"]),
    ("Personal", ["weekly-saltwater-fishing-report", "saltwater-multiday-refresh", "freshwater-trip-log"]),
]

# launchd script jobs. Evidence rules per job (see check_script_jobs):
#   tokenburn pair keys off the last-success stamp — the watchdog is SILENT BY DESIGN
#   when the stamp is fresh (its log only grows on heals; log mtime is NOT health evidence).
TOKENBURN_STAMP = Path.home() / "Library/Logs/tokenburn/last-success"
SCRIPT_JOBS_STATIC = [
    {"id": "openbrain.digest", "desc": "Open Brain digest script (run_digest.sh)",
     "schedule": "Daily 7:00 AM", "evidence": Path.home() / "Open-Brain/.digest.log", "max_age_h": 26},
    {"id": "earnings-put-weekly-scan (py)", "desc": "Raw Python screener — stage 1 of the Sunday pipeline",
     "schedule": "Sunday 6:00 PM",
     "evidence": Path.home() / "Documents/Claude/earnings-put-screener/output/_launchd_scan.log", "max_age_h": 8 * 24},
]

SERVERS = [
    {"id": "ai-briefing", "kind": "port", "port": 8765, "desc": "Morning AI Briefing site"},
    {"id": "openbrain-review-dashboard", "kind": "port", "port": 8787, "desc": "Open Brain review dashboard"},
    {"id": "earnings-put-scanner @ eds-mac-studio", "kind": "http",
     "url": "http://eds-mac-studio.local:8080/latest.html", "max_age_h": 8 * 24, "remote": True,
     "desc": "Earnings Put Screener served from the Mac Studio"},
]

# ---------------------------------------------------------------- cron matching

def _field_matches(field: str, value: int, lo: int, hi: int) -> bool:
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


def cron_matches(expr: str, dt: datetime) -> bool:
    minute, hour, dom, mon, dow = expr.split()
    cron_dow = (dt.weekday() + 1) % 7  # cron: 0 = Sunday
    return (
        _field_matches(minute, dt.minute, 0, 59)
        and _field_matches(hour, dt.hour, 0, 23)
        and _field_matches(dom, dt.day, 1, 31)
        and _field_matches(mon, dt.month, 1, 12)
        and _field_matches(dow, cron_dow, 0, 6)
    )


def prev_fire(expr: str, now: datetime, lookback_days: int = 9):
    dt = now.replace(second=0, microsecond=0)
    for _ in range(lookback_days * 1440):
        if cron_matches(expr, dt):
            return dt
        dt -= timedelta(minutes=1)
    return None

# ---------------------------------------------------------------- helpers

def parse_iso(s):
    """Tolerant ISO parse. Returns aware datetime in local tz, or None.

    MUST stay tolerant. Python 3.9's fromisoformat is strict and raises on
    `-0700` style offsets, which 72 of the 223 real heartbeat.jsonl rows use
    (routines write their footers with differing formatters). This function
    used to raise, and only survived because Ed's interactive PATH resolves to
    a newer python -- under /usr/bin/python3 (3.9.6, what launchd would use)
    it crashed on the first such row. Found 2026-08-30 while building
    fleet_watchdog.py. A bad row must never take down the sweep.
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
        d = d.astimezone()
    return d.astimezone()


def fmt(dt, now):
    if dt is None:
        return "—"
    d = dt.strftime("%a %-m/%-d %-I:%M %p")
    delta = now - dt
    if timedelta(0) <= delta < timedelta(hours=48):
        hrs = delta.total_seconds() / 3600
        d += f" ({hrs:.0f}h ago)" if hrs >= 1 else f" ({delta.total_seconds()/60:.0f}m ago)"
    return d


def mtime_dt(path: Path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        return None

# ---------------------------------------------------------------- routine health

def read_heartbeats():
    """Latest heartbeat row per task. Absence is neutral — not every routine has reported yet."""
    latest = {}
    if HEARTBEAT.exists():
        for line in HEARTBEAT.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = parse_iso(row.get("ts"))
            if row.get("task") and ts and (row["task"] not in latest or ts > latest[row["task"]][0]):
                latest[row["task"]] = (ts, row.get("status", ""), row.get("note", ""))
    return latest


def assess(task: dict, now: datetime, heartbeats=None) -> dict:
    out = {
        "taskId": task["taskId"],
        "description": task.get("description", ""),
        "schedule": task.get("schedule", ""),
        "enabled": task.get("enabled", False),
        "lastRunAt": task.get("lastRunAt"),
        "nextRunAt": task.get("nextRunAt"),
        "oneTime": "fireAt" in task,
    }
    last = parse_iso(task.get("lastRunAt"))
    jitter = timedelta(seconds=task.get("jitterSeconds", 0) or 0)

    if out["oneTime"]:
        fire = parse_iso(task.get("fireAt"))
        if last is not None:
            out["status"], out["detail"] = "done", "one-time task, completed"
        elif not task.get("enabled"):
            out["status"], out["detail"] = "off", "one-time task, disabled without running"
        elif fire and fire < now - RUN_GRACE:
            out["status"], out["detail"] = "missed", f"one-time fire {fmt(fire, now)} passed with no run"
        else:
            out["status"], out["detail"] = "scheduled", f"fires {fmt(fire, now)}"
        return out

    expr = task.get("cronExpression")
    if not task.get("enabled"):
        out["status"] = "off"
        out["detail"] = "recurring task is DISABLED — verify this is intentional"
        return out
    if not expr:
        if "manual" in (task.get("schedule") or "").lower():
            out["status"], out["detail"] = "manual", "on-demand — runs only when started manually"
        else:
            out["status"], out["detail"] = "note", "enabled but no cronExpression"
        return out

    expected = prev_fire(expr, now)
    out["expectedLast"] = expected.isoformat() if expected else None
    if expected is None:
        out["status"], out["detail"] = "ok", "no fire due in lookback window"
    elif last is not None and last >= expected - LATE_SLACK:
        out["status"], out["detail"] = "ok", f"ran {fmt(last, now)}"
        hb = (heartbeats or {}).get(task["taskId"])
        if hb and hb[0] >= expected - LATE_SLACK and hb[1] in ("failed", "partial"):
            out["status"] = hb[1]
            out["detail"] = f"run started {fmt(last, now)} but self-reported {hb[1]}: {hb[2]}"
        elif hb is not None and hb[0] < expected - LATE_SLACK and now > last + STALL_GRACE:
            # Fired, but no heartbeat for THIS fire and the grace has elapsed.
            # Guarded on `hb is not None`: a task that has never reported keeps
            # the old neutral-absence treatment, so this cannot false-alarm on a
            # routine that simply lacks a footer. All 17 enabled recurring tasks
            # had heartbeat history when this was added (2026-09-03).
            silent_h = (now - last).total_seconds() / 3600
            out["status"] = "stalled"
            out["detail"] = (f"fired {fmt(last, now)} but wrote no heartbeat — "
                             f"silent {silent_h:.1f}h; last report was "
                             f"{fmt(hb[0], now)}. Suspect an unanswered approval prompt.")
    elif now <= expected + jitter + RUN_GRACE:
        out["status"] = "pending"
        out["detail"] = f"fire window open (due {expected.strftime('%-I:%M %p')}, jitter+grace not elapsed)"
    elif last is None:
        out["status"] = "new"
        out["detail"] = "never run yet (newly created/migrated) — first fire pending"
    else:
        out["status"] = "missed"
        out["detail"] = f"expected {fmt(expected, now)}, last ran {fmt(last, now)}"
    return out

# ---------------------------------------------------------------- digest queue

def read_digest(now: datetime):
    items, counts = [], {"new": 0, "sent": 0, "expiring": 0, "stale": 0}
    if DIGEST.exists():
        for line in DIGEST.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            st = row.get("status", "new")
            counts[st] = counts.get(st, 0) + 1
            ts = parse_iso(row.get("ts"))
            age = (now - ts).days if ts else None
            if st in ("new", "expiring"):
                items.append({
                    "ts": row.get("ts"), "age_days": age,
                    "severity": row.get("severity", ""), "category": row.get("category", ""),
                    "text": row.get("text", ""), "source": row.get("source", ""), "status": st,
                })
    return items, counts

# ---------------------------------------------------------------- script jobs (launchd)

def check_script_jobs(now: datetime):
    jobs = []

    # tokenburn pair — health keys off the last-success stamp, not log mtimes.
    stamp_dt, age_h = None, None
    try:
        stamp_dt = datetime.fromtimestamp(int(TOKENBURN_STAMP.read_text().strip())).astimezone()
        age_h = (now - stamp_dt).total_seconds() / 3600
    except (OSError, ValueError):
        pass
    if age_h is None:
        ing = ("failed", "last-success stamp missing/unreadable — pipeline state unknown")
        wd = ("failed", "cannot evaluate: stamp missing")
    elif age_h <= 26:
        ing = ("ok", f"pipeline last full success {fmt(stamp_dt, now)}")
        wd = ("ok", "armed — silent by design; re-runs ingest only when stamp exceeds 26h")
    elif age_h <= 28:
        ing = ("stale", f"stamp {age_h:.0f}h old — inside watchdog heal window")
        wd = ("pending", "heal window open — watchdog should re-run ingest this hour")
    else:
        ing = ("failed", f"stamp {age_h:.0f}h old and not healed")
        wd = ("failed", f"stamp {age_h:.0f}h old — hourly watchdog is not healing; check watchdog.err.log")
    jobs.append({"id": "tokenburn.ingest", "desc": "Token Burn data refresh (run-all.sh)",
                 "schedule": "Daily 6:00 PM", "status": ing[0], "detail": ing[1],
                 "last": stamp_dt.isoformat() if stamp_dt else None})
    jobs.append({"id": "tokenburn.watchdog", "desc": "Hourly self-heal for the Token Burn ingest",
                 "schedule": "Every hour", "status": wd[0], "detail": wd[1],
                 "last": stamp_dt.isoformat() if stamp_dt else None})

    for j in SCRIPT_JOBS_STATIC:
        dt = mtime_dt(j["evidence"])
        if dt is None:
            st, detail = "failed", f"evidence file missing: {j['evidence']}"
        elif (now - dt).total_seconds() / 3600 <= j["max_age_h"]:
            st, detail = "ok", f"last activity {fmt(dt, now)}"
        else:
            st, detail = "stale", f"no activity since {fmt(dt, now)} (max {j['max_age_h']}h)"
        jobs.append({"id": j["id"], "desc": j["desc"], "schedule": j["schedule"],
                     "status": st, "detail": detail, "last": dt.isoformat() if dt else None})
    return jobs

# ---------------------------------------------------------------- servers

def check_servers(now: datetime):
    out = []
    for s in SERVERS:
        row = {"id": s["id"], "desc": s["desc"], "remote": s.get("remote", False)}
        if s["kind"] == "port":
            row["target"] = f":{s['port']}"
            try:
                with socket.create_connection(("127.0.0.1", s["port"]), timeout=3):
                    row["status"], row["detail"] = "up", "port responding"
            except OSError:
                row["status"], row["detail"] = "down", "port not responding"
        else:  # http
            row["target"] = s["url"]
            try:
                req = urllib.request.Request(s["url"], method="HEAD")
                with urllib.request.urlopen(req, timeout=6) as resp:
                    lm = resp.headers.get("Last-Modified")
                lm_dt = None
                if lm:
                    lm_dt = datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z").replace(
                        tzinfo=__import__("datetime").timezone.utc).astimezone()
                if lm_dt and (now - lm_dt).total_seconds() / 3600 > s["max_age_h"]:
                    row["status"] = "stale"
                    row["detail"] = f"reachable but content last modified {fmt(lm_dt, now)} — scan may not have landed"
                else:
                    row["status"] = "up"
                    row["detail"] = f"reachable · content modified {fmt(lm_dt, now)}" if lm_dt else "reachable"
            except Exception as e:
                row["status"] = "unreachable" if row["remote"] else "down"
                row["detail"] = f"no response ({type(e).__name__}) — remote host may be asleep/off-network" \
                    if row["remote"] else f"no response ({type(e).__name__})"
        out.append(row)
    return out

# ---------------------------------------------------------------- dashboard

BADGE = {
    "ok":        ("#15803D", "#2E9E5B", "OK"),
    "up":        ("#15803D", "#2E9E5B", "Up"),
    "pending":   ("#2B6CB0", "#2B6CB0", "In window"),
    "missed":    ("#B91C1C", "#D64545", "Missed"),
    "stalled":   ("#B91C1C", "#D64545", "Stalled"),
    "failed":    ("#B91C1C", "#D64545", "Failed"),
    "down":      ("#B91C1C", "#D64545", "Down"),
    "stale":     ("#B91C1C", "#D64545", "Stale"),
    "partial":   ("#B45309", "#E0A33E", "Partial"),
    "unreachable": ("#B45309", "#E0A33E", "Unreachable"),
    "new":       ("#2B6CB0", "#2B6CB0", "Not yet run"),
    "manual":    ("#4D5757", "#97A3A3", "On-demand"),
    "done":      ("#4D5757", "#97A3A3", "Done"),
    "off":       ("#B45309", "#E0A33E", "Disabled"),
    "note":      ("#B45309", "#E0A33E", "Note"),
    "scheduled": ("#2B6CB0", "#2B6CB0", "Scheduled"),
}

BAD_ROUTINE = ("missed", "failed", "stalled")
WARN_ROUTINE = ("partial", "off", "note")
BAD_JOB = ("failed", "stale")
BAD_SERVER = ("down", "stale")


def badge(status):
    text, dot, label = BADGE.get(status, BADGE["note"])
    return (f'<span class="badge" style="--dot:{dot};--btext:{text}">'
            f'<span class="dot"></span>{label}</span>')


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_html(assessed, digest_items, digest_counts, jobs, servers, now):
    by_id = {t["taskId"]: t for t in assessed}
    active = [t for t in assessed if not t["oneTime"] and t["enabled"]]
    retired = [t for t in assessed if t["oneTime"] or not t["enabled"]]
    grouped_ids = {tid for _, ids in GROUPS for tid in ids}
    other = [t for t in active if t["taskId"] not in grouped_ids]

    n_sched = [t for t in active if t["status"] != "manual"]
    n_ok = sum(1 for t in n_sched if t["status"] == "ok")
    n_bad = sum(1 for t in n_sched if t["status"] in BAD_ROUTINE)
    n_new = sum(1 for t in n_sched if t["status"] == "new")
    n_issue = (n_bad + sum(1 for j in jobs if j["status"] in BAD_JOB)
               + sum(1 for s in servers if s["status"] in BAD_SERVER and not s["remote"])
               + sum(1 for s in servers if s["status"] == "stale"))
    n_queue = digest_counts.get("new", 0) + digest_counts.get("expiring", 0)

    order = {"missed": 0, "failed": 0, "stalled": 0, "partial": 1, "pending": 2, "off": 3, "note": 3, "new": 4, "ok": 5, "manual": 6}

    def routine_rows(tasks):
        rows = []
        for t in sorted(tasks, key=lambda x: (order.get(x["status"], 7), x["taskId"])):
            rows.append(
                f'<tr id="{esc(t["taskId"])}"><td>{badge(t["status"])}</td>'
                f'<td class="tname"><strong>{esc(t["taskId"])}</strong>'
                f'<div class="tdetail">{esc(t["detail"])}</div></td>'
                f'<td class="sched">{esc(t["schedule"])}</td>'
                f'<td class="mono">{esc(fmt(parse_iso(t.get("lastRunAt")), now))}</td></tr>'
            )
        return "\n".join(rows)

    cards = []
    for gname, ids in GROUPS + ([("Other", [t["taskId"] for t in other])] if other else []):
        gtasks = [by_id[i] for i in ids if i in by_id]
        if not gtasks:
            continue
        sched = [t for t in gtasks if t["status"] != "manual"]
        g_ok = sum(1 for t in sched if t["status"] == "ok")
        g_bad = sum(1 for t in sched if t["status"] in BAD_ROUTINE)
        g_new = sum(1 for t in sched if t["status"] == "new")
        if g_bad:
            roll, rc = f"{g_bad} issue{'s' if g_bad > 1 else ''}", "#B91C1C"
        elif g_new and g_ok == 0:
            roll, rc = f"{g_new} not yet run", "#2B6CB0"
        elif g_new:
            roll, rc = f"{g_ok}/{len(sched)} OK · {g_new} pending first run", "#2B6CB0"
        elif sched:
            roll, rc = f"{g_ok}/{len(sched)} OK", "#15803D"
        else:
            roll, rc = "on-demand", "#6B7777"
        cards.append(
            f'<div class="gcard"><div class="ghead"><h3>{esc(gname)}</h3>'
            f'<span class="roll" style="color:{rc}">{roll}</span></div>'
            f'<table><tbody>{routine_rows(gtasks)}</tbody></table></div>'
        )

    job_rows = "\n".join(
        f'<tr><td>{badge(j["status"])}<div class="tdetail">{esc(j["detail"])}</div></td>'
        f'<td class="tname"><strong>{esc(j["id"])}</strong> <span class="tag">launchd</span>'
        f'<div class="tdesc">{esc(j["desc"])}</div></td>'
        f'<td class="sched">{esc(j["schedule"])}</td>'
        f'<td class="mono">{esc(fmt(parse_iso(j.get("last")), now))}</td></tr>'
        for j in jobs
    )

    server_spans = "\n".join(
        f'<span>{badge(s["status"])}&nbsp; <strong>{esc(s["id"])}</strong> '
        f'<span class="mono">{esc(s["target"] if s["target"].startswith(":") else "")}</span>'
        f'<div class="tdetail">{esc(s["detail"])}</div></span>'
        for s in servers
    )

    attention = ([t for t in active if t["status"] in BAD_ROUTINE + WARN_ROUTINE]
                 + [j for j in jobs if j["status"] in BAD_JOB]
                 + [s for s in servers if s["status"] in BAD_SERVER])
    aging = [i for i in digest_items if i["status"] == "expiring" or (i["age_days"] or 0) >= 12]
    attention_html = ""
    if attention or aging:
        lines = [f'<li><strong>{esc(x.get("taskId") or x.get("id"))}</strong> — {esc(x["detail"])}</li>'
                 for x in attention]
        lines += [f'<li><strong>digest item aging</strong> — {esc(i["text"])[:140]} (day {i["age_days"]})</li>'
                  for i in aging]
        attention_html = '<section id="attention"><h2>Needs attention</h2><ul class="attn">' + "\n".join(lines) + "</ul></section>"

    digest_rows = "\n".join(
        f'<tr><td class="mono">{esc((i["ts"] or "")[:10])}</td>'
        f'<td class="mono">{i["age_days"]}d</td><td>{esc(i["severity"])}</td>'
        f'<td>{esc(i["category"])}</td><td>{esc(i["text"])}</td></tr>'
        for i in sorted(digest_items, key=lambda x: x["ts"] or "")
    ) or '<tr><td colspan="5" class="empty">Queue is clear.</td></tr>'

    stamp = now.strftime("%A %B %-d, %Y · %-I:%M %p %Z")
    gen_ms = int(now.timestamp() * 1000)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mission Control — Ed Matibag</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,900&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{ --bg:#FFFFFF; --surface:#FFFFFF; --raised:#F7F9F9; --border:#DDE3E3; --text:#0B0F0F;
  --muted:#6B7777; --brand:#2C7A6B; --accent:#2B4C7E; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0F1414; --surface:#161D1C; --raised:#1E2726; --border:#2A3433; --text:#ECF1F0;
    --muted:#97A3A3; --brand:#5BAE9E; --accent:#7FA6D6; }} }}
* {{ box-sizing:border-box; margin:0; }}
body {{ background:var(--bg); color:var(--text); font:400 15px/1.5 Inter,system-ui,sans-serif;
  max-width:1120px; margin:0 auto; padding:32px 24px 64px; }}
h1 {{ font:900 36px/1.15 Fraunces,serif; }}
h2 {{ font:600 22px/1.2 Fraunces,serif; color:var(--accent); margin:36px 0 12px; }}
h3 {{ font:600 18px/1.2 Fraunces,serif; color:var(--accent); }}
.sub {{ color:var(--muted); font-size:14px; margin-top:4px; }}
header {{ display:flex; align-items:center; gap:14px; border-bottom:3px solid var(--brand); padding-bottom:18px; }}
.tile {{ width:44px; height:44px; border-radius:10px; flex:none;
  background:linear-gradient(135deg,#2C7A6B,#2B4C7E); display:flex; align-items:center; justify-content:center;
  color:#fff; font:600 20px Fraunces,serif; }}
#stale-banner {{ display:none; background:#FBEAEA; border:1px solid #D64545; border-left:4px solid #B91C1C;
  border-radius:8px; padding:10px 16px; margin:18px 0 0; font-size:14px; color:#0B0F0F; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin-top:22px; }}
.stat {{ background:var(--raised); border:1px solid var(--border); border-radius:12px; padding:13px 16px; }}
.stat .n {{ font:600 30px/1.1 "IBM Plex Mono",monospace; }}
.stat .l {{ color:var(--muted); font-size:13px; margin-top:2px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(480px,1fr)); gap:14px; margin-top:14px; }}
.gcard {{ background:var(--raised); border:1px solid var(--border); border-radius:12px; padding:14px 16px 8px; }}
.ghead {{ display:flex; justify-content:space-between; align-items:baseline; border-bottom:2px solid var(--brand);
  padding-bottom:8px; margin-bottom:4px; gap:12px; }}
.roll {{ font:600 13px "IBM Plex Mono",monospace; white-space:nowrap; }}
table {{ border-collapse:collapse; width:100%; font-size:13.5px; }}
td {{ padding:8px 8px 8px 0; border-bottom:1px solid var(--border); vertical-align:top; }}
tr:last-child td {{ border-bottom:none; }}
.mono {{ font-family:"IBM Plex Mono",monospace; font-size:12.5px; font-variant-numeric:tabular-nums; white-space:nowrap; }}
.tname strong {{ font-weight:600; }}
.sched {{ color:var(--muted); font-size:12.5px; min-width:110px; }}
.tdesc,.tdetail {{ color:var(--muted); font-size:12px; margin-top:2px; max-width:420px; }}
.badge {{ display:inline-flex; align-items:center; gap:6px; font:600 12px Inter; color:var(--btext); white-space:nowrap; }}
.badge .dot {{ width:8px; height:8px; border-radius:50%; background:var(--dot); flex:none; }}
@media (prefers-color-scheme: dark) {{ .badge {{ color:var(--dot); }}
  #stale-banner {{ background:#3A1A1A; color:#ECF1F0; }} }}
.tag {{ font:500 10.5px "IBM Plex Mono",monospace; color:var(--muted); border:1px solid var(--border);
  border-radius:4px; padding:1px 5px; vertical-align:middle; }}
.tablewrap {{ overflow-x:auto; border:1px solid var(--border); border-radius:12px; background:var(--raised); padding:4px 14px; }}
.servers {{ display:flex; gap:26px; margin-top:10px; background:var(--raised); border:1px solid var(--border);
  border-radius:12px; padding:12px 16px; font-size:13.5px; flex-wrap:wrap; }}
.attn {{ background:var(--raised); border:1px solid var(--border); border-left:4px solid #D64545;
  border-radius:8px; padding:14px 18px 14px 34px; }}
.attn li {{ margin:4px 0; }}
.empty {{ color:var(--muted); text-align:center; padding:18px; }}
details {{ margin-top:10px; }} summary {{ cursor:pointer; color:var(--muted); font-size:14px; }}
footer {{ margin-top:44px; color:var(--muted); font-size:12.5px; border-top:1px solid var(--border); padding-top:14px; }}
</style></head><body>
<header>
  <div class="tile">EM</div>
  <div><h1>Mission Control</h1>
  <div class="sub">Routines, script jobs &amp; servers · generated {esc(stamp)}</div></div>
</header>

<div id="stale-banner"><strong>This view is stale.</strong> Generated more than 26 hours ago — the
ops-watcher may not have run. Check the Scheduled panel or run <span class="mono">python3 ops/watch.py</span>.</div>

<div class="tiles">
  <div class="stat"><div class="n" style="color:{'#15803D' if n_bad==0 else 'var(--text)'}">{n_ok}/{len(n_sched)}</div><div class="l">routines healthy</div></div>
  <div class="stat"><div class="n" style="color:{'#B91C1C' if n_issue else 'var(--text)'}">{n_issue}</div><div class="l">issues (all sources)</div></div>
  <div class="stat"><div class="n" style="color:{'#2B6CB0' if n_new else 'var(--text)'}">{n_new}</div><div class="l">awaiting first run</div></div>
  <div class="stat"><div class="n">{n_queue}</div><div class="l">open digest items</div></div>
</div>

{attention_html}

<section><h2>Routine groups</h2>
<div class="grid">
{''.join(cards)}
</div></section>

<section id="jobs"><h2>Script jobs (launchd)</h2>
<div class="tablewrap"><table><tbody>{job_rows}</tbody></table></div></section>

<section id="servers"><h2>Servers</h2>
<div class="servers">{server_spans}</div></section>

<section id="digest"><h2>Digest queue (Lane 2)</h2>
<div class="tablewrap"><table>
<thead><tr><th style="text-align:left;color:var(--muted);font:600 11px Inter;text-transform:uppercase;letter-spacing:.08em;padding:8px 8px 8px 0">Filed</th><th style="text-align:left;color:var(--muted);font:600 11px Inter;text-transform:uppercase;letter-spacing:.08em">Age</th><th style="text-align:left;color:var(--muted);font:600 11px Inter;text-transform:uppercase;letter-spacing:.08em">Severity</th><th style="text-align:left;color:var(--muted);font:600 11px Inter;text-transform:uppercase;letter-spacing:.08em">Category</th><th style="text-align:left;color:var(--muted);font:600 11px Inter;text-transform:uppercase;letter-spacing:.08em">Item</th></tr></thead>
<tbody>{digest_rows}</tbody></table></div>
<div class="sub">Delivered nightly by <strong>evening-digest</strong> · expiring at day 12 · stale at day 14 (per ESCALATION-POLICY.md)</div></section>

<section><details><summary>One-time &amp; retired tasks ({len(retired)})</summary>
<div class="tablewrap" style="margin-top:10px"><table><tbody>{routine_rows(retired)}</tbody></table></div></details></section>

<footer>Ops Watcher · AI-orchestration-layer Phase 4 attention layer · source of truth:
runs/ops-status.json, runs/digest.jsonl, runs/heartbeat.jsonl · this page is generated — do not edit.</footer>
<script>
(function() {{
  var gen = {gen_ms};
  if (Date.now() - gen > 26 * 3600 * 1000) {{
    document.getElementById("stale-banner").style.display = "block";
  }}
}})();
</script>
</body></html>
"""

# ---------------------------------------------------------------- main

def main():
    now = datetime.now().astimezone()
    if not SNAPSHOT.exists():
        print(f"ERROR: snapshot not found at {SNAPSHOT} — write list_scheduled_tasks output there first.")
        return
    # Fail loud on a stale snapshot instead of emitting false verdicts. Learned
    # 2026-08-31: the fleet-sentinel's 20:00 sweep ran against the 08:05
    # snapshot and flagged 8 routines MISSED that had all run. Every runner
    # must refresh the snapshot immediately before invoking this engine.
    age_h = (now.timestamp() - SNAPSHOT.stat().st_mtime) / 3600
    if age_h > 3:
        print(f"ERROR: snapshot is {age_h:.1f}h old — refresh it (list_scheduled_tasks → "
              f"{SNAPSHOT}) before running. Refusing to compute verdicts from stale data.")
        return
    tasks = json.loads(SNAPSHOT.read_text())
    heartbeats = read_heartbeats()
    assessed = [assess(t, now, heartbeats) for t in tasks]
    digest_items, digest_counts = read_digest(now)
    jobs = check_script_jobs(now)
    servers = check_servers(now)

    active = [t for t in assessed if not t["oneTime"] and t["enabled"] and t["status"] != "manual"]
    bad_routines = [t for t in active if t["status"] in BAD_ROUTINE]
    flags = [t for t in assessed if t["status"] in WARN_ROUTINE and not t["oneTime"]]
    bad_jobs = [j for j in jobs if j["status"] in BAD_JOB]
    bad_servers = [s for s in servers if s["status"] in BAD_SERVER]
    warn_servers = [s for s in servers if s["status"] == "unreachable"]
    aging = [i for i in digest_items if i["status"] == "expiring" or (i["age_days"] or 0) >= 12]

    payload = {
        "generated_at": now.isoformat(),
        "tasks": assessed,
        "script_jobs": jobs,
        "servers": servers,
        "digest": {"counts": digest_counts, "open_items": digest_items},
        "summary": {"active": len(active), "ok": sum(1 for t in active if t["status"] == "ok"),
                    "pending": sum(1 for t in active if t["status"] == "pending"),
                    "new": sum(1 for t in active if t["status"] == "new"),
                    "missed_failed": len(bad_routines), "flags": len(flags),
                    "job_issues": len(bad_jobs), "server_issues": len(bad_servers),
                    "digest_open": len(digest_items)},
    }
    STATUS_OUT.write_text(json.dumps(payload, indent=2))
    HISTORY_DIR.mkdir(exist_ok=True)
    (HISTORY_DIR / f"ops-status-{now.strftime('%Y-%m-%d')}.json").write_text(json.dumps(payload, indent=2))
    HTML_OUT.write_text(render_html(assessed, digest_items, digest_counts, jobs, servers, now))

    print(f"OPS-WATCH {now.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"ROUTINES: {len(active)} active — {payload['summary']['ok']} OK, "
          f"{payload['summary']['pending']} in-window, {payload['summary']['new']} not-yet-run, "
          f"{len(bad_routines)} MISSED/FAILED")
    for t in bad_routines:
        print(f"  [{t['status'].upper()}] {t['taskId']} — {t['detail']}")
    for t in flags:
        print(f"  [FLAG] {t['taskId']} — {t['detail']}")
    print(f"SCRIPT-JOBS: {len(jobs) - len(bad_jobs)}/{len(jobs)} OK")
    for j in bad_jobs:
        print(f"  [{j['status'].upper()}] {j['id']} — {j['detail']}")
    print(f"SERVERS: {sum(1 for s in servers if s['status'] == 'up')}/{len(servers)} up")
    for s in bad_servers:
        print(f"  [{s['status'].upper()}] {s['id']} — {s['detail']}")
    for s in warn_servers:
        print(f"  [UNREACHABLE] {s['id']} — {s['detail']} (amber — not escalation-worthy alone)")
    print(f"DIGEST: {digest_counts.get('new', 0)} new, {digest_counts.get('expiring', 0)} expiring, "
          f"{digest_counts.get('stale', 0)} stale")
    for i in aging:
        print(f"  [AGING] day {i['age_days']}: {i['text'][:110]}")
    print(f"WROTE: {STATUS_OUT.relative_to(ROOT)} + history archive + {HTML_OUT.name}")
    escalate = bad_routines or bad_jobs or bad_servers
    print(f"ESCALATE-CANDIDATE: {'YES' if escalate else 'no'}"
          + (f" — {len(bad_routines)} routine(s), {len(bad_jobs)} script job(s), "
             f"{len(bad_servers)} server(s); investigate cause, then Slack DM Ed per severity gate"
             if escalate else ""))


if __name__ == "__main__":
    sys.exit(main())
