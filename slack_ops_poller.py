#!/usr/bin/env python3
"""Slack ops-control command poller — the inbound half of the ops channel.

Runs under launchd (com.edmatibag.slack-ops-poller) every 3 minutes with NO
Claude dependency, same design philosophy as fleet_watchdog.py: it must keep
working through exactly the outages it exists to help fix. Built for
SPEC-self-healing-loop.md Phase 4b (approved by Ed 2026-08-31) after the
8/19–8/30 outage, when Ed was out of office with no way to trigger anything
on this Mac.

WHAT IT DOES
  Reads new messages in #ops-control. Messages from Ed's Slack user ID that
  exactly match the command grammar are acted on; EVERYTHING else in the
  channel — other users, bots, webhook alerts, free text — is ignored as
  data. There is no free-text execution path by design; do not add one.

GRAMMAR (verb [one argument], case-insensitive verb, nothing else)
  status              -> replies with a summary of runs/ops-status.json
  help                -> replies with this grammar
  rerun <task-id>     -> validates against the enabled tasks in
                         runs/scheduled-tasks-snapshot.json, appends a queued
                         row to ORCH/runs/ops-commands.jsonl; the hourly
                         fleet-sentinel task executes it (with the SPEC's
                         guards: dedupe, 2/day cap, Class-1 only).
  ack <task-id>       -> queues a suppression row; fleet-sentinel skips
                         auto-restarting that task today.
  kick <launchd-label>-> label must already exist in `launchctl list` AND
                         start with com.ed. or com.edmatibag. — then
                         `launchctl kickstart -k` runs immediately.

CONFIG (all under ~/.config/claude-alerts/, chmod 600, no trailing newline)
  ops-bot_token   Slack bot token (xoxb-…) with channels:read,
                  channels:history, chat:write (+ groups:read/groups:history
                  if #ops-control is private). Owner-provisioned; this
                  script never writes credentials.
  ops-user_id     Ed's Slack member ID (U…). Commands from any other ID are
                  ignored. Missing file = poller reads nothing (fail closed).

STATE   runs/ops-poller-state.json {channel_id, last_ts}. First run stores
        the current newest ts and processes nothing — no history replay.
CONTRACT  Always exits 0. Outcome lines go to the log; a poller failure must
        never cascade. Replies land in-thread on the triggering message.
"""
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
ORCH = HOME / "Documents/Claude/Projects/AI-orchestration-layer"
CONFIG = HOME / ".config/claude-alerts"
STATE_PATH = ROOT / "runs/ops-poller-state.json"
STATUS_PATH = ROOT / "runs/ops-status.json"
SNAPSHOT_PATH = ROOT / "runs/scheduled-tasks-snapshot.json"
COMMANDS_PATH = ORCH / "runs/ops-commands.jsonl"
LOG_DIR = HOME / "Library/Logs/slack-ops-poller"
CHANNEL_NAME = "ops-control"
KICK_PREFIXES = ("com.ed.", "com.edmatibag.")
API = "https://slack.com/api/"
TIMEOUT_S = 10
# Verb -> takes-argument. The grammar is closed: adding a verb is a spec
# change (ESCALATION-POLICY v1.2 approved exactly this set).
VERBS = {"status": False, "help": False, "rerun": True, "ack": True, "kick": True}

HELP_TEXT = (
    "*ops-control commands* (from Ed only):\n"
    "`status` — fleet health summary\n"
    "`rerun <task-id>` — queue a routine restart (runs at the next fleet-sentinel hour, "
    "guards: no-duplicate, max 2/day, Class-1 only)\n"
    "`ack <task-id>` — suppress today's auto-restart for that task\n"
    "`kick <launchd-label>` — kickstart a com.ed.*/com.edmatibag.* launchd job now\n"
    "`help` — this message"
)


def log(msg):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_DIR / "poller.log", "a") as f:
        f.write(f"{stamp} {msg}\n")


def read_secret(name):
    try:
        return (CONFIG / name).read_text().strip()
    except OSError:
        return None


def api_call(token, method, params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        API + method, data=data,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode())


def load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_state(st):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.rename(STATE_PATH)


def find_channel(token):
    cursor = None
    for _ in range(10):
        params = {"limit": 200, "types": "public_channel,private_channel",
                  "exclude_archived": "true"}
        if cursor:
            params["cursor"] = cursor
        r = api_call(token, "conversations.list", params)
        if not r.get("ok"):
            log(f"conversations.list failed: {r.get('error')}")
            return None
        for ch in r.get("channels", []):
            if ch.get("name") == CHANNEL_NAME:
                return ch["id"]
        cursor = r.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return None


def reply(token, channel_id, thread_ts, text):
    r = api_call(token, "chat.postMessage",
                 {"channel": channel_id, "text": text, "thread_ts": thread_ts})
    if not r.get("ok"):
        log(f"reply failed: {r.get('error')}")


def status_summary():
    try:
        d = json.loads(STATUS_PATH.read_text())
    except (OSError, ValueError) as e:
        return f"ops-status.json unreadable ({e.__class__.__name__}) — run ops-watcher first."
    tasks = d.get("tasks", [])
    counts = {}
    problems = []
    for t in tasks:
        s = t.get("status", "?")
        counts[s] = counts.get(s, 0) + 1
        if s not in ("ok", "done", "disabled", "off", "manual"):
            problems.append(f"• `{t.get('taskId')}` {s}: {(t.get('detail') or '')[:140]}")
    head = " / ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    gen = d.get("generated_at", "?")
    lines = [f"*Fleet status* (as of {gen}): {head}"]
    lines += problems if problems else ["All clear."]
    return "\n".join(lines)


