# Pre-edit backups — scheduled-task SKILL.md, 2026-08-30

`~/.claude/scheduled-tasks/` is **not a git repository**, so these two task files had no version
history at all. On 2026-08-30 both gained attention-layer heartbeat footers (they were the only
two of 15 routines emitting no heartbeat, which left them undetectable during the 2026-08-19
outage). These are the exact pre-edit copies, kept here because this repo is the fleet's
system-of-record and nothing else would preserve them.

**The edits are pure appends**, so a revert does not need these files:

| Task | Live file | Revert |
|---|---|---|
| `rockwell-daily-capture` | `~/.claude/scheduled-tasks/rockwell-daily-capture/SKILL.md` | keep lines 1–29 |
| `evening-digest` | `~/.claude/scheduled-tasks/evening-digest/SKILL.md` | keep lines 1–23 |

Everything from the trailing `---` and `ATTENTION-LAYER FOOTER` onward is the 2026-08-30 addition.

Not runtime data and not a scrubbed preview — this directory is an exception to the usual purpose
of `samples/`, recorded here so it is not mistaken for one.
