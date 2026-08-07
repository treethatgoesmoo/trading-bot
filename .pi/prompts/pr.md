---
description: Pre-PR readiness checklist — smoke test, diagnostics, diff summary, draft commit message
argument-hint: "[optional: short description of the change]"
---
<!--
  Prompt template for pre-PR readiness.
  pi users: trigger as /pr [description]  ($@ = optional description)
  Other toolkits: paste this file into chat; replace $@ with your description or leave blank.
-->

Change description (if given): $@

Run through this checklist and report results for each step — stop and flag if any step fails.

## 1. Smoke test

```bash
python -c "import main, bot.core, zero_dte.cog, swing.cog, core.analytics"
```

## 2. Diagnostics

Run `lens_diagnostics` (mode `all`) over files touched in this change. Report any errors; warnings are informational unless they indicate an actual bug.

## 3. Relevant backtest

If the change touches strategy logic (`zero_dte/strategy.py`, `swing/strategy.py`, `core/indicators.py`, weight files), confirm the relevant backtest from `/backtest` was run and passed. If not run yet, run it now.

## 4. Diff summary

```bash
git status --short
git diff --stat
```

Summarize:

- Files changed and why (one line each)
- Whether anything under `data/` is touched (it shouldn't be — that's auto-committed trading history, never hand-edited)
- Whether any `data/{kind}-*.jsonl` file-kind names or `data/swing_open.json` registry schema changed (these must stay backward compatible — see CLAUDE.md conventions)

## 5. Draft commit message

Propose a concise commit message (imperative mood, one-line summary + body if needed) based on the diff and the change description above. Wait for confirmation before committing.

## 6. Final check

Confirm:

- No stray `__pycache__`/debug prints/commented-out code left in
- `.env`-dependent behavior degrades gracefully if a key is missing (per CLAUDE.md conventions)
- ET vs UTC timestamp handling is correct if timestamps were touched
