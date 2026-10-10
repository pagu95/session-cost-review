# session-cost-review

A Claude Code skill that measures what a session really cost from its transcript, explains what drove the cost, and saves the lessons to memory.

## Install

Clone into your personal skills directory:

```bash
git clone https://github.com/pagu95/session-cost-review.git ~/.claude/skills/session-cost-review
```

Then ask Claude Code something like "why was this session so expensive?" and the skill will trigger.

## Contents

- `SKILL.md` — the skill instructions
- `analyze.py` — reads a session's main transcript (`~/.claude/projects/<slug>/<sessionId>.jsonl`) **and all its subagent transcripts** (`<sessionId>/subagents/agent-*.jsonl`) and prints the token/cost breakdown: component split, thread × model × effort × speed matrix, batching, tool-result sizes, context growth, cache-write spikes, most expensive turns. `--report <path>` writes it as Markdown. Requires Python 3.
- `prices.json` — dated per-model rates from the [official pricing page](https://platform.claude.com/docs/en/about-claude/pricing). The script flags it as stale after 30 days and leaves unknown models unpriced; the skill tells Claude how to refresh it.

## Design

Counting is a script, so the same transcript always gives the same numbers and baselines stay comparable. Prices and transcript-format drift are research: prices come from a dated file refreshed from the official page, and when the format changes the script exits with code 3 instead of printing wrong numbers, so it gets fixed once rather than worked around.