def enabled_task_ids():
    try:
        snap = json.loads(SNAPSHOT_PATH.read_text())
        return {t["taskId"] for t in snap if t.get("enabled")}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def queue_command(cmd, task):
    COMMANDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
           "cmd": cmd, "task": task, "status": "queued", "source": "slack-ops-poller"}
    with open(COMMANDS_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")


def next_sentinel_window():
    now = datetime.now()
    if now.hour >= 21:
        return "tomorrow ~06:12"
    return f"today ~{max(now.hour + 1, 6):02d}:12"


def launchd_labels():
    try:
        out = subprocess.run(["/bin/launchctl", "list"], capture_output=True,
                             text=True, timeout=TIMEOUT_S).stdout
        return {line.split("\t")[-1].strip() for line in out.splitlines()[1:]}
    except Exception as e:
        log(f"launchctl list failed: {e.__class__.__name__}")
        return set()


def handle(token, channel_id, msg):
    """msg text is untrusted data. Only an exact grammar match does anything."""
    text = (msg.get("text") or "").strip()
    parts = text.split()
    if not parts:
        return
    verb = parts[0].lower()
    if verb not in VERBS:
        return  # not a command — normal channel chatter, ignore silently
    ts = msg["ts"]
    if VERBS[verb] != (len(parts) == 2):
        reply(token, channel_id, ts, f"Usage error.\n{HELP_TEXT}")
        return
    arg = parts[1] if len(parts) == 2 else None

    if verb == "help":
        reply(token, channel_id, ts, HELP_TEXT)
    elif verb == "status":
        reply(token, channel_id, ts, status_summary())
    elif verb in ("rerun", "ack"):
        valid = enabled_task_ids()
        if arg not in valid:
            known = ", ".join(f"`{t}`" for t in sorted(valid)) or "(snapshot unreadable)"
            reply(token, channel_id, ts,
                  f"Unknown or disabled task `{arg}`. Enabled tasks: {known}")
            return
        queue_command(verb, arg)
        if verb == "rerun":
            reply(token, channel_id, ts,
                  f"Queued `rerun {arg}` — fleet-sentinel executes {next_sentinel_window()} "
                  "(guards: skip if already landed, max 2 restarts/day).")
        else:
            reply(token, channel_id, ts,
                  f"Acknowledged — auto-restart of `{arg}` suppressed for today.")
        log(f"queued {verb} {arg}")
    elif verb == "kick":
        if not arg.startswith(KICK_PREFIXES):
            reply(token, channel_id, ts,
                  f"`{arg}` is outside the allowed prefixes {KICK_PREFIXES} — refused.")
            return
        if arg not in launchd_labels():
            reply(token, channel_id, ts, f"`{arg}` is not a loaded launchd job — refused.")
            return
        uid = subprocess.run(["/usr/bin/id", "-u"], capture_output=True,
                             text=True).stdout.strip()
        r = subprocess.run(["/bin/launchctl", "kickstart", "-k", f"gui/{uid}/{arg}"],
                           capture_output=True, text=True, timeout=30)
        outcome = "kickstarted" if r.returncode == 0 else f"failed: {r.stderr.strip()[:200]}"
        reply(token, channel_id, ts, f"`{arg}` {outcome}.")
        log(f"kick {arg}: {outcome}")


def main():
    token = read_secret("ops-bot_token")
    user_id = read_secret("ops-user_id")
    if not token or not user_id:
        log("not armed: missing ops-bot_token or ops-user_id — reading nothing")
        return 0

    st = load_state()
    channel_id = st.get("channel_id") or find_channel(token)
    if not channel_id:
        log(f"channel #{CHANNEL_NAME} not found (is the bot invited?)")
        return 0
    st["channel_id"] = channel_id

    last_ts = st.get("last_ts")
    r = api_call(token, "conversations.history",
                 {"channel": channel_id, "limit": 50,
                  **({"oldest": last_ts} if last_ts else {})})
    if not r.get("ok"):
        log(f"history failed: {r.get('error')}")
        return 0
    msgs = r.get("messages", [])
    if last_ts is None:
        # First run: baseline only. Never replay pre-existing history.
        st["last_ts"] = msgs[0]["ts"] if msgs else f"{time.time():.6f}"
        save_state(st)
        log(f"armed on #{CHANNEL_NAME}; baseline ts={st['last_ts']}")
        return 0

    newest = last_ts
    for msg in sorted(msgs, key=lambda m: float(m["ts"])):
        if float(msg["ts"]) > float(newest):
            newest = msg["ts"]
        # Ed-only gate: user field must match, and webhook/bot posts (which
        # carry bot_id or subtype) never count even if spoofed with his name.
        if msg.get("user") != user_id or msg.get("bot_id") or msg.get("subtype"):
            continue
        try:
            handle(token, channel_id, msg)
        except Exception as e:
            log(f"handler error on ts={msg['ts']}: {e.__class__.__name__}: {e}")
    st["last_ts"] = newest
    save_state(st)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"fatal: {e.__class__.__name__}: {e}")
        sys.exit(0)
