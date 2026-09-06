#!/usr/bin/env python3
"""morning_page.py — one-screen morning summary over Mission Control, the Claude Token
Command Center, and the Token Burn Dashboard.

READ-ONLY over every source. Writes exactly one file: ROOT/morning-page.html.
Never touches ops-status.json, mission-control.html, the token dashboards, or any
routine's own output. Stdlib only, always exits 0, never hangs (no network).

Sources (all optional except ops-status.json — a missing source degrades to a labeled gap):
  runs/ops-status.json                     fleet verdicts (watch.py output)
  runs/scheduled-tasks-snapshot.json       today's fire times (cron expressions)
  runs/heartbeat.jsonl                     what actually ran today
  ORCH/runs/repair.jsonl, ops-commands.jsonl   restarts + queued Slack commands
  Token Burn Dashboard/daily-burn.json     tokens by source per day
  Token Burn Dashboard/sessions.json       notional API-equivalent cost per session
  Token Burn Dashboard/chatgpt-export-meta.json   how far the manual ChatGPT export reaches
  ~/Documents/Claude/claude-token-dashboard.html   USAGE_SUMMARY block (plan limits)
  runs/morning-page.local.json             Slack workspace + channel ids (gitignored; optional)

Run:  python3 morning_page.py      (from ROOT, after watch.py)
"""
import json
import re
import sys
from datetime import datetime, timedelta, date
from html import escape as esc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watch  # noqa: E402  — cron_matches / parse_iso; importing runs no side effects

ROOT = Path(__file__).resolve().parent
ORCH = Path.home() / "Documents/Claude/Projects/AI-orchestration-layer"
TOKENBURN = Path.home() / "Documents/Claude/Projects/Token Burn Dashboard"
STATUS = ROOT / "runs" / "ops-status.json"
SNAPSHOT = ROOT / "runs" / "scheduled-tasks-snapshot.json"
HEARTBEAT = ROOT / "runs" / "heartbeat.jsonl"
LOCAL_CFG = ROOT / "runs" / "morning-page.local.json"
REPAIR = ORCH / "runs" / "repair.jsonl"
COMMANDS = ORCH / "runs" / "ops-commands.jsonl"
DAILY_BURN = TOKENBURN / "daily-burn.json"
SESSIONS = TOKENBURN / "sessions.json"
CHATGPT_META = TOKENBURN / "chatgpt-export-meta.json"
COMMAND_CENTER = Path.home() / "Documents/Claude/claude-token-dashboard.html"
TOKEN_BURN_HTML = Path.home() / "Documents/Claude/token-burn-dashboard.html"
MC_HTML = ROOT / "mission-control.html"
OUT = ROOT / "morning-page.html"

STALE_AFTER = timedelta(hours=26)
DUE_GRACE = timedelta(minutes=45)     # matches watch.RUN_GRACE
LOOKAHEAD_ONE_TIME = timedelta(hours=48)

# Short names for the timeline cells (task id -> what Ed calls it). Unlisted ids show as-is.
NAMES = {
    "substack-inbox-watcher": "Substack inbox",
    "daily-ai-morning-briefing": "AI briefing",
    "ops-watcher": "Ops watcher",
    "weekly-brain-review": "Weekly brain review",
    "saltwater-multiday-refresh": "Multi-day boats",
    "fleet-sentinel": "Fleet sentinel",
    "action-item-triage": "Action-item triage",
    "claude-token-dashboard-update": "Token dashboard",
    "token-dashboard-sentinel": "Token sentinel",
    "longboard-daily-capture": "Longboard",
    "rockwell-daily-capture": "Rockwell",
    "mastermind-daily-capture": "Mastermind",
    "evening-digest": "Evening digest",
    "earnings-put-am-recheck": "Earnings AM recheck",
    "earnings-put-pxo-capture": "Earnings trade log",
    "earnings-put-weekly-scan": "Earnings weekly scan",
    "weekly-saltwater-fishing-report": "Fishing report",
    "open-brain-wiki-update": "Brain wiki",
    "skills-inventory-review": "Skills review",
    "saltwater-multiday-first-run-check": "Multi-day first-run check",
}
# Routines that fire many times a day: keep only these local hours on the timeline.
FOLD = {"fleet-sentinel": ({9, 20}, "restart window")}
# launchd jobs worth a timeline cell: (label, script_jobs id in ops-status, cron)
TIMELINE_LAUNCHD = [
    ("Token burn ingest", "tokenburn.ingest", "0 18 * * *"),
    ("Open Brain digest", "openbrain.digest", "0 7 * * *"),
    ("Earnings weekly scan (py)", "earnings-put-weekly-scan (py)", "0 18 * * 0"),
]
SOURCE_LINKS = [
    ("Mission Control", MC_HTML.as_uri()),
    ("Claude Token Command Center", COMMAND_CENTER.as_uri()),
    ("Token Burn Dashboard", TOKEN_BURN_HTML.as_uri()),
    ("AI Briefing", "http://localhost:8765/"),
    ("Earnings Put Screener", "http://eds-mac-studio.local:8080/latest.html"),
    ("Open Brain review", "http://localhost:8787/"),
]
# Channel names only; ids + workspace come from runs/morning-page.local.json (never committed).
ALERT_CHANNELS = ["ops-control", "token-dashboard-alerts", "ai-briefing",
                  "put-earnings-scanner", "fishing-report-alerts", "open-brain-review"]

BROKEN = ("missed", "failed", "stalled")


# ---------------------------------------------------------------- helpers

def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def load_jsonl(path):
    rows = []
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return rows


def ktok(n):
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.0f}k"
    return f"{n / 1e6:.2f}M"


def clock(dt):
    return dt.strftime("%-I:%M%p").lower().replace(":00", "") if dt else ""


def clock_min(dt):
    s = dt.strftime("%-I:%M%p").lower()
    return s[:-2] + s[-2]  # 8:05a / 6:12p


def first_sentence(text, limit=200):
    text = (text or "").strip()
    text = re.sub(r"^[a-z0-9-]+ \d{4}-\d{2}-\d{2}[^:]{0,40}:\s*", "", text)   # "evening-digest 2026-09-05 (…): "
    text = re.sub(r"^One-time.*?—\s*", "", text)                        # "One-time Mon ... — "
    cut = re.split(r"(?<=[.!])\s", text, maxsplit=1)[0]
    if len(cut) > limit:
        cut = cut[:limit - 1].rstrip() + "…"
    return cut


