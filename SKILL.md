---
name: session-cost-review
description: Use when asked to analyse what a Claude Code session cost, why it was expensive, or to turn that into durable lessons — "how many tokens did this use", "why is this so expensive", "make the next session cheaper"
---

# Session Cost Review

Measure a session's real token cost from its transcript, explain what drove it, and save the lessons so the next session is cheaper.

**Announce at start:** "I'm using the session-cost-review skill to measure this session."

## Why a script and not eyeballing it

Transcripts are large (several MB) and **reading one into context to analyse it is itself a major cost** — the thing you are trying to measure. Always run the bundled script. Never `cat` a transcript.

## Checklist

Create a todo per item and do them in order:

1. **Pick the target** — decide which session to analyse, and say which
2. **Measure** — run the script
3. **Sanity-check** — confirm pricing was verified, not assumed
4. **Display** — present the matrix and the drivers
5. **Compare** — against the baseline in memory, if one exists
6. **Diagnose** — tie cost to specific episodes in the session
7. **Save** — write lessons to memory (only with the user's go-ahead)

## 1. Pick the target

Unless the user names a session, decide between two modes:

- **End-of-session review** — the current session, once real work is done.
- **Retrospective** — a prior session. Use this whenever the current session
  is new. A fresh session has no history worth measuring; analysing it
  reports a few hundred tokens and teaches nothing.

To find a prior session, run `analyze.py --list` and pick the most recent
substantial transcript whose path contains the current project's slug. Note
that worktrees get their OWN project slug (the worktree name is baked into
the directory), so the most recent transcript overall is often from a
different checkout than the one you are sitting in.

**Always state which session you picked and why before analysing it.** If the
choice is genuinely ambiguous — several large recent sessions — ask rather
than guess; analysing the wrong one wastes the whole exercise.

## 2. Measure

```bash
python3 ~/.claude/skills/session-cost-review/analyze.py            # current/most recent session
python3 ~/.claude/skills/session-cost-review/analyze.py <sessionId>
python3 ~/.claude/skills/session-cost-review/analyze.py --list     # find a transcript
```

Transcripts live at `~/.claude/projects/<project-slug>/<sessionId>.jsonl`.

## 3. Sanity-check before quoting any number

Three traps the script handles — know them, because if the format shifts you must catch it:

- **`cost-state` records go stale.** They are periodic checkpoints and silently stop updating. One measured session froze at $22.65 against a true $195.48. Never quote them as the total; the script prints the last one only for contrast.
- **Multiple transcript lines share one `requestId`.** An API call is split into thinking / text / tool_use lines and *each repeats the same usage object*. Summing lines overcounts by roughly 15x. Dedupe by `requestId`.
- **Real per-call numbers live in `message.usage.iterations[]`.** Top-level `usage` fields are often zero.

If the script reports pricing could NOT be reconciled, say so explicitly and present tokens as the hard number with cost as an estimate. Do not quietly present an unverified dollar figure.

## 4. Display

Lead with the component matrix — it reframes the problem:

| Component | Tokens | Cost | Share |
|---|---:|---:|---:|

In nearly every long session, **cache reads dominate and output is a rounding error** (one measured session: 62% cache read, 32% cache write, 6% output). The useful conclusion is that cost tracks *how many times context is re-read*, not how much work was produced. Say that plainly — users usually assume the opposite.

Then: calls, turns, avg context/call, $/call, the batching stat, context growth, and the most expensive turns.

## 5. Compare against the baseline

Raw numbers mean little alone — $40 is good or bad only relative to something.
Before diagnosing, check the user's memory directory for a prior cost baseline
(look for a memory about session cost analysis). If one exists, compare this
session against it on the dimensions that matter: $/call, average context per
call, tools per call, and the cache-read / cache-write / output split. Say
plainly whether this session was better or worse, and on which dimension.

If no baseline exists, say so and treat this session as the baseline — then
make sure step 7 saves one, so the next review has something to measure
against.

Keep the baseline in MEMORY, never hardcoded in this skill. The numbers are
specific to one person, one project and one model; this skill is shared.

## 6. Diagnose

Numbers alone don't change behaviour. Tie each to something that actually happened. The recurring drivers, in order:

**Serial tool calls.** Check the batching stat. An average near 1.0 tool/call means almost everything was a separate round-trip, each re-reading the whole context. This is usually the single biggest recoverable cost.

**Context growth.** Compare early vs late $/call. A 2–4x rise means work done late was charged several times over for the same effort. The fix is compacting at phase boundaries, not at the auto-compact limit.

**Cache-write spikes.** Writes cost ~20x reads. Large spikes mean a cold cache — usually a long idle gap past the 1h TTL, forcing a full prefix re-write. Correlate spike turns with gaps in the conversation.

**Rework.** The script can't detect this; you must read your own history. Find episodes where work was undone — a wrong command fanned out, a file committed then removed, an approach abandoned — and sum those turns. Report it honestly even when the cause was your own mistake; that is the most actionable category and the user cannot see it otherwise.

Quantify each lever in dollars and be explicit that estimates overlap rather than add up.

## 7. Save the lessons

**Ask before writing.** Then save each lesson as its own memory file, not one blob — they surface independently later.

Use the memory format the environment specifies (typically `~/.claude/projects/<project-slug>/memory/`, one fact per file with frontmatter, plus a one-line pointer in `MEMORY.md`). A good lesson has:

- **The behaviour**, stated as an instruction
- **The evidence** — the measured number and the date, so it can be re-checked and so a future reader can tell when it has gone stale
- **Why** it costs money
- **How to apply** it concretely

Write the specific measurement into the memory. "Batch tool calls" is advice anyone could ignore; "502 of 519 calls carried exactly one tool, costing an estimated $60–100 of a $195 session" is evidence.

Also save (or update) a **baseline** memory with the headline numbers —
total, API calls, turns, avg context/call, $/call, tools per call, and the
component split — so the next review has something to compare against. Date
it, so a future reader can tell when it has gone stale.

## Tone

The user is paying for this. Be direct about waste, including your own. Don't pad the analysis with caveats, and don't soften a rework number because it reflects badly on you — an unflattering finding they can act on is worth more than a flattering one they can't.
