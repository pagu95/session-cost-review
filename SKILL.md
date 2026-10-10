---
name: session-cost-review
description: Use when asked to analyse what a Claude Code session cost, why it was expensive, or to turn that into durable lessons — "how many tokens did this use", "why is this so expensive", "make the next session cheaper"
---

# Session Cost Review

Measure a session's real token cost from its transcripts, explain what drove it, and save the lessons so the next session is cheaper.

**Announce at start:** "I'm using the session-cost-review skill to measure this session."

## How the work is split

This is a hybrid: a script for what must be identical every run, research for what changes.

- **Script (`analyze.py`) — counting.** Deduplication, subagent discovery, per-call pricing, tool attribution. It must give the same answer for the same transcript every time, or baselines are meaningless. Never `cat` a transcript or write an ad-hoc parser instead — reading transcripts into context is itself a major cost, the very thing being measured.
- **Research — prices and format drift.** Rates live in a dated `prices.json` refreshed from the official page, never assumed. When the transcript format changes, the script fails loudly and you fix the script, once.
- **You — judgement.** Diagnosis, rework, lessons.

## Checklist

Create a todo per item and do them in order:

1. **Pick the target** — decide which session to analyse, and say which
2. **Measure** — run the script
3. **Handle failures** — schema failure, stale or missing prices
4. **Display** — present the matrices and the drivers
5. **Compare** — against the baseline in memory, if one exists
6. **Diagnose** — tie cost to specific episodes in the session
7. **Save** — write lessons to memory (only with the user's go-ahead)

## 1. Pick the target

Unless the user names a session, decide between two modes:

- **End-of-session review** — the current session, once real work is done. The script cuts off at the latest human prompt, so the analysis does not measure itself.
- **Retrospective** — a prior session. Use this whenever the current session is new; a fresh session has nothing worth measuring.

`analyze.py --list` shows recent sessions with total size including subagents, subagent count, and marks the current one. Worktrees get their OWN project slug, so the most recent transcript overall is often from a different checkout than the one you are in.

**Always state which session you picked and why before analysing it.** If several large recent sessions are plausible, ask rather than guess.

## 2. Measure

```bash
python3 ~/.claude/skills/session-cost-review/analyze.py              # current session
python3 ~/.claude/skills/session-cost-review/analyze.py <sessionId>  # prefix ok
python3 ~/.claude/skills/session-cost-review/analyze.py --list
```

Options: `--report <path>` also writes the output as Markdown (only when the user wants a file; default to the working directory); `--cutoff <ISO>|none` overrides the self-exclusion cutoff; `--prices <file>`; `--main-only` (never quote its result as a session total).

The script reads `<sessionId>.jsonl` plus every `<sessionId>/subagents/agent-*.jsonl`, links each subagent to the turn that spawned it, and prices every call by its own model, cache-write TTL (5m/1h), speed (fast mode), long-context tier and data-residency multiplier.

## 3. Handle failures — never route around them

**Exit 3 — SCHEMA CHECK FAILED.** The format changed and the numbers would be wrong, so none are printed. Do not hand-count instead. Inspect the drift with a small streaming probe that prints *aggregates only* (record types, key sets, a few field names — never transcript content), fix `analyze.py`, re-run, and tell the user what changed. The traps the script already handles, so you can recognise drift around them:

- One API call is split over several assistant lines that each repeat the same usage. Dedupe by `requestId` or totals inflate ~15x.
- Real per-call numbers live in `message.usage.iterations[]`; top-level fields can be zero.
- Subagents live in separate files; a main-only count can miss most of the cost (79% in one measured session).
- `cost-state` records are stale snapshots and are no longer emitted; they are not used.

**Exit 2 — nothing to analyse** (empty session, or nothing before the cutoff). Pick another.

**Warnings** print under the header. Repeat any that affect the numbers to the user — notably Agent calls without a transcript (missing subagent cost) and orphan subagents (turn attribution unknown).

**Pricing is research, not memory.** Refresh `prices.json` when the header says `STALE` (older than 30 days), `no prices file`, or lists `UNPRICED` calls:

1. Fetch `https://platform.claude.com/docs/en/about-claude/pricing` (`docs.claude.com` redirects there).
2. Extract the model table, fast-mode, long-context and data-residency sections with grep on the saved page — do not read the whole page into context.
3. Update `prices.json`: exact model IDs as they appear in transcripts (`message.model`), all five rates, `retrieved` date, `source`. Record any derived rate in `notes`.
4. Re-run.

Never guess a rate for an unpriced model, and never alias it to a similar one (the script deliberately won't). If the lookup fails, present tokens as the hard number and dollars as partial or dated — say so explicitly.

## 4. Display

Lead with the component matrix — it reframes the problem:

| Component | Tokens | Cost | Share |
|---|---:|---:|---:|

In nearly every long session, **cache reads and writes dominate and output is small** (5–25% across measured sessions). Cost tracks *how many times context is re-read*, not how much work was produced. Say that plainly — users usually assume the opposite.

Then: the thread × model × effort × speed matrix and the subagent share, cost per active skill, batching, the tool-results table, context growth, cache-write spikes with the idle gap before each, and the most expensive turns.

Label the tool-results numbers correctly when you quote them: result characters are **measured**; tokens and "re-read exposure" (result size × later calls before compaction) are **estimates** at ~4 chars/token. Exact per-tool billing is not in the log — never present an estimate as a share of the bill, and never add it to the measured totals.

## 5. Compare against the baseline

Raw numbers mean little alone. Check the user's memory for a prior cost baseline (look for a memory about session cost analysis) and compare on $/call, average context per call, tools per call, subagent share, and the component split. Say plainly whether this session was better or worse, and on which dimension.

Comparability: baselines recorded before 2026-10-10 came from the old script — main thread only unless stated, and dollars at a hardcoded table that overpriced the 5.5 models (Opus 5.5 and Sonnet 5.5 cache reads at 2.5–3x the real rate). Compare tokens per call across that boundary, not dollars, and say so.

If no baseline exists, say so, treat this session as the baseline, and make sure step 7 saves one. Keep baselines in MEMORY, never in this skill — they are specific to one person, project and model; this skill is shared.

## 6. Diagnose

Tie each number to something that actually happened. The recurring drivers, in order:

**Serial tool calls.** An average near 1.0 tool/call means almost everything was a separate round-trip, each re-reading the whole context. Usually the single biggest recoverable cost.

**Context growth.** Compare early vs late $/call. A 2–4x rise means late work was charged several times over. The fix is compacting at phase boundaries, not at the auto-compact limit.

**Bulky tool results.** The tool-results table shows what fills the context: whole-file reads, unfiltered grep/cat output, a skill loaded into context. A large result early in a long thread is re-read on every later call. Name the specific results and the narrower alternative (line ranges, `head`, `--files-with-matches`, a subagent that returns conclusions only).

**Subagent fan-out.** Check the subagent share and the top subagents. Subagents are worth it when they keep bulk out of the main context or run on a cheaper model; they are waste when they re-read the same files the main thread already holds, or run serial single-tool calls themselves.

**Cache-write spikes.** Writes cost 12–40x reads. A spike after a long idle gap is a cold cache (past the 1h TTL) forcing a full prefix re-write.

**Rework.** The script can't detect this; read your own history. Find work that was undone — a wrong command fanned out, a file committed then removed, an approach abandoned — and sum those turns. Report it honestly even when the cause was your own mistake; that is the most actionable category and the user cannot see it otherwise.

Quantify each lever in dollars and be explicit that estimates overlap rather than add up.

## 7. Save the lessons

**Ask before writing.** Then save each lesson as its own memory file, not one blob — they surface independently later.

Use the memory format the environment specifies (typically `~/.claude/projects/<project-slug>/memory/`, one fact per file with frontmatter, plus a one-line pointer in `MEMORY.md`). A good lesson has:

- **The behaviour**, stated as an instruction
- **The evidence** — the measured number and the date, so it can be re-checked and a future reader can tell when it has gone stale
- **Why** it costs money
- **How to apply** it concretely

Write the specific measurement into the memory. "Batch tool calls" is advice anyone could ignore; "502 of 519 calls carried exactly one tool, costing an estimated $60–100 of a $195 session" is evidence.

Also save (or update) a **baseline** memory with the headline numbers — total, main vs subagent split, API calls, turns, avg context/call, $/call, tools per call, component split, and the `prices.json` retrieval date used — so the next review can compare.

## Tone

The user is paying for this. Be direct about waste, including your own. Don't pad the analysis with caveats, and don't soften a rework number because it reflects badly on you — an unflattering finding they can act on is worth more than a flattering one they can't.
