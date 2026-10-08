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
- `analyze.py` — reads a session transcript (`~/.claude/projects/<slug>/<sessionId>.jsonl`) and prints the token/cost breakdown. Requires Python 3.