def usage_summary():
    """Parse the `const USAGE_SUMMARY = {...};` block out of the Command Center HTML."""
    try:
        text = COMMAND_CENTER.read_text(errors="ignore")
    except Exception:
        return None
    m = re.search(r"const USAGE_SUMMARY\s*=\s*(\{.*?\});", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def boost_note(us):
    note = (us or {}).get("note") or ""
    m = re.search(r"boost banner[^']*'([^']+)'", note)
    return m.group(1) if m else ""


def gauge_class(pct):
    return "bad" if pct >= 80 else ("warn" if pct >= 50 else "")


# ---------------------------------------------------------------- data assembly

def today_fires(snapshot, status_tasks, heartbeats, jobs, now):
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    by_id = {t["taskId"]: t for t in status_tasks}
    hb_today = {}
    for h in heartbeats:
        ts = watch.parse_iso(h.get("ts"))
        if ts and ts >= day0:
            hb_today.setdefault(h.get("task"), []).append((ts, h.get("status") or "ok"))
    slots = []

    def state_for(task_id, fire, jitter):
        if fire > now:
            return "up", "upcoming"
        for ts, st in sorted(hb_today.get(task_id, [])):
            if ts >= fire - timedelta(minutes=5):
                return {"ok": ("done", "ran"), "partial": ("warn", "partial"),
                        "failed": ("bad", "failed")}.get(st, ("done", "ran"))
        t = by_id.get(task_id, {})
        last = watch.parse_iso(t.get("lastRunAt"))
        if last and last >= fire - timedelta(minutes=2):
            if t.get("status") == "stalled":
                return "bad", "stalled"
            return "done", "started"
        if now - fire <= timedelta(seconds=jitter or 0) + DUE_GRACE:
            return "due", "due now"
        return "bad", "missed"

    for t in snapshot or []:
        if not t.get("enabled"):
            continue
        tid = t.get("taskId")
        expr = t.get("cronExpression")
        fires = []
        if expr:
            for m in range(0, 1440, 1):
                dt = day0 + timedelta(minutes=m)
                if watch.cron_matches(expr, dt):
                    fires.append(dt)
        elif t.get("fireAt"):
            fa = watch.parse_iso(t["fireAt"])
            if fa and fa.date() == now.date():
                fires.append(fa)
        if tid in FOLD:
            hours, label = FOLD[tid]
            fires = [f for f in fires if f.hour in hours]
        else:
            label = ""
        for f in fires:
            cls, st = state_for(tid, f, t.get("jitterSeconds"))
            if label and cls == "up":
                st = label
            slots.append({"t": f, "name": NAMES.get(tid, tid), "id": tid, "cls": cls, "st": st})

    job_last = {j["id"]: watch.parse_iso(j.get("last")) for j in jobs}
    for label, jid, expr in TIMELINE_LAUNCHD:
        for m in range(0, 1440):
            dt = day0 + timedelta(minutes=m)
            if watch.cron_matches(expr, dt):
                if dt > now:
                    cls, st = "up", "launchd"
                elif job_last.get(jid) and job_last[jid] >= dt - timedelta(minutes=5):
                    cls, st = "done", "ran"
                elif now - dt <= DUE_GRACE:
                    cls, st = "due", "due now"
                else:
                    cls, st = "warn", "no stamp yet"
                slots.append({"t": dt, "name": label, "id": jid, "cls": cls, "st": st})
    slots.sort(key=lambda s: s["t"])
    return slots


def burn_window(rows, now):
    yday = (now.date() - timedelta(days=1)).isoformat()
    complete = [r for r in rows if r.get("date") and r["date"] <= yday]
    complete.sort(key=lambda r: r["date"])
    week = complete[-7:]

    def split(r):
        c = (r.get("cowork_tokens") or 0) + (r.get("claude_code_tokens") or 0) + (r.get("claude_chat_est") or 0)
        o = (r.get("codex_tokens") or 0) + (r.get("chatgpt_est") or 0)
        return c, o

    y = next((r for r in complete if r["date"] == yday), None)
    yc, yo = split(y) if y else (0, 0)
    wc = sum(split(r)[0] for r in week)
    wo = sum(split(r)[1] for r in week)
    days = [{"date": r["date"], "claude": split(r)[0], "openai": split(r)[1]} for r in week]
    return {"yday": yday, "yday_row": y, "yc": yc, "yo": yo, "wc": wc, "wo": wo, "days": days}


def cost_window(sessions, days):
    dates = {d["date"] for d in days}
    claude = 0.0
    for r in sessions or []:
        if r.get("date") in dates and r.get("source") in ("claude_code", "cowork", "claude_chat"):
            claude += r.get("cost") or 0
    return claude


def nice_step(maxv):
    for step in (100_000, 200_000, 250_000, 500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000):
        if maxv / step <= 4:
            return step
    return 20_000_000


# ---------------------------------------------------------------- render

def render(ctx):
    now = ctx["now"]
    s = ctx["status"]
    summ = s["summary"]
    tasks = s["tasks"]
    jobs = s["script_jobs"]
    servers = s["servers"]
    digest = s["digest"]
    active = [t for t in tasks if not t["oneTime"] and t["enabled"] and t["status"] != "manual"]
    broken = [t for t in active if t["status"] in BROKEN]
    partial = [t for t in active if t["status"] == "partial"]
    pending = [t for t in active if t["status"] == "pending"]
    off = [t for t in tasks if t["status"] == "off" and not t["oneTime"]]
    bad_jobs = [j for j in jobs if j["status"] in watch.BAD_JOB]
    bad_srv = [x for x in servers if x["status"] in watch.BAD_SERVER]
    warn_srv = [x for x in servers if x["status"] == "unreachable"]
    upcoming = []
    for t in tasks:
        if t["oneTime"] and t["enabled"] and t["status"] == "scheduled":
            fa = watch.parse_iso(t.get("nextRunAt"))
            if fa and now < fa <= now + LOOKAHEAD_ONE_TIME:
                upcoming.append((fa, t))
    upcoming.sort(key=lambda x: x[0])
    open_items = sorted(digest.get("open_items", []), key=lambda i: i.get("ts") or "", reverse=True)
    us = ctx["usage"]
    b = ctx["burn"]
    gen = watch.parse_iso(s.get("generated_at"))
    stale = gen is None or now - gen > STALE_AFTER

    # ---- verdict
    n_bad = len(broken) + len(bad_jobs) + len(bad_srv)
    n_warn = len(partial) + len(warn_srv) + summ.get("flags", 0)
    if n_bad:
        vcls, vhead = "red", f"{n_bad} item{'s' if n_bad > 1 else ''} need attention."
    elif n_warn:
        vcls, vhead = "amber", ("One routine needs a look." if len(partial) + len(broken) == 1
                                 else f"{n_warn} things worth a look.")
    else:
        vcls, vhead = "green", "All clear."
    weekly = (us or {}).get("weeklyAllPct")
    vsub = (f"{summ['ok']} of {summ['active']} healthy · "
            + (f"plan usage {'low' if weekly < 50 else 'climbing' if weekly < 80 else 'near limit'}" if weekly is not None else "plan limits unavailable")
            + f" · digest has {len(open_items)} open")

    # ---- summary bullets
    def names(ts):
        return ", ".join(f"<b>{esc(t['taskId'])}</b>" for t in ts)

    bl = []
    r1 = f"<b>Routines:</b> {summ['ok']} of {summ['active']} ran on time."
    if broken:
        parts = []
        for t in broken:
            nxt = watch.parse_iso(t.get("nextRunAt"))
            nx = f", next chance {nxt.strftime('%a %-I:%M %p')}" if nxt and nxt > now else ""
            hb = ctx["hb_after"].get(t["taskId"])
            hbn = f" (a heartbeat at {hb[0].strftime('%-I:%M %p')} says {esc(hb[1])})" if hb else ""
            parts.append(f"<b>{esc(t['taskId'])}</b> is {esc(t['status'])}{nx}{hbn}")
        r1 += " " + "; ".join(parts) + "."
    if pending:
        r1 += f" {len(pending)} in the fire window now."
    if not broken and not pending:
        r1 += " Nothing broken."
    bl.append(r1)
    r2 = "<b>Degraded:</b> "
    d_parts = [f"<b>{esc(t['taskId'])}</b> finished partial" for t in partial]
    if off:
        d_parts.append(f"{len(off)} routine{'s' if len(off) > 1 else ''} disabled by design")
    if warn_srv:
        d_parts.append(f"{len(warn_srv)} remote server unreachable (amber)")
    bl.append(r2 + ("; ".join(d_parts) + "." if d_parts else "nothing."))
    if us:
        boost = boost_note(us)
        r3 = (f"<b>Claude plan:</b> <span class=\"num\">{us.get('weeklyAllPct', 0)}%</span> of the week used, "
              f"resets {esc(us.get('weeklyAllReset', '?'))}; session at <span class=\"num\">{us.get('currentSessionPct', 0)}%</span>.")
        if boost:
            r3 += f" Boost active: {esc(boost)}."
    else:
        r3 = "<b>Claude plan:</b> limits unavailable (Command Center block not found)."
    bl.append(r3)
    tot = b["yc"] + b["yo"]
    if tot:
        pc = round(100 * b["yc"] / tot)
        r4 = (f"<b>Tokens yesterday:</b> <span class=\"num\">{ktok(tot)}</span>, "
              f"Claude {pc}% / OpenAI {100 - pc}%.")
    else:
        r4 = f"<b>Tokens yesterday:</b> no row for {b['yday']} yet."
    bl.append(r4)
    sev = {}
    for i in open_items:
        sev[i.get("severity", "?")] = sev.get(i.get("severity", "?"), 0) + 1
    if open_items:
        newest = watch.parse_iso(open_items[0].get("ts"))
        age = f"newest {newest.strftime('%a %-I:%M %p')}" if newest else ""
        r5 = f"<b>Digest:</b> {len(open_items)} open (" + ", ".join(f"{v} {k}" for k, v in sev.items()) + f"); {age}."
    else:
        r5 = "<b>Digest:</b> queue is clear."
    bl.append(r5)
    if upcoming:
        fa, t = upcoming[0]
        day = "Today" if fa.date() == now.date() else ("Tomorrow" if fa.date() == now.date() + timedelta(days=1) else fa.strftime("%a"))
        r6 = f"<b>{day} {fa.strftime('%-I:%M %p')}:</b> {esc(first_sentence(t['description'], 120))}"
    else:
        r6 = "<b>One-time checks:</b> none in the next 48 hours."
    bl.append(r6)
    summary_html = "\n".join(f"      <li>{x}</li>" for x in bl)

    # ---- timeline
    slots = ctx["slots"]
    cells = []
    now_inserted = False
    for sl in slots:
        if not now_inserted and sl["t"] > now:
            cells.append(f'<div class="now"><span>NOW {now.strftime("%-H:%M")}</span></div>')
            now_inserted = True
        cells.append(f'<div class="slot {sl["cls"]}"><i></i><span class="t">{clock_min(sl["t"])}</span>'
                     f'<span class="n">{esc(sl["name"])}</span><span class="st">{esc(sl["st"])}</span></div>')
    if not now_inserted:
        cells.append(f'<div class="now"><span>NOW {now.strftime("%-H:%M")}</span></div>')
    timeline_html = "\n      ".join(cells) or '<div class="slot up"><span class="n">Nothing scheduled today</span></div>'

    # ---- needs a look
    mc = MC_HTML.as_uri()
    look = []
    for t in broken:
        body = first_sentence(t["detail"], 150)
        hb = ctx["hb_after"].get(t["taskId"])
        if hb:
            body = body.rstrip(".") + "." if body else body
            body += f" But a heartbeat at {hb[0].strftime('%-I:%M %p')} reports {hb[1]}: {first_sentence(hb[2], 110)}"
        look.append(("bad", "Broken", f"{t['taskId']} {t['status']}", body, f"{mc}#{t['taskId']}", "open routine"))
    for j in bad_jobs:
        look.append(("bad", "Broken", f"{j['id']} {j['status']}", first_sentence(j["detail"], 150), f"{mc}#jobs", "open jobs"))
    for x in bad_srv:
        look.append(("bad", "Broken", f"{x['id']} {x['status']}", first_sentence(x["detail"], 150), f"{mc}#servers", "open servers"))
    for t in partial:
        look.append(("warn", "Degraded", f"{t['taskId']} partial", first_sentence(t["detail"], 150),
                     f"{mc}#{t['taskId']}", "open routine"))
    for x in warn_srv:
        look.append(("warn", "Degraded", f"{x['id']} unreachable", first_sentence(x["detail"], 150), f"{mc}#servers", "open servers"))
    for i in open_items:
        if i.get("status") == "expiring" or (i.get("age_days") or 0) >= 12:
            look.append(("warn", "Degraded", f"digest item aging, day {i.get('age_days')}", first_sentence(i.get("text"), 160),
                         f"{mc}#digest", "open digest"))
    for t in off:
        look.append(("info", "Heads-up", f"{t['taskId']} disabled", first_sentence(t["detail"], 160),
                     f"{mc}#{t['taskId']}", "open routine"))
    for fa, t in upcoming:
        day = "Today" if fa.date() == now.date() else "Tomorrow" if fa.date() == now.date() + timedelta(days=1) else fa.strftime("%a")
        look.append(("info", "Heads-up", f"{day} {fa.strftime('%-I:%M %p')} one-time check",
                     first_sentence(t["description"], 140), "", "scheduled"))
    if look:
        look_html = "\n".join(
            f'<li class="{c}"><span class="s"></span><div><b><em class="tag">{tag}</em>{esc(title)}</b><p>{esc(body)}</p></div>'
            + (f'<a class="act" href="{esc(href)}">{esc(act)}</a>' if href else f'<span class="act muted">{esc(act)}</span>')
            + "</li>"
            for c, tag, title, body, href, act in look)
    else:
        look_html = '<li class="ok"><span class="s"></span><div><b>Nothing needs a look.</b><p>Every routine, job, and server reported healthy.</p></div></li>'

    # ---- alerts
    if open_items:
        shown = open_items[:4]
        dg_rows = "\n".join(
            f'<div class="r"><span class="sev">{esc(i.get("severity", ""))}</span><span>{esc(first_sentence(i.get("text"), 96))}'
            f' <span class="when">({(watch.parse_iso(i.get("ts")) or now).strftime("%b %-d")})</span></span></div>'
            for i in shown)
        if len(open_items) > 4:
            dg_rows += f'\n<div class="r"><span class="sev"></span><span>+{len(open_items) - 4} older</span></div>'
    else:
        dg_rows = '<div class="r"><span class="sev"></span><span>Queue is clear.</span></div>'
    cfg = ctx["cfg"] or {}
    ws = cfg.get("slack_workspace")
    ids = cfg.get("channels", {})
    ch_rows = []
    for name in ALERT_CHANNELS:
        cid = ids.get(name)
        link = f'<a href="https://{esc(ws)}.slack.com/archives/{esc(cid)}">{esc(name)}</a>' if ws and cid else f'<span class="nolink">{esc(name)}</span>'
        ch_rows.append(f'<div class="row">{link}<span class="n z">–</span><span class="z">not read yet</span></div>')
    ch_html = "\n".join(ch_rows)

    # ---- fleet tile
    chips = []
    chips.append(f'<span class="chip {"bad" if broken else "ok"}"><span class="num">{len(broken)}</span> broken</span>')
    chips.append(f'<span class="chip {"warn" if partial else "ok"}"><span class="num">{len(partial)}</span> partial</span>')
    chips.append(f'<span class="chip {"warn" if summ.get("flags") else "ok"}"><span class="num">{summ.get("flags", 0)}</span> standing flags</span>')
    chips.append(f'<span class="chip {"bad" if bad_jobs else "ok"}"><span class="num">{len(jobs) - len(bad_jobs)}</span>/{len(jobs)} script jobs</span>')
    up = sum(1 for x in servers if x["status"] == "up")
    chips.append(f'<span class="chip {"bad" if bad_srv else ("warn" if warn_srv else "ok")}"><span class="num">{up}</span>/{len(servers)} servers up</span>')
    hb_last = ctx["hb_last"]
    fleet_kv = [
        ("Auto-restarts today", str(ctx["restarts_today"])),
        ("#ops-control commands queued", str(ctx["queued"])),
        ("Last heartbeat", f"{hb_last[0].strftime('%-I:%M %p')} {hb_last[1]}" if hb_last else "none today"),
        ("Off / done / manual", f"{len(off)} · {sum(1 for t in tasks if t['status'] == 'done')} · {sum(1 for t in tasks if t['status'] == 'manual')}"),
    ]
    fleet_kv_html = "\n".join(f'<span class="k">{esc(k)}</span><span></span><span class="v">{esc(v)}</span>' for k, v in fleet_kv)

    # ---- plan tile
    if us:
        rows = [
            ("Current session", us.get("currentSessionPct", 0), f"{us.get('currentSessionPct', 0)}%", us.get("currentSessionReset", "")),
            ("Weekly, all models", us.get("weeklyAllPct", 0), f"{us.get('weeklyAllPct', 0)}%", us.get("weeklyAllReset", "")),
            ("Weekly, Fable", us.get("weeklySonnetPct", 0), f"{us.get('weeklySonnetPct', 0)}%", us.get("weeklySonnetReset", "")),
            ("Extra usage", us.get("extraUsagePct", 0), f"${us.get('extraUsageSpent', 0):.0f}", f"of ${us.get('extraUsageLimit', 0):.0f} · {us.get('extraUsageReset', '')}"),
        ]
        gauge_html = "\n".join(
            f'<span class="k">{esc(k)}</span><span class="bar"><i class="{gauge_class(p)}" style="width:{max(0, min(100, p))}%"></i></span>'
            f'<span class="pct">{esc(v)}</span><span class="reset">{esc(str(r))}</span>' for k, p, v, r in rows)
        boost = boost_note(us)
        plan_kv = [("Balance", f"${us.get('currentBalance', 0):.2f}"),
                   ("Boost", boost or "none"),
                   ("Billing period", us.get("billingPeriod", ""))]
        plan_kv_html = "\n".join(f'<span class="k">{esc(k)}</span><span></span><span class="v">{esc(str(v))}</span>' for k, v in plan_kv)
        plan_stamp = f"{esc(us.get('plan', ''))} · read {esc(us.get('lastFetched', ''))}"
    else:
        gauge_html = '<span class="k" style="grid-column:1/-1">Plan limits unavailable: USAGE_SUMMARY block not found in the Command Center file.</span>'
        plan_kv_html = ""
        plan_stamp = "unavailable"

    # ---- token burn
    wt = b["wc"] + b["wo"]
    share_c = round(100 * b["wc"] / wt) if wt else 0
    cost_c = ctx["cost_claude"]
    days = b["days"]
    maxv = max((d["claude"] + d["openai"] for d in days), default=0) or 1
    step = nice_step(maxv)
    ticks = list(range(0, int(maxv // step) * step + step + 1, step))
    top = ticks[-1]
    H, BASE, PAD = 78, 90, 12
    scale = H / top
    n = max(len(days), 1)
    width = 760
    inner_left, inner_right = 46, 752
    pitch = (inner_right - inner_left) / n
    barw = pitch * 0.64
    svg = []
    for tv in ticks:
        y = BASE - tv * scale
        svg.append(f'<line class="grid" x1="{inner_left}" x2="{inner_right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        svg.append(f'<text x="{inner_left - 6}" y="{y + 3:.1f}" text-anchor="end">{ktok(tv)}</text>')
    for i, d in enumerate(days):
        x = inner_left + i * pitch + (pitch - barw) / 2
        hc = d["claude"] * scale
        ho = d["openai"] * scale
        if hc:
            svg.append(f'<rect class="cl" x="{x:.1f}" y="{BASE - hc:.1f}" width="{barw:.1f}" height="{hc:.1f}"/>')
        if ho:
            svg.append(f'<rect class="oa" x="{x:.1f}" y="{BASE - hc - ho:.1f}" width="{barw:.1f}" height="{ho:.1f}"/>')
        dd = date.fromisoformat(d["date"])
        cx = x + barw / 2
        svg.append(f'<text x="{cx:.1f}" y="{BASE + 16}" text-anchor="middle">{dd.strftime("%a %-m/%-d")}</text>')
        svg.append(f'<text x="{cx:.1f}" y="{BASE - hc - ho - 5:.1f}" text-anchor="middle" class="v">{ktok(d["claude"] + d["openai"])}</text>')
    chart_svg = (f'<svg class="chart" viewBox="0 -{PAD} {width} {BASE + PAD + 22}" role="img" aria-label="Daily tokens, last 7 days, Claude and OpenAI stacked">'
                 + "\n".join(svg) + "</svg>")
    rng = f"{date.fromisoformat(days[0]['date']).strftime('%b %-d')} – {date.fromisoformat(days[-1]['date']).strftime('%b %-d')}" if days else "no data"
    meta = ctx["chatgpt_meta"] or {}
    covered = meta.get("covered_through")
    burn_stamp = f"Token Burn · file {ctx['burn_mtime'].strftime('%-I:%M %p %a') if ctx['burn_mtime'] else 'missing'} · cache excluded"

    # ---- stamps
    hdr_stamp = (f"Rendered {now.strftime('%-I:%M %p')} from files stamped "
                 f"{gen.strftime('%-I:%M %p %a') if gen else '?'} (fleet), "
                 f"{esc((us or {}).get('lastFetched', '?'))} (plan), "
                 f"{ctx['burn_mtime'].strftime('%-I:%M %p %a') if ctx['burn_mtime'] else '?'} (burn)")
    links_html = "\n    ".join(f'<a href="{esc(u)}">{esc(n)}</a>' for n, u in SOURCE_LINKS)
    stale_html = (f'<div class="stale"><strong>This view is stale.</strong> Fleet status was generated more than 26 hours ago '
                  f'({gen.strftime("%a %-I:%M %p") if gen else "unknown"}). The ops-watcher did not run.</div>' if stale else "")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Morning Page</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root{{
  --success:#2E9E5B;--success-text:#15803D;--danger:#D64545;--danger-strong:#B91C1C;--warning:#E0A33E;--warning-text:#B45309;--info:#2B6CB0;
  --bg:#FFFFFF;--surface:#FFFFFF;--raised:#F7F9F9;--border:#DDE3E3;--border-strong:#C2CBCB;
  --text:#0B0F0F;--text-2:#4D5757;--muted:#6B7777;--brand:#2C7A6B;--brand-text:#184F46;--accent:#2B4C7E;--on-brand:#FFFFFF;
  --chip-ok:#EAF4F1;--chip-warn:#FBF1DE;--chip-bad:#FBE7E7;--chip-neutral:#EEF1F1;
  --bar-claude:#2B4C7E;--bar-openai:#75B3A6;--track:#EEF1F1;--gauge:#2B4C7E;
  --font-display:"Fraunces",Georgia,serif;--font-body:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;--font-mono:"IBM Plex Mono",ui-monospace,Menlo,monospace;
}}
@media (prefers-color-scheme:dark){{
  :root:not([data-theme="light"]){{
    --bg:#0F1414;--surface:#161D1C;--raised:#1E2726;--border:#2A3433;--border-strong:#3A4645;
    --text:#ECF1F0;--text-2:#9FACAB;--muted:#6F7C7B;--brand:#5BAE9E;--brand-text:#5BAE9E;--accent:#7FA6D6;--on-brand:#0B0F0F;
    --success-text:#4CC57A;--warning-text:#E8B45C;--danger-strong:#F07474;
    --chip-ok:#173129;--chip-warn:#3A2D14;--chip-bad:#3D1B1B;--chip-neutral:#1E2726;
    --bar-claude:#7FA6D6;--bar-openai:#5BAE9E;--track:#1E2726;--gauge:#7FA6D6;
  }}
}}
:root[data-theme="dark"]{{
  --bg:#0F1414;--surface:#161D1C;--raised:#1E2726;--border:#2A3433;--border-strong:#3A4645;
  --text:#ECF1F0;--text-2:#9FACAB;--muted:#6F7C7B;--brand:#5BAE9E;--brand-text:#5BAE9E;--accent:#7FA6D6;--on-brand:#0B0F0F;
  --success-text:#4CC57A;--warning-text:#E8B45C;--danger-strong:#F07474;
  --chip-ok:#173129;--chip-warn:#3A2D14;--chip-bad:#3D1B1B;--chip-neutral:#1E2726;
  --bar-claude:#7FA6D6;--bar-openai:#5BAE9E;--track:#1E2726;--gauge:#7FA6D6;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:var(--font-body);font-size:14px;line-height:1.45;-webkit-font-smoothing:antialiased}}
a{{color:var(--brand-text);text-decoration:none}}a:hover{{text-decoration:underline}}
a:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
.num{{font-family:var(--font-mono);font-variant-numeric:tabular-nums}}
.page{{max-width:1280px;margin:0 auto;padding:16px 28px 18px;display:grid;gap:12px}}
.hdr{{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;padding-bottom:12px;border-bottom:2px solid var(--brand)}}
.hdr h1{{font-family:var(--font-display);font-weight:600;font-size:30px;line-height:1.1;letter-spacing:-.01em;margin:0}}
.hdr .sub{{color:var(--text-2);margin-top:4px}}
.hdr>div:last-child{{max-width:460px;text-align:right}}
.verdict{{display:inline-flex;align-items:center;gap:10px;padding:8px 14px;border:1px solid var(--border);border-radius:8px;background:var(--raised);text-align:left}}
.verdict .dot{{flex:0 0 auto}}
.verdict .dot{{width:10px;height:10px;border-radius:50%;background:var(--warning)}}
.page{{overflow-x:hidden}}
.verdict.green .dot{{background:var(--success)}}.verdict.red .dot{{background:var(--danger)}}
.verdict b{{font-weight:600}}
.stamp{{color:var(--muted);font-size:12px}}
.stale{{padding:10px 14px;border-radius:8px;background:var(--chip-bad);color:var(--danger-strong)}}
.eyebrow{{font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--accent);margin:0 0 8px}}
.eyebrow small{{font-weight:500;letter-spacing:0;text-transform:none;color:var(--muted);margin-left:8px}}
.summary{{margin:0;padding:12px 18px 12px 34px;border-left:3px solid var(--brand);background:var(--raised);border-radius:0 8px 8px 0;font-size:14px;line-height:1.5;display:grid;grid-template-columns:1fr 1fr;gap:4px 28px}}
.summary li{{padding-left:2px}}.summary li::marker{{color:var(--brand)}}.summary b{{font-weight:600}}.summary .num{{font-weight:600}}
.tl{{display:flex;align-items:stretch;border:1px solid var(--border);border-radius:10px;background:var(--surface);overflow-x:auto}}
.tl .slot{{flex:1 1 0;min-width:74px;padding:9px 5px;text-align:center;border-right:1px solid var(--border);display:flex;flex-direction:column;align-items:center;gap:6px}}
.tl .slot:last-child{{border-right:0}}
.tl .slot i{{width:12px;height:12px;border-radius:50%;border:2px solid var(--border-strong);background:var(--surface);display:block}}
.tl .slot.done i{{background:var(--success);border-color:var(--success)}}
.tl .slot.due i{{background:var(--warning);border-color:var(--warning)}}
.tl .slot.warn i{{background:var(--warning);border-color:var(--warning)}}
.tl .slot.bad i{{background:var(--danger);border-color:var(--danger)}}
.tl .slot .t{{font-family:var(--font-mono);font-size:12px;font-weight:600;color:var(--text)}}
.tl .slot .n{{font-size:11.5px;color:var(--text-2);line-height:1.25}}
.tl .slot .st{{font-size:11px;font-weight:600;color:var(--success-text)}}
.tl .slot.due .st,.tl .slot.warn .st{{color:var(--warning-text)}}
.tl .slot.bad .st{{color:var(--danger-strong)}}
.tl .slot.up .st{{color:var(--muted);font-weight:500}}
.tl .now{{flex:0 0 auto;width:28px;display:flex;align-items:center;justify-content:center;background:var(--chip-bad);border-right:1px solid var(--border)}}
.tl .now span{{writing-mode:vertical-rl;transform:rotate(180deg);font-size:10px;font-weight:700;letter-spacing:.14em;color:var(--danger-strong)}}
.tlkey{{display:flex;gap:16px;font-size:12px;color:var(--muted);margin-top:6px;flex-wrap:wrap}}
.tlkey i{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;vertical-align:-1px;border:2px solid var(--border-strong);background:var(--surface)}}
.tlkey .d i{{background:var(--success);border-color:var(--success)}}.tlkey .u i{{background:var(--warning);border-color:var(--warning)}}.tlkey .b i{{background:var(--danger);border-color:var(--danger)}}
.two{{display:grid;grid-template-columns:1.35fr 1fr;gap:14px}}
.tiles{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.tile,.list{{border:1px solid var(--border);border-radius:10px;padding:14px 16px;background:var(--surface);display:flex;flex-direction:column;gap:10px;min-width:0}}
.list{{gap:0}}
.tile h2,.list h2{{font-size:15px;font-weight:700;margin:0;display:flex;justify-content:space-between;align-items:baseline;gap:12px}}
.list h2{{margin-bottom:10px}}
.tile h2 .stamp,.list h2 .stamp{{font-weight:500;text-align:right}}
.big{{display:flex;align-items:baseline;gap:8px}}
.big .n{{font-family:var(--font-mono);font-size:34px;font-weight:600;line-height:1}}
.big .l{{color:var(--text-2)}}
.kv{{display:grid;grid-template-columns:auto 1fr auto;gap:6px 12px;align-items:center;font-size:13px}}
.kv .k{{color:var(--text-2)}}.kv .v{{text-align:right;font-family:var(--font-mono)}}
.chips{{display:flex;flex-wrap:wrap;gap:6px}}
.chip{{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border-radius:999px;font-size:12px;font-weight:600;background:var(--chip-neutral);color:var(--text)}}
.chip.ok{{background:var(--chip-ok);color:var(--success-text)}}.chip.warn{{background:var(--chip-warn);color:var(--warning-text)}}.chip.bad{{background:var(--chip-bad);color:var(--danger-strong)}}
.chip .num{{font-weight:600}}
.gauge{{display:grid;grid-template-columns:118px 1fr 44px 96px;gap:8px 10px;align-items:center;font-size:13px}}
.gauge .k{{color:var(--text-2)}}
.gauge .bar{{height:8px;border-radius:4px;background:var(--track);overflow:hidden}}
.gauge .bar i{{display:block;height:100%;border-radius:4px;background:var(--gauge)}}
.gauge .bar i.warn{{background:var(--warning)}}.gauge .bar i.bad{{background:var(--danger)}}
.gauge .pct{{text-align:right;font-family:var(--font-mono);font-weight:600}}
.gauge .reset{{color:var(--muted);font-size:12px;font-family:var(--font-mono)}}
.footnote{{color:var(--muted);font-size:12px;margin:0}}
.footnote .est{{display:inline-block;padding:0 5px;border:1px solid var(--border-strong);border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.06em;margin-right:4px}}
.burn{{display:grid;grid-template-columns:400px 1fr;gap:28px;align-items:start}}
.bt{{border-collapse:collapse;width:100%;font-size:13px;margin-top:2px}}
.bt th{{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);text-align:right;padding:0 0 8px 14px;border-bottom:1px solid var(--border)}}
.bt td{{padding:6px 0 6px 14px;text-align:right;border-bottom:1px solid var(--border);white-space:nowrap}}
.bt td:first-child,.bt th:first-child{{text-align:left;padding-left:0}}
.bt td.num{{font-size:17px;font-weight:600}}.bt td.sh{{font-size:13px;font-weight:500;color:var(--text-2)}}
.bt tr.tot td{{border-bottom:0;color:var(--text-2)}}.bt tr.tot td.num{{font-size:14px;font-weight:500}}
.bt td.cost{{font-size:15px}}.bt td.na{{color:var(--muted);font-weight:400;font-size:13px}}
.bt .sw{{width:10px;height:10px;border-radius:2px;display:inline-block;margin-right:8px;vertical-align:-1px}}
.chartwrap{{display:grid;gap:6px;min-width:0}}
.chart{{width:100%;height:auto;max-height:150px;display:block}}
.chart text{{font-family:var(--font-mono);font-size:11px;fill:var(--muted)}}
.chart text.v{{font-size:10px;font-weight:600;fill:var(--text-2)}}
.chart .grid{{stroke:var(--border);stroke-width:1}}.chart .cl{{fill:var(--bar-claude)}}.chart .oa{{fill:var(--bar-openai)}}
.legend{{display:flex;gap:16px;font-size:12px;color:var(--text-2);align-items:center}}
.legend span:first-child{{margin-right:auto;font-weight:600;color:var(--text)}}
.legend i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}}
.att{{list-style:none;margin:0 0 10px;padding:0;display:grid;gap:6px}}
.att li{{display:grid;grid-template-columns:4px 1fr auto;gap:12px;align-items:start;padding:6px 10px 6px 0;border-radius:6px;background:var(--raised)}}
.att li .s{{align-self:stretch;border-radius:6px 0 0 6px;background:var(--border-strong)}}
.att li.bad .s{{background:var(--danger)}}.att li.warn .s{{background:var(--warning)}}.att li.info .s{{background:var(--info)}}.att li.ok .s{{background:var(--success)}}
.att li b{{font-weight:600}}.att li p{{margin:2px 0 0;color:var(--text-2);font-size:13px}}
.att li .act{{font-size:12px;white-space:nowrap;padding-top:2px}}.att li .act.muted{{color:var(--muted)}}
.tag{{font-style:normal;display:inline-block;padding:0 7px;border-radius:999px;font-weight:700;font-size:11px;margin-right:8px;vertical-align:1px}}
.att li.bad .tag{{background:var(--chip-bad);color:var(--danger-strong)}}.att li.warn .tag{{background:var(--chip-warn);color:var(--warning-text)}}.att li.info .tag{{background:var(--chip-neutral);color:var(--info)}}
.key{{font-size:12px;color:var(--muted);line-height:1.6;margin:auto 0 0;padding-top:8px;border-top:1px solid var(--border)}}
.key span{{display:inline-block;padding:0 7px;border-radius:999px;font-weight:700;font-size:11px;margin-right:2px}}
.key .bad{{background:var(--chip-bad);color:var(--danger-strong)}}.key .warn{{background:var(--chip-warn);color:var(--warning-text)}}.key .info{{background:var(--chip-neutral);color:var(--info)}}
.alerts{{display:grid;gap:12px}}
.ch{{display:grid;border-radius:6px;overflow:hidden}}
.ch .row{{display:grid;grid-template-columns:1fr auto auto;gap:2px 12px;align-items:center;font-size:13px;padding:6px 10px}}
.ch .row:nth-child(even){{background:var(--raised)}}
.ch .n{{font-family:var(--font-mono);text-align:right;font-weight:600}}.ch .z{{color:var(--muted);font-weight:400}}
.ch a,.ch .nolink{{color:var(--text)}}.ch a::before,.ch .nolink::before{{content:"#";color:var(--muted)}}
.dg{{display:grid;gap:6px;font-size:13px}}
.dg .r{{display:grid;grid-template-columns:auto 1fr;gap:10px;align-items:baseline}}
.dg .sev{{font-family:var(--font-mono);font-size:11px;color:var(--muted);width:44px}}
.dg .r span:last-child{{color:var(--text-2)}}.dg .when{{color:var(--muted);font-size:12px}}
.links{{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center;padding-top:10px;border-top:1px solid var(--border);font-size:13px}}
.links .lbl{{color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;margin-right:4px}}
.links a::after{{content:" ↗";font-size:11px;color:var(--muted)}}
.links .note{{margin-left:auto;color:var(--muted);font-size:12px}}
@media (max-width:1000px){{.tiles,.two,.summary{{grid-template-columns:1fr}}.burn{{grid-template-columns:1fr}}}}
</style></head><body>
<div class="page">
  {stale_html}
  <header class="hdr">
    <div>
      <h1>Morning Page</h1>
      <div class="sub">{now.strftime('%A, %B %-d, %Y')} · one screen over Mission Control, the Token Command Center, and Token Burn. Read-only; nothing here is written back.</div>
    </div>
    <div>
      <div class="verdict {vcls}"><span class="dot"></span><b>{esc(vhead)}</b><span>{esc(vsub)}</span></div>
      <div class="stamp" style="text-align:right;margin-top:4px">{hdr_stamp}</div>
    </div>
  </header>

  <section>
    <p class="eyebrow">Summary <small>computed from the same files, no model in the loop</small></p>
    <ul class="summary">
{summary_html}
    </ul>
  </section>

  <section>
    <p class="eyebrow">Today's fires <small>{now.strftime('%A')} · local time · in order</small></p>
    <div class="tl">
      {timeline_html}
    </div>
    <div class="tlkey"><span class="d"><i></i>ran</span><span class="u"><i></i>due now / partial</span><span class="b"><i></i>missed / failed / stalled</span><span><i></i>upcoming</span><span>Hourly fleet-sentinel checks are folded into the 9 AM and 8 PM restart windows.</span></div>
  </section>

  <section class="two">
    <div class="list">
      <h2>Needs a look <span class="stamp">from Mission Control's verdicts</span></h2>
      <ul class="att">
{look_html}
      </ul>
      <div class="key"><span class="bad">Broken</span> a routine missed, failed, or stalled · <span class="warn">Degraded</span> ran but partial, or a flag carried over · <span class="info">Heads-up</span> nothing wrong, just upcoming</div>
    </div>
    <div class="list">
      <h2>Alerts to review <span class="stamp">last 24 h</span></h2>
      <div class="alerts">
        <div>
          <p class="eyebrow">Evening digest <small>{len(open_items)} open · Lane 2</small></p>
          <div class="dg">
{dg_rows}
          </div>
        </div>
        <div>
          <p class="eyebrow">Slack channels <small>Phase 2 · counts not read yet · links open the channel</small></p>
          <div class="ch">
{ch_html}
          </div>
        </div>
      </div>
    </div>
  </section>

  <section class="tiles">
    <div class="tile">
      <h2>Fleet <span class="stamp">Mission Control · {gen.strftime('%-I:%M %p %a') if gen else '?'}</span></h2>
      <div class="big"><span class="n">{summ['ok']}<span style="color:var(--muted);font-weight:400">/{summ['active']}</span></span><span class="l">routines healthy</span></div>
      <div class="chips">{"".join(chips)}</div>
      <div class="kv">
{fleet_kv_html}
      </div>
    </div>
    <div class="tile">
      <h2>Claude plan <span class="stamp">{plan_stamp}</span></h2>
      <div class="gauge">
{gauge_html}
      </div>
      <div class="kv">
{plan_kv_html}
      </div>
      <p class="footnote">Bars turn amber at 50% and red at 80%.</p>
    </div>
  </section>

  <section class="burnrow">
    <div class="tile">
      <h2>Token burn by vendor <span class="stamp">{esc(burn_stamp)}</span></h2>
      <div class="burn">
        <table class="bt">
          <thead><tr><th>Vendor</th><th>Yesterday</th><th>7 days</th><th>Share</th><th>Cost, 7 days</th></tr></thead>
          <tbody>
            <tr><td><i class="sw" style="background:var(--bar-claude)"></i>Claude</td><td class="num">{ktok(b['yc'])}</td><td class="num">{ktok(b['wc'])}</td><td class="num sh">{share_c}%</td><td class="num cost">${cost_c:,.0f}</td></tr>
            <tr><td><i class="sw" style="background:var(--bar-openai)"></i>OpenAI</td><td class="num">{ktok(b['yo'])}</td><td class="num">{ktok(b['wo'])}</td><td class="num sh">{100 - share_c if wt else 0}%</td><td class="num cost na">n/a</td></tr>
            <tr class="tot"><td>Total</td><td class="num">{ktok(b['yc'] + b['yo'])}</td><td class="num">{ktok(wt)}</td><td class="num sh"></td><td class="num cost">${cost_c:,.0f}</td></tr>
          </tbody>
        </table>
        <div class="chartwrap">
          <div class="legend"><span>Daily tokens, {esc(rng)}</span><span><i style="background:var(--bar-claude)"></i>Claude</span><span><i style="background:var(--bar-openai)"></i>OpenAI</span></div>
          {chart_svg}
        </div>
      </div>
      <p class="footnote">Claude = Cowork + Claude Code. OpenAI = Codex exact + ChatGPT <span class="est">EST</span>from a manual export{', covered through ' + esc(covered) if covered else ''}. Cache tokens excluded from counts. Cost is the API list-price equivalent from sessions.json, not what the Max plan bills; cache priced at the read rate. OpenAI shows n/a because sessions.json carries no Codex or ChatGPT cost rows.</p>
    </div>
  </section>

  <footer class="links">
    <span class="lbl">Open the source</span>
    {links_html}
    <span class="note">Read-only over ops-status.json, USAGE_SUMMARY, daily-burn.json, digest.jsonl</span>
  </footer>
</div>
</body></html>
"""


# ---------------------------------------------------------------- main

def main():
    now = datetime.now().astimezone()
    status = load_json(STATUS)
    if not status:
        print(f"ERROR: {STATUS} missing or unreadable — run watch.py first. No page written.")
        return 0
    snapshot = load_json(SNAPSHOT, [])
    heartbeats = load_jsonl(HEARTBEAT)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hb_last = None
    for h in heartbeats:
        ts = watch.parse_iso(h.get("ts"))
        if ts and ts >= day0 and (hb_last is None or ts > hb_last[0]):
            hb_last = (ts, h.get("task", ""))
    hb_after = {}
    expected = {t["taskId"]: watch.parse_iso(t.get("expectedLast")) for t in status["tasks"]}
    for h in heartbeats:
        ts = watch.parse_iso(h.get("ts"))
        exp = expected.get(h.get("task"))
        if ts and exp and ts >= exp - timedelta(minutes=5) and (h.get("task") not in hb_after or ts > hb_after[h["task"]][0]):
            hb_after[h["task"]] = (ts, h.get("status") or "ok", h.get("note") or "")
    restarts_today = sum(1 for r in load_jsonl(REPAIR)
                         if (r.get("result") == "repaired" and (watch.parse_iso(r.get("ts")) or day0 - timedelta(days=1)) >= day0))
    cmds = load_jsonl(COMMANDS)
    done = {(c.get("cmd"), c.get("task"), c.get("ts")) for c in cmds if c.get("status") != "queued"}
    queued = sum(1 for c in cmds if c.get("status") == "queued" and (c.get("cmd"), c.get("task"), c.get("ts")) not in done)

    burn_rows = load_json(DAILY_BURN, [])
    burn = burn_window(burn_rows, now)
    cost_claude = cost_window(load_json(SESSIONS, []), burn["days"])
    try:
        burn_mtime = datetime.fromtimestamp(DAILY_BURN.stat().st_mtime).astimezone()
    except Exception:
        burn_mtime = None

    ctx = {
        "now": now, "status": status, "usage": usage_summary(), "burn": burn,
        "cost_claude": cost_claude, "burn_mtime": burn_mtime,
        "chatgpt_meta": load_json(CHATGPT_META), "cfg": load_json(LOCAL_CFG),
        "slots": today_fires(snapshot, status["tasks"], heartbeats, status["script_jobs"], now),
        "hb_last": hb_last, "hb_after": hb_after, "restarts_today": restarts_today, "queued": queued,
    }
    OUT.write_text(render(ctx))
    summ = status["summary"]
    gaps = []
    if ctx["usage"] is None:
        gaps.append("plan-limits")
    if not burn_rows:
        gaps.append("daily-burn")
    if ctx["cfg"] is None:
        gaps.append("slack-links")
    print(f"MORNING-PAGE {now.strftime('%Y-%m-%d %H:%M %Z')}: {summ['ok']}/{summ['active']} routines OK, "
          f"{len(ctx['slots'])} fires on today's timeline, tokens yesterday {ktok(burn['yc'] + burn['yo'])} "
          f"(Claude {ktok(burn['yc'])} / OpenAI {ktok(burn['yo'])}), digest {summ['digest_open']} open"
          + (f"; gaps: {', '.join(gaps)}" if gaps else ""))
    print(f"WROTE: {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
